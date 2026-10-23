"""`moxi install`, by its parts and by its steps against a fake environment.

The steps run against fake distributions in the `site` fixture: a fake runner stands in
for uv, installing the fake modules a command names and writing the receipt uv would.
`test_install_uv.py` runs the same installer against uv and local wheels.
"""

from __future__ import annotations

import importlib
import io
import shutil
import sys

import pytest

import moxi.cli as cli
import moxi.install as install
import moxi.modules as modules
from moxi.install import InstallError, Req, receipt_entry, receipt_options, union

# -- requirements --------------------------------------------------------------------------


def test_req_text():
    assert Req("moxi-comms").text() == "moxi-comms"
    assert Req("moxi-comms", ["cache", "codex"], ">=0.1,<0.2").text() == (
        "moxi-comms[cache,codex]>=0.1,<0.2")
    assert Req("moxi-turns", url="git+https://h/turns@v1").text() == (
        "moxi-turns @ git+https://h/turns@v1")


def test_union_keeps_every_distribution_and_extra_the_receipt_holds():
    held = [Req("moxi-comms", ["cache"]), Req("moxi-turns", url="git+https://h/t")]
    out = union(held, [Req("moxi-comms", ["codex"]), Req("moxi-turns"), Req("moxi-cache")])
    assert [r.text() for r in out] == [
        "moxi-comms[cache,codex]", "moxi-turns @ git+https://h/t", "moxi-cache"]


def test_union_a_from_url_replaces_an_index_name_and_keeps_extras():
    out = union([Req("Moxi_Comms", ["cache"], ">=0.1")],
                [Req("moxi-comms", url="git+https://h/comms@main")])
    assert [r.text() for r in out] == ["Moxi_Comms[cache] @ git+https://h/comms@main"]


def test_parse_from():
    req = install.parse_from("moxi-turns @ git+https://github.com/o/turns@v0.1.0")
    assert (req.name, req.url) == ("moxi-turns", "git+https://github.com/o/turns@v0.1.0")
    assert install.parse_from("moxi @ file:///w/moxi.whl").url == "file:///w/moxi.whl"
    for bad, why in [("moxi-turns", "names no URL"),
                     ("requests @ git+https://h/r", "not a Moxi distribution"),
                     ("moxi-Bad_Name2x @ git+https://h", "not a Moxi distribution"),
                     ("moxi-turns @ git+https://h ; python_version>'3'", "marker")]:
        with pytest.raises(InstallError, match=why):
            install.parse_from(bad)


# -- the receipt ---------------------------------------------------------------------------

@pytest.mark.parametrize("entry, text", [
    ({"name": "moxi-comms"}, "moxi-comms"),
    ({"name": "moxi-comms", "extras": ["cache"], "specifier": ">=0.1, <0.2"},
     "moxi-comms[cache]>=0.1,<0.2"),
    ({"name": "moxi-turns", "path": "/w/moxi_turns-0.1.0-py3-none-any.whl"},
     "moxi-turns @ file:///w/moxi_turns-0.1.0-py3-none-any.whl"),
    ({"name": "moxi-turns", "directory": "/src/turns"}, "moxi-turns @ file:///src/turns"),
    ({"name": "x", "url": "https://h/x.tar.gz", "subdirectory": "pkg"},
     "x @ https://h/x.tar.gz#subdirectory=pkg"),
    ({"name": "moxi-cache", "extras": ["a"], "git": "https://github.com/o/cache?rev=private"},
     "moxi-cache[a] @ git+https://github.com/o/cache@private"),
    ({"name": "moxi-cache", "git": "file:///r/cache?subdirectory=sub"},
     "moxi-cache @ git+file:///r/cache#subdirectory=sub"),
    ({"name": "moxi-cache", "git": "ssh://git@github.com/o/cache"},
     "moxi-cache @ git+ssh://git@github.com/o/cache"),
])
def test_receipt_entry_writes_back(entry, text):
    assert receipt_entry(entry).text() == text


def test_receipt_entry_editable():
    req = receipt_entry({"name": "moxi-cache", "editable": "/src/cache"})
    assert req.editable == "/src/cache"


@pytest.mark.parametrize("entry, why", [
    ({"name": "x", "marker": "python_version > '3'"}, "unknown key marker"),
    ({"name": "x", "editable": "/src/x", "extras": ["a"]}, "editable install with extras"),
    ({"name": "x", "git": "https://h/x?lfs=true"}, "lfs="),
    ({"name": "x", "path": "/a", "url": "https://b"}, "more than one source"),
    ({"name": "x", "subdirectory": "s"}, "subdirectory without a URL"),
    ({"extras": []}, "names no distribution"),
])
def test_receipt_entry_refuses_what_it_cannot_write_back(entry, why):
    with pytest.raises(InstallError, match=why):
        receipt_entry(entry)


def test_receipt_options_are_passed_back():
    options = {
        "find-links": ["file:///w"], "index-url": "https://i/simple",
        "extra-index-url": ["https://e/simple"], "no-index": False,
        "compile-bytecode": True, "prerelease": "allow",
        "index": [{"name": "foo", "url": "https://f/simple", "explicit": False,
                   "default": False, "format": "simple", "authenticate": "auto"},
                  {"url": "https://d/simple", "default": True}],
    }
    assert receipt_options(options) == [
        "--find-links", "file:///w", "--index-url", "https://i/simple",
        "--extra-index-url", "https://e/simple", "--compile-bytecode",
        "--prerelease", "allow", "--index", "foo=https://f/simple",
        "--default-index", "https://d/simple"]
    with pytest.raises(InstallError, match="config-settings"):
        receipt_options({"config-settings": {"a": "b"}})
    with pytest.raises(InstallError, match="index"):
        receipt_options({"index": [{"url": "https://f", "explicit": True}]})


def test_tool_command_re_runs_with_everything_the_receipt_holds(tmp_path):
    prefix = tmp_path / "tools" / "moxi-comms"
    receipt = {"tool": {
        "requirements": [{"name": "moxi-comms", "extras": ["cache"]},
                         {"name": "moxi-turns", "git": "https://h/turns?rev=v1"},
                         {"name": "moxi-x", "editable": "/src/x"}],
        "constraints": [{"name": "websockets", "specifier": "<16"}],
        "excludes": ["foo"],
        "python": "3.12",
        "entrypoints": [{"name": "moxi", "install-path": "/b/moxi", "from": "moxi-comms"}],
        "options": {"find-links": ["file:///w"]},
    }}
    argv, env, reqs = install.tool_command(receipt, prefix, [Req("moxi-comms", ["codex"])],
                                           "uv", tmp_path)
    assert argv == [
        "uv", "tool", "install", "moxi-comms[cache,codex]",
        "--with", "moxi-turns @ git+https://h/turns@v1", "--with-editable", "/src/x",
        "--constraints", str(tmp_path / "constraints.txt"),
        "--excludes", str(tmp_path / "excludes.txt"),
        "--python", "3.12", "--find-links", "file:///w"]
    assert (tmp_path / "constraints.txt").read_text() == "websockets<16\n"
    assert env == {"UV_TOOL_DIR": str(tmp_path / "tools"), "UV_TOOL_BIN_DIR": "/b"}
    assert [r.key for r in reqs] == ["moxi-comms", "moxi-turns", "moxi-x"]


def test_tool_command_refuses_an_unknown_receipt_key(tmp_path):
    receipt = {"tool": {"requirements": [{"name": "moxi"}], "lockfile": "x"}}
    with pytest.raises(InstallError, match="lockfile"):
        install.tool_command(receipt, tmp_path, [], "uv", tmp_path)


# -- the environment -----------------------------------------------------------------------

def test_environment_refuses_the_system_python(monkeypatch):
    monkeypatch.setattr(sys, "base_prefix", sys.prefix)
    with pytest.raises(InstallError, match="system Python"):
        install.environment()


def test_environment_refuses_an_editable_moxi(monkeypatch):
    monkeypatch.setattr(sys, "base_prefix", "/somewhere/else")
    monkeypatch.setattr(install, "_direct_url", lambda name: {
        "url": "file:///w/moxi", "dir_info": {"editable": True}})
    with pytest.raises(InstallError, match="editable"):
        install.environment()


def test_environment_tells_a_tool_environment_by_its_receipt(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "base_prefix", "/somewhere/else")
    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    monkeypatch.setattr(install, "_direct_url", lambda name: None)
    assert install.environment().kind == install.PLAIN
    (tmp_path / "uv-receipt.toml").write_text('[tool]\nrequirements = [{ name = "moxi" }]\n')
    env = install.environment()
    assert env.kind == install.TOOL
    assert env.receipt["tool"]["requirements"] == [{"name": "moxi"}]


def test_installed_source_reads_direct_url(site):
    site.distribution("moxi-comms", direct_url={
        "url": "https://github.com/o/comms",
        "vcs_info": {"vcs": "git", "commit_id": "abc", "requested_revision": "v0.1.0"}})
    site.distribution("moxi-turns", direct_url={"url": "file:///w/t.whl",
                                                "archive_info": {}})
    site.distribution("moxi-cache")
    assert install.installed_source("moxi-comms").text() == (
        "moxi-comms @ git+https://github.com/o/comms@v0.1.0")
    assert install.installed_source("moxi-turns").text() == "moxi-turns @ file:///w/t.whl"
    assert install.installed_source("moxi-cache") is None


# -- the steps, against a fake environment -------------------------------------------------

#: What the fake index holds: each distribution's module, requirements and extras.
CATALOG = {
    "moxi-turns": dict(module="turns", extras={"codex": ["websockets>=13"]},
                       OPTIONAL="{'codex': 'Reaches Codex sessions.'}",
                       SETUP="'moxi_turns.setup:run'"),
    "moxi-cache": dict(module="cache", SETUP="'moxi_cache.setup:run'"),
    "moxi-comms": dict(module="comms", requires=("moxi-turns>=0.1,<0.2",),
                       extras={"cache": ["moxi-cache>=0.1,<0.2"],
                               "codex": ["moxi-turns[codex]>=0.1,<0.2"]},
                       OPTIONAL="{'cache': 'Holds a cold send.', "
                                "'codex': 'Delivers to Codex.'}"),
    "moxi-codex": dict(module="codex"),
    "websockets": dict(),
}


class Fake:
    """A fake uv and a fake environment over the `site` fixture."""

    def __init__(self, site, tmp_path, monkeypatch, kind=install.TOOL,
                 receipt=({"name": "moxi"},)):
        self.site = site
        self.prefix = tmp_path / "tools" / "moxi"
        self.prefix.mkdir(parents=True)
        self.kind = kind
        self.runs: list[list[str]] = []
        self.envs: list[dict] = []
        self.setups: list[str] = []
        self.fail_setup: set[str] = set()
        self.fail_run = False
        self.installed: set[str] = set()
        self.receipt = list(receipt)
        if kind == install.TOOL:
            self._write_receipt()
        monkeypatch.setattr(install, "environment", self.environment)
        monkeypatch.setattr(install, "run", self.run)
        monkeypatch.setattr(install, "snapshot", self.snapshot)
        monkeypatch.setattr(install, "run_setup", self.run_setup)
        real_which = shutil.which
        monkeypatch.setattr(shutil, "which",
                            lambda name, *a, **k: "/fake/uv" if name == "uv"
                            else real_which(name, *a, **k))

    def environment(self):
        receipt = None
        if self.kind == install.TOOL:
            import tomllib

            receipt = tomllib.loads((self.prefix / "uv-receipt.toml").read_text())
        return install.Environment(self.kind, self.prefix, "/fake/python", receipt)

    def snapshot(self, python):
        assert python == "/fake/python"
        modules.reset()
        importlib.invalidate_caches()
        return cli.describe()

    def run_setup(self, python, target):
        self.setups.append(target)
        return 1 if target in self.fail_setup else 0

    def run(self, argv, env=None):
        self.runs.append(argv)
        self.envs.append(env or {})
        if self.fail_run:
            return 1
        if self.kind == install.TOOL:
            assert argv[:3] == ["/fake/uv", "tool", "install"]
            texts = [argv[3]] + [argv[i + 1] for i, a in enumerate(argv) if a == "--with"]
            self.receipt = []
            for text in texts:
                req = install.parse_from(text) if " @ " in text else None
                head = cli._Requirement(text)
                entry = {"name": head.name}
                if head.extras:
                    entry["extras"] = list(head.extras)
                if req is not None:
                    entry["url"] = req.url
                self.receipt.append(entry)
            self._write_receipt()
        else:
            texts = argv[5:] if argv[1:3] == ["pip", "install"] else argv[4:]
        for text in texts:
            self._install(cli._Requirement(text))
        return 0

    def _install(self, req):
        name, extras = req.name, req.extras
        if name == "moxi":
            return
        spec = CATALOG[name]
        if name not in self.installed:
            self.installed.add(name)
            fields = {k: v for k, v in spec.items()
                      if k not in ("module", "requires", "extras")}
            if "module" in spec:
                self.site.module(spec["module"], requires=spec.get("requires", ()),
                                 extras=spec.get("extras"), **fields)
            else:
                self.site.distribution(name, "13.1")
        for dep in spec.get("requires", ()):
            self._install(cli._Requirement(dep))
        for extra in extras:
            for dep in spec.get("extras", {}).get(extra, ()):
                self._install(cli._Requirement(dep))

    def _write_receipt(self):
        lines = ["[tool]", "requirements = ["]
        for entry in self.receipt:
            parts = [f'name = "{entry["name"]}"']
            if entry.get("extras"):
                parts.append("extras = [" + ", ".join(f'"{e}"' for e in entry["extras"])
                             + "]")
            if entry.get("url"):
                parts.append(f'url = "{entry["url"]}"')
            lines.append("    { " + ", ".join(parts) + " },")
        lines += ["]", "entrypoints = [",
                  '    { name = "moxi", install-path = "/b/moxi", from = "moxi" },', "]",
                  "", "[tool.options]", 'find-links = ["file:///w"]', ""]
        (self.prefix / "uv-receipt.toml").write_text("\n".join(lines))


@pytest.fixture
def fake(site, tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    return Fake(site, tmp_path, monkeypatch)


def moxi_install(capsys, *argv):
    code = cli.main(["install", *argv])
    out, err = capsys.readouterr()
    return code, out, err


def test_install_a_module_with_its_required_modules_only(fake, capsys):
    code, out, err = moxi_install(capsys, "comms")
    assert code == 0, err
    # One re-run, keeping the receipt's moxi and its options, adding moxi-comms.
    assert fake.runs == [["/fake/uv", "tool", "install", "moxi", "--with", "moxi-comms",
                          "--find-links", "file:///w"]]
    assert fake.envs[0] == {"UV_TOOL_DIR": str(fake.prefix.parent),
                            "UV_TOOL_BIN_DIR": "/b"}
    assert fake.installed == {"moxi-comms", "moxi-turns"}
    # turns is set up (comms declares no SETUP); required before requiring.
    assert fake.setups == ["moxi_turns.setup:run"]
    # Standard input is not a terminal: every extra is no, with the line that adds it.
    assert "comms [cache] not installed: Holds a cold send." in out
    assert "add it later with: moxi install comms --with cache" in out
    assert "installed comms (moxi-comms 0.1.0)" in out
    assert "installed turns (moxi-turns 0.1.0)" in out
    # A required module's own extras are named, not asked for.
    assert "turns offers codex too: moxi install turns" in out


def test_install_beside_a_depending_module_goes_through_its_extra(fake, capsys):
    moxi_install(capsys, "comms")
    fake.runs.clear()
    code, out, err = moxi_install(capsys, "cache")
    assert code == 0, err
    assert "comms declares the extra cache (moxi-cache>=0.1,<0.2)" in out
    assert fake.runs[0][3:6] == ["moxi", "--with", "moxi-comms[cache]"]
    assert fake.setups[-1] == "moxi_cache.setup:run"
    assert "installed cache" in out


def test_install_a_module_named_like_a_feature_extra_goes_to_its_own_distribution(
        fake, capsys):
    """Comms' feature extra codex (moxi-turns[codex]) does not route a module
    named codex; it installs from moxi-codex."""
    moxi_install(capsys, "comms")
    fake.runs.clear()
    code, out, err = moxi_install(capsys, "codex")
    assert code == 0, err
    assert "declares the extra codex" not in out
    assert fake.runs[0][3:] == ["moxi", "--with", "moxi-comms", "--with", "moxi-codex",
                                "--find-links", "file:///w"]
    assert "installed codex" in out


def test_install_extras_keep_what_the_receipt_holds(fake, capsys):
    moxi_install(capsys, "comms", "--with", "cache")
    assert fake.runs[-1][3:6] == ["moxi", "--with", "moxi-comms[cache]"]
    fake.setups.clear()
    code, out, err = moxi_install(capsys, "comms", "--with", "codex")
    assert code == 0, err
    # Already present: no step 2, its setup again (none declared), then codex added
    # beside the cache the receipt already holds.
    assert "comms is already installed" in out
    assert fake.runs[-1][3:6] == ["moxi", "--with", "moxi-comms[cache,codex]"]
    assert "now on: comms[codex]: Delivers to Codex." in out
    assert "websockets" in fake.installed


def test_install_a_present_module_re_runs_its_setup(fake, capsys):
    moxi_install(capsys, "turns", "--none")
    fake.setups.clear()
    runs = len(fake.runs)
    code, out, _ = moxi_install(capsys, "turns", "--none")
    assert code == 0
    assert fake.setups == ["moxi_turns.setup:run"] and len(fake.runs) == runs


def test_install_all_and_none(fake, capsys):
    code, out, _ = moxi_install(capsys, "comms", "--all")
    assert code == 0
    assert fake.runs[-1][3:6] == ["moxi", "--with", "moxi-comms[cache,codex]"]
    assert "now on: comms[cache]: Holds a cold send." in out


def test_install_asks_at_a_terminal(fake, capsys, monkeypatch):
    class Tty(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr(sys, "stdin", Tty("y\nn\n"))
    code, out, _ = moxi_install(capsys, "comms")
    assert code == 0
    assert "comms [cache]: Holds a cold send.\nInstall cache? [y/N] " in out
    assert "Install codex? [y/N] " in out
    assert fake.runs[-1][3:6] == ["moxi", "--with", "moxi-comms[cache]"]


def test_install_without_answers_ahead(fake, capsys):
    code, out, _ = moxi_install(capsys, "comms", "--all", "--without", "codex")
    assert code == 0
    assert fake.runs[-1][3:6] == ["moxi", "--with", "moxi-comms[cache]"]


def test_install_refuses_an_extra_the_module_does_not_declare(fake, capsys):
    code, _, err = moxi_install(capsys, "comms", "--with", "nope")
    assert code == 1 and "comms declares no extra nope (its extras: cache, codex)" in err


def test_install_refuses_with_and_without_the_same_extra(fake, capsys):
    code, _, err = moxi_install(capsys, "comms", "--with", "cache", "--without", "cache")
    assert code == 1 and "both name cache" in err and fake.runs == []


def test_install_refuses_a_bad_name_or_a_verb(fake, capsys):
    assert "not a module name" in moxi_install(capsys, "Comms")[2]
    assert "moxi's own commands" in moxi_install(capsys, "check")[2]


def test_install_from_puts_every_spec_into_the_same_run(fake, capsys):
    code, _, err = moxi_install(capsys, "comms", "--none",
                                "--from", "moxi-comms @ git+https://h/comms@main",
                                "--from", "moxi-turns @ git+https://h/turns@main")
    assert code == 0, err
    assert fake.runs == [["/fake/uv", "tool", "install", "moxi",
                          "--with", "moxi-comms @ git+https://h/comms@main",
                          "--with", "moxi-turns @ git+https://h/turns@main",
                          "--find-links", "file:///w"]]
    # The next run keeps both sources from the receipt.
    moxi_install(capsys, "comms", "--with", "cache",
                 "--from", "moxi-cache @ git+https://h/cache@main")
    assert fake.runs[-1][3:10] == [
        "moxi", "--with", "moxi-comms[cache] @ git+https://h/comms@main",
        "--with", "moxi-turns @ git+https://h/turns@main",
        "--with", "moxi-cache @ git+https://h/cache@main"]


def test_install_failure_sets_nothing_up_and_names_the_closure(fake, capsys):
    fake.fail_run = True
    code, _, err = moxi_install(capsys, "comms",
                                "--from", "moxi-comms @ git+https://h/comms")
    assert code == 1
    assert "the install failed (exit 1); nothing was set up" in err
    assert "one --from for each Moxi distribution the module needs" in err
    assert fake.setups == []


def test_install_a_failed_setup_keeps_the_install(fake, capsys):
    fake.fail_setup.add("moxi_turns.setup:run")
    code, out, err = moxi_install(capsys, "comms", "--none")
    assert code == 1
    assert "setup turns: failed (exit 1). The install stays" in err
    assert "installed comms" in out


def test_install_a_distribution_that_is_no_module(fake, capsys):
    CATALOG["moxi-nomod"] = {}
    try:
        code, _, err = moxi_install(capsys, "nomod")
    finally:
        del CATALOG["moxi-nomod"]
    assert code == 1 and "no installed distribution declares the Moxi module" in err


def test_install_in_a_plain_environment(site, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    fake = Fake(site, tmp_path, monkeypatch, kind=install.PLAIN)
    code, _, err = moxi_install(capsys, "comms", "--with", "cache")
    assert code == 0, err
    assert fake.runs[0] == ["/fake/uv", "pip", "install", "--python", sys.executable,
                            "moxi-comms"]
    assert fake.runs[1][-1] == "moxi-comms[cache]"


def test_install_in_a_plain_environment_keeps_a_direct_source(site, tmp_path, monkeypatch,
                                                             capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    fake = Fake(site, tmp_path, monkeypatch, kind=install.PLAIN)
    fake.installed |= {"moxi-comms", "moxi-turns"}
    spec = CATALOG["moxi-comms"]
    site.module("comms", requires=spec["requires"], extras=spec["extras"],
                OPTIONAL=spec["OPTIONAL"],
                direct_url={"url": "https://h/comms",
                            "vcs_info": {"vcs": "git", "commit_id": "abc",
                                         "requested_revision": "main"}})
    site.module("turns")
    code, _, err = moxi_install(capsys, "cache")
    assert code == 0, err
    assert fake.runs[0][-1] == "moxi-comms[cache] @ git+https://h/comms@main"


def test_install_snapshot_and_setup_run_in_a_fresh_process(tmp_path, monkeypatch):
    """The real `snapshot`, `run_setup` and `run`, against this interpreter."""
    info = install.snapshot(sys.executable)
    assert info["python"] == sys.executable
    pkg = tmp_path / "setupmod"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "s.py").write_text("def ok():\n    print('did it')\n"
                              "def bad():\n    return 3\n")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    assert install.run_setup(sys.executable, "setupmod.s:ok") == 0
    assert install.run_setup(sys.executable, "setupmod.s:bad") == 3
    assert install.run(["/nonexistent/uv"]) == 127


def test_plain_environment_without_uv_uses_pip(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None)
    assert install._plain_command([Req("moxi-comms", ["cache"])]) == [
        sys.executable, "-m", "pip", "install", "moxi-comms[cache]"]
    with pytest.raises(InstallError, match="editable"):
        install._plain_command([Req("moxi-x", editable="/src/x")])
