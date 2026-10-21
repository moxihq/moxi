"""`moxi install`: install a module and its required modules, set them up, offer extras.

    moxi install <module> [--with EXTRA,...] [--without EXTRA,...] [--all | --none]
                          [--from SPEC]...

1. Which environment: the one `moxi` runs in. A uv tool environment is known by its
   `uv-receipt.toml`; any other virtual environment is a plain one. The system Python is
   refused, and so is an environment whose `moxi` is an editable install.
2. Install the module with its required modules only. In a uv tool environment this
   re-runs `uv tool install` with everything the receipt holds plus the new requirement,
   since a re-run without an earlier requirement removes it; in a plain environment it
   runs `uv pip install` (or `pip install`). When installed modules declare an extra named
   by the module, the requirement is each such extra (`moxi-comms[cache]`), so every
   depending module's version range applies. `--from` gives requirements with a direct
   URL (`moxi-turns @ git+...`), for before PyPI holds the modules: give one for each Moxi
   distribution the module needs that the environment lacks.
3. Run the `SETUP` of each module the step installed, in a fresh process.
4. Offer each optional extra of the module not yet installed, one by one.
5. Install the chosen extras by step 2's route, and set up each new module.
6. Report.

A module already present skips step 2 and has its setup run again.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import names
from .cli import normalize, speakers_of

TOOL = "tool"
PLAIN = "plain"


class InstallError(Exception):
    """The installer refuses, saying why."""


# -- requirements ----------------------------------------------------------------------

@dataclass
class Req:
    """A requirement as the installer writes it: name, extras, and where it comes from."""

    name: str
    extras: list[str] = field(default_factory=list)
    specifier: str = ""
    #: A direct reference (`git+https://...@v1`, `file:///...whl`), or None.
    url: str | None = None
    #: A local directory installed editable (uv tool environments only), or None.
    editable: str | None = None

    @property
    def key(self) -> str:
        return normalize(self.name)

    def text(self) -> str:
        extras = f"[{','.join(self.extras)}]" if self.extras else ""
        if self.url:
            return f"{self.name}{extras} @ {self.url}"
        return f"{self.name}{extras}{self.specifier}"

    def merged(self, other: Req) -> Req:
        """This requirement with `other`'s extras added; `other`'s source wins when it
        names one, so a `--from` URL replaces an index name and a plain name keeps an
        earlier URL."""
        extras = list(self.extras)
        for extra in other.extras:
            if normalize(extra) not in map(normalize, extras):
                extras.append(extra)
        if other.url or other.editable:
            return Req(self.name, extras, url=other.url, editable=other.editable)
        if other.specifier and not (self.url or self.editable):
            return Req(self.name, extras, specifier=other.specifier)
        return Req(self.name, extras, self.specifier, self.url, self.editable)


def union(base: list[Req], added: list[Req]) -> list[Req]:
    """Every requirement of `base` keeps its place, extras and source; each of `added` is
    merged into the one of the same name, or appended."""
    out: dict[str, Req] = {}
    for req in [*base, *added]:
        out[req.key] = out[req.key].merged(req) if req.key in out else req
    return list(out.values())


def parse_from(spec: str) -> Req:
    """A `--from` value: one Moxi distribution's requirement with a direct URL."""
    from .cli import _Requirement

    try:
        req = _Requirement(spec)
    except ValueError:
        raise InstallError(f"--from {spec!r} is not a requirement") from None
    if not req.url:
        raise InstallError(f"--from {spec!r} names no URL; write it as "
                           f"'<distribution> @ git+https://...'")
    if req.extra is not None or ";" in spec:
        raise InstallError(f"--from {spec!r} carries a marker; give the requirement alone")
    if not _is_moxi_distribution(req.name):
        raise InstallError(f"--from {spec!r} is not a Moxi distribution (moxi or "
                           f"moxi-<module>)")
    return Req(req.name, list(req.extras), url=req.url)


def _is_moxi_distribution(name: str) -> bool:
    name = normalize(name)
    if name == names.HARNESS:
        return True
    prefix = f"{names.HARNESS}-"
    return name.startswith(prefix) and names.is_module_name(name[len(prefix):])


# -- the uv tool receipt ------------------------------------------------------------------

_ENTRY_KEYS = {"name", "extras", "specifier", "path", "directory", "url", "git",
               "subdirectory", "editable"}


def receipt_entry(entry: dict, where: str = "requirements") -> Req:
    """One receipt requirement as a `Req`. Raises `InstallError` for one it cannot write
    back."""
    from urllib.parse import parse_qsl, urlsplit, urlunsplit

    def refuse(why: str) -> InstallError:
        return InstallError(f"the uv receipt's {where} entry {entry!r} cannot be written "
                            f"back ({why}); reinstall the tool by hand with uv")

    if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
        raise refuse("it names no distribution")
    unknown = sorted(set(entry) - _ENTRY_KEYS)
    if unknown:
        raise refuse(f"unknown key {', '.join(unknown)}")
    name = entry["name"]
    extras = entry.get("extras") or []
    if not isinstance(extras, list) or not all(isinstance(e, str) for e in extras):
        raise refuse("its extras are not a list of names")
    sources = [k for k in ("specifier", "path", "directory", "url", "git", "editable")
               if k in entry]
    if len(sources) > 1:
        raise refuse(f"it has more than one source ({', '.join(sources)})")
    subdirectory = entry.get("subdirectory")
    if subdirectory is not None and (not sources or sources[0] not in ("url", "git")):
        raise refuse("a subdirectory without a URL")
    kind = sources[0] if sources else None
    value = entry.get(kind) if kind else None
    if kind is not None and not isinstance(value, str):
        raise refuse(f"its {kind} is not a string")
    if kind is None:
        return Req(name, list(extras))
    if kind == "specifier":
        return Req(name, list(extras), specifier=value.replace(" ", ""))
    if kind in ("path", "directory"):
        return Req(name, list(extras), url=Path(value).absolute().as_uri())
    if kind == "editable":
        if extras:
            raise refuse("an editable install with extras")
        return Req(name, [], editable=value)
    if kind == "url":
        url = value + (f"#subdirectory={subdirectory}" if subdirectory else "")
        return Req(name, list(extras), url=url)
    # git: "<url>?rev=<ref>&subdirectory=<dir>"
    parts = urlsplit(value)
    if parts.fragment:
        raise refuse("a git URL with a fragment")
    ref = None
    for qkey, qvalue in parse_qsl(parts.query, keep_blank_values=True):
        if qkey in ("rev", "tag", "branch") and ref is None:
            ref = qvalue
        elif qkey == "subdirectory" and subdirectory is None:
            subdirectory = qvalue
        else:
            raise refuse(f"a git URL with {qkey}=")
    base = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    url = f"git+{base}" + (f"@{ref}" if ref else "")
    if subdirectory:
        url += f"#subdirectory={subdirectory}"
    return Req(name, list(extras), url=url)


#: Receipt options written back as `--<key> VALUE`, or once per item of a list.
_OPTION_VALUES = {"index-url", "index-strategy", "keyring-provider", "resolution",
                  "prerelease", "fork-strategy", "exclude-newer", "link-mode"}
_OPTION_LISTS = {"find-links", "extra-index-url", "no-build-package", "no-binary-package",
                 "no-build-isolation-package", "no-sources-package"}
#: Receipt options written back as `--<key>` when true.
_OPTION_FLAGS = {"no-index", "compile-bytecode", "no-build", "no-binary",
                 "no-build-isolation", "no-sources"}


def receipt_options(options: dict) -> list[str]:
    """The receipt's `[tool.options]` as flags, since a re-run without them drops them."""
    def refuse(key: str, value) -> InstallError:
        return InstallError(f"the uv receipt's option {key} = {value!r} cannot be written "
                            f"back; reinstall the tool by hand with uv")

    flags: list[str] = []
    for key, value in options.items():
        if key in _OPTION_VALUES and isinstance(value, str):
            flags += [f"--{key}", value]
        elif key in _OPTION_LISTS and isinstance(value, list) and all(
                isinstance(v, str) for v in value):
            for item in value:
                flags += [f"--{key}", item]
        elif key in _OPTION_FLAGS and isinstance(value, bool):
            if value:
                flags.append(f"--{key}")
        elif key == "index" and isinstance(value, list):
            for index in value:
                flags += _index_flag(index, refuse)
        else:
            raise refuse(key, value)
    return flags


def _index_flag(index, refuse) -> list[str]:
    if not isinstance(index, dict) or not isinstance(index.get("url"), str):
        raise refuse("index", index)
    plain = {"explicit": False, "format": "simple", "authenticate": "auto",
             "default": index.get("default", False)}
    for key, value in index.items():
        if key in ("url", "name"):
            continue
        if key not in plain or value != plain[key]:
            raise refuse("index", index)
    if index.get("default"):
        return ["--default-index", index["url"]]
    name = index.get("name")
    return ["--index", f"{name}={index['url']}" if name else index["url"]]


_TOOL_KEYS = {"requirements", "constraints", "overrides", "build-constraint-dependencies",
              "excludes", "python", "entrypoints", "options"}
_TOOL_FILES = {"constraints": "--constraints", "overrides": "--overrides",
               "build-constraint-dependencies": "--build-constraints"}


def tool_command(receipt: dict, prefix: Path, added: list[Req], uv: str,
                 scratch: Path) -> tuple[list[str], dict[str, str], list[Req]]:
    """The `uv tool install` re-run that keeps everything `receipt` holds and adds `added`.

    Returns the argv, the environment variables to set, and the requirements it installs.
    Files for the receipt's constraints, overrides and excludes are written in `scratch`.
    """
    tool = receipt.get("tool")
    if not isinstance(tool, dict):
        raise InstallError(f"{prefix / 'uv-receipt.toml'} has no [tool] table")
    unknown = sorted(set(tool) - _TOOL_KEYS)
    if unknown:
        raise InstallError(f"the uv receipt holds {', '.join(unknown)}, which the installer "
                           f"cannot write back; reinstall the tool by hand with uv")
    held = tool.get("requirements")
    if not isinstance(held, list) or not held:
        raise InstallError("the uv receipt names no requirement")
    reqs = union([receipt_entry(e) for e in held], added)
    head, rest = reqs[0], reqs[1:]
    argv = [uv, "tool", "install"]
    argv += ["--editable", head.editable] if head.editable else [head.text()]
    for req in rest:
        argv += ["--with-editable", req.editable] if req.editable else ["--with", req.text()]
    for key, flag in _TOOL_FILES.items():
        entries = tool.get(key)
        if not entries:
            continue
        lines = []
        for entry in entries:
            req = receipt_entry(entry, key)
            if req.editable:
                raise InstallError(f"the uv receipt's {key} entry {entry!r} cannot be "
                                   f"written back (editable)")
            lines.append(req.text())
        path = scratch / f"{key}.txt"
        path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
        argv += [flag, str(path)]
    excludes = tool.get("excludes")
    if excludes:
        if not isinstance(excludes, list) or not all(isinstance(e, str) for e in excludes):
            raise InstallError(f"the uv receipt's excludes {excludes!r} cannot be written "
                               f"back")
        path = scratch / "excludes.txt"
        path.write_text("".join(f"{e}\n" for e in excludes), encoding="utf-8")
        argv += ["--excludes", str(path)]
    python = tool.get("python")
    if python is not None:
        if not isinstance(python, str):
            raise InstallError(f"the uv receipt's python {python!r} cannot be written back")
        argv += ["--python", python]
    options = tool.get("options") or {}
    if not isinstance(options, dict):
        raise InstallError("the uv receipt's [tool.options] is not a table")
    argv += receipt_options(options)

    env = {"UV_TOOL_DIR": str(prefix.parent)}
    bins = {str(Path(e["install-path"]).parent) for e in tool.get("entrypoints") or ()
            if isinstance(e, dict) and isinstance(e.get("install-path"), str)}
    if len(bins) == 1:
        env["UV_TOOL_BIN_DIR"] = bins.pop()
    return argv, env, reqs


# -- the environment ----------------------------------------------------------------------

@dataclass
class Environment:
    kind: str
    prefix: Path
    #: The environment's own interpreter, for fresh processes.
    python: str
    receipt: dict | None = None


def environment() -> Environment:
    """The environment `moxi` runs in, or `InstallError` when the installer refuses it."""
    import sysconfig

    prefix = Path(sys.prefix)
    if sys.prefix == sys.base_prefix:
        raise InstallError(f"moxi runs from the system Python ({sys.executable}); install "
                           f"Moxi with `uv tool install moxi-<module>` or into a virtual "
                           f"environment")
    url = _direct_url("moxi")
    if url and (url.get("dir_info") or {}).get("editable"):
        raise InstallError(f"moxi is an editable install here ({prefix}), a workspace's; "
                           f"add a module by declaring it in the workspace and running "
                           f"`uv sync`")
    scripts = Path(sysconfig.get_path("scripts"))
    python = scripts / "python"
    python_text = str(python) if python.exists() else sys.executable
    receipt_path = prefix / "uv-receipt.toml"
    if receipt_path.is_file():
        import tomllib

        try:
            receipt = tomllib.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise InstallError(f"{receipt_path} cannot be read: {exc}") from None
        return Environment(TOOL, prefix, python_text, receipt)
    return Environment(PLAIN, prefix, python_text)


def _direct_url(dist_name: str) -> dict | None:
    from importlib.metadata import PackageNotFoundError, distribution

    try:
        text = distribution(dist_name).read_text("direct_url.json")
    except PackageNotFoundError:
        return None
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def installed_source(dist_name: str) -> Req | None:
    """A requirement naming where an installed distribution came from, when it came from a
    direct URL (`direct_url.json`), so a plain environment's install keeps that source."""
    data = _direct_url(dist_name)
    if not data or not isinstance(data.get("url"), str):
        return None
    url = data["url"]
    vcs = data.get("vcs_info")
    if isinstance(vcs, dict):
        ref = vcs.get("requested_revision") or vcs.get("commit_id")
        url = f"{vcs.get('vcs', 'git')}+{url}" + (f"@{ref}" if ref else "")
    elif (data.get("dir_info") or {}).get("editable"):
        raise InstallError(f"{dist_name} is an editable install here; install its extras "
                           f"in its workspace")
    subdirectory = data.get("subdirectory")
    if subdirectory:
        url += f"#subdirectory={subdirectory}"
    return Req(dist_name, url=url)


# -- running things (replaced in tests) ----------------------------------------------------

def run(argv: list[str], env: dict[str, str] | None = None) -> int:
    """Run a command with the installer's output; returns its exit code."""
    environ = None
    if env:
        environ = dict(os.environ)
        environ.update(env)
    print("$ " + " ".join(_quote(a) for a in argv), flush=True)
    try:
        return subprocess.run(argv, env=environ, check=False).returncode
    except OSError as exc:
        print(f"moxi install: cannot run {argv[0]}: {exc}", file=sys.stderr)
        return 127


def _quote(arg: str) -> str:
    import shlex

    return shlex.quote(arg)


_SNAPSHOT = "import sys\nfrom moxi.cli import main\nsys.exit(main(['modules', '--json']))\n"

_SETUP = """\
import importlib, sys
module, _, attr = sys.argv[1].partition(":")
result = getattr(importlib.import_module(module), attr)()
sys.exit(result if isinstance(result, int) and not isinstance(result, bool) else 0)
"""


def snapshot(python: str) -> dict:
    """`moxi modules --json`, read by a fresh process so it sees what was just installed."""
    done = subprocess.run([python, "-c", _SNAPSHOT], capture_output=True, text=True,
                          check=False)
    if done.returncode != 0:
        raise InstallError(f"`moxi modules --json` failed after the install "
                           f"(exit {done.returncode}): {done.stderr.strip()}")
    try:
        return json.loads(done.stdout)
    except ValueError:
        raise InstallError("`moxi modules --json` printed no JSON") from None


def run_setup(python: str, target: str) -> int:
    """Run a module's `SETUP` target in a fresh process; returns its exit code."""
    try:
        return subprocess.run([python, "-c", _SETUP, target], check=False).returncode
    except OSError as exc:
        print(f"moxi install: cannot run {python}: {exc}", file=sys.stderr)
        return 127


# -- the installer ------------------------------------------------------------------------

def _parser(prog: str):
    import argparse

    parser = argparse.ArgumentParser(
        prog=prog, description="Install a Moxi module with its required modules, set it "
                               "up, and offer each of its optional extras.")
    parser.add_argument("module", help="the module, by its name (comms, cache, ...)")
    parser.add_argument("--with", dest="with_", action="append", default=[],
                        metavar="EXTRA,...", help="answer yes for these extras")
    parser.add_argument("--without", action="append", default=[], metavar="EXTRA,...",
                        help="answer no for these extras")
    every = parser.add_mutually_exclusive_group()
    every.add_argument("--all", action="store_true", help="answer yes for every extra")
    every.add_argument("--none", action="store_true", help="answer no for every extra")
    parser.add_argument("--from", dest="from_", action="append", default=[],
                        metavar="SPEC",
                        help="a Moxi distribution's requirement with a URL, as "
                             "'moxi-turns @ git+https://...'; before PyPI holds the "
                             "modules, give one for each Moxi distribution the module "
                             "needs that this environment lacks")
    return parser


def _names(values: list[str]) -> list[str]:
    out = []
    for value in values:
        for part in value.split(","):
            part = normalize(part)
            if part and part not in out:
                out.append(part)
    return out


def main(argv: list[str] | None = None, prog: str = "moxi install") -> int:
    args = _parser(prog).parse_args(sys.argv[1:] if argv is None else argv)
    try:
        return Installer(args).run()
    except InstallError as exc:
        print(f"moxi install: {exc}", file=sys.stderr)
        return 1


class Installer:
    def __init__(self, args):
        self.module = args.module
        self.yes = _names(args.with_)
        self.no = _names(args.without)
        self.every = True if args.all else False if args.none else None
        self.froms = [parse_from(spec) for spec in args.from_]
        self.setup_failed: list[str] = []
        self.installed: list[str] = []
        self.on: list[str] = []
        self.declined: list[str] = []

    # step 1 ----------------------------------------------------------------------
    def run(self) -> int:
        module = self.module
        if not names.is_module_name(module):
            raise InstallError(f"{module!r} is not a module name: lower-case ASCII letters "
                               f"and digits only")
        if module in names.VERBS:
            raise InstallError(f"{module!r} is one of moxi's own commands, not a module")
        both = sorted(set(self.yes) & set(self.no))
        if both:
            raise InstallError(f"--with and --without both name {', '.join(both)}")
        env = environment()
        self.env = env
        before = snapshot(env.python)
        present = _by_name(before)

        # step 2 and 3 ------------------------------------------------------------
        if module in present:
            print(f"{module} is already installed "
                  f"({present[module]['distribution']} {present[module]['version']}); "
                  f"running its setup again.")
            self._check_answers(present[module])
            after = before
            self._setup([present[module]])
        else:
            speakers = speakers_of(module)
            if speakers:
                reqs = [Req(s.distribution, [module]) for s in speakers]
                for s in speakers:
                    print(f"{s.module} declares the extra {module} ({s.requirement}); "
                          f"installing it through {s.distribution}[{module}].")
            else:
                reqs = [Req(names.distribution(module))]
            if not self._install(reqs, before):
                return 1
            after = snapshot(env.python)
            found = _by_name(after).get(module)
            if found is None:
                raise InstallError(f"the install finished, but no installed distribution "
                                   f"declares the Moxi module {module!r}; is "
                                   f"{names.distribution(module)} a Moxi module?")
            self._check_answers(found)
            new = _new(before, after)
            self.installed += [m["name"] for m in new]
            self._setup(_setup_order(new, module))

        # step 4 ----------------------------------------------------------------------
        target = _by_name(after)[module]
        if target["state"] != "present":
            print(f"{module} is {target['state']}: {target['reason']}", file=sys.stderr)
            self._report(after)
            return 1
        chosen = self._ask(target)

        # step 5 ----------------------------------------------------------------------
        if chosen:
            reqs = [Req(target["distribution"], chosen)]
            if not self._install(reqs, after):
                self._report(after)
                return 1
            final = snapshot(env.python)
            new = _new(after, final)
            self.installed += [m["name"] for m in new]
            self._setup(_setup_order(new, module))
            after = final
            for extra in chosen:
                sentence = _offer(target, extra)
                self.on.append(f"{module}[{extra}]: {sentence}")
        elif self.froms and module in present:
            print("nothing was installed, so --from was not used.")

        # step 6 ----------------------------------------------------------------------
        self._report(after)
        return 1 if self.setup_failed else 0

    # the route ---------------------------------------------------------------------
    def _install(self, reqs: list[Req], before: dict) -> bool:
        env = self.env
        reqs = union(reqs, self.froms)
        if env.kind == TOOL:
            import shutil
            import tempfile

            uv = shutil.which("uv")
            if uv is None:
                raise InstallError("this is a uv tool environment, and uv is not on PATH")
            with tempfile.TemporaryDirectory(prefix="moxi-install-") as scratch:
                argv, extra_env, _ = tool_command(env.receipt, env.prefix, reqs, uv,
                                                  Path(scratch))
                code = run(argv, extra_env)
        else:
            sourced = []
            for req in reqs:
                source = None if req.url or req.editable else installed_source(req.name)
                sourced.append(source.merged(req) if source else req)
            reqs = sourced
            code = run(_plain_command(reqs))
        if code != 0:
            print(f"moxi install: the install failed (exit {code}); nothing was set up.",
                  file=sys.stderr)
            if any(r.url for r in reqs) or self.froms:
                print("Before PyPI holds the modules, give one --from for each Moxi "
                      "distribution the module needs that this environment lacks; one "
                      "left out is looked for on the index.", file=sys.stderr)
            return False
        if env.kind == TOOL:
            # The re-run rewrote the receipt; read it again for the next run.
            import tomllib

            path = env.prefix / "uv-receipt.toml"
            try:
                env.receipt = tomllib.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
                raise InstallError(f"{path} cannot be read after the install: "
                                   f"{exc}") from None
        return True

    # step 3 ------------------------------------------------------------------------
    def _setup(self, items: list[dict]) -> None:
        for item in items:
            target = item.get("setup")
            name = item["name"]
            if item.get("state") != "present":
                print(f"setup {name}: skipped, the module is {item.get('state')}: "
                      f"{item.get('reason')}", file=sys.stderr)
                self.setup_failed.append(name)
                continue
            if not target:
                continue
            print(f"setup {name}: running {target}", flush=True)
            code = run_setup(self.env.python, target)
            if code == 0:
                print(f"setup {name}: done")
            else:
                print(f"setup {name}: failed (exit {code}). The install stays; run "
                      f"`moxi install {name}` again once the cause is fixed.",
                      file=sys.stderr)
                self.setup_failed.append(name)

    # step 4 ------------------------------------------------------------------------
    def _check_answers(self, item: dict) -> None:
        offered = [normalize(o["extra"]) for o in item.get("optional", ())]
        unknown = [e for e in [*self.yes, *self.no] if e not in offered]
        if unknown:
            have = ", ".join(offered) or "none"
            raise InstallError(f"{item['name']} declares no extra "
                               f"{', '.join(unknown)} (its extras: {have})")

    def _ask(self, target: dict) -> list[str]:
        chosen = []
        interactive = sys.stdin is not None and sys.stdin.isatty()
        for offer in target.get("optional", ()):
            extra = offer["extra"]
            if offer["installed"]:
                continue
            key = normalize(extra)
            if key in self.yes:
                answer = True
            elif key in self.no:
                answer = False
            elif self.every is not None:
                answer = self.every
            elif interactive:
                answer = _prompt(f"{target['name']} [{extra}]: {offer['sentence']}\n"
                                 f"Install {extra}? [y/N] ")
            else:
                answer = False
            if answer:
                chosen.append(extra)
            else:
                line = f"{target['name']} [{extra}] not installed: {offer['sentence']}"
                print(f"{line}\n    add it later with: {offer['add']}")
                self.declined.append(extra)
        return chosen

    # step 6 ------------------------------------------------------------------------
    def _report(self, after: dict) -> None:
        present = _by_name(after)
        print()
        if self.installed:
            for name in self.installed:
                item = present.get(name, {})
                print(f"installed {name} ({item.get('distribution')} "
                      f"{item.get('version')})")
        else:
            print("installed no new module")
        for line in self.on:
            print(f"now on: {line}")
        for name in self.installed:
            if name == self.module:
                continue
            item = present.get(name, {})
            left = [o["extra"] for o in item.get("optional", ()) if not o["installed"]]
            if left:
                print(f"{name} offers {', '.join(left)} too: moxi install {name}")
        for name in self.setup_failed:
            print(f"setup failed for {name}; see above", file=sys.stderr)


def _prompt(question: str) -> bool:
    try:
        answer = input(question)
    except EOFError:
        print()
        return False
    return answer.strip().lower() in ("y", "yes")


def _plain_command(reqs: list[Req]) -> list[str]:
    import shutil

    for req in reqs:
        if req.editable:
            raise InstallError(f"{req.name} is editable; a plain environment's install "
                               f"cannot write it back")
    texts = [req.text() for req in reqs]
    uv = shutil.which("uv")
    if uv is not None:
        return [uv, "pip", "install", "--python", sys.executable, *texts]
    return [sys.executable, "-m", "pip", "install", *texts]


def _by_name(info: dict) -> dict[str, dict]:
    return {item["name"]: item for item in info.get("modules", ())}


def _new(before: dict, after: dict) -> list[dict]:
    had = set(_by_name(before))
    return [item for item in after.get("modules", ()) if item["name"] not in had]


def _offer(item: dict, extra: str) -> str:
    for offer in item.get("optional", ()):
        if normalize(offer["extra"]) == normalize(extra):
            return offer["sentence"]
    return ""


def _setup_order(items: list[dict], last: str) -> list[dict]:
    """Required modules before the modules that require them; `last` as late as it can."""
    pending = {item["name"]: item for item in items}
    out: list[dict] = []
    while pending:
        ready = sorted((n for n, item in pending.items()
                        if not any(r in pending for r in item.get("requires", ()))),
                       key=lambda n: (n == last, n))
        if not ready:  # a cycle: take them as they come
            ready = sorted(pending, key=lambda n: (n == last, n))
        out.append(pending.pop(ready[0]))
    return out


__all__ = ["main", "Installer", "InstallError", "Req", "union", "parse_from",
           "receipt_entry", "receipt_options", "tool_command", "environment",
           "installed_source", "snapshot", "run", "run_setup"]
