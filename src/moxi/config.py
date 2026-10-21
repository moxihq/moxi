"""One config file per module, born declared, read only through this module.

Every value a user may set lives in its module's one config file,
`<config root>/<module>/config.toml` (see `moxi.paths`), and every key in that file is
declared in the module's schema: a `settings.toml` shipped in the module's package and
named by its announcement's `SETTINGS`.

The schema
----------

Each key is a TOML table whose dotted name is the key's place in the config file. A table
with a `type` (or a `retired`) is a key; any other table groups keys. So the schema

    [backend]
    type = "enum"
    values = ["local", "remote"]
    default = "local"
    doc = "Where messages are recorded."
    read = "start"
    env = "MOXI_COMMS_GATEWAY"

    [local.retain_days]
    type = "int"
    default = 30
    doc = "How many days the local record keeps a message."
    read = "start"

declares `backend` and `local.retain_days`, which the user sets as `backend = "remote"`
and `[local] retain_days = 7`. A key's fields:

    type     bool, int, float, string, enum, path, list or table
    default  the value when neither the file nor `env` sets one; for a `path`, "" means
             no value (the setting reads None and the module applies its own rule)
    doc      one line saying what the key does
    read     "start" (read once, when the module starts) or "use" (read at each use)
    values   for an enum, the strings it may take
    env      optional: the one variable that overrides the key for one process; spelled
             with the module's prefix (`MOXI_<MODULE>_`), and not on a list or a table
    retired  on a key the module has dropped: what to do now. Setting it is an error.

A `path` expands `~` and is refused when relative. A `list` is any TOML array and a
`table` any TOML table; the module checks their contents.

Reading
-------

`load(module, schema)` returns frozen `Settings`: each live key's value and where it came
from (`default`, `file` or `env`). An `env` override wins over the file, which wins over
the default. The file is parsed again only when its size or modification time changes. A
missing file means every default. A malformed file, an unknown key, a retired key or a
value of the wrong type raises `ConfigError` naming the file, the key and the fix.

`Config(module, schema)` is the same reader for a running module: `start()` raises, and
`use(key)` reads one key at the point of use, keeping the last good settings when the file
turns bad and reporting the error once per change of the file.
"""

from __future__ import annotations

import copy
import os
import re
import sys
import threading
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from . import names
from .paths import LOCATORS, Layout, _expanduser

TYPES = ("bool", "int", "float", "string", "enum", "path", "list", "table")
READS = ("start", "use")
FIELDS = ("type", "default", "doc", "read", "values", "env", "retired")

DEFAULT = "default"
FILE = "file"
ENV = "env"

_KEY_PART = re.compile(r"[A-Za-z0-9_-]+")
_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]*")
_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")


class ConfigError(Exception):
    """A config file, or a schema, that cannot be read. Names the file, the key and the fix."""

    def __init__(self, file: Path | str | None, key: str | None, problem: str, fix: str):
        self.file = None if file is None else str(file)
        self.key = key
        self.problem = problem
        self.fix = fix
        where = self.file or "config"
        if key:
            where += f": key {key!r}"
        super().__init__(f"{where}: {problem}; fix: {fix}")


class SchemaError(ConfigError):
    """A module's schema that does not hold to the format. The module's own bug."""


@dataclass(frozen=True)
class Key:
    """One declared key."""

    name: str
    type: str | None
    default: Any
    doc: str
    read: str | None
    values: tuple[str, ...] | None = None
    env: str | None = None
    retired: str | None = None


class Schema:
    """A module's declared keys, by dotted name, retired ones included."""

    def __init__(self, keys: Mapping[str, Key], source: str = "settings.toml"):
        self.keys: Mapping[str, Key] = MappingProxyType(dict(keys))
        self.source = source
        # Every dotted prefix of a key is a group the file may hold as a table.
        self._groups = frozenset(".".join(k.split(".")[:i])
                                 for k in self.keys for i in range(1, k.count(".") + 1))

    def __repr__(self) -> str:
        return f"Schema({len(self.keys)} keys from {self.source})"

    def live(self) -> Iterator[Key]:
        """Every key that is not retired, in declared order."""
        return (key for key in self.keys.values() if key.retired is None)

    @classmethod
    def parse(cls, text: str, *, source: str = "settings.toml") -> Schema:
        import tomllib

        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise SchemaError(source, None, f"is not valid TOML ({exc})",
                              "correct the schema's TOML") from None
        keys: dict[str, Key] = {}
        _walk_schema(data, "", source, keys)
        return cls(keys, source)

    @classmethod
    def from_path(cls, path: Path | str) -> Schema:
        path = Path(path)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise SchemaError(path, None, f"cannot be read ({exc})",
                              "reinstall the module") from None
        return cls.parse(text, source=str(path))

    @classmethod
    def from_resource(cls, package: str, resource: str) -> Schema:
        from importlib.resources import files

        source = f"{package}/{resource}"
        try:
            text = files(package).joinpath(resource).read_text(encoding="utf-8")
        except (OSError, ModuleNotFoundError, UnicodeDecodeError) as exc:
            raise SchemaError(source, None, f"cannot be read ({exc})",
                              "reinstall the module") from None
        return cls.parse(text, source=source)

    def check_env(self, module: str) -> None:
        """Refuse an `env` override not spelled with `module`'s prefix, or one that would
        shadow a root locator or another key's override."""
        prefix = names.env_prefix(module)
        locators = {f"{prefix}{suffix}" for suffix in LOCATORS}
        seen: dict[str, str] = {}
        for key in self.live():
            if key.env is None:
                continue
            if not key.env.startswith(prefix) or key.env == prefix:
                raise SchemaError(self.source, key.name,
                                  f"env {key.env!r} is not spelled with the module's "
                                  f"prefix {prefix}", f"name it {prefix}<NAME>")
            if key.env in locators:
                raise SchemaError(self.source, key.name,
                                  f"env {key.env!r} is a root locator of moxi.paths",
                                  "choose another name")
            if key.env in seen:
                raise SchemaError(self.source, key.name,
                                  f"env {key.env!r} already overrides {seen[key.env]!r}",
                                  "give each key its own variable")
            seen[key.env] = key.name

    def is_group(self, dotted: str) -> bool:
        return dotted in self._groups


def schema_for(found) -> Schema | None:
    """The schema a loaded module's announcement names, or None when it names none.

    `found` is a `moxi.modules.Found` in the `present` state.
    """
    if found.settings is None:
        return None
    if not found.package:
        raise SchemaError(found.target, None, "the announcement is not in a package, so "
                          "SETTINGS names nothing", "move the announcement into the "
                          "module's package")
    return Schema.from_resource(found.package, found.settings)


@dataclass(frozen=True)
class Settings:
    """A module's settings at one reading: every live key's value and its source."""

    module: str
    file: Path
    schema: Schema
    values: Mapping[str, Any]
    sources: Mapping[str, str]

    def __getitem__(self, key: str) -> Any:
        try:
            return self.values[key]
        except KeyError:
            raise KeyError(f"{self.module} declares no setting {key!r}") from None

    def __contains__(self, key: object) -> bool:
        return key in self.values

    def __iter__(self) -> Iterator[str]:
        return iter(self.values)

    def source(self, key: str) -> str:
        self[key]
        return self.sources[key]


def load(module: str, schema: Schema, *, env: Mapping[str, str] | None = None) -> Settings:
    """Read module `module`'s config file against `schema`. Raises `ConfigError`."""
    environ = os.environ if env is None else env
    path = Layout(module).config_file(environ)
    return _settings(module, schema, path, _parsed(path), environ)


class Config:
    """The reader a running module keeps: start once, then read keys at their use."""

    def __init__(self, module: str, schema: Schema, *,
                 env: Mapping[str, str] | None = None,
                 report: Callable[[ConfigError], None] | None = None):
        self.module = names.check_module_name(module)
        self.schema = schema
        self._env = env
        self._report = report or _report_to_stderr
        self._good: Settings | None = None
        self._reported: tuple | None = None
        self._lock = threading.Lock()

    def start(self) -> Settings:
        """Read the settings at start. A `ConfigError` here refuses the start."""
        settings = load(self.module, self.schema, env=self._env)
        with self._lock:
            self._good = settings
            self._reported = None
        return settings

    def current(self) -> Settings:
        """The settings now, or the last good ones while the file is in error."""
        with self._lock:
            if self._good is None:
                raise RuntimeError("Config.current() before start()")
            try:
                self._good = load(self.module, self.schema, env=self._env)
                self._reported = None
            except ConfigError as exc:
                stamp = _stamp(self._good.file)
                if stamp != self._reported:
                    self._reported = stamp
                    self._report(exc)
            return self._good

    def use(self, key: str) -> Any:
        """One key's value at its point of use."""
        return self.current()[key]


# -- reading the file ------------------------------------------------------------------

_cache: dict[Path, tuple[tuple, dict | ConfigError]] = {}
_cache_lock = threading.Lock()


def _stamp(path: Path) -> tuple | None:
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        return ("error", str(exc))
    return (st.st_size, st.st_mtime_ns, st.st_ino)


def _parsed(path: Path) -> dict | None:
    """The file's TOML, parsed again only when its size or modification time changed."""
    import tomllib

    stamp = _stamp(path)
    if stamp is None:
        return None
    with _cache_lock:
        hit = _cache.get(path)
    if hit is not None and hit[0] == stamp:
        result = hit[1]
    else:
        try:
            with open(path, "rb") as fh:
                result = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            result = ConfigError(path, None, f"is not valid TOML ({exc})",
                                 "correct the file's TOML syntax")
        except (OSError, UnicodeDecodeError) as exc:
            result = ConfigError(path, None, f"cannot be read ({exc})",
                                 "make the file a readable TOML file, or remove it")
        with _cache_lock:
            _cache[path] = (stamp, result)
    if isinstance(result, ConfigError):
        raise result
    return result


def _settings(module: str, schema: Schema, path: Path, data: dict | None,
              environ: Mapping[str, str]) -> Settings:
    schema.check_env(module)
    from_file: dict[str, Any] = {}
    if data is not None:
        _walk_file(data, "", schema, path, from_file, environ)
    values: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for key in schema.live():
        raw = environ.get(key.env) if key.env else None
        if raw:
            values[key.name] = _from_env(key, raw, environ)
            sources[key.name] = ENV
        elif key.name in from_file:
            values[key.name] = copy.deepcopy(from_file[key.name])
            sources[key.name] = FILE
        else:
            values[key.name] = _default_value(key, environ)
            sources[key.name] = DEFAULT
    return Settings(module=module, file=path, schema=schema,
                    values=MappingProxyType(values), sources=MappingProxyType(sources))


def _walk_file(table: dict, prefix: str, schema: Schema, path: Path,
               out: dict[str, Any], environ: Mapping[str, str]) -> None:
    fix_unknown = "remove it, or check its spelling against the module's schema"
    for name, value in table.items():
        dotted = f"{prefix}{name}"
        key = schema.keys.get(dotted)
        if key is not None:
            if key.retired is not None:
                raise ConfigError(path, dotted, "is retired", key.retired)
            out[dotted] = _check_value(key, value, path, environ)
        elif schema.is_group(dotted):
            if not isinstance(value, dict):
                raise ConfigError(path, dotted, "is a table of settings, not a value",
                                  f"write it as [{dotted}] with its keys under it")
            _walk_file(value, f"{dotted}.", schema, path, out, environ)
        else:
            raise ConfigError(path, dotted, "is not a setting of this module", fix_unknown)


def _check_value(key: Key, value: Any, path: Path | str,
                 environ: Mapping[str, str]) -> Any:
    """`value` as key `key`'s type, or `ConfigError`."""
    kind = key.type
    ok = True
    if kind == "bool":
        ok = isinstance(value, bool)
    elif kind == "int":
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif kind == "float":
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
        value = float(value) if ok else value
    elif kind in ("string", "enum", "path"):
        ok = isinstance(value, str)
    elif kind == "list":
        ok = isinstance(value, list)
    elif kind == "table":
        ok = isinstance(value, dict)
    if not ok:
        raise ConfigError(path, key.name, f"is {_toml_kind(value)}, not {_a(kind)}",
                          f"set it to {_a(kind)}{_enum_hint(key)}")
    if kind == "enum" and value not in key.values:
        raise ConfigError(path, key.name, f"is {value!r}, which it cannot be",
                          f"set it to one of {', '.join(map(repr, key.values))}")
    if kind == "path":
        return _path(key, value, path, environ)
    return value


def _path(key: Key, value: str, source: Path | str,
          environ: Mapping[str, str]) -> Path | None:
    if value == "":
        return None
    expanded = Path(_expanduser(value, environ))
    if not expanded.is_absolute():
        raise ConfigError(source, key.name, f"is the relative path {value!r}",
                          "give an absolute path, or one starting with ~")
    return expanded


def _from_env(key: Key, raw: str, environ: Mapping[str, str]) -> Any:
    where = f"environment variable {key.env}"
    kind = key.type
    if kind == "bool":
        if raw.lower() in _TRUE:
            return True
        if raw.lower() in _FALSE:
            return False
        raise ConfigError(where, key.name, f"is {raw!r}, not a bool",
                          "set it to 1, true, yes or on, or 0, false, no or off")
    if kind in ("int", "float"):
        try:
            return int(raw) if kind == "int" else float(raw)
        except ValueError:
            raise ConfigError(where, key.name, f"is {raw!r}, not {_a(kind)}",
                              f"set it to {_a(kind)}") from None
    return _check_value(key, raw, where, environ)


def _default_value(key: Key, environ: Mapping[str, str]) -> Any:
    if key.type == "path":
        return _path(key, key.default, "schema", environ)
    if key.type in ("list", "table"):

        return copy.deepcopy(key.default)
    return key.default


def _toml_kind(value: Any) -> str:
    if isinstance(value, bool):
        return "a bool"
    if isinstance(value, int):
        return "an int"
    if isinstance(value, float):
        return "a float"
    if isinstance(value, str):
        return "a string"
    if isinstance(value, list):
        return "a list"
    if isinstance(value, dict):
        return "a table"
    return f"a {type(value).__name__}"


def _a(kind: str | None) -> str:
    return {"int": "an int", "enum": "an enum string"}.get(kind or "", f"a {kind}")


def _enum_hint(key: Key) -> str:
    return f" ({', '.join(map(repr, key.values))})" if key.values else ""


def _report_to_stderr(exc: ConfigError) -> None:
    print(f"moxi: {exc}; keeping the last good settings", file=sys.stderr)


# -- reading the schema ----------------------------------------------------------------

def _walk_schema(table: dict, prefix: str, source: str, out: dict[str, Key]) -> None:
    for name, value in table.items():
        dotted = f"{prefix}{name}"
        if not _KEY_PART.fullmatch(name):
            raise SchemaError(source, dotted, "is not a bare TOML key",
                              "use letters, digits, - and _ only")
        if not isinstance(value, dict):
            raise SchemaError(source, dotted, "is a value, not a key's table",
                              "declare it as a table with type, default, doc and read")
        if isinstance(value.get("type"), str) or isinstance(value.get("retired"), str):
            out[dotted] = _key(dotted, value, source)
        else:
            if not value:
                raise SchemaError(source, dotted, "is an empty table",
                                  "declare a key in it, or remove it")
            _walk_schema(value, f"{dotted}.", source, out)


def _key(name: str, spec: dict, source: str) -> Key:
    def bad(problem: str, fix: str) -> SchemaError:
        return SchemaError(source, name, problem, fix)

    unknown = sorted(set(spec) - set(FIELDS))
    if unknown:
        raise bad(f"has unknown field(s) {', '.join(unknown)}",
                  f"use only {', '.join(FIELDS)}")
    retired = spec.get("retired")
    if retired is not None:
        if not isinstance(retired, str) or not retired.strip():
            raise bad("has an empty retired", "say what to do now instead")
        return Key(name=name, type=spec.get("type"), default=spec.get("default"),
                   doc=spec.get("doc", ""), read=spec.get("read"), retired=retired)

    for required in ("type", "default", "doc", "read"):
        if required not in spec:
            raise bad(f"lacks {required}", f"give it a {required}")
    kind, doc, read = spec["type"], spec["doc"], spec["read"]
    if kind not in TYPES:
        raise bad(f"has type {kind!r}", f"use one of {', '.join(TYPES)}")
    if not isinstance(doc, str) or not doc.strip() or "\n" in doc:
        raise bad("has no one-line doc", "say in one line what the key does")
    if read not in READS:
        raise bad(f"has read {read!r}", "use \"start\" or \"use\"")

    values = spec.get("values")
    if kind == "enum":
        if (not isinstance(values, list) or not values
                or not all(isinstance(v, str) for v in values)
                or len(set(values)) != len(values)):
            raise bad("is an enum without values", "list the strings it may take")
        values = tuple(values)
    elif values is not None:
        raise bad("has values but is not an enum", "remove values")

    env = spec.get("env")
    if env is not None:
        if not isinstance(env, str) or not _ENV_NAME.fullmatch(env):
            raise bad(f"has env {env!r}", "name one variable in upper case")
        if kind in ("list", "table"):
            raise bad(f"has env on a {kind}", "remove env; a list or table is set in "
                                              "the file")

    key = Key(name=name, type=kind, default=spec["default"], doc=doc, read=read,
              values=values, env=env)
    try:
        _check_value(key, key.default, source, os.environ)
    except ConfigError as exc:
        raise bad(f"has a default that {exc.problem}", exc.fix) from None
    return key


__all__ = ["TYPES", "READS", "FIELDS", "DEFAULT", "FILE", "ENV", "ConfigError",
           "SchemaError", "Key", "Schema", "Settings", "schema_for", "load", "Config"]
