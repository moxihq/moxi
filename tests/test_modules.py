import dataclasses
import subprocess
import sys
from pathlib import Path

import pytest

import moxi.modules as modules
from moxi.modules import ABSENT, BROKEN, PRESENT


def test_installed_and_present_read_metadata_and_import_nothing(site):
    site.module("alpha", version="1.2.0")
    site.module("beta")
    entries = [e for e in modules.installed() if e.name in ("alpha", "beta")]
    assert entries == [
        modules.Entry("alpha", "moxi-alpha", "1.2.0", "moxi_alpha.module"),
        modules.Entry("beta", "moxi-beta", "0.1.0", "moxi_beta.module"),
    ]
    assert modules.present("alpha")
    assert not modules.present("gamma")
    assert "moxi_alpha.module" not in sys.modules
    assert "moxi_alpha" not in sys.modules


def test_load_reads_the_announcement(site):
    site.write("moxi_alpha/api.py", "def hello():\n    return 'hi'\n")
    site.module("alpha", INTERFACES="frozenset({1, 2})", API="'moxi_alpha.api'",
                CLI="'moxi_alpha.cli:main'", SUMMARY="'Says hello.'",
                OPTIONAL="{'cache': 'Holds a send while the cache is cold.'}",
                GATED="{'moxi-alpha-codex': 'codex'}",
                INTERNAL_ENV="{'MOXI_ALPHA_NAME': 'stamped by the launcher'}",
                SETTINGS="'settings.toml'", SETUP="'moxi_alpha.setup:run'",
                SEGMENT="'moxi_alpha.segment'")
    found = modules.load("alpha")
    assert found.state == PRESENT and found.ok and found.reason == ""
    assert (found.distribution, found.version) == ("moxi-alpha", "0.1.0")
    assert found.target == "moxi_alpha.module"
    assert found.package == "moxi_alpha"
    assert found.interfaces == frozenset({1, 2})
    assert found.api_name == "moxi_alpha.api"
    assert found.api.hello() == "hi"
    assert found.cli == "moxi_alpha.cli:main"
    assert found.summary == "Says hello."
    assert dict(found.optional) == {"cache": "Holds a send while the cache is cold."}
    assert dict(found.gated) == {"moxi-alpha-codex": "codex"}
    assert dict(found.internal_env) == {"MOXI_ALPHA_NAME": "stamped by the launcher"}
    assert (found.settings, found.setup, found.segment) == (
        "settings.toml", "moxi_alpha.setup:run", "moxi_alpha.segment")


def test_found_is_frozen(site):
    site.module("alpha", OPTIONAL="{'x': 'y'}")
    found = modules.load("alpha")
    with pytest.raises(dataclasses.FrozenInstanceError):
        found.state = BROKEN
    with pytest.raises(TypeError):
        found.optional["z"] = "w"


def test_a_module_with_no_api_is_present_without_one(site):
    site.module("alpha")
    found = modules.load("alpha")
    assert found.state == PRESENT
    assert found.api is None and found.api_name is None


def test_no_claimant_is_absent(site):
    found = modules.load("nothere")
    assert found.state == ABSENT
    assert "nothere" in found.reason
    assert found.api is None


def test_two_claimants_are_broken_naming_both(site):
    site.module("alpha", dist="moxi-alpha")
    site.distribution("other-alpha", "2.0", entry_points={"alpha": "moxi_alpha.module"})
    found = modules.load("alpha")
    assert found.state == BROKEN
    assert "moxi-alpha 0.1.0" in found.reason and "other-alpha 2.0" in found.reason


def test_a_core_verb_is_broken_naming_the_clash(site):
    site.module("check")
    found = modules.load("check")
    assert found.state == BROKEN
    assert "verbs" in found.reason and "moxi check" in found.reason


def test_a_name_that_breaks_the_rule_is_broken(site):
    site.module("my_mod", package="mymod")
    found = modules.load("my_mod")
    assert found.state == BROKEN
    assert "not a Moxi module name" in found.reason


def test_an_entry_point_naming_an_attribute_is_broken(site):
    site.write("moxi_alpha/__init__.py", "")
    site.write("moxi_alpha/module.py", "x = 1\n")
    site.distribution("moxi-alpha", entry_points={"alpha": "moxi_alpha.module:x"})
    assert "must name the announcement module" in modules.load("alpha").reason


def test_an_announcement_that_fails_to_import_is_broken(site):
    site.write("moxi_alpha/__init__.py", "")
    site.write("moxi_alpha/module.py", "raise RuntimeError('no good')\n")
    site.distribution("moxi-alpha", entry_points={"alpha": "moxi_alpha.module"})
    found = modules.load("alpha")
    assert found.state == BROKEN
    assert "RuntimeError: no good" in found.reason


def test_an_announcement_missing_names_is_broken_naming_them(site):
    site.module("alpha", drop=("SETUP", "SEGMENT"))
    found = modules.load("alpha")
    assert found.state == BROKEN
    assert "SETUP, SEGMENT" in found.reason


@pytest.mark.parametrize("name, value, words", [
    ("INTERFACES", "{1}", "frozenset of integers"),
    ("INTERFACES", "frozenset({'1'})", "frozenset of integers"),
    ("INTERFACES", "frozenset({True})", "frozenset of integers"),
    ("API", "'not a name'", "dotted module name"),
    ("CLI", "'moxi_alpha.cli'", "package.module:function"),
    ("SETUP", "3", "package.module:function"),
    ("SUMMARY", "'two\\nlines'", "one non-empty line"),
    ("SUMMARY", "''", "one non-empty line"),
    ("OPTIONAL", "['cache']", "map names to strings"),
    ("GATED", "{'x': 1}", "map names to strings"),
    ("SETTINGS", "''", "resource name"),
])
def test_an_announcement_value_of_the_wrong_shape_is_broken(site, name, value, words):
    site.module("alpha", **{name: value})
    found = modules.load("alpha")
    assert found.state == BROKEN
    assert name in found.reason and words in found.reason


def test_the_interface_check_runs_on_every_call(site):
    site.write("moxi_alpha/api.py", "VALUE = 7\n")
    site.module("alpha", version="0.3.0", INTERFACES="frozenset({2})",
                API="'moxi_alpha.api'")
    old = modules.load("alpha", interface=1)
    assert old.state == BROKEN
    assert old.reason == "moxi-alpha 0.3.0 offers interfaces {2}; the caller speaks 1"
    assert old.api is None
    assert "moxi_alpha.api" not in sys.modules
    new = modules.load("alpha", interface=2)
    assert new.state == PRESENT and new.api.VALUE == 7
    assert modules.load("alpha").state == PRESENT


def test_an_api_that_fails_to_import_is_broken(site):
    site.write("moxi_alpha/api.py", "import not_a_module_anywhere\n")
    site.module("alpha", API="'moxi_alpha.api'")
    found = modules.load("alpha")
    assert found.state == BROKEN
    assert "moxi_alpha.api" in found.reason and "ModuleNotFoundError" in found.reason


def test_one_record_per_name_per_process_until_reset(site):
    package = site.module("alpha")
    assert modules.load("alpha").state == PRESENT
    # The announcement turns bad on disk; this process keeps its record.
    (site.root / package / "module.py").write_text("raise RuntimeError('changed')\n")
    del sys.modules[f"{package}.module"]
    assert modules.load("alpha").state == PRESENT
    modules.reset()
    assert modules.load("alpha").state == BROKEN


def test_load_never_raises(site, monkeypatch):
    import importlib.metadata

    def boom(**kwargs):
        raise OSError("metadata unreadable")

    monkeypatch.setattr(importlib.metadata, "entry_points", boom)
    found = modules.load("alpha")
    assert found.state == BROKEN
    assert "metadata unreadable" in found.reason


def test_importing_the_core_loads_no_metadata_machinery():
    src = Path(modules.__file__).parent
    assert (src / "__init__.py").read_bytes() == b""
    code = ("import sys; import moxi.modules, moxi.names, moxi.paths, moxi.config; "
            "print('importlib.metadata' in sys.modules, 'tomllib' in sys.modules)")
    out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True,
                         text=True, check=True)
    assert out.stdout.split() == ["False", "False"]
