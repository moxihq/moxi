"""The module contract check, rule by rule, over small repos written for each test."""

import subprocess
import textwrap
from pathlib import Path

import pytest

from moxi import contract
from moxi.contract import CheckError, check

ANNOUNCEMENT = {
    "INTERFACES": "frozenset({1})",
    "API": "None",
    "CLI": "None",
    "SUMMARY": "'A module made for a test.'",
    "OPTIONAL": "{}",
    "GATED": "{}",
    "INTERNAL_ENV": "{}",
    "SETTINGS": "None",
    "SETUP": "None",
    "SEGMENT": "None",
}


def announcement(drop=(), **values) -> str:
    fields = {**ANNOUNCEMENT, **values}
    return "".join(f"{k} = {v}\n" for k, v in fields.items() if k not in drop)


def pyproject(name="moxi-alpha", *, deps=("moxi>=0.1",), extras=None, groups=None,
              scripts=None, entry_points=None, tool="") -> str:
    lines = ["[project]", f'name = "{name}"', 'version = "0.1.0"',
             "dependencies = [" + ", ".join(f'"{d}"' for d in deps) + "]"]
    if extras:
        lines.append("[project.optional-dependencies]")
        lines += [f"{k} = [" + ", ".join(f'"{r}"' for r in v) + "]" for k, v in extras.items()]
    if scripts is None:
        scripts = {"moxi": "moxi.cli:main"}
    if scripts:
        lines.append("[project.scripts]")
        lines += [f'"{k}" = "{v}"' for k, v in scripts.items()]
    if entry_points is None:
        module = name.removeprefix("moxi-")
        entry_points = {module: f"moxi_{module}.module"}
    if entry_points:
        lines.append('[project.entry-points."moxi.modules"]')
        lines += [f'"{k}" = "{v}"' for k, v in entry_points.items()]
    groups = {"dev": ["pytest"]} if groups is None else groups
    if groups:
        lines.append("[dependency-groups]")
        lines += [f"{k} = [" + ", ".join(f'"{r}"' for r in v) + "]" for k, v in groups.items()]
    return "\n".join(lines) + "\n" + textwrap.dedent(tool)


class Repo:
    """A module repo on disk, alpha unless named otherwise."""

    def __init__(self, root: Path, name: str = "alpha"):
        self.root = root
        self.name = name
        self.pkg = f"moxi_{name}"

    def write(self, rel: str, text: str = "") -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text))
        return path

    def src(self, rel: str, text: str = "") -> Path:
        return self.write(f"src/{self.pkg}/{rel}", text)

    def findings(self, which: str | None = None, *, staged: bool = False) -> list[str]:
        found = check(self.root, staged=staged)
        return [f"{f.path}:{f.line}: {f.message}" for f in found
                if which is None or f.check == which]

    def git(self, *args: str) -> None:
        subprocess.run(["git", "-C", str(self.root), *args], check=True,
                       capture_output=True)


@pytest.fixture
def repo(tmp_path):
    """A module repo that keeps the contract: alpha, requiring the core and beta."""
    beta = Repo(tmp_path / "beta", "beta")
    beta.write("pyproject.toml", pyproject("moxi-beta"))
    beta.src("__init__.py")
    beta.src("api.py", "def send(): pass\n")
    beta.src("module.py", announcement(API="'moxi_beta.api'"))
    r = Repo(tmp_path / "alpha")
    r.write("pyproject.toml", pyproject(deps=("moxi>=0.1", "moxi-beta>=0.1")))
    r.src("__init__.py")
    r.src("module.py", announcement())
    r.src("core.py", """\
        import os
        from moxi import names
        from moxi.paths import Layout
        import moxi.config
        from moxi_beta.api import send
        """)
    r.write("tests/test_core.py", "import pytest\nimport moxi_alpha.core\n")
    return r


def test_a_repo_that_keeps_the_contract_has_no_findings(repo):
    assert repo.findings() == []


def test_no_pyproject_is_an_error(tmp_path):
    with pytest.raises(CheckError, match="no pyproject.toml"):
        check(tmp_path)


def test_a_finding_reads_as_path_line_check_message():
    f = contract.Finding("src/a.py", 3, "2", "imports x")
    assert str(f) == "src/a.py:3: check 2: imports x"
    assert str(contract.Finding("pyproject.toml", 0, "7", "m")) == "pyproject.toml: check 7: m"


def test_a_file_that_does_not_parse_is_a_finding(repo):
    repo.src("broken.py", "def (:\n")
    assert any("does not parse" in m for m in repo.findings("read"))


# --- check 1 ---------------------------------------------------------------------------------

def test_another_modules_internals_are_refused(repo):
    repo.src("core.py", "import moxi_beta.engine\nfrom moxi_beta import api\n")
    assert repo.findings("1") == [
        "src/moxi_alpha/core.py:1: imports moxi_beta.engine, which is not moxi-beta's API "
        "module (moxi_beta.api); import the API module"]


def test_a_sibling_name_beside_the_api_is_refused(repo):
    repo.src("core.py", "from moxi_beta import api, engine\n")
    assert len(repo.findings("1")) == 1


def test_a_private_name_from_an_api_is_refused(repo):
    repo.src("core.py", "from moxi_beta.api import _secret\n")
    assert "a name starting with _" in repo.findings("1")[0]


def test_internals_in_tests_are_refused_too(repo):
    repo.write("tests/test_core.py", "import moxi_beta.module\n")
    assert repo.findings("1")[0].startswith("tests/test_core.py:1:")


def test_a_module_without_an_api_cannot_be_imported(repo):
    repo.write("../beta/src/moxi_beta/module.py", announcement())
    assert "its announcement declares none" in repo.findings("1")[0]


def test_an_optional_module_is_reached_only_through_load(repo):
    repo.write("pyproject.toml", pyproject(extras={"beta": ["moxi-beta>=0.1"]}))
    repo.src("module.py", announcement(OPTIONAL="{'beta': 'Beta things.'}"))
    found = repo.findings("1")
    assert len(found) == 1 and "moxi.modules.load('beta')" in found[0]


def test_a_required_module_an_extra_also_names_stays_importable(repo):
    repo.write("pyproject.toml", pyproject(deps=("moxi>=0.1", "moxi-beta>=0.1"),
                                           extras={"codex": ["moxi-beta[codex]>=0.1"]}))
    repo.src("module.py", announcement(OPTIONAL="{'codex': 'The Codex host.'}"))
    assert repo.findings() == []


def test_an_unreadable_announcement_of_a_dependency_is_a_finding(repo, tmp_path):
    (tmp_path / "beta" / "pyproject.toml").unlink()
    assert "cannot read to learn its API module" in repo.findings("1")[0]


@pytest.mark.parametrize("line", [
    "from moxi.paths import _expanduser",
    "import moxi._private",
    "from moxi import _thing",
    "import moxi.secret",
    "from moxi import secret",
    "import moxi.names.deeper",
])
def test_core_imports_outside_the_table_are_refused(repo, line):
    repo.src("core.py", line + "\n")
    assert len(repo.findings("1")) == 1


@pytest.mark.parametrize("line", ["import moxi", "from moxi import names, paths, config",
                                  "import moxi.modules", "from moxi.contract import check",
                                  "import moxi.cli", "from moxi.cli import main"])
def test_core_imports_in_the_table_pass(repo, line):
    repo.src("core.py", line + "\n")
    assert repo.findings("1") == []


def core_repo(root: Path) -> Repo:
    r = Repo(root, "core")
    r.pkg = "moxi"
    r.write("pyproject.toml", pyproject("moxi", deps=(), entry_points={}))
    r.src("__init__.py")
    r.src("cli.py", "def main(argv=None):\n    return 0\n")
    return r


def test_the_core_imports_no_module(tmp_path):
    r = core_repo(tmp_path / "moxi")
    r.write("pyproject.toml", pyproject("moxi", deps=("moxi-beta",), entry_points={}))
    r.src("names.py", "import moxi_beta.api\n")
    assert "the core imports no module's package" in r.findings("1")[0]


def test_the_core_reaches_its_own_internals(tmp_path):
    r = core_repo(tmp_path / "moxi")
    r.src("config.py", "from .paths import _expanduser\nfrom moxi.paths import _root\n")
    r.src("paths.py", "def _expanduser(): pass\ndef _root(): pass\n")
    assert r.findings() == []


# --- check 2 ---------------------------------------------------------------------------------

def test_an_undeclared_import_is_refused(repo):
    repo.src("core.py", "def f():\n    import requests\n")
    assert repo.findings("2") == [
        "src/moxi_alpha/core.py:2: imports requests, which no distribution the repo declares "
        "provides; declare it in dependencies or an extra, or map the import name to its "
        "distribution in [tool.moxi.imports]"]


def test_a_declared_distribution_counts_whether_or_not_it_is_installed(repo):
    repo.write("pyproject.toml", pyproject(
        deps=("moxi>=0.1", "moxi-beta", "no-such-dist-anywhere>=1"),
        extras={"fast": ["another_missing.dist"]}))
    repo.src("module.py", announcement(OPTIONAL="{'fast': 'Faster.'}"))
    repo.src("core.py", "import no_such_dist_anywhere\ndef f():\n"
                        "    import another_missing_dist\n")
    assert repo.findings() == []


def test_the_imports_table_maps_an_import_to_its_distribution(repo):
    repo.write("pyproject.toml", pyproject(deps=("moxi", "PyYAML>=6"),
                                           tool='[tool.moxi.imports]\nyaml = "pyyaml"\n'))
    repo.src("core.py", "import yaml\n")
    assert repo.findings() == []
    repo.write("pyproject.toml", pyproject(deps=("moxi", "PyYAML>=6")))
    assert len(repo.findings("2")) == 1


def test_a_moxi_import_maps_by_its_spelling(repo):
    repo.write("pyproject.toml", pyproject(deps=("moxi-beta",)))
    repo.src("core.py", "from moxi import names\n")
    assert "imports moxi," in repo.findings("2")[0]


def test_tests_may_import_a_dependency_group(repo):
    repo.write("tests/test_core.py", "import pytest\nimport hypothesis\n")
    repo.write("pyproject.toml", pyproject(deps=("moxi", "moxi-beta"),
                                           groups={"dev": ["pytest", "hypothesis>=6"]}))
    assert repo.findings() == []


def test_src_may_not_import_a_dependency_group(repo):
    repo.src("core.py", "import pytest\n")
    assert len(repo.findings("2")) == 1


def test_tests_import_their_own_helpers(repo):
    repo.write("tests/helpers.py", "")
    repo.write("tests/fakes/__init__.py", "")
    repo.write("tests/test_core.py", "import helpers\nfrom fakes import x\nimport conftest\n")
    assert repo.findings() == []


def test_relative_and_future_imports_pass(repo):
    repo.src("core.py", "from __future__ import annotations\nfrom . import module\n")
    assert repo.findings() == []


# --- check 3 ---------------------------------------------------------------------------------

@pytest.fixture
def extra_repo(repo):
    repo.write("pyproject.toml", pyproject(
        deps=("moxi", "moxi-beta"), extras={"codex": ["websockets>=17"]},
        scripts={"moxi": "moxi.cli:main", "moxi-alpha-codex": "moxi_alpha.codex:main",
                 "moxi-alpha-daemon": "moxi_alpha.daemon:main"}))
    repo.src("module.py", announcement(OPTIONAL="{'codex': 'The Codex host.'}",
                                       GATED="{'moxi-alpha-codex': 'codex'}",
                                       CLI="'moxi_alpha.cli:main'"))
    repo.src("cli.py", "def main(argv=None, *, prog='moxi alpha'):\n    return 0\n")
    repo.src("daemon.py", "def main(): pass\n")
    repo.src("adapters/__init__.py")
    repo.src("adapters/ws.py", "import websockets\n")
    repo.src("codex.py", "from .adapters import ws\ndef main(): pass\n")
    return repo


def test_an_extra_behind_a_gated_script_passes(extra_repo):
    assert extra_repo.findings() == []


def test_an_extra_reached_from_an_ungated_script_is_refused(extra_repo):
    extra_repo.src("daemon.py", "from moxi_alpha.adapters.ws import x\ndef main(): pass\n")
    found = extra_repo.findings("3")
    assert len(found) == 1
    assert found[0].startswith("src/moxi_alpha/adapters/ws.py:1:")
    assert "the console script moxi-alpha-daemon" in found[0]


def test_an_extra_reached_from_the_cli_target_through_a_chain_is_refused(extra_repo):
    extra_repo.src("helper.py", "from .adapters.ws import thing\n")
    extra_repo.src("cli.py", "from . import helper\n"
                             "def main(argv=None, *, prog='moxi alpha'):\n    return 0\n")
    assert "the CLI target" in extra_repo.findings("3")[0]


def test_an_extra_in_a_package_init_reached_by_a_submodule_is_refused(extra_repo):
    extra_repo.src("adapters/__init__.py", "import websockets\n")
    extra_repo.src("api.py", "from moxi_alpha.adapters import other\n")
    extra_repo.src("adapters/other.py", "")
    extra_repo.src("module.py", announcement(OPTIONAL="{'codex': 'The Codex host.'}",
                                             GATED="{'moxi-alpha-codex': 'codex'}",
                                             CLI="'moxi_alpha.cli:main'",
                                             API="'moxi_alpha.api'"))
    assert "the API module" in extra_repo.findings("3")[0]


def test_an_extra_imported_inside_a_function_or_for_typing_passes(extra_repo):
    extra_repo.src("daemon.py", """\
        from typing import TYPE_CHECKING
        if TYPE_CHECKING:
            import websockets
        def main():
            import websockets
        """)
    assert extra_repo.findings() == []


# --- check 4 ---------------------------------------------------------------------------------

def test_an_extra_without_a_sentence_and_a_sentence_without_an_extra(repo):
    repo.write("pyproject.toml", pyproject(deps=("moxi", "moxi-beta"),
                                           extras={"fast": ["orjson"]}))
    repo.src("module.py", announcement(OPTIONAL="{'slow': 'Slower.'}"))
    found = repo.findings("4")
    assert len(found) == 2
    assert "declares the extra 'fast' with no OPTIONAL sentence" in found[0] + found[1]
    assert "OPTIONAL names 'slow'" in found[0] + found[1]


def test_an_extra_installing_a_module_is_named_by_it(repo):
    repo.write("pyproject.toml", pyproject(deps=("moxi",),
                                           extras={"with-beta": ["moxi-beta>=0.1"]}))
    repo.src("module.py", announcement(OPTIONAL="{'with-beta': 'Beta.'}"))
    repo.src("core.py", "")
    assert "is named by it: 'beta'" in repo.findings("4")[0]


def test_gated_names_must_exist(repo):
    repo.src("module.py", announcement(GATED="{'moxi-alpha-x': 'nothing'}"))
    found = repo.findings("4")
    assert len(found) == 2


def test_the_core_declares_no_extras(tmp_path):
    r = core_repo(tmp_path / "moxi")
    r.write("pyproject.toml", pyproject("moxi", deps=(), entry_points={},
                                        extras={"fast": ["orjson"]}))
    assert "the core declares no extras" in r.findings("4")[0]


# --- check 5 ---------------------------------------------------------------------------------

def test_a_moxi_key_outside_internal_env_is_refused(repo):
    repo.src("core.py", """\
        import os
        A = os.environ.get("MOXI_ALPHA_MODE")
        B = os.environ["MOXI_TURNS_COMMANDS"]
        C = os.getenv("HOME")
        D = "MOXI_ALPHA_NAME" in os.environ
        """)
    found = repo.findings("5")
    assert [f.split(":")[1] for f in found] == ["2", "3", "5"]
    assert "reads MOXI_ALPHA_MODE, which INTERNAL_ENV does not declare" in found[0]


def test_internal_env_and_its_prefixes_cover_reads(repo):
    repo.src("module.py", announcement(INTERNAL_ENV="{'MOXI_ALPHA_NAME': 'stamped', "
                                                    "'MOXI_ALPHA_SECRET_': 'keys'}"))
    repo.src("core.py", """\
        import os
        from os import environ, getenv
        PREFIX = "MOXI_ALPHA_SECRET_"
        A = os.environ.get("MOXI_ALPHA_NAME")
        B = environ["MOXI_ALPHA_SECRET_GATEWAY"]
        def secret(role):
            return getenv(f"MOXI_ALPHA_SECRET_{role}") or os.environ.get(PREFIX + role)
        """)
    assert repo.findings() == []


def test_a_computed_key_is_refused_and_a_constant_is_read(repo):
    repo.src("core.py", """\
        import os as o
        KEY = "MOXI_ALPHA_MODE"
        OTHER = "EDITOR"
        def f(name):
            o.environ.get(name)
            o.environ.get(KEY)
            o.environ.get(OTHER)
            o.environ.get(f"MOXI_ALPHA_{name}")
        """)
    found = repo.findings("5")
    assert [f.split(":")[1] for f in found] == ["5", "6", "8"]
    assert "neither a literal nor a module-level string constant" in found[0]
    assert "reads MOXI_ALPHA_MODE" in found[1]


def test_writes_and_tests_are_not_reads_checked(repo):
    repo.src("core.py", "import os\nos.environ['MOXI_ALPHA_X'] = '1'\n"
                        "env = dict(os.environ)\n")
    repo.write("tests/test_core.py", "import os\nos.environ.get('MOXI_ALPHA_X')\n")
    assert repo.findings("5") == []


def test_the_core_reads_computed_keys_only_in_paths_and_config(tmp_path):
    r = core_repo(tmp_path / "moxi")
    r.src("paths.py", "import os\ndef root(p, s):\n    return os.environ.get(f'{p}{s}')\n")
    r.src("config.py", "import os\ndef env(k):\n    return os.environ.get(k)\n")
    assert r.findings() == []
    r.src("names.py", "import os\ndef env(k):\n    return os.environ.get(k)\n")
    assert len(r.findings("5")) == 1
    r.src("names.py", "import os\nos.environ.get('MOXI_X')\n")
    assert "reads MOXI_X" in r.findings("5")[0]


# --- check 6 ---------------------------------------------------------------------------------

def test_its_own_script_looked_up_on_path_is_refused(extra_repo):
    extra_repo.src("daemon.py", """\
        import shutil
        from shutil import which as w
        DAEMON = "moxi-alpha-daemon"
        def main():
            shutil.which("git")
            shutil.which(DAEMON)
            w("moxi-alpha-codex")
        """)
    found = extra_repo.findings("6")
    assert [f.split(":")[1] for f in found] == ["6", "7"]
    assert "moxi.paths.script" in found[0]


def test_the_core_looks_moxi_up_only_in_its_cli(tmp_path):
    r = core_repo(tmp_path / "moxi")
    r.src("cli.py", "import shutil\ndef main(argv=None):\n"
                    "    return shutil.which('moxi'), shutil.which(argv[0])\n")
    assert r.findings() == []
    r.src("paths.py", "import shutil\nWHERE = shutil.which('moxi')\n")
    assert r.findings("6")[0].startswith("src/moxi/paths.py:2:")


# --- check 7 ---------------------------------------------------------------------------------

def test_a_distribution_not_spelled_moxi_name(repo):
    repo.write("pyproject.toml", pyproject("alpha-tools", deps=("moxi", "moxi-beta"),
                                           entry_points={"alpha": "moxi_alpha.module"}))
    assert any("names the distribution 'alpha-tools'" in m for m in repo.findings("7"))


def test_a_module_named_by_a_core_verb(tmp_path):
    r = Repo(tmp_path / "check", "check")
    r.write("pyproject.toml", pyproject("moxi-check"))
    r.src("__init__.py")
    r.src("module.py", announcement())
    assert "one of the umbrella's own verbs" in r.findings("7")[0]


@pytest.mark.parametrize("eps, needle", [
    ({}, "declares no"),
    ({"other": "moxi_alpha.module"}, "it is named by the module: 'alpha'"),
    ({"alpha": "moxi_alpha.module:thing"}, "with no attribute"),
    ({"alpha": "moxi_alpha.nowhere"}, "which is no module under src/"),
    ({"alpha": "moxi_alpha.module", "beta": "moxi_alpha.module"}, "declares 2"),
])
def test_the_entry_point(repo, eps, needle):
    repo.write("pyproject.toml", pyproject(deps=("moxi", "moxi-beta"), entry_points=eps))
    assert any(needle in m for m in repo.findings("7")), repo.findings("7")


def test_console_scripts_are_named_moxi_module_part(repo):
    repo.write("pyproject.toml", pyproject(deps=("moxi", "moxi-beta"), scripts={
        "moxi": "moxi.cli:main", "moxi-alpha-daemon": "moxi_alpha.d:main",
        "alpha-daemon": "moxi_alpha.d:main", "moxi-alpha-": "moxi_alpha.d:main",
        "moxi-beta-x": "moxi_alpha.d:main"}))
    assert len(repo.findings("7")) == 3


def test_a_module_s_moxi_script_is_the_umbrella(repo):
    """[R11]: a module's moxi console script points at moxi.cli:main and nothing else."""
    repo.write("pyproject.toml", pyproject(deps=("moxi", "moxi-beta"), scripts={
        "moxi": "moxi_alpha.module:main"}))
    assert repo.findings("7") == [
        "pyproject.toml:0: declares the console script moxi = 'moxi_alpha.module:main'; "
        "every Moxi distribution declares moxi = 'moxi.cli:main'"]
    repo.write("pyproject.toml", pyproject(deps=("moxi", "moxi-beta"), scripts={
        "moxi": "moxi.cli : main"}))
    assert repo.findings("7") == []


def test_a_second_import_package(repo):
    repo.write("src/helpers/__init__.py", "")
    assert repo.findings("7") == ["src/helpers:0: is an import package other than moxi_alpha"]


def test_the_announcement_defines_every_name_as_a_literal(repo):
    repo.src("module.py", announcement(drop=("SEGMENT",), API="make()",
                                       SUMMARY="'two\\nlines'"))
    found = repo.findings("7")
    assert any("does not define SEGMENT" in m for m in found)
    assert any("defines API by an expression" in m for m in found)
    assert any("one non-empty line" in m for m in found)


def test_the_cli_target_takes_argv_and_prog(repo):
    repo.src("module.py", announcement(CLI="'moxi_alpha.cli:main'"))
    repo.src("cli.py", "def main(argv=None, ctx=None):\n    return 0\n")
    assert repo.findings("7") == [
        "src/moxi_alpha/cli.py:1: the CLI target main lacks prog; `moxi <module>` calls it "
        "as main(argv, prog=\"moxi <module>\")"]
    repo.src("cli.py", "def main(argv=None, *, prog='moxi alpha'):\n    return 0\n")
    assert repo.findings() == []
    repo.src("cli.py", "")
    assert "no top-level function main" in repo.findings("7")[0]


def test_the_settings_schema_parses(repo):
    repo.src("module.py", announcement(SETTINGS="'settings.toml'"))
    assert "does not exist" in repo.findings("7")[0]
    repo.src("settings.toml", '[mode]\ntype = "string"\ndefault = "a"\nread = "start"\n')
    assert "lacks doc" in repo.findings("7")[0]
    repo.src("settings.toml", '[mode]\ntype = "string"\ndefault = "a"\nread = "start"\n'
                              'doc = "The mode."\nenv = "OTHER_MODE"\n')
    assert "prefix MOXI_ALPHA_" in repo.findings("7")[0]
    repo.src("settings.toml", '[mode]\ntype = "string"\ndefault = "a"\nread = "start"\n'
                              'doc = "The mode."\nenv = "MOXI_ALPHA_MODE"\n')
    assert repo.findings() == []


def test_the_core_is_held_to_its_own_names(tmp_path):
    r = core_repo(tmp_path / "moxi")
    assert r.findings() == []
    r.write("pyproject.toml", pyproject("moxi", deps=(), entry_points={"moxi": "moxi.mod"},
                                        scripts={"moxi": "moxi.other:main", "mx": "moxi.x:y"}))
    found = r.findings("7")
    assert len(found) == 3
    assert any("the core is not a module" in m for m in found)
    r.write("pyproject.toml", pyproject("moxi", deps=(), entry_points={}, scripts={}))
    assert "does not declare the console script moxi" in r.findings("7")[0]


# --- the status line segment -----------------------------------------------------------------

def test_a_segment_shows_and_triggers_nothing(repo):
    repo.src("module.py", announcement(SEGMENT="'moxi_alpha.segment'"))
    repo.src("segment.py", "from . import clock\ndef segment():\n    return clock.read()\n")
    repo.src("clock.py", "import json\ndef read():\n    return ''\n")
    assert repo.findings() == []
    repo.src("clock.py", """\
        import os
        from os import kill
        def read():
            import subprocess
            os.system("true")
            os.execv("x", [])
            kill(1, 0)
        """)
    found = repo.findings("segment")
    assert [f.split(":")[1] for f in found] == ["4", "5", "6", "7"]


def test_a_segment_may_not_reach_turns(repo):
    repo.src("module.py", announcement(SEGMENT="'moxi_alpha.segment'"))
    repo.src("segment.py", "import urllib.request\nfrom http import client\n")
    assert len(repo.findings("segment")) == 2


# --- staged and whole-tree modes -------------------------------------------------------------

def test_staged_reads_the_index_not_the_working_tree(repo):
    repo.git("init", "-q")
    repo.git("add", ".")
    repo.src("core.py", "import requests\n")
    assert repo.findings(staged=True) == []
    assert len(repo.findings("2")) == 1
    repo.git("add", ".")
    repo.src("core.py", "")
    assert len(repo.findings("2", staged=True)) == 1
    assert repo.findings() == []


def test_staged_sees_a_file_deleted_from_the_index(repo):
    repo.src("bad.py", "import requests\n")
    repo.git("init", "-q")
    repo.git("add", ".")
    assert len(repo.findings("2", staged=True)) == 1
    repo.git("rm", "-q", "--cached", "src/moxi_alpha/bad.py")
    assert repo.findings(staged=True) == []


def test_staged_reads_a_staged_settings_schema(repo):
    repo.src("module.py", announcement(SETTINGS="'schema.cfg'"))
    repo.src("schema.cfg", "not toml [")
    repo.git("init", "-q")
    repo.git("add", ".")
    assert "is not valid TOML" in repo.findings("7", staged=True)[0]


def test_staged_outside_git_is_an_error(repo):
    with pytest.raises(CheckError, match="not inside a git work tree"):
        check(repo.root, staged=True)


def test_whole_tree_in_git_skips_ignored_files(repo):
    repo.git("init", "-q")
    repo.write(".gitignore", "scratch/\n")
    repo.write("scratch/probe.py", "import requests\n")
    repo.write("notes.py", "import requests\n")
    found = repo.findings("2")
    assert found == ["notes.py:1: imports requests, which no distribution the repo declares "
                     "provides; declare it in dependencies or an extra, or map the import "
                     "name to its distribution in [tool.moxi.imports]"]


def test_whole_tree_outside_git_skips_environments(repo):
    repo.write(".venv/lib/x.py", "import requests\n")
    repo.write("build/lib/x.py", "import requests\n")
    assert repo.findings() == []


# --- main ------------------------------------------------------------------------------------

def test_main_exits_0_1_and_2(repo, capsys):
    assert contract.main([str(repo.root)]) == 0
    assert capsys.readouterr().out == ""
    repo.src("core.py", "import requests\n")
    assert contract.main([str(repo.root)]) == 1
    out = capsys.readouterr()
    assert out.out.startswith("src/moxi_alpha/core.py:1: check 2: imports requests")
    assert "1 finding in" in out.err
    assert contract.main(["--staged", str(repo.root)]) == 2
    assert "not inside a git work tree" in capsys.readouterr().err


def test_main_names_its_prog(capsys):
    with pytest.raises(SystemExit):
        contract.main(["--help"], prog="moxi check")
    assert capsys.readouterr().out.startswith("usage: moxi check [-h] [--staged] [PATH]")
