"""The `moxi` command: the umbrella every module's commands run under.

    moxi <module> <verb> ...     runs the module's own command, in this process
    moxi modules [--json]        what this environment holds
    moxi install <module> ...    installs a module, sets it up, offers its extras
    moxi shortcut [<module>]     the opt-in bare command for a module
    moxi config <module> ...     a module's settings and its config file
    moxi check [PATH]            the module contract check
    moxi help [<verb>|<module>]  help
    moxi version                 versions

The verbs are reserved: no module may take one as its name. A module owns only what
follows its name: `moxi comms send ...` calls the `CLI` target comms' announcement names
as `main(argv, prog="moxi comms")` and exits with what it returns.

Every Moxi distribution declares the console script `moxi = "moxi.cli:main"`, so a plain
`uv tool install moxi-<module>` puts `moxi` on PATH.
"""

from __future__ import annotations

import os
import re
import sys

from . import names

USAGE = """\
usage: moxi <module> <verb> ...
       moxi <command> ...

commands:
  modules [--json]                   list the Moxi modules this environment holds
  install <module> [options]         install a module and offer its optional extras
  shortcut [<module>] [--remove]     put a bare <module> command on PATH, or take it off
  config <module> [show|path|check|init]
                                     a module's settings and its config file
  check [--staged] [PATH]            check a module repo against the module contract
  help [<command>|<module>]          this help, or a command's or a module's
  version                            the versions of moxi and its modules
"""

#: How a shortcut file marks itself; `moxi shortcut --remove` removes only a file
#: carrying the marker of the module named.
SHORTCUT_MARKER = "# moxi shortcut: "


def main(argv: list[str] | None = None) -> int:
    """The `moxi` console script. Returns the exit code."""
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        return _main(argv)
    except KeyboardInterrupt:
        print("moxi: interrupted", file=sys.stderr)
        return 130
    except BrokenPipeError:
        # Output piped into a reader that went away (`moxi modules | head`).
        try:
            sys.stdout = open(os.devnull, "w")  # noqa: SIM115
        except OSError:
            pass
        return 1


def _main(argv: list[str]) -> int:
    if not argv:
        sys.stderr.write(USAGE)
        return 2
    first, rest = argv[0], argv[1:]
    if first in ("-h", "--help"):
        sys.stdout.write(USAGE)
        return 0
    if first == "--version":
        return _version([])
    verb = _VERBS.get(first)
    if verb is not None:
        return _exit_code(verb, rest)
    if first.startswith("-"):
        _error(f"unknown option {first!r}")
        sys.stderr.write(USAGE)
        return 2
    return dispatch(first, rest)


def _exit_code(func, argv: list[str]) -> int:
    """Run a verb, turning argparse's SystemExit into an exit code."""
    try:
        code = func(argv)
    except SystemExit as exc:
        return _system_exit_code(exc)
    return 0 if code is None else int(code)


def _system_exit_code(exc: SystemExit) -> int:
    code = exc.code
    if code is None:
        return 0
    if isinstance(code, int):
        return code
    print(code, file=sys.stderr)
    return 1


def _error(message: str) -> None:
    print(f"moxi: {message}", file=sys.stderr)


def _parser(verb: str, description: str):
    import argparse

    return argparse.ArgumentParser(prog=f"moxi {verb}", description=description)


# -- dispatch to a module ---------------------------------------------------------------

def dispatch(name: str, argv: list[str]) -> int:
    """Run module `name`'s command with `argv`, in this process. Returns its exit code.

    An absent or broken module, or one with no command, prints why and returns 2.
    """
    from . import modules

    if not names.is_module_name(name):
        _error(f"{name!r} is neither a moxi command nor a module name")
        sys.stderr.write(USAGE)
        return 2
    found = modules.load(name)
    if found.state != modules.PRESENT:
        _error(found.reason)
        print(install_hint(name), file=sys.stderr)
        return 2
    if found.cli is None:
        print(f"{name}: {found.summary}", file=sys.stderr)
        print(f"{name} has no command of its own; other modules call it.", file=sys.stderr)
        users = users_of(name)
        if users:
            print(f"used here by: {', '.join(users)}", file=sys.stderr)
        else:
            print("no installed module uses it.", file=sys.stderr)
        return 2
    target, _, attr = found.cli.partition(":")
    try:
        import importlib

        func = getattr(importlib.import_module(target), attr)
    except Exception as exc:  # noqa: BLE001 - a broken module is reported, not raised
        _error(f"{name}'s command {found.cli} cannot be loaded: "
               f"{type(exc).__name__}: {exc}")
        print(install_hint(name), file=sys.stderr)
        return 2
    try:
        code = func(argv, prog=f"moxi {name}")
    except SystemExit as exc:
        return _system_exit_code(exc)
    # What the command returns means what `sys.exit` would make of it.
    return _system_exit_code(SystemExit(code))


def install_hint(name: str) -> str:
    """The one command that installs module `name`, as RULE 2 names it.

    When installed modules declare an extra named by `name` that installs it (see
    `speakers_of`), the line goes through the first of them (`moxi install comms --with cache`) and says which range each speaks,
    so the depending module's range applies. Otherwise it is `moxi install <name>`.
    """
    speakers = speakers_of(name)
    if not speakers:
        return f"install it: moxi install {name}"
    first = speakers[0]
    ranges = "; ".join(f"{s.module} speaks {s.requirement}" for s in speakers)
    return f"install it: moxi install {first.module} --with {name}   ({ranges})"


# -- what the environment holds ----------------------------------------------------------

class _Requirement:
    """One Requires-Dist line, read with the standard library alone."""

    __slots__ = ("name", "extras", "specifier", "url", "extra")

    _HEAD = re.compile(r"\s*([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)\s*"
                       r"(?:\[([^\]]*)\])?\s*(.*)$", re.S)
    _EXTRA = re.compile(r"""\bextra\s*==\s*(['"])([^'"]+)\1""")

    def __init__(self, line: str):
        body, _, marker = line.partition(";")
        match = self._HEAD.match(body)
        if match is None:
            raise ValueError(f"cannot read the requirement {line!r}")
        self.name = normalize(match.group(1))
        self.extras = tuple(normalize(e) for e in (match.group(2) or "").split(",")
                            if e.strip())
        tail = match.group(3).strip()
        self.url = None
        if tail.startswith("@"):
            self.url = tail[1:].strip()
            tail = ""
        self.specifier = tail.strip("() ").replace(" ", "")
        found = self._EXTRA.search(marker)
        self.extra = normalize(found.group(2)) if found else None

    def __str__(self) -> str:
        extras = f"[{','.join(self.extras)}]" if self.extras else ""
        if self.url:
            return f"{self.name}{extras} @ {self.url}"
        return f"{self.name}{extras}{self.specifier}"


def normalize(name: str) -> str:
    """A distribution or extra name in its normalized form (PEP 503, PEP 685)."""
    return re.sub(r"[-_.]+", "-", name.strip()).lower()


def _distribution(name: str):
    from importlib.metadata import PackageNotFoundError, distribution

    try:
        return distribution(name)
    except PackageNotFoundError:
        return None


def requirements(dist_name: str) -> list[_Requirement]:
    """The requirements a distribution's metadata declares, extras' included."""
    dist = _distribution(dist_name)
    if dist is None:
        return []
    out = []
    for line in dist.requires or ():
        try:
            out.append(_Requirement(line))
        except ValueError:
            continue
    return out


def provided_extras(dist_name: str) -> list[str]:
    dist = _distribution(dist_name)
    if dist is None:
        return []
    return [normalize(e) for e in dist.metadata.get_all("Provides-Extra") or ()]


def extra_installed(dist_name: str, extra: str, _seen: set | None = None) -> bool:
    """True when every requirement extra `extra` of `dist_name` adds is installed.

    A requirement is installed when its distribution is, and every extra it names is in
    turn. Version ranges and markers other than `extra == ...` are not evaluated.
    """
    seen = set() if _seen is None else _seen
    key = (normalize(dist_name), normalize(extra))
    if key in seen:
        return True
    seen.add(key)
    wanted = [r for r in requirements(dist_name) if r.extra == key[1]]
    for req in wanted:
        if _distribution(req.name) is None:
            return False
        if not all(extra_installed(req.name, e, seen) for e in req.extras):
            return False
    return True


class Speaker:
    """An installed module that declares an extra named by another module."""

    __slots__ = ("module", "distribution", "requirement")

    def __init__(self, module: str, distribution: str, requirement: str):
        self.module = module
        self.distribution = distribution
        self.requirement = requirement


def speakers_of(name: str) -> list[Speaker]:
    """Installed modules declaring an extra that installs module `name`, with the
    requirement it adds."""
    from . import modules

    # Only an extra named by a module and installing that module's distribution routes
    # it (§4.3, [R75]). A feature extra that happens to share the name (comms' `codex`,
    # which adds moxi-turns[codex]) or a third party's extra says nothing about module
    # `name`, so the hint and `moxi install` would go through the wrong distribution.
    wanted = normalize(names.distribution(name))
    out = []
    for entry in modules.installed():
        if entry.name == name or not entry.distribution:
            continue
        if normalize(name) not in provided_extras(entry.distribution):
            continue
        own = [r for r in requirements(entry.distribution)
               if r.extra == normalize(name) and r.name == wanted]
        if not own:
            continue
        out.append(Speaker(entry.name, entry.distribution, ", ".join(str(r) for r in own)))
    return out


def _module_of_distribution() -> dict[str, str]:
    from . import modules

    return {normalize(e.distribution): e.name for e in modules.installed() if e.distribution}


def required_modules(dist_name: str, by_dist: dict[str, str] | None = None) -> list[str]:
    """The installed Moxi modules a distribution requires outright (not through an extra)."""
    by_dist = _module_of_distribution() if by_dist is None else by_dist
    out = []
    for req in requirements(dist_name):
        if req.extra is None and req.name in by_dist and by_dist[req.name] not in out:
            out.append(by_dist[req.name])
    return out


def users_of(name: str) -> list[str]:
    """Installed modules that require module `name`, or take it through an extra."""
    from . import modules

    wanted = normalize(names.distribution(name))
    out = []
    for entry in modules.installed():
        if entry.name == name or not entry.distribution:
            continue
        for req in requirements(entry.distribution):
            if req.name != wanted:
                continue
            if req.extra is None:
                out.append(entry.name)
            elif extra_installed(entry.distribution, req.extra):
                out.append(f"{entry.name} (through its extra {req.extra})")
            break
    return out


# -- moxi modules -------------------------------------------------------------------------

def describe() -> dict:
    """Everything `moxi modules --json` prints: one object for this environment."""
    from . import modules

    shortcut_dir = _shortcut_dir()
    by_dist = _module_of_distribution()
    listed = []
    for entry in modules.installed():
        if any(item["name"] == entry.name for item in listed):
            continue
        found = modules.load(entry.name)
        dist = found.distribution or entry.distribution
        optional = []
        for extra, sentence in found.optional.items():
            optional.append({
                "extra": extra,
                "sentence": sentence,
                "installed": bool(dist) and extra_installed(dist, extra),
                "add": f"moxi install {entry.name} --with {extra}",
            })
        listed.append({
            "name": entry.name,
            "distribution": dist,
            "version": found.version or entry.version,
            "state": found.state,
            "reason": found.reason,
            "summary": found.summary,
            "cli": found.cli,
            "setup": found.setup,
            "shortcut": shortcut_dir is not None and _is_shortcut(
                os.path.join(shortcut_dir, entry.name), entry.name),
            "requires": required_modules(dist, by_dist) if dist else [],
            "optional": optional,
        })
    return {
        "moxi": _version_of("moxi"),
        "python": sys.executable,
        "prefix": sys.prefix,
        "modules": listed,
    }


def _modules(argv: list[str]) -> int:
    parser = _parser("modules", "List the Moxi modules this Python environment holds.")
    parser.add_argument("--json", action="store_true", help="print one JSON object")
    args = parser.parse_args(argv)
    info = describe()
    if args.json:
        import json

        print(json.dumps(info, indent=2))
        return 0
    if not info["modules"]:
        print("No Moxi module is installed in this environment.")
        print("Install one with: moxi install <module>")
        return 0
    for item in info["modules"]:
        head = f"{item['name']}  {item['distribution']} {item['version']}  {item['state']}"
        if item["cli"]:
            head += f"  shortcut {'on' if item['shortcut'] else 'off'}"
        print(head)
        if item["reason"]:
            print(f"    {item['reason']}")
        if item["summary"]:
            print(f"    {item['summary']}")
        for opt in item["optional"]:
            mark = "installed" if opt["installed"] else "not installed"
            line = f"    [{opt['extra']}] {mark}: {opt['sentence']}"
            if not opt["installed"]:
                line += f"  ({opt['add']})"
            print(line)
    return 0


# -- moxi shortcut ------------------------------------------------------------------------

def _shortcut_dir() -> str | None:
    """The directory of the `moxi` on PATH, where shortcuts are written."""
    import shutil

    found = shutil.which("moxi")
    return os.path.dirname(found) if found else None


def _is_shortcut(path: str, module: str | None = None) -> bool:
    """True when `path` is a shortcut file (for `module`, when given)."""
    marked = _shortcut_module(path)
    return marked is not None and (module is None or marked == module)


def _shortcut_module(path: str) -> str | None:
    try:
        if not os.path.isfile(path):
            return None
        with open(path, "rb") as fh:
            head = fh.read(256)
    except OSError:
        return None
    for line in head.decode("utf-8", "replace").splitlines()[:3]:
        if line.startswith(SHORTCUT_MARKER):
            return line[len(SHORTCUT_MARKER):].strip() or None
    return None


def shortcut_text(module: str) -> str:
    """The content of module `module`'s shortcut file."""
    return f'#!/bin/sh\n{SHORTCUT_MARKER}{module}\nexec moxi {module} "$@"\n'


def _shortcut(argv: list[str]) -> int:
    parser = _parser("shortcut", "Put a bare <module> command on PATH that runs "
                                 "`moxi <module>`, or take it off. With no module, "
                                 "list the shortcuts that are on.")
    parser.add_argument("module", nargs="?")
    parser.add_argument("--remove", action="store_true", help="take the shortcut off")
    args = parser.parse_args(argv)
    import shutil

    where = _shortcut_dir()
    if args.module is None:
        if args.remove:
            parser.error("--remove needs a module")
        if where is None:
            _error("moxi is not on PATH, so no shortcut can be on")
            return 1
        on = []
        try:
            entries = sorted(os.listdir(where))
        except OSError as exc:
            _error(f"cannot read {where}: {exc}")
            return 1
        for entry in entries:
            path = os.path.join(where, entry)
            module = _shortcut_module(path)
            if module is not None:
                on.append((module, path))
        if not on:
            print(f"No shortcut is on (they would be in {where}).")
        for module, path in on:
            print(f"{module}  {path}")
        return 0

    module = args.module
    if not names.is_module_name(module):
        _error(f"{module!r} is not a module name")
        return 2
    if where is None:
        _error("moxi is not on PATH; a shortcut is written beside it, so put the "
               "directory holding moxi on PATH first")
        return 1
    path = os.path.join(where, module)

    if args.remove:
        if not os.path.lexists(path):
            _error(f"no shortcut for {module} at {path}")
            return 1
        if not _is_shortcut(path, module):
            _error(f"{path} is not moxi's shortcut for {module}; leaving it alone")
            return 1
        os.unlink(path)
        print(f"removed the shortcut {path}")
        return 0

    from . import modules

    found = modules.load(module)
    if found.state != modules.PRESENT:
        _error(found.reason)
        print(install_hint(module), file=sys.stderr)
        return 1
    if found.cli is None:
        _error(f"{module} has no command of its own, so it takes no shortcut")
        return 1
    clash = shutil.which(module)
    if clash is not None:
        if _is_shortcut(clash, module):
            print(f"the shortcut for {module} is already on: {clash}")
            return 0
        _error(f"{module} is already on PATH at {clash}; refusing to shadow it. "
               f"Run it as `moxi {module}`.")
        return 1
    if os.path.lexists(path):
        _error(f"{path} exists; refusing to overwrite it")
        return 1
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o755)
        with os.fdopen(fd, "w") as fh:
            fh.write(shortcut_text(module))
    except OSError as exc:
        _error(f"cannot write {path}: {exc}")
        return 1
    print(f"wrote the shortcut {path}: `{module} ...` now runs `moxi {module} ...`")
    return 0


# -- moxi config --------------------------------------------------------------------------

def _config(argv: list[str]) -> int:
    parser = _parser("config", "A module's settings and its config file.")
    parser.add_argument("module")
    parser.add_argument("action", nargs="?", default="show",
                        choices=("show", "path", "check", "init"),
                        help="show (the default): every key with its value, source, doc "
                             "and when it is read; path: the file's path; check: exit "
                             "non-zero when the file cannot be read; init: write the file "
                             "with every key commented out at its default")
    args = parser.parse_args(argv)
    from . import config, modules
    from .paths import Layout

    module = args.module
    if not names.is_module_name(module):
        _error(f"{module!r} is not a module name")
        return 2
    found = modules.load(module)
    if found.state != modules.PRESENT:
        _error(found.reason)
        print(install_hint(module), file=sys.stderr)
        return 2
    path = Layout(module).config_file()
    if args.action == "path":
        print(path)
        return 0
    try:
        schema = config.schema_for(found)
    except config.ConfigError as exc:
        _error(f"{module}'s settings schema cannot be read: {exc}")
        return 1
    if schema is None:
        print(f"{module} has no settings.")
        return 0
    if args.action == "init":
        if os.path.lexists(path):
            _error(f"{path} exists; refusing to overwrite it")
            return 1
        try:
            schema.check_env(module)
        except config.ConfigError as exc:
            _error(str(exc))
            return 1
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(config_template(module, schema))
        except OSError as exc:
            _error(f"cannot write {path}: {exc}")
            return 1
        print(f"wrote {path}")
        return 0
    try:
        settings = config.load(module, schema)
    except config.ConfigError as exc:
        _error(str(exc))
        return 1
    if args.action == "check":
        state = "" if path.exists() else " (no file: every key at its default)"
        print(f"ok: {path}{state}")
        return 0
    print(f"# {path}{'' if path.exists() else '  (no file: every key at its default)'}")
    for key in schema.live():
        value = settings[key.name]
        shown = "(none)" if value is None else _toml_value(
            str(value) if key.type == "path" else value)
        print(f"{key.name} = {shown}    [{settings.source(key.name)}]")
        extra = f"read at {key.read}"
        if key.env:
            extra += f"; env {key.env}"
        print(f"    {key.doc} ({extra})")
    return 0


def config_template(module: str, schema) -> str:
    """The text `moxi config <module> init` writes: every live key commented out at its
    default, under its doc. Uncommenting every line gives a file that reads as the
    defaults."""
    lines = [f"# The config file of the Moxi module {module}.",
             "# Every key is shown commented out at its default. Uncomment a key to set it;",
             "# `moxi config " + module + "` shows what is in effect.", ""]
    groups: dict[str, list] = {}
    for key in schema.live():
        group, _, leaf = key.name.rpartition(".")
        groups.setdefault(group, []).append((leaf, key))
    order = sorted(groups, key=lambda g: (g != "", list(groups).index(g)))
    for group in order:
        if group:
            lines.append(f"# [{group}]")
        for leaf, key in groups[group]:
            notes = [f"read at {key.read}"]
            if key.values:
                notes.append("one of " + ", ".join(map(_toml_value, key.values)))
            if key.env:
                notes.append(f"env {key.env} overrides it")
            lines.append(f"# {key.doc} ({'; '.join(notes)})")
            lines.append(f"# {_toml_key(leaf)} = {_toml_value(key.default)}")
        lines.append("")
    return "\n".join(lines)


_BARE = re.compile(r"[A-Za-z0-9_-]+")


def _toml_key(key: str) -> str:
    return key if _BARE.fullmatch(key) else _toml_string(key)


def _toml_string(text: str) -> str:
    out = ['"']
    for ch in text:
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _toml_value(value) -> str:
    """A TOML inline value for a schema default."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value:
            return "nan"
        if value in (float("inf"), float("-inf")):
            return "inf" if value > 0 else "-inf"
        return repr(value)
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        if not value:
            return "{}"
        return "{ " + ", ".join(f"{_toml_key(str(k))} = {_toml_value(v)}"
                                for k, v in value.items()) + " }"
    raise TypeError(f"cannot write {value!r} as TOML")


# -- moxi check, help, version --------------------------------------------------------------

def _check(argv: list[str]) -> int:
    from . import contract

    return contract.main(argv)


def _install(argv: list[str]) -> int:
    from . import install

    return install.main(argv)


def _help(argv: list[str]) -> int:
    if not argv:
        sys.stdout.write(USAGE)
        return 0
    topic, rest = argv[0], argv[1:]
    if topic in ("help", "-h", "--help"):
        sys.stdout.write(USAGE)
        return 0
    verb = _VERBS.get(topic)
    if verb is not None:
        return _exit_code(verb, ["--help"])
    return dispatch(topic, ["--help", *rest])


def _version_of(dist: str) -> str | None:
    found = _distribution(dist)
    return found.version if found is not None else None


def _version(argv: list[str]) -> int:
    parser = _parser("version", "The versions of moxi and the modules it sees.")
    parser.parse_args(argv)
    from . import modules

    print(f"moxi {_version_of('moxi') or 'unknown'}")
    for entry in modules.installed():
        print(f"{entry.name} {entry.version} ({entry.distribution})")
    return 0


_VERBS = {
    "modules": _modules,
    "install": _install,
    "shortcut": _shortcut,
    "config": _config,
    "check": _check,
    "help": _help,
    "version": _version,
}


__all__ = ["main", "dispatch", "install_hint", "describe", "speakers_of", "users_of",
           "required_modules", "extra_installed", "normalize", "shortcut_text",
           "config_template", "SHORTCUT_MARKER"]
