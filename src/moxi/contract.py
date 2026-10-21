"""The module contract check: what `moxi check` runs over a module's repo.

`check(root, *, staged=False)` reads a repo's `pyproject.toml` and its Python files with
`ast`, imports none of them, and returns a sorted list of `Finding`. With `staged` it
reads every file from git's index instead of the working tree, so a partly staged commit
is judged as it will land. An empty list means the repo keeps the contract.

The checks, numbered as each finding names them:

1. Another module's internals. In `src/` and `tests/`, an import of another Moxi
   module's package other than its `API` module; any import of an optional Moxi module
   (one the distribution installs only through an extra), which is reached only through
   `moxi.modules.load`; an import of a core module outside the core's public set, or of
   a name starting with `_`. In the core's own repo, any import of a module's package.
2. An undeclared dependency. An import that is not the standard library, the repo's own
   package, or provided by a distribution the repo declares in `dependencies` or an
   extra (under `tests/`, also a `[dependency-groups]` group). An import maps to a
   distribution by what the repo declares, never by what is installed: `moxi_<name>` to
   `moxi-<name>`, any other to the declared distribution of the same normalized name, or
   else through `[tool.moxi.imports]`. So the answer does not depend on which extras
   the checking environment holds.
3. An extra reached at import time from the announcement, the `API` module, the `CLI`
   target, the `SETUP` target or a console script not in `GATED`.
4. Extras and reasons out of step: an extra without an `OPTIONAL` sentence or the
   reverse; an extra that installs an optional Moxi module under another name than the
   module's; a `GATED` script or extra that does not exist.
5. A setting in the environment (in `src/`): an environment read whose key is neither a
   literal nor a module-level string constant, unless it falls under a prefix entry of
   `INTERNAL_ENV`; a key starting `MOXI_` that `INTERNAL_ENV` does not declare.
6. A module's own console script looked up on PATH with `shutil.which` (in `src/`).
7. The names: the `moxi.modules` entry point, the distribution and import names, the
   console scripts, the announcement's ten names, the `CLI` target's `argv` and `prog`
   parameters and the `SETTINGS` schema.

`segment` findings hold a status line segment to showing only: the import-time closure of
the announcement's `SEGMENT` module, within the module's package, may not import
`subprocess`, `socket`, `multiprocessing`, `ctypes`, `http`, `urllib` or turns' package,
nor call `os.system`, `os.exec*`, `os.spawn*`, `os.fork` or `os.kill`.

The core's own repo, known by its distribution name `moxi`, is held to checks 1 to 6 with
two exemptions: check 5 does not apply inside `moxi.paths` and `moxi.config`, which read
every module's root locators and declared overrides by computed keys, and check 6 does
not apply to `shutil.which("moxi")` inside `moxi.cli`, where the shortcut verb looks up
the umbrella's place. Check 7 is replaced: the core declares no `moxi.modules` entry
point, its import package is `moxi`, and its one console script is
`moxi = "moxi.cli:main"`. With no announcement, any extra the core declares fails check 4.
"""

from __future__ import annotations

import ast
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import names

#: The distribution and import name of the core.
CORE = names.HARNESS
#: The entry-point group a module announces itself in (as `moxi.modules.GROUP`).
GROUP = "moxi.modules"
#: The core modules another module may import (the core's table of what it holds).
CORE_MODULES = frozenset({"names", "paths", "config", "modules", "contract", "cli"})
#: The names an announcement defines (as `moxi.modules.ANNOUNCEMENT`).
ANNOUNCEMENT = ("INTERFACES", "API", "CLI", "SUMMARY", "OPTIONAL", "GATED", "INTERNAL_ENV",
                "SETTINGS", "SETUP", "SEGMENT")
#: The core's one console script.
CORE_SCRIPT = ("moxi", "moxi.cli:main")
#: Inside these core modules check 5 does not apply.
CORE_ENV_READERS = frozenset({"moxi.paths", "moxi.config"})
#: What a segment's closure may not import (top-level package names), besides turns.
SEGMENT_IMPORTS = frozenset({"subprocess", "socket", "multiprocessing", "ctypes", "http",
                             "urllib"})

_MODULE_IMPORT = re.compile(r"moxi_([a-z0-9]+)")
_MODULE_DIST = re.compile(r"moxi-([a-z0-9]+)")
_SCRIPT_PART = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
_DOTTED = re.compile(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)*")
_TARGET = re.compile(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)*:[A-Za-z_]\w*")
_REQUIREMENT = re.compile(r"\s*([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)\s*"
                          r"(?:\[([^\]]*)\])?")


class CheckError(Exception):
    """The check cannot run over this path at all (no `pyproject.toml`, not a git repo)."""


@dataclass(frozen=True, order=True)
class Finding:
    """One way the repo breaks the contract."""

    path: str
    line: int
    check: str
    message: str

    def __str__(self) -> str:
        where = f"{self.path}:{self.line}" if self.line else self.path
        return f"{where}: check {self.check}: {self.message}"


def check(root: Path | str, *, staged: bool = False) -> list[Finding]:
    """Check the module repo at `root` against the contract; return its findings, sorted.

    With `staged`, every file is read from git's index, as the commit will land.
    """
    root = Path(root)
    tree = _IndexTree(root) if staged else _WorkTree(root)
    return sorted(set(_Repo(root, tree).run()))


def main(argv: Sequence[str] | None = None, *, prog: str = "moxi check") -> int:
    """`moxi check [--staged] [PATH]`: print the findings; exit 1 on any, 2 on an error."""
    import argparse

    parser = argparse.ArgumentParser(
        prog=prog,
        description="Check a Moxi module's repo against the module contract: imports, "
                    "declared dependencies, extras, environment reads and names.")
    parser.add_argument("--staged", action="store_true",
                        help="read the files from git's index, as the next commit lands")
    parser.add_argument("path", nargs="?", default=".", metavar="PATH",
                        help="the repo to check (default: the current directory)")
    args = parser.parse_args(argv)
    try:
        findings = check(args.path, staged=args.staged)
    except CheckError as exc:
        print(f"{prog}: {exc}", file=sys.stderr)
        return 2
    for finding in findings:
        print(finding)
    if findings:
        count = len(findings)
        print(f"{prog}: {count} finding{'s' if count != 1 else ''} in {args.path}",
              file=sys.stderr)
        return 1
    return 0


# --- reading the tree ----------------------------------------------------------------------

def _git(root: Path, *args: str, staged: bool, data: bytes | None = None) -> bytes:
    import os
    import subprocess

    env = dict(os.environ)
    if not staged:
        # Outside a hook's staged run, find the repo by the path alone.
        for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
            env.pop(name, None)
    try:
        done = subprocess.run(["git", "-C", str(root), *args], input=data,
                              capture_output=True, env=env, check=False)
    except OSError as exc:
        raise CheckError(f"cannot run git: {exc}") from None
    if done.returncode != 0:
        raise CheckError(f"git {args[0]} failed in {root}: "
                         f"{done.stderr.decode(errors='replace').strip()}")
    return done.stdout


def _is_git(root: Path, *, staged: bool) -> bool:
    try:
        out = _git(root, "rev-parse", "--is-inside-work-tree", staged=staged)
    except CheckError:
        return False
    return out.strip() == b"true"


class _WorkTree:
    """The files of a working tree: tracked and untracked-unignored in a git repo,
    else every file outside hidden, build and environment directories."""

    _SKIP = frozenset({"__pycache__", "build", "dist", "node_modules"})

    def __init__(self, root: Path):
        if not root.is_dir():
            raise CheckError(f"{root} is not a directory")
        self.root = root
        if _is_git(root, staged=False):
            out = _git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard",
                       staged=False)
            listed = sorted({p for p in out.decode("utf-8", "surrogateescape").split("\0")
                             if p})
            self.paths = [p for p in listed if (root / p).is_file()]
        else:
            self.paths = sorted(self._walk(root, PurePosixPath()))

    def _walk(self, here: Path, rel: PurePosixPath) -> Iterable[str]:
        for entry in sorted(here.iterdir()):
            sub = rel / entry.name
            if entry.is_dir():
                if (entry.name.startswith(".") or entry.name in self._SKIP
                        or entry.name.endswith(".egg-info")
                        or (entry / "pyvenv.cfg").exists()):
                    continue
                yield from self._walk(entry, sub)
            elif entry.is_file():
                yield str(sub)

    def files(self) -> list[str]:
        return self.paths

    def read(self, path: str) -> bytes | None:
        try:
            return (self.root / path).read_bytes()
        except OSError:
            return None


class _IndexTree:
    """The files of git's index: what the next commit holds."""

    def __init__(self, root: Path):
        if not root.is_dir():
            raise CheckError(f"{root} is not a directory")
        if not _is_git(root, staged=True):
            raise CheckError(f"{root} is not inside a git work tree, so it has no index "
                             "to check; drop --staged")
        self.root = root
        out = _git(root, "ls-files", "-z", "--stage", staged=True)
        self.blobs: dict[str, str] = {}
        self.unmerged: list[str] = []
        for entry in out.decode("utf-8", "surrogateescape").split("\0"):
            if not entry:
                continue
            info, path = entry.split("\t", 1)
            mode, sha, stage = info.split()
            if stage != "0":
                self.unmerged.append(path)
            elif mode in ("100644", "100755"):
                self.blobs[path] = sha
        self.paths = sorted(self.blobs)
        self._cache: dict[str, bytes] = {}

    def files(self) -> list[str]:
        return self.paths

    def read(self, path: str) -> bytes | None:
        if path not in self.blobs:
            return None
        if not self._cache:
            self._load()
        if path not in self._cache:
            self._cache[path] = _git(self.root, "cat-file", "blob", self.blobs[path],
                                     staged=True)
        return self._cache[path]

    def _load(self) -> None:
        # One `git cat-file --batch` for every blob the check may read.
        wanted = [p for p in self.paths if p.endswith((".py", ".toml"))]
        if not wanted:
            self._cache[""] = b""
            return
        out = _git(self.root, "cat-file", "--batch", staged=True,
                   data="".join(f"{self.blobs[p]}\n" for p in wanted).encode())
        at = 0
        for path in wanted:
            end = out.index(b"\n", at)
            header = out[at:end].split()
            size = int(header[2])
            self._cache[path] = out[end + 1:end + 1 + size]
            at = end + 1 + size + 1


# --- requirements and names ----------------------------------------------------------------

def _normalize(name: str) -> str:
    """A distribution or extra name as packaging compares them."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirement(text: object) -> tuple[str, frozenset[str]] | None:
    """The normalized distribution name and extras of one requirement string."""
    if not isinstance(text, str):
        return None
    match = _REQUIREMENT.match(text)
    if not match:
        return None
    extras = frozenset(_normalize(e.strip()) for e in (match.group(2) or "").split(",")
                       if e.strip())
    return _normalize(match.group(1)), extras


def _literal(node: ast.AST):
    """The value of an announcement's assignment, as `ast.literal_eval` reads it, with
    `frozenset(...)`, `set(...)`, `dict(...)` and `tuple(...)` of literals too."""
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id in ("frozenset", "set", "dict", "tuple")
            and len(node.args) <= 1):
        make = {"frozenset": frozenset, "set": set, "dict": dict, "tuple": tuple}
        inner = _literal(node.args[0]) if node.args else ()
        if node.keywords:
            if node.func.id != "dict" or any(k.arg is None for k in node.keywords):
                raise ValueError("not a literal")
            return dict(inner, **{k.arg: _literal(k.value) for k in node.keywords})
        return make[node.func.id](inner)
    return ast.literal_eval(node)


def _assignments(tree: ast.Module) -> dict[str, ast.AST]:
    """Each module-level name bound by a plain or annotated assignment; the last wins."""
    found: dict[str, ast.AST] = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    found[target.id] = stmt.value
        elif (isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
              and stmt.value is not None):
            found[stmt.target.id] = stmt.value
    return found


def _string_constants(tree: ast.Module) -> dict[str, str]:
    return {name: value.value for name, value in _assignments(tree).items()
            if isinstance(value, ast.Constant) and isinstance(value.value, str)}


def _is_type_checking(test: ast.AST) -> bool:
    return ((isinstance(test, ast.Name) and test.id == "TYPE_CHECKING")
            or (isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"))


@dataclass(frozen=True)
class _Import:
    line: int
    module: str           # as written; "" for `from . import x`
    names: tuple[str, ...]
    level: int
    at_import: bool       # runs when the file is imported (not in a function or a
                          # TYPE_CHECKING block)


class _ImportVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.found: list[_Import] = []
        self.deferred = 0

    def _function(self, node: ast.AST) -> None:
        self.deferred += 1
        self.generic_visit(node)
        self.deferred -= 1

    visit_FunctionDef = visit_AsyncFunctionDef = visit_Lambda = _function

    def visit_If(self, node: ast.If) -> None:
        if _is_type_checking(node.test):
            self.visit(node.test)
            self.deferred += 1
            for stmt in node.body:
                self.visit(stmt)
            self.deferred -= 1
            for stmt in node.orelse:
                self.visit(stmt)
        else:
            self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.found.append(_Import(node.lineno, alias.name, (), 0, not self.deferred))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self.found.append(_Import(node.lineno, node.module or "",
                                  tuple(a.name for a in node.names), node.level,
                                  not self.deferred))


class _Source:
    """One parsed Python file of the repo."""

    def __init__(self, path: str, tree: ast.Module, module: str | None, is_package: bool):
        self.path = path
        self.tree = tree
        #: Its dotted name when it is in the repo's import package(s) under `src/`.
        self.module = module
        self.is_package = is_package
        visitor = _ImportVisitor()
        visitor.visit(tree)
        self.imports = visitor.found
        parts = PurePosixPath(path).parts
        self.in_src = parts[0] == "src"
        self.in_tests = parts[0] == "tests"

    def absolute(self, imp: _Import) -> str | None:
        """The absolute module an import names; None for a relative import outside a
        package the check knows."""
        if imp.level == 0:
            return imp.module
        if self.module is None:
            return None
        package = self.module.split(".") if self.is_package else self.module.split(".")[:-1]
        if imp.level - 1 > len(package) - 1:
            return None
        base = package[:len(package) - (imp.level - 1)]
        return ".".join(base + ([imp.module] if imp.module else []))


# --- the repo --------------------------------------------------------------------------------

class _Repo:
    def __init__(self, root: Path, tree):
        self.root = root
        self.tree = tree
        self.findings: list[Finding] = []

    def add(self, path: str, line: int, check: str, message: str) -> None:
        self.findings.append(Finding(path, line, check, message))

    # -- setup ---------------------------------------------------------------------------

    def run(self) -> list[Finding]:
        for path in getattr(self.tree, "unmerged", ()):
            self.add(path, 0, "read", "has unmerged entries in the index; resolve the "
                                      "merge first")
        if not self._read_pyproject():
            return self.findings
        self._read_sources()
        self._read_announcement()
        self.check_imports()           # checks 1 and 2
        self.check_extras_at_import()  # check 3
        self.check_extras()            # check 4
        self.check_environment()       # check 5
        self.check_which()             # check 6
        if self.core:
            self.check_core_names()    # check 7, the core's form
        else:
            self.check_names()         # check 7
        self.check_segment()
        return self.findings

    def _read_pyproject(self) -> bool:
        import tomllib

        raw = self.tree.read("pyproject.toml")
        if raw is None:
            raise CheckError(f"{self.root} has no pyproject.toml, so it is not a module "
                             "repo")
        try:
            data = tomllib.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            self.add("pyproject.toml", 0, "read", f"cannot be parsed ({exc})")
            return False
        project = data.get("project")
        if not isinstance(project, dict) or not isinstance(project.get("name"), str):
            self.add("pyproject.toml", 0, "7", "has no [project] name")
            return False
        self.dist = _normalize(project["name"])
        self.core = self.dist == CORE
        match = _MODULE_DIST.fullmatch(self.dist)
        #: The module's name, when the distribution is spelled `moxi-<name>`.
        self.name = match.group(1) if match else None

        self.deps: dict[str, frozenset[str]] = {}
        self.extras: dict[str, list[tuple[str, frozenset[str]]]] = {}
        self.extra_names: dict[str, str] = {}   # normalized -> as written
        for req in project.get("dependencies") or []:
            parsed = _requirement(req)
            if parsed:
                self.deps[parsed[0]] = self.deps.get(parsed[0], frozenset()) | parsed[1]
        for extra, reqs in (project.get("optional-dependencies") or {}).items():
            norm = _normalize(extra)
            self.extra_names[norm] = extra
            self.extras[norm] = [p for p in map(_requirement, reqs or []) if p]
        self.groups: set[str] = set()
        for reqs in (data.get("dependency-groups") or {}).values():
            for req in reqs or []:
                parsed = _requirement(req)
                if parsed:
                    self.groups.add(parsed[0])
        in_extras = {dist for reqs in self.extras.values() for dist, _ in reqs}
        #: Distributions installed only through an extra.
        self.extra_only = in_extras - set(self.deps) - {self.dist}
        self.declared = set(self.deps) | in_extras | {self.dist}

        table = ((data.get("tool") or {}).get("moxi") or {}).get("imports") or {}
        self.import_table = {k: _normalize(v) for k, v in table.items()
                             if isinstance(k, str) and isinstance(v, str)}
        self.scripts: dict[str, str] = {}
        for group in ("scripts", "gui-scripts"):
            for script, target in (project.get(group) or {}).items():
                self.scripts[script] = target if isinstance(target, str) else ""
        eps = (project.get("entry-points") or {}).get(GROUP)
        self.entry_points: dict[str, str] = dict(eps) if isinstance(eps, dict) else {}
        self.own_import = CORE if self.core else (f"moxi_{self.name}" if self.name else None)
        return True

    def _read_sources(self) -> None:
        self.sources: dict[str, _Source] = {}
        self.modules: dict[str, _Source] = {}
        src_tops = set()
        tests_local = {"tests", "conftest"}
        for path in self.tree.files():
            parts = PurePosixPath(path).parts
            if parts[0] == "src" and len(parts) >= 2:
                src_tops.add(parts[1].removesuffix(".py"))
            if parts[0] == "tests" and len(parts) >= 2:
                tests_local.update(p.removesuffix(".py") for p in parts[1:])
        #: The top-level import packages under `src/`.
        self.src_tops = {t for t in src_tops if t.isidentifier()}
        self.own = set(self.src_tops) | ({self.own_import} if self.own_import else set())
        self.tests_local = {t for t in tests_local if t.isidentifier()}
        for path in self.tree.files():
            if not path.endswith(".py"):
                continue
            raw = self.tree.read(path)
            try:
                parsed = ast.parse(raw if raw is not None else b"", filename=path)
            except (SyntaxError, ValueError) as exc:
                why = exc.msg if isinstance(exc, SyntaxError) else str(exc)
                self.add(path, getattr(exc, "lineno", 0) or 0, "read",
                         f"does not parse, so the check cannot read it ({why})")
                continue
            parts = PurePosixPath(path).with_suffix("").parts
            module, is_package = None, False
            if (parts[0] == "src" and len(parts) >= 2
                    and all(p.isidentifier() for p in parts[1:])):
                dotted = list(parts[1:])
                if dotted[-1] == "__init__":
                    dotted.pop()
                    is_package = True
                module = ".".join(dotted) if dotted else None
            source = _Source(path, parsed, module, is_package)
            self.sources[path] = source
            if module:
                self.modules[module] = source
        self._edges = self._import_edges()

    def _read_announcement(self) -> None:
        """Read the announcement the entry point names, without importing it."""
        self.announcement: dict[str, object] | None = None
        self.announcement_source: _Source | None = None
        self.announcement_problems: list[str] = []
        if self.core or len(self.entry_points) != 1:
            return
        target = next(iter(self.entry_points.values()))
        if not isinstance(target, str) or target not in self.modules:
            return
        source = self.modules[target]
        self.announcement_source = source
        bound = _assignments(source.tree)
        values: dict[str, object] = {}
        for name in ANNOUNCEMENT:
            if name not in bound:
                self.announcement_problems.append(f"does not define {name}")
                continue
            try:
                values[name] = _literal(bound[name])
            except (ValueError, TypeError, SyntaxError, RecursionError):
                self.announcement_problems.append(
                    f"defines {name} by an expression the check cannot read; write it as "
                    "a literal")
        self.announcement = values

    def ann(self, name: str, default=None):
        value = (self.announcement or {}).get(name, default)
        return default if value is None else value

    def ann_map(self, name: str) -> dict[str, str]:
        value = self.ann(name, {})
        if isinstance(value, Mapping):
            return {k: v for k, v in value.items() if isinstance(k, str)}
        return {}

    # -- import resolution -----------------------------------------------------------------

    def provider(self, top: str) -> list[str]:
        """The distributions that may provide import name `top`, by the repo's own
        declarations only."""
        if top == CORE:
            return [CORE]
        match = _MODULE_IMPORT.fullmatch(top)
        if match:
            return [f"moxi-{match.group(1)}"]
        found = [_normalize(top)]
        if top in self.import_table:
            found.append(self.import_table[top])
        return found

    def _import_edges(self) -> dict[str, set[str]]:
        """Own modules each own module imports when it is imported."""
        edges: dict[str, set[str]] = {}
        for module, source in self.modules.items():
            out = edges.setdefault(module, set())
            for imp in source.imports:
                if not imp.at_import:
                    continue
                target = source.absolute(imp)
                if not target or target.split(".")[0] not in self.own:
                    continue
                out.add(target)
                for name in imp.names:
                    if f"{target}.{name}" in self.modules:
                        out.add(f"{target}.{name}")
        return edges

    def closure(self, starts: Iterable[str]) -> set[str]:
        """The own modules loaded when each of `starts` is imported."""
        seen: set[str] = set()
        stack = list(starts)
        while stack:
            module = stack.pop()
            if module in seen:
                continue
            seen.add(module)
            parts = module.split(".")
            stack.extend(".".join(parts[:i]) for i in range(1, len(parts)))
            stack.extend(self._edges.get(module, ()))
        return {m for m in seen if m in self.modules}

    # -- checks 1 and 2 ----------------------------------------------------------------------

    def check_imports(self) -> None:
        stdlib = set(sys.stdlib_module_names) | {"__main__"}
        self._apis: dict[str, tuple[bool, str | None]] = {}
        for source in self.sources.values():
            for imp in source.imports:
                if imp.level:
                    continue
                top = imp.module.split(".")[0]
                if top in stdlib or top in self.own:
                    continue
                if source.in_tests and top in self.tests_local:
                    continue
                if source.in_src or source.in_tests:
                    if self._check_internals(source, imp, top):
                        continue
                self._check_declared(source, imp, top)

    def _check_internals(self, source: _Source, imp: _Import, top: str) -> bool:
        """Check 1 for one import; True when it settled the import (check 2 then has
        nothing to add)."""
        where = (source.path, imp.line, "1")
        if top == CORE:
            self._check_core_import(source, imp)
            return False
        match = _MODULE_IMPORT.fullmatch(top)
        if not match:
            return False
        module, dist = match.group(1), f"moxi-{match.group(1)}"
        if self.core:
            self.add(*where, f"imports {imp.module}: the core imports no module's package; "
                             "a module may import the core, never the reverse")
            return True
        if dist in self.extra_only:
            self.add(*where, f"imports {imp.module}, but {dist} is an optional module "
                             f"(installed only through an extra); reach it through "
                             f"moxi.modules.load({module!r}) at the point of use")
            return True
        if dist not in self.deps:
            return False
        known, api = self._api_of(module, dist)
        if not known:
            self.add(*where, f"imports {imp.module} from {dist}, whose announcement this "
                             f"check cannot read to learn its API module; install the "
                             f"repo's dependencies (uv sync) or check {dist} out beside "
                             "this repo")
            return True
        targets = [imp.module]
        if imp.names and api and imp.module == api.rpartition(".")[0]:
            targets = [api if name == api.rpartition(".")[2] else f"{imp.module}.{name}"
                       for name in imp.names]
        for target in targets:
            if target != api:
                self.add(*where, f"imports {target}, which is not {dist}'s API module "
                                 + (f"({api}); import the API module" if api else
                                    "(its announcement declares none); reach it only "
                                    "through what its API offers once it has one"))
                return True
        for name in imp.names:
            if name.startswith("_"):
                self.add(*where, f"imports {name} from {imp.module}: a name starting "
                                 "with _ is not part of an API")
        return True

    def _check_core_import(self, source: _Source, imp: _Import) -> None:
        where = (source.path, imp.line, "1")
        parts = imp.module.split(".")
        if len(parts) == 1:
            for name in imp.names:
                if name.startswith("_"):
                    self.add(*where, f"imports moxi.{name}: a name starting with _ is "
                                     "not part of the core's contract")
                elif name != "*" and name not in CORE_MODULES:
                    self.add(*where, f"imports moxi.{name}, which is not one of the "
                                     f"core's modules ({', '.join(sorted(CORE_MODULES))})")
            return
        if any(p.startswith("_") for p in parts[1:]):
            self.add(*where, f"imports {imp.module}: a name starting with _ is not part of "
                             "the core's contract")
            return
        if parts[1] not in CORE_MODULES or (len(parts) > 2 and parts[1] != "cli"):
            self.add(*where, f"imports {imp.module}, which is not one of the core's modules "
                             f"({', '.join(sorted(CORE_MODULES))})")
            return
        for name in imp.names:
            if name.startswith("_"):
                self.add(*where, f"imports {name} from {imp.module}: a name starting with "
                                 "_ is not part of the core's contract")

    def _check_declared(self, source: _Source, imp: _Import, top: str) -> None:
        allowed = self.declared | (self.groups if source.in_tests else set())
        if any(dist in allowed for dist in self.provider(top)):
            return
        where_from = ("dependencies, an extra or a [dependency-groups] group"
                      if source.in_tests else "dependencies or an extra")
        self.add(source.path, imp.line, "2",
                 f"imports {top}, which no distribution the repo declares provides; "
                 f"declare it in {where_from}, or map the import name to its distribution "
                 "in [tool.moxi.imports]")

    def _api_of(self, module: str, dist: str) -> tuple[bool, str | None]:
        """Whether the announcement of required module `module` could be read, and the
        `API` it declares. Read from this environment's metadata without importing, else
        from a checkout of it beside this repo."""
        if module not in self._apis:
            found = _api_from_environment(dist)
            if found is None:
                found = _api_from_checkout(self.root.resolve().parent / module, dist)
            self._apis[module] = found if found is not None else (False, None)
        return self._apis[module]

    # -- check 3 -----------------------------------------------------------------------------

    def check_extras_at_import(self) -> None:
        if not self.extra_only:
            return
        starts: dict[str, str] = {}
        own = self.own

        def start(dotted: object, label: str) -> None:
            if isinstance(dotted, str) and dotted.split(".")[0] in own:
                starts.setdefault(label, dotted)

        if self.announcement_source is not None:
            start(self.announcement_source.module, "the announcement")
            start(self.ann("API"), "the API module")
            for name in ("CLI", "SETUP"):
                target = self.ann(name)
                if isinstance(target, str):
                    start(target.split(":")[0], f"the {name} target")
        gated = self.ann_map("GATED")
        for script, target in self.scripts.items():
            if script not in gated and target:
                start(target.split(":")[0].strip(), f"the console script {script}")
        hits: dict[tuple[str, int], tuple[str, set[str]]] = {}
        for label, dotted in starts.items():
            for module in self.closure([dotted]):
                source = self.modules[module]
                for imp in source.imports:
                    if not imp.at_import or imp.level:
                        continue
                    top = imp.module.split(".")[0]
                    if top in own:
                        continue
                    extra = [d for d in self.provider(top) if d in self.extra_only]
                    if extra and not any(d in self.deps for d in self.provider(top)):
                        key = (source.path, imp.line)
                        hits.setdefault(key, (f"{top} ({extra[0]})", set()))[1].add(label)
        for (path, line), (what, labels) in hits.items():
            self.add(path, line, "3",
                     f"imports {what} at import time, which comes only with an extra, and "
                     f"is reached from {', '.join(sorted(labels))}; import it inside the "
                     "function that needs it, or gate the console script in GATED")

    # -- check 4 -----------------------------------------------------------------------------

    def check_extras(self) -> None:
        if self.core:
            for norm in self.extras:
                self.add("pyproject.toml", 0, "4",
                         f"declares the extra {self.extra_names[norm]!r}, but the core has "
                         "no announcement to give it an OPTIONAL sentence; the core "
                         "declares no extras")
            return
        if self.announcement is None:
            return
        optional = {_normalize(k): k for k in self.ann_map("OPTIONAL")}
        path = self.announcement_source.path
        for norm in sorted(set(self.extras) - set(optional)):
            self.add("pyproject.toml", 0, "4",
                     f"declares the extra {self.extra_names[norm]!r} with no OPTIONAL "
                     f"sentence in {path}; add one saying what installing it enables")
        for norm in sorted(set(optional) - set(self.extras)):
            self.add(path, 0, "4",
                     f"OPTIONAL names {optional[norm]!r}, which pyproject.toml does not "
                     "declare as an extra")
        for norm, reqs in sorted(self.extras.items()):
            for dist, _ in reqs:
                match = _MODULE_DIST.fullmatch(dist)
                if match and dist in self.extra_only and norm != match.group(1):
                    self.add("pyproject.toml", 0, "4",
                             f"the extra {self.extra_names[norm]!r} installs the optional "
                             f"module {dist}; an extra that installs a module is named by "
                             f"it: {match.group(1)!r}")
        for script, extra in self.ann_map("GATED").items():
            if script not in self.scripts:
                self.add(path, 0, "4", f"GATED names the console script {script!r}, which "
                                       "pyproject.toml does not declare")
            if not isinstance(extra, str) or _normalize(extra) not in self.extras:
                self.add(path, 0, "4", f"GATED gates {script!r} on the extra {extra!r}, "
                                       "which pyproject.toml does not declare")

    # -- checks 5 and 6 ----------------------------------------------------------------------

    def check_environment(self) -> None:
        internal = self.ann_map("INTERNAL_ENV")
        exact = {k for k in internal if not k.endswith("_")}
        prefixes = tuple(k for k in internal if k.endswith("_"))
        for source in self.sources.values():
            if not source.in_src:
                continue
            if self.core and source.module in CORE_ENV_READERS:
                continue
            names_ = _OsNames(source.tree)
            consts = _string_constants(source.tree)
            for line, key in names_.environment_reads():
                literal, prefix = _key_text(key, consts)
                if literal is None:
                    if prefixes and prefix.startswith(prefixes):
                        continue
                    self.add(source.path, line, "5",
                             "reads the environment by a key that is neither a literal nor "
                             "a module-level string constant; a setting belongs in the "
                             "config file, read through moxi.config")
                elif literal.startswith("MOXI_") and literal not in exact and not (
                        prefixes and literal.startswith(prefixes)):
                    self.add(source.path, line, "5",
                             f"reads {literal}, which INTERNAL_ENV does not declare; a "
                             "setting belongs in the config file (an env override is "
                             "declared in the schema), a root locator is read by "
                             "moxi.paths, and other plumbing is named in INTERNAL_ENV")

    def check_which(self) -> None:
        if not self.scripts:
            return
        for source in self.sources.values():
            if not source.in_src:
                continue
            names_ = _OsNames(source.tree)
            consts = _string_constants(source.tree)
            for line, arg in names_.which_calls():
                literal, _ = _key_text(arg, consts)
                if literal is None or literal not in self.scripts:
                    continue
                if (self.core and literal == CORE_SCRIPT[0] and source.module
                        and (source.module == "moxi.cli"
                             or source.module.startswith("moxi.cli."))):
                    continue
                self.add(source.path, line, "6",
                         f"looks up its own console script {literal!r} on PATH, where "
                         "another program's script of that name may stand; use "
                         "moxi.paths.script(name)")

    # -- check 7 -----------------------------------------------------------------------------

    def check_core_names(self) -> None:
        here = "pyproject.toml"
        for name in self.entry_points:
            self.add(here, 0, "7", f"declares the {GROUP} entry point {name!r}: the core "
                                   "is not a module and declares none")
        if CORE not in self.src_tops:
            self.add(here, 0, "7", "has no src/moxi: the core's import package is moxi")
        for top in sorted(self.src_tops - {CORE}):
            self.add(f"src/{top}", 0, "7", "is a second import package; the core's one "
                                           "package is moxi")
        script, target = CORE_SCRIPT
        if _target(self.scripts.get(script)) != target:
            have = self.scripts.get(script)
            self.add(here, 0, "7", f"declares the console script {script} = {have!r}; the "
                                   f"core declares {script} = {target!r}" if have else
                                   f"does not declare the console script {script} = "
                                   f"{target!r}")
        for other in sorted(set(self.scripts) - {script}):
            self.add(here, 0, "7", f"declares the console script {other!r}; the core "
                                   f"declares {script} alone")

    def check_names(self) -> None:
        here = "pyproject.toml"
        name = self.name
        if name is None:
            self.add(here, 0, "7", f"names the distribution {self.dist!r}; a module's "
                                   "distribution is moxi-<name>, the name lower-case ASCII "
                                   "letters and digits")
        elif name in names.VERBS:
            self.add(here, 0, "7", f"names the module {name!r}, one of the umbrella's own "
                                   "verbs; `moxi " + name + "` could never reach it")
        if name and self.own_import not in self.src_tops:
            self.add(here, 0, "7", f"has no src/{self.own_import}: module {name!r} is "
                                   f"imported as {self.own_import}")
        for top in sorted(self.src_tops - {self.own_import}):
            self.add(f"src/{top}", 0, "7",
                     f"is an import package other than {self.own_import}" if name else
                     "is an import package not spelled moxi_<name>")
        # The entry point.
        if not self.entry_points:
            self.add(here, 0, "7", f"declares no [project.entry-points.\"{GROUP}\"] entry "
                                   "point naming the module's announcement")
        elif len(self.entry_points) > 1:
            self.add(here, 0, "7", f"declares {len(self.entry_points)} {GROUP} entry "
                                   "points; a module declares one, named by the module")
        else:
            (ep_name, target), = self.entry_points.items()
            if name and ep_name != name:
                self.add(here, 0, "7", f"names its {GROUP} entry point {ep_name!r}; it "
                                       f"is named by the module: {name!r}")
            if not isinstance(target, str) or not _DOTTED.fullmatch(target):
                self.add(here, 0, "7", f"points its {GROUP} entry point at {target!r}; it "
                                       "names the announcement module alone, with no "
                                       "attribute")
            elif target not in self.modules:
                self.add(here, 0, "7", f"points its {GROUP} entry point at {target}, which "
                                       "is no module under src/")
            elif self.own_import and target.split(".")[0] != self.own_import:
                self.add(here, 0, "7", f"points its {GROUP} entry point at {target}, "
                                       f"outside the module's package {self.own_import}")
        # Console scripts.
        for script in sorted(self.scripts):
            if script == CORE_SCRIPT[0]:
                # [R11]: a module's moxi is the umbrella's, or a tool environment it
                # heads would run something else as moxi.
                if _target(self.scripts[script]) != CORE_SCRIPT[1]:
                    self.add(here, 0, "7", f"declares the console script {script} = "
                                           f"{self.scripts[script]!r}; every Moxi "
                                           f"distribution declares {script} = "
                                           f"{CORE_SCRIPT[1]!r}")
                continue
            ok = (name is not None and script.startswith(f"moxi-{name}-")
                  and _SCRIPT_PART.fullmatch(script[len(f"moxi-{name}-"):]))
            if not ok:
                self.add(here, 0, "7", f"declares the console script {script!r}; a "
                                       f"module's scripts other than moxi are named "
                                       f"moxi-{name or '<module>'}-<part>")
        # The announcement.
        source = self.announcement_source
        if source is None:
            return
        for problem in self.announcement_problems:
            self.add(source.path, 0, "7", f"the announcement {problem}")
        self._check_announcement_values(source)
        self._check_cli_target(source)
        self._check_settings(source)

    def _check_announcement_values(self, source: _Source) -> None:
        values = self.announcement or {}

        def bad(field: str, what: str) -> None:
            self.add(source.path, 0, "7",
                     f"the announcement has {field} = {values[field]!r}; it must be {what}")

        if "INTERFACES" in values:
            v = values["INTERFACES"]
            if not isinstance(v, frozenset) or not all(
                    isinstance(i, int) and not isinstance(i, bool) for i in v):
                bad("INTERFACES", "a frozenset of integers")
        if "SUMMARY" in values:
            v = values["SUMMARY"]
            if not isinstance(v, str) or not v.strip() or "\n" in v:
                bad("SUMMARY", "one non-empty line")
        for field, pattern, what in (("API", _DOTTED, "a dotted module name"),
                                     ("SEGMENT", _DOTTED, "a dotted module name"),
                                     ("CLI", _TARGET, '"package.module:function"'),
                                     ("SETUP", _TARGET, '"package.module:function"'),
                                     ("SETTINGS", None, "a resource name")):
            if field in values and values[field] is not None:
                v = values[field]
                if not isinstance(v, str) or not v or (pattern and not pattern.fullmatch(v)):
                    bad(field, f"{what} or None")
        for field in ("OPTIONAL", "GATED", "INTERNAL_ENV"):
            if field in values:
                v = values[field]
                if not isinstance(v, Mapping) or not all(
                        isinstance(k, str) and k and isinstance(x, str)
                        for k, x in v.items()):
                    bad(field, "a dict of names to strings")

    def _check_cli_target(self, source: _Source) -> None:
        target = self.ann("CLI")
        if not isinstance(target, str) or not _TARGET.fullmatch(target):
            return
        module, _, function = target.partition(":")
        where = source.path
        if module.split(".")[0] not in self.own:
            self.add(where, 0, "7", f"CLI names {target}, outside the module's package")
            return
        found = self.modules.get(module)
        defn = None
        if found is not None:
            for stmt in found.tree.body:
                if (isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and stmt.name == function):
                    defn = stmt
        if defn is None:
            self.add(where, 0, "7", f"CLI names {target}, and no top-level function "
                                    f"{function} is defined in {module}")
            return
        args = defn.args
        named = {a.arg for a in args.posonlyargs + args.args + args.kwonlyargs}
        by_keyword = {a.arg for a in args.args + args.kwonlyargs}
        missing = [n for n, ok in (("argv", "argv" in named),
                                   ("prog", "prog" in by_keyword)) if not ok]
        if missing:
            self.add(found.path, defn.lineno, "7",
                     f"the CLI target {function} lacks {' and '.join(missing)}; `moxi "
                     f"<module>` calls it as {function}(argv, prog=\"moxi <module>\")")

    def _check_settings(self, source: _Source) -> None:
        resource = self.ann("SETTINGS")
        if not isinstance(resource, str) or not resource:
            return
        package_dir = PurePosixPath(source.path).parent
        path = str(package_dir / resource)
        raw = self.tree.read(path)
        if raw is None:
            self.add(source.path, 0, "7", f"SETTINGS names {resource!r}, and {path} does "
                                          "not exist")
            return
        from .config import ConfigError, Schema

        try:
            schema = Schema.parse(raw.decode("utf-8"), source=path)
            if self.name:
                schema.check_env(self.name)
        except UnicodeDecodeError as exc:
            self.add(path, 0, "7", f"the SETTINGS schema cannot be read ({exc})")
        except ConfigError as exc:
            self.add(path, 0, "7", f"the SETTINGS schema does not hold: {exc}")

    # -- the status line segment -------------------------------------------------------------

    def check_segment(self) -> None:
        segment = self.ann("SEGMENT")
        if not isinstance(segment, str) or segment.split(".")[0] not in self.own:
            return
        if segment not in self.modules:
            self.add(self.announcement_source.path, 0, "segment",
                     f"SEGMENT names {segment}, which is no module under src/")
            return
        forbidden = SEGMENT_IMPORTS | {"moxi_turns"}
        for module in sorted(self.closure([segment])):
            source = self.modules[module]
            for imp in source.imports:
                if imp.level:
                    continue
                top = imp.module.split(".")[0]
                if top in forbidden:
                    self.add(source.path, imp.line, "segment",
                             f"imports {imp.module} inside the closure of the segment "
                             f"{segment}; a status line segment shows status and triggers "
                             "nothing")
            for line, call in _OsNames(source.tree).process_calls():
                self.add(source.path, line, "segment",
                         f"calls {call} inside the closure of the segment {segment}; a "
                         "status line segment shows status and triggers nothing")


def _target(text: str | None) -> str | None:
    return None if text is None else re.sub(r"\s+", "", text)


def _key_text(node: ast.AST, consts: Mapping[str, str]) -> tuple[str | None, str]:
    """A key expression's text when it is a literal or a module-level string constant,
    and the literal text it starts with in any case."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value, node.value
    if isinstance(node, ast.Name) and node.id in consts:
        return consts[node.id], consts[node.id]
    if isinstance(node, ast.JoinedStr):
        prefix = ""
        for part in node.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                prefix += part.value
            else:
                return None, prefix
        return prefix, prefix
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, left_prefix = _key_text(node.left, consts)
        if left is None:
            return None, left_prefix
        right, right_prefix = _key_text(node.right, consts)
        if right is None:
            return None, left + right_prefix
        return left + right, left + right
    return None, ""


class _OsNames:
    """How one file spells `os`, `os.environ`, `os.getenv` and `shutil.which`."""

    _PROCESS = ("system", "fork", "kill")

    def __init__(self, tree: ast.Module):
        self.tree = tree
        self.os: set[str] = set()
        self.environ: set[str] = set()
        self.getenv: set[str] = set()
        self.shutil: set[str] = set()
        self.which: set[str] = set()
        self.process: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    bound = alias.asname if alias.asname and "." not in alias.name else top
                    if alias.name == "os" or (top == "os" and not alias.asname):
                        self.os.add(bound)
                    elif alias.name == "shutil":
                        self.shutil.add(alias.asname or "shutil")
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                for alias in node.names:
                    bound = alias.asname or alias.name
                    if node.module == "os":
                        if alias.name in ("environ", "environb"):
                            self.environ.add(bound)
                        elif alias.name in ("getenv", "getenvb"):
                            self.getenv.add(bound)
                        elif self._is_process(alias.name):
                            self.process[bound] = f"os.{alias.name}"
                    elif node.module == "shutil" and alias.name == "which":
                        self.which.add(bound)

    @classmethod
    def _is_process(cls, attr: str) -> bool:
        return attr in cls._PROCESS or attr.startswith(("exec", "spawn"))

    def _is_os(self, node: ast.AST) -> bool:
        return isinstance(node, ast.Name) and node.id in self.os

    def _is_environ(self, node: ast.AST) -> bool:
        return ((isinstance(node, ast.Attribute) and node.attr in ("environ", "environb")
                 and self._is_os(node.value))
                or (isinstance(node, ast.Name) and node.id in self.environ))

    def environment_reads(self) -> Iterable[tuple[int, ast.AST]]:
        """(line, key expression) for each keyed read of the environment."""
        if not (self.os or self.environ or self.getenv):
            return
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call) and node.args:
                func = node.func
                if ((isinstance(func, ast.Attribute)
                     and func.attr in ("get", "pop", "setdefault")
                     and self._is_environ(func.value))
                        or (isinstance(func, ast.Attribute)
                            and func.attr in ("getenv", "getenvb") and self._is_os(func.value))
                        or (isinstance(func, ast.Name) and func.id in self.getenv)):
                    yield node.lineno, node.args[0]
            elif (isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load)
                  and self._is_environ(node.value)):
                yield node.lineno, node.slice
            elif isinstance(node, ast.Compare):
                for op, right in zip(node.ops, node.comparators, strict=True):
                    if isinstance(op, (ast.In, ast.NotIn)) and self._is_environ(right):
                        yield node.lineno, node.left

    def which_calls(self) -> Iterable[tuple[int, ast.AST]]:
        if not (self.shutil or self.which):
            return
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call) and node.args:
                func = node.func
                if ((isinstance(func, ast.Attribute) and func.attr == "which"
                     and isinstance(func.value, ast.Name) and func.value.id in self.shutil)
                        or (isinstance(func, ast.Name) and func.id in self.which)):
                    yield node.lineno, node.args[0]

    def process_calls(self) -> Iterable[tuple[int, str]]:
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (isinstance(func, ast.Attribute) and self._is_os(func.value)
                    and self._is_process(func.attr)):
                yield node.lineno, f"os.{func.attr}"
            elif isinstance(func, ast.Name) and func.id in self.process:
                yield node.lineno, self.process[func.id]


# --- another module's announcement ---------------------------------------------------------

def _api_in(path: Path) -> tuple[bool, str | None] | None:
    try:
        tree = ast.parse(path.read_bytes(), filename=str(path))
    except (OSError, SyntaxError, ValueError):
        return None
    bound = _assignments(tree)
    if "API" not in bound:
        return None
    try:
        api = _literal(bound["API"])
    except (ValueError, TypeError, SyntaxError):
        return None
    if api is None or (isinstance(api, str) and _DOTTED.fullmatch(api)):
        return True, api
    return None


def _module_file(bases: Iterable[str | Path], dotted: str) -> Path | None:
    rest = dotted.split(".")[1:]
    for base in bases:
        if rest:
            candidates = [Path(base, *rest[:-1], rest[-1] + ".py"),
                          Path(base, *rest, "__init__.py")]
        else:
            candidates = [Path(base, "__init__.py")]
        for candidate in candidates:
            if candidate.is_file():
                return candidate
    return None


def _api_from_environment(dist: str) -> tuple[bool, str | None] | None:
    """Read a required module's announcement through this environment's metadata, finding
    its file without importing anything."""
    from importlib import metadata, util

    try:
        found = metadata.distribution(dist)
    except metadata.PackageNotFoundError:
        return None
    except Exception:  # noqa: BLE001 - a damaged install reads as not found
        return None
    eps = [ep for ep in found.entry_points if ep.group == GROUP]
    if len(eps) != 1 or ":" in eps[0].value:
        return None
    target = eps[0].value
    try:
        spec = util.find_spec(target.split(".")[0])
    except (ImportError, ValueError):
        return None
    if spec is None or not spec.submodule_search_locations:
        return None
    path = _module_file(spec.submodule_search_locations, target)
    return _api_in(path) if path else None


def _api_from_checkout(repo: Path, dist: str) -> tuple[bool, str | None] | None:
    """Read a required module's announcement from its repo checked out at `repo`."""
    import tomllib

    try:
        data = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    project = data.get("project") or {}
    if _normalize(str(project.get("name", ""))) != dist:
        return None
    eps = (project.get("entry-points") or {}).get(GROUP) or {}
    if len(eps) != 1:
        return None
    target = next(iter(eps.values()))
    if not isinstance(target, str) or not _DOTTED.fullmatch(target):
        return None
    path = _module_file([repo / "src" / target.split(".")[0]], target)
    return _api_in(path) if path else None


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["CORE", "GROUP", "CORE_MODULES", "ANNOUNCEMENT", "CORE_SCRIPT", "CheckError",
           "Finding", "check", "main"]
