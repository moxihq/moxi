"""Module discovery: which Moxi modules this Python environment holds, and loading one.

Each module declares one entry point in the group `moxi.modules`, named by the module and
pointing at a small announcement module of its own, which imports only the standard
library:

    [project.entry-points."moxi.modules"]
    cache = "moxi_cache.module"

The announcement defines every name in `ANNOUNCEMENT`:

    INTERFACES    frozenset of int: the versions of what the module offers others
    API           dotted name of the module other modules call, or None
    CLI           "package.module:function" that `moxi <module>` runs, or None
    SUMMARY       one line, for `moxi modules`
    OPTIONAL      {extra: one sentence saying what installing it enables}
    GATED         {console script: the extra it needs}
    INTERNAL_ENV  {variable: why the module reads it}; a name ending in `_` is a prefix
    SETTINGS      resource name of the config schema in the announcement's package, or None
    SETUP         "package.module:function" the installer runs after installing, or None
    SEGMENT       dotted name of a module whose `segment` renders a status line segment,
                  or None

`present` and `installed` read entry-point metadata and import nothing. `load` imports
the announcement and the `API` module and returns a frozen `Found` whose state is
`present`, `absent` or `broken`; it never raises. A feature that needs an optional module
asks `load` where it needs it and is off when the answer is not `present`.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType, ModuleType

from . import names

#: The entry-point group every module declares itself in.
GROUP = "moxi.modules"

PRESENT = "present"
ABSENT = "absent"
BROKEN = "broken"

#: The names an announcement defines.
ANNOUNCEMENT = ("INTERFACES", "API", "CLI", "SUMMARY", "OPTIONAL", "GATED", "INTERNAL_ENV",
                "SETTINGS", "SETUP", "SEGMENT")

_EMPTY: Mapping[str, str] = MappingProxyType({})
_DOTTED = re.compile(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)*")
_TARGET = re.compile(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)*:[A-Za-z_]\w*")


@dataclass(frozen=True)
class Entry:
    """One `moxi.modules` entry point, as the environment's metadata declares it."""

    name: str
    distribution: str
    version: str
    target: str


@dataclass(frozen=True)
class Found:
    """What `load` found for one module name."""

    name: str
    state: str
    reason: str = ""
    distribution: str | None = None
    version: str | None = None
    target: str | None = None
    #: The package the announcement lives in; `SETTINGS` names a resource in it.
    package: str | None = None
    interfaces: frozenset[int] = frozenset()
    api_name: str | None = None
    #: The imported `API` module, when the state is `present` and `API` is not None.
    api: ModuleType | None = field(default=None, repr=False, compare=False)
    cli: str | None = None
    summary: str = ""
    optional: Mapping[str, str] = _EMPTY
    gated: Mapping[str, str] = _EMPTY
    internal_env: Mapping[str, str] = _EMPTY
    settings: str | None = None
    setup: str | None = None
    segment: str | None = None

    @property
    def ok(self) -> bool:
        return self.state == PRESENT


def installed() -> list[Entry]:
    """Every `moxi.modules` entry point in this environment, sorted by name. Imports none."""
    from importlib.metadata import entry_points

    found = []
    for ep in entry_points(group=GROUP):
        dist = getattr(ep, "dist", None)
        found.append(Entry(name=ep.name,
                           distribution=_dist_name(dist),
                           version=getattr(dist, "version", None) or "",
                           target=ep.value))
    return sorted(found, key=lambda e: (e.name, e.distribution))


def present(name: str) -> bool:
    """True when at least one distribution declares module `name`. Imports nothing.

    It promises nothing about loading: a feature asks `load`.
    """
    return any(entry.name == name for entry in installed())


# One record per name per process: the announcement is imported once, the API module at
# most once. The interface check runs on every call against the record.
_records: dict[str, "_Record"] = {}
_lock = threading.RLock()


def load(name: str, *, interface: int | None = None) -> Found:
    """Load module `name` and, when `interface` is given, check that it offers it.

    Never raises: anything that goes wrong is a `broken` `Found` with the reason.
    """
    with _lock:
        record = _records.get(name)
        if record is None:
            record = _records[name] = _Record.read(name)
        return record.found(interface)


def reset() -> None:
    """Forget every record, so the next `load` reads the environment again (for tests)."""
    with _lock:
        _records.clear()


class _Record:
    """What one name resolved to in this process."""

    def __init__(self, base: Found, api_name: str | None = None):
        self.base = base
        self.api_name = api_name
        self.api: ModuleType | None = None
        self.api_error: str | None = None
        self.api_tried = False

    @classmethod
    def read(cls, name: str) -> _Record:
        try:
            return cls._read(name)
        except Exception as exc:  # noqa: BLE001 - load never raises
            return cls(Found(name=name, state=BROKEN,
                             reason=f"reading the environment's modules failed: "
                                    f"{_describe(exc)}"))

    @classmethod
    def _read(cls, name: str) -> _Record:
        from importlib.metadata import entry_points

        claims = [ep for ep in entry_points(group=GROUP) if ep.name == name]
        if not claims:
            return cls(Found(name=name, state=ABSENT,
                             reason=f"no distribution in this environment declares the "
                                    f"Moxi module {name!r}"))
        if len(claims) > 1:
            who = ", ".join(sorted(_dist_label(ep) for ep in claims))
            return cls(Found(name=name, state=BROKEN,
                             reason=f"{len(claims)} distributions declare the Moxi module "
                                    f"{name!r} ({who}); uninstall all but one"))
        ep = claims[0]
        dist = getattr(ep, "dist", None)
        where = dict(name=name, distribution=_dist_name(dist) or None,
                     version=getattr(dist, "version", None), target=ep.value)
        if not names.is_module_name(name):
            return cls(Found(state=BROKEN, **where,
                             reason=f"{name!r} is not a Moxi module name: lower-case "
                                    "ASCII letters and digits only"))
        if name in names.VERBS:
            return cls(Found(state=BROKEN, **where,
                             reason=f"{name!r} is one of the umbrella's own verbs, so "
                                    f"`moxi {name}` can never reach this module; the "
                                    "module needs another name"))
        if ":" in ep.value:
            return cls(Found(state=BROKEN, **where,
                             reason=f"the entry point {ep.value!r} names an attribute; "
                                    "it must name the announcement module alone"))
        try:
            module = ep.load()
        except Exception as exc:  # noqa: BLE001
            return cls(Found(state=BROKEN, **where,
                             reason=f"importing its announcement {ep.value} failed: "
                                    f"{_describe(exc)}"))
        try:
            fields = _announcement(module)
        except _Bad as exc:
            return cls(Found(state=BROKEN, **where,
                             reason=f"its announcement {ep.value} {exc}"))
        except Exception as exc:  # noqa: BLE001
            return cls(Found(state=BROKEN, **where,
                             reason=f"reading its announcement {ep.value} failed: "
                                    f"{_describe(exc)}"))
        package = module.__name__ if hasattr(module, "__path__") else module.__package__
        api_name = fields.pop("api_name")
        return cls(Found(state=PRESENT, **where, package=package or None,
                         api_name=api_name, **fields), api_name)

    def found(self, interface: int | None) -> Found:
        base = self.base
        if base.state != PRESENT:
            return base
        if interface is not None and interface not in base.interfaces:
            offered = ", ".join(map(str, sorted(base.interfaces))) or "none"
            return _replace(base, state=BROKEN,
                            reason=f"{base.distribution} {base.version} offers interfaces "
                                   f"{{{offered}}}; the caller speaks {interface}")
        if self.api_name is None:
            return base
        if not self.api_tried:
            self.api_tried = True
            try:
                import importlib

                self.api = importlib.import_module(self.api_name)
            except Exception as exc:  # noqa: BLE001
                self.api_error = (f"importing its API module {self.api_name} failed: "
                                  f"{_describe(exc)}")
        if self.api_error is not None:
            return _replace(base, state=BROKEN, reason=self.api_error)
        return _replace(base, api=self.api)


class _Bad(Exception):
    """An announcement that does not hold to the contract."""


def _announcement(module: ModuleType) -> dict:
    missing = [n for n in ANNOUNCEMENT if not hasattr(module, n)]
    if missing:
        raise _Bad("does not define " + ", ".join(missing))
    get = module.__dict__.get

    interfaces = get("INTERFACES")
    if not isinstance(interfaces, frozenset) or not all(
            isinstance(i, int) and not isinstance(i, bool) for i in interfaces):
        raise _Bad(f"has INTERFACES = {interfaces!r}; it must be a frozenset of integers")
    summary = get("SUMMARY")
    if not isinstance(summary, str) or not summary.strip() or "\n" in summary:
        raise _Bad(f"has SUMMARY = {summary!r}; it must be one non-empty line")
    return dict(
        interfaces=interfaces,
        api_name=_optional(get("API"), "API", _DOTTED, "a dotted module name"),
        cli=_optional(get("CLI"), "CLI", _TARGET, '"package.module:function"'),
        summary=summary,
        optional=_mapping(get("OPTIONAL"), "OPTIONAL"),
        gated=_mapping(get("GATED"), "GATED"),
        internal_env=_mapping(get("INTERNAL_ENV"), "INTERNAL_ENV"),
        settings=_optional(get("SETTINGS"), "SETTINGS", None, "a resource name"),
        setup=_optional(get("SETUP"), "SETUP", _TARGET, '"package.module:function"'),
        segment=_optional(get("SEGMENT"), "SEGMENT", _DOTTED, "a dotted module name"),
    )


def _optional(value, label: str, pattern: re.Pattern | None, what: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and value and (pattern is None or pattern.fullmatch(value)):
        return value
    raise _Bad(f"has {label} = {value!r}; it must be {what} or None")


def _mapping(value, label: str) -> Mapping[str, str]:
    if not isinstance(value, Mapping) or not all(
            isinstance(k, str) and k and isinstance(v, str) for k, v in value.items()):
        raise _Bad(f"has {label} = {value!r}; it must map names to strings")
    return MappingProxyType(dict(value))


def _replace(found: Found, **changes) -> Found:
    from dataclasses import replace

    return replace(found, **changes)


def _dist_name(dist) -> str:
    if dist is None:
        return ""
    try:
        return dist.metadata["Name"] or ""
    except Exception:  # noqa: BLE001
        return getattr(dist, "name", "") or ""


def _dist_label(ep) -> str:
    dist = getattr(ep, "dist", None)
    name = _dist_name(dist) or "an unnamed distribution"
    version = getattr(dist, "version", None)
    return f"{name} {version}" if version else name


def _describe(exc: BaseException) -> str:
    text = str(exc)
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


__all__ = ["GROUP", "PRESENT", "ABSENT", "BROKEN", "ANNOUNCEMENT", "Entry", "Found",
           "installed", "present", "load", "reset"]
