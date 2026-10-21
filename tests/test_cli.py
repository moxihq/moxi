"""The umbrella: the verbs, dispatch to a module, `modules`, `shortcut`, `config`, `check`."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import types

import pytest

import moxi
import moxi.cli as cli
from moxi import names

CLI_SOURCE = """\
import argparse

CALLS = []

def main(argv=None, prog=None):
    CALLS.append((list(argv), prog))
    if argv and argv[0] == "--help":
        argparse.ArgumentParser(prog=prog).parse_args(argv)
    if argv and argv[0] == "fail":
        return 3
    if argv and argv[0] == "exit":
        raise SystemExit(4)
    print("ran", prog, *argv)
"""


def run(capsys, *argv):
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


@pytest.fixture
def alpha(site):
    site.write("moxi_alpha/cli.py", CLI_SOURCE)
    site.module("alpha", CLI="'moxi_alpha.cli:main'", SUMMARY="'Says hello.'")
    return site


# -- the verbs ---------------------------------------------------------------------------

def test_the_verbs_are_the_reserved_names():
    assert frozenset(cli._VERBS) == names.VERBS


def test_no_arguments_prints_usage_and_exits_2(capsys):
    code, out, err = run(capsys)
    assert code == 2 and "usage: moxi <module> <verb>" in err and out == ""


def test_help_prints_usage(capsys):
    for argv in (["help"], ["--help"], ["-h"]):
        code, out, _ = run(capsys, *argv)
        assert code == 0 and "commands:" in out


def test_help_on_a_verb_prints_that_verb_s_help(capsys):
    code, out, _ = run(capsys, "help", "modules")
    assert code == 0 and "usage: moxi modules" in out


def test_help_on_a_module_runs_its_help(alpha, capsys):
    code, out, _ = run(capsys, "help", "alpha")
    assert code == 0 and "usage: moxi alpha" in out


def test_unknown_option_exits_2(capsys):
    code, _, err = run(capsys, "--bogus")
    assert code == 2 and "unknown option" in err


def test_version_names_moxi_and_each_module(alpha, capsys):
    code, out, _ = run(capsys, "version")
    assert code == 0
    assert re.match(r"moxi \S+\n", out)
    assert "alpha 0.1.0 (moxi-alpha)" in out
    assert run(capsys, "--version")[0] == 0


def test_a_verb_s_usage_error_is_exit_2(capsys):
    code, _, err = run(capsys, "modules", "--bogus")
    assert code == 2 and "usage: moxi modules" in err


# -- dispatch ----------------------------------------------------------------------------

def test_dispatch_calls_the_cli_target_in_process_with_prog(alpha, capsys):
    code, out, _ = run(capsys, "alpha", "send", "--to", "x")
    assert code == 0
    assert out == "ran moxi alpha send --to x\n"
    mod = sys.modules["moxi_alpha.cli"]
    assert mod.CALLS == [(["send", "--to", "x"], "moxi alpha")]


def test_dispatch_returns_the_module_s_exit_code(alpha, capsys):
    assert run(capsys, "alpha", "fail")[0] == 3
    assert run(capsys, "alpha", "exit")[0] == 4


def test_module_help_reads_moxi_module(alpha, capsys):
    code, out, _ = run(capsys, "alpha", "--help")
    assert code == 0 and out.startswith("usage: moxi alpha")


def test_absent_module_prints_reason_and_install_line(site, capsys):
    code, out, err = run(capsys, "cache", "read")
    assert code == 2 and out == ""
    assert "no distribution in this environment declares the Moxi module 'cache'" in err
    assert "install it: moxi install cache" in err


def test_absent_module_install_line_goes_through_the_depending_extra(site, capsys):
    """RULE 2: the command named goes through the depending module's extra, and says the
    range that module speaks."""
    site.module("comms", extras={"cache": ["moxi-cache>=0.1,<0.2"]},
                OPTIONAL="{'cache': 'Holds a send while the cache is cold.'}")
    code, _, err = run(capsys, "cache")
    assert code == 2
    assert "install it: moxi install comms --with cache" in err
    assert "comms speaks moxi-cache>=0.1,<0.2" in err
    assert cli.install_hint("cache") == (
        "install it: moxi install comms --with cache   (comms speaks moxi-cache>=0.1,<0.2)")


def test_the_install_line_ignores_a_feature_extra_of_the_same_name(site):
    """[R75]: only an extra that installs moxi-<name> routes module <name>; a feature
    extra named codex says nothing about a module named codex."""
    site.module("turns", extras={"codex": ["websockets>=13"]},
                OPTIONAL="{'codex': 'Reaches Codex sessions.'}")
    site.module("comms", requires=("moxi-turns>=0.1,<0.2",),
                extras={"codex": ["moxi-turns[codex]>=0.1,<0.2"]},
                OPTIONAL="{'codex': 'Delivers to Codex.'}")
    assert cli.speakers_of("codex") == []
    assert cli.install_hint("codex") == "install it: moxi install codex"


def test_broken_module_prints_reason_and_exits_2(site, capsys):
    site.module("alpha", drop=("SUMMARY",))
    code, _, err = run(capsys, "alpha")
    assert code == 2 and "does not define SUMMARY" in err
    assert "moxi install alpha" in err


def test_a_module_with_no_command_says_what_it_is_and_who_uses_it(site, capsys):
    site.module("turns", SUMMARY="'One interface to a harness session.'")
    site.module("comms", requires=("moxi-turns>=0.1,<0.2",))
    code, _, err = run(capsys, "turns", "x")
    assert code == 2
    assert "turns: One interface to a harness session." in err
    assert "used here by: comms" in err


def test_a_module_with_no_command_and_no_users(site, capsys):
    site.module("turns")
    code, _, err = run(capsys, "turns")
    assert code == 2 and "no installed module uses it" in err


def test_a_cli_target_that_cannot_load_exits_2(site, capsys):
    site.module("alpha", CLI="'moxi_alpha.nothere:main'")
    code, _, err = run(capsys, "alpha")
    assert code == 2 and "cannot be loaded" in err and "ModuleNotFoundError" in err


def test_a_name_that_is_no_module_name_exits_2(capsys):
    code, _, err = run(capsys, "Not-A-Name")
    assert code == 2 and "neither a moxi command nor a module name" in err


# -- moxi modules ------------------------------------------------------------------------

def test_modules_with_nothing_installed(site, capsys):
    code, out, _ = run(capsys, "modules")
    assert code == 0 and "No Moxi module is installed" in out


def test_modules_json_describes_each_module(site, capsys):
    site.write("moxi_comms/cli.py", CLI_SOURCE)
    site.module("turns", extras={"codex": ["websockets>=13"]},
                OPTIONAL="{'codex': 'Reaches Codex sessions.'}")
    site.module("comms", version="0.2.0", CLI="'moxi_comms.cli:main'",
                SUMMARY="'Messages between sessions.'",
                requires=("moxi>=0.1,<0.2", "moxi-turns>=0.1,<0.2"),
                extras={"cache": ["moxi-cache>=0.1,<0.2"],
                        "codex": ["moxi-turns[codex]>=0.1,<0.2"]},
                OPTIONAL="{'cache': 'Holds a cold send.', 'codex': 'Delivers to Codex.'}",
                SETUP="'moxi_comms.cli:main'")
    code, out, _ = run(capsys, "modules", "--json")
    assert code == 0
    info = json.loads(out)
    assert info["python"] == sys.executable and info["prefix"] == sys.prefix
    comms = next(m for m in info["modules"] if m["name"] == "comms")
    assert comms == {
        "name": "comms", "distribution": "moxi-comms", "version": "0.2.0",
        "state": "present", "reason": "", "summary": "Messages between sessions.",
        "cli": "moxi_comms.cli:main", "setup": "moxi_comms.cli:main",
        "shortcut": False, "requires": ["turns"],
        "optional": [
            {"extra": "cache", "sentence": "Holds a cold send.", "installed": False,
             "add": "moxi install comms --with cache"},
            {"extra": "codex", "sentence": "Delivers to Codex.", "installed": False,
             "add": "moxi install comms --with codex"},
        ],
    }
    # codex needs moxi-turns[codex], which needs websockets: install it and both read on.
    site.distribution("websockets", "13.1")
    info = json.loads(run(capsys, "modules", "--json")[1])
    comms = next(m for m in info["modules"] if m["name"] == "comms")
    turns = next(m for m in info["modules"] if m["name"] == "turns")
    assert [o["installed"] for o in comms["optional"]] == [False, True]
    assert turns["optional"][0]["installed"] is True


def test_modules_text_lists_state_shortcut_and_extras(site, capsys):
    site.write("moxi_comms/cli.py", CLI_SOURCE)
    site.module("comms", CLI="'moxi_comms.cli:main'", SUMMARY="'Messages.'",
                extras={"cache": ["moxi-cache"]}, OPTIONAL="{'cache': 'Holds a send.'}")
    site.module("broken", drop=("API",))
    code, out, _ = run(capsys, "modules")
    assert code == 0
    assert "comms  moxi-comms 0.1.0  present  shortcut off" in out
    assert "    Messages." in out
    assert ("    [cache] not installed: Holds a send.  (moxi install comms --with cache)"
            in out)
    assert "broken  moxi-broken 0.1.0  broken" in out
    assert "does not define API" in out


# -- moxi shortcut -----------------------------------------------------------------------

@pytest.fixture
def path_dirs(tmp_path, monkeypatch):
    """A PATH of two directories: `bin` holding a stub `moxi`, and `other`."""
    bin_dir, other = tmp_path / "bin", tmp_path / "other"
    bin_dir.mkdir()
    other.mkdir()
    log = tmp_path / "moxi.log"
    stub = bin_dir / "moxi"
    stub.write_text(f'#!/bin/sh\necho "$@" >> {log}\n')
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{other}")
    return types.SimpleNamespace(bin=bin_dir, other=other, log=log)


def test_shortcut_writes_an_executable_beside_moxi(alpha, path_dirs, capsys):
    code, out, _ = run(capsys, "shortcut", "alpha")
    assert code == 0 and "wrote the shortcut" in out
    path = path_dirs.bin / "alpha"
    assert path.read_text() == '#!/bin/sh\n# moxi shortcut: alpha\nexec moxi alpha "$@"\n'
    assert path.stat().st_mode & stat.S_IXUSR
    # It runs `moxi alpha` with every argument, found by name on PATH.
    subprocess.run([str(path), "send", "a b"], check=True)
    assert path_dirs.log.read_text() == "alpha send a b\n"
    # Listed, shown by `moxi modules`, and a second write is a no-op.
    assert run(capsys, "shortcut")[1] == f"alpha  {path}\n"
    info = json.loads(run(capsys, "modules", "--json")[1])
    assert info["modules"][0]["shortcut"] is True
    code, out, _ = run(capsys, "shortcut", "alpha")
    assert code == 0 and "already on" in out


def test_shortcut_refuses_a_name_already_on_path(alpha, path_dirs, capsys):
    clash = path_dirs.other / "alpha"
    clash.write_text("#!/bin/sh\n")
    clash.chmod(0o755)
    code, _, err = run(capsys, "shortcut", "alpha")
    assert code == 1 and str(clash) in err
    assert not (path_dirs.bin / "alpha").exists()


def test_shortcut_refuses_without_moxi_on_path(alpha, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PATH", str(tmp_path))
    code, _, err = run(capsys, "shortcut", "alpha")
    assert code == 1 and "moxi is not on PATH" in err


def test_shortcut_refuses_an_absent_module_or_one_with_no_command(site, path_dirs, capsys):
    code, _, err = run(capsys, "shortcut", "cache")
    assert code == 1 and "moxi install cache" in err
    site.module("turns")
    code, _, err = run(capsys, "shortcut", "turns")
    assert code == 1 and "no command of its own" in err


def test_shortcut_remove_removes_only_its_own_file(alpha, path_dirs, capsys):
    run(capsys, "shortcut", "alpha")
    code, out, _ = run(capsys, "shortcut", "alpha", "--remove")
    assert code == 0 and not (path_dirs.bin / "alpha").exists()
    foreign = path_dirs.bin / "alpha"
    foreign.write_text("#!/bin/sh\necho mine\n")
    code, _, err = run(capsys, "shortcut", "alpha", "--remove")
    assert code == 1 and "not moxi's shortcut" in err and foreign.exists()
    # Another module's shortcut is not this one's.
    foreign.write_text(cli.shortcut_text("beta"))
    assert run(capsys, "shortcut", "alpha", "--remove")[0] == 1
    assert run(capsys, "shortcut", "nothere", "--remove")[0] == 1


def test_shortcut_listing_when_none_is_on(path_dirs, capsys):
    code, out, _ = run(capsys, "shortcut")
    assert code == 0 and "No shortcut is on" in out


# -- moxi config -------------------------------------------------------------------------

SCHEMA = """\
[backend]
type = "enum"
values = ["local", "remote"]
default = "local"
doc = "Where messages are recorded."
read = "start"
env = "MOXI_ALPHA_BACKEND"

[local.retain_days]
type = "int"
default = 30
doc = "How many days a message is kept."
read = "use"

[local.root]
type = "path"
default = ""
doc = "Where the record lives; empty means the module's data directory."
read = "start"

[codex.defaults]
type = "table"
default = { model = "o3", "odd key" = [1, 2.5, true] }
doc = "Defaults for a Codex session."
read = "start"

[name]
type = "string"
default = "a \\"quoted\\" name\\twith a tab"
doc = "A display name."
read = "use"

[old]
retired = "use backend instead"
"""


@pytest.fixture
def configured(site, tmp_path, monkeypatch):
    site.write("moxi_alpha/settings.toml", SCHEMA)
    site.module("alpha", SETTINGS="'settings.toml'")
    home = tmp_path / "config"
    monkeypatch.setenv("MOXI_ALPHA_CONFIG_HOME", str(home))
    monkeypatch.delenv("MOXI_ALPHA_BACKEND", raising=False)
    return home / "alpha" / "config.toml"


def test_config_path(configured, capsys):
    code, out, _ = run(capsys, "config", "alpha", "path")
    assert code == 0 and out == f"{configured}\n"


def test_config_show_gives_value_source_doc_and_read(configured, monkeypatch, capsys):
    code, out, _ = run(capsys, "config", "alpha")
    assert code == 0
    assert "no file: every key at its default" in out
    assert 'backend = "local"    [default]' in out
    assert "    Where messages are recorded. (read at start; env MOXI_ALPHA_BACKEND)" in out
    assert "local.root = (none)    [default]" in out
    assert "old" not in out.split("\n", 1)[1]
    configured.parent.mkdir(parents=True)
    configured.write_text("[local]\nretain_days = 7\n")
    monkeypatch.setenv("MOXI_ALPHA_BACKEND", "remote")
    out = run(capsys, "config", "alpha", "show")[1]
    assert "local.retain_days = 7    [file]" in out
    assert 'backend = "remote"    [env]' in out


def test_config_check(configured, capsys):
    code, out, _ = run(capsys, "config", "alpha", "check")
    assert code == 0 and out.startswith("ok: ")
    configured.parent.mkdir(parents=True)
    configured.write_text("old = 1\n")
    code, _, err = run(capsys, "config", "alpha", "check")
    assert code == 1 and "retired" in err and "use backend instead" in err
    configured.write_text("backend = \n")
    assert run(capsys, "config", "alpha", "check")[0] == 1


def test_config_init_writes_every_key_commented_at_its_default(configured, capsys):
    from moxi import config

    code, out, _ = run(capsys, "config", "alpha", "init")
    assert code == 0 and out == f"wrote {configured}\n"
    text = configured.read_text()
    assert "# Where messages are recorded." in text
    # Commented out, the file reads as every default.
    schema = config.schema_for(moxi.modules.load("alpha"))
    defaults = config.load("alpha", schema)
    assert set(defaults.sources.values()) == {"default"}
    # Uncommented, it reads as the same values, now from the file.
    keyline = re.compile(r"^# (\[[A-Za-z0-9_.-]+\]|[A-Za-z0-9_\"' -]+ = .*)$")
    configured.write_text("\n".join(
        line[2:] if keyline.match(line) else line for line in text.splitlines()) + "\n")
    settings = config.load("alpha", schema)
    assert dict(settings.values) == dict(defaults.values)
    assert set(settings.sources.values()) == {"file"}
    # It refuses to overwrite.
    code, _, err = run(capsys, "config", "alpha", "init")
    assert code == 1 and "exists" in err


def test_config_of_a_module_without_settings_or_absent(site, capsys):
    site.module("beta")
    code, out, _ = run(capsys, "config", "beta")
    assert code == 0 and "beta has no settings" in out
    code, _, err = run(capsys, "config", "nothere")
    assert code == 2 and "moxi install nothere" in err


# -- moxi check --------------------------------------------------------------------------

def test_check_runs_moxi_contract_main_with_its_arguments(monkeypatch, capsys):
    seen = []
    stub = types.ModuleType("moxi.contract")
    stub.main = lambda argv: seen.append(argv) or 5
    monkeypatch.setitem(sys.modules, "moxi.contract", stub)
    monkeypatch.setattr(moxi, "contract", stub, raising=False)
    assert cli.main(["check", "--staged", "some/path"]) == 5
    assert seen == [["--staged", "some/path"]]


# -- the console script ------------------------------------------------------------------

def test_pyproject_declares_the_moxi_script_and_no_other():
    import tomllib
    from pathlib import Path

    data = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    assert data["project"]["scripts"] == {"moxi": "moxi.cli:main"}
    assert "optional-dependencies" not in data["project"]


def test_importing_the_cli_loads_no_verb_module():
    code = ("import sys, moxi.cli\n"
            "bad = [m for m in ('moxi.install', 'moxi.config', 'moxi.modules', "
            "'moxi.contract', 'importlib.metadata', 'tomllib', 'argparse') "
            "if m in sys.modules]\n"
            "print(bad)\n")
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          check=True)
    assert done.stdout.strip() == "[]"
