"""uv's behaviour the umbrella and the installer stand on, kept as tests.

Each test is one probe of who exposes `moxi` and how a uv tool environment grows, run
against local wheels with an offline uv in a throwaway tool directory. The `moxi` wheel is
this checkout's core; the module wheels are small fakes with real announcements. If a uv
release changes one of these behaviours, its test fails and says which.
"""

from __future__ import annotations

import json
import subprocess
import tomllib
from pathlib import Path

import pytest


def first_set(wheels, out: Path) -> Path:
    """The four wheels of the probe: the core, turns, cache and comms."""
    wheels.core(out)
    wheels.module(out, "turns", requires=("moxi>=0.1,<0.2",))
    wheels.module(out, "cache", requires=("moxi>=0.1,<0.2",),
                 scripts={"moxi": "moxi.cli:main"})
    wheels.module(out, "comms", requires=("moxi>=0.1,<0.2", "moxi-turns>=0.1,<0.2"),
                 extras={"cache": ["moxi-cache>=0.1,<0.2"]},
                 optional={"cache": "Holds a cold send."},
                 scripts={"moxi": "moxi.cli:main", "moxi-comms-daemon": "moxi.cli:main"})
    return out


class Uv:
    def __init__(self, tmp_path: Path, env: dict[str, str], wheels):
        self.tmp = tmp_path
        self.env = env
        self.wheels = first_set(wheels, tmp_path / "wheels")
        self.tools = Path(env["UV_TOOL_DIR"])
        self.bin = Path(env["UV_TOOL_BIN_DIR"])

    def __call__(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        done = subprocess.run(["uv", *args], env=self.env, capture_output=True, text=True,
                              check=False, timeout=120)
        if check and done.returncode != 0:
            raise AssertionError(f"uv {' '.join(args)} failed:\n{done.stderr}")
        return done

    def tool(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return self("tool", *args, "--no-index", "--find-links", str(self.wheels),
                    check=check)

    def receipt(self, tool: str) -> dict:
        return tomllib.loads((self.tools / tool / "uv-receipt.toml").read_text())["tool"]

    def modules(self, moxi: Path) -> list[str]:
        done = subprocess.run([str(moxi), "modules", "--json"], env=self.env,
                              capture_output=True, text=True, check=True, timeout=60)
        return sorted(m["name"] for m in json.loads(done.stdout)["modules"])


@pytest.fixture
def uv(tmp_path, uv_env, wheels):
    return Uv(tmp_path, uv_env, wheels)


def test_a_plain_module_install_puts_moxi_on_path(uv):
    done = uv.tool("install", "moxi-comms")
    assert "Installed 2 executables: moxi, moxi-comms-daemon" in done.stderr
    assert uv.modules(uv.bin / "moxi") == ["comms", "turns"]
    receipt = uv.receipt("moxi-comms")
    assert receipt["requirements"] == [{"name": "moxi-comms"}]
    assert {e["name"]: e["from"] for e in receipt["entrypoints"]} == {
        "moxi": "moxi-comms", "moxi-comms-daemon": "moxi-comms"}


def test_a_second_tool_declaring_moxi_is_refused(uv):
    uv.tool("install", "moxi-comms")
    done = uv.tool("install", "moxi-cache", check=False)
    assert done.returncode != 0
    assert "Executable already exists: moxi" in done.stderr


def test_with_adds_a_module_to_the_tool(uv):
    uv.tool("install", "moxi-comms", "--with", "moxi-cache")
    assert uv.modules(uv.bin / "moxi") == ["cache", "comms", "turns"]
    assert uv.receipt("moxi-comms")["requirements"] == [{"name": "moxi-comms"},
                                                        {"name": "moxi-cache"}]


def test_an_extra_is_recorded_in_the_receipt(uv):
    uv.tool("install", "moxi-comms[cache]")
    assert uv.modules(uv.bin / "moxi") == ["cache", "comms", "turns"]
    assert uv.receipt("moxi-comms")["requirements"] == [
        {"name": "moxi-comms", "extras": ["cache"]}]


def test_an_upgrade_keeps_a_with(uv):
    uv.tool("install", "moxi-comms", "--with", "moxi-cache")
    uv.tool("upgrade", "moxi-comms")
    assert uv.modules(uv.bin / "moxi") == ["cache", "comms", "turns"]


def test_a_re_run_without_a_with_removes_it_and_with_it_moxi(uv):
    uv.tool("install", "moxi-comms", "--with", "moxi-cache")
    done = uv.tool("install", "moxi-comms")
    assert "Installed 2 executables: moxi, moxi-comms-daemon" in done.stderr
    site = next((uv.tools / "moxi-comms" / "lib").glob("python*/site-packages"))
    assert not list(site.glob("moxi_cache-*.dist-info"))
    # moxi-cache's RECORD listed bin/moxi, and its removal deleted the file moxi-comms
    # also wrote: the PATH link dangles.
    assert not (uv.tools / "moxi-comms" / "bin" / "moxi").exists()
    assert (uv.bin / "moxi").is_symlink() and not (uv.bin / "moxi").exists()


def test_a_dangling_moxi_link_is_no_clash(uv):
    uv.tool("install", "moxi-comms", "--with", "moxi-cache")
    uv.tool("install", "moxi-comms")
    done = uv.tool("install", "moxi")
    assert "Installed 1 executable: moxi" in done.stderr


def test_another_tool_s_internal_scripts_stay_off_path(uv):
    done = uv.tool("install", "moxi", "--with", "moxi-comms")
    assert "Installed 1 executable: moxi" in done.stderr
    assert not (uv.bin / "moxi-comms-daemon").exists()
    assert (uv.tools / "moxi" / "bin" / "moxi-comms-daemon").exists()
    assert uv.modules(uv.bin / "moxi") == ["comms", "turns"]


def test_reinstall_package_repairs_a_lost_moxi(uv):
    uv.tool("install", "moxi-comms", "--with", "moxi-cache")
    uv.tool("install", "moxi-comms")
    uv.tool("install", "moxi-comms", "--reinstall-package", "moxi-comms")
    assert (uv.tools / "moxi-comms" / "bin" / "moxi").exists()
    assert uv.modules(uv.bin / "moxi") == ["comms", "turns"]


def test_with_executables_from_clashes_when_modules_declare_moxi(uv):
    done = uv.tool("install", "moxi-comms", "--with-executables-from", "moxi",
                   check=False)
    assert done.returncode != 0
    assert "Executable already exists: moxi" in done.stderr


def test_a_plain_environment_loses_moxi_with_a_module(uv):
    venv = uv.tmp / "venv"
    uv("venv", str(venv))
    python = str(venv / "bin" / "python")
    uv("pip", "install", "--python", python, "--no-index", "--find-links", str(uv.wheels),
       "moxi-comms", "moxi-cache")
    assert uv.modules(venv / "bin" / "moxi") == ["cache", "comms", "turns"]
    uv("pip", "uninstall", "--python", python, "moxi-cache")
    assert not (venv / "bin" / "moxi").exists()


def test_a_re_run_drops_the_receipt_s_options_and_python(uv):
    """A re-run keeps no `[tool.options]` or `python` of its own, so the installer
    passes them back."""
    uv("tool", "install", "moxi-comms", "--no-index", "--find-links", str(uv.wheels),
       "--prerelease", "allow")
    receipt = uv.receipt("moxi-comms")
    assert receipt["options"]["prerelease"] == "allow"
    assert receipt["options"]["find-links"] == [uv.wheels.as_uri()]
    uv("tool", "install", "moxi-comms", "--no-index", "--find-links", str(uv.wheels))
    assert "prerelease" not in uv.receipt("moxi-comms")["options"]


def test_a_direct_url_with_satisfies_a_dependency(uv):
    """`--from`'s ground: a `--with` naming a distribution by URL satisfies another
    requirement's dependency on it, with no index holding it."""
    other = uv.tmp / "elsewhere"
    other.mkdir()
    turns = next(uv.wheels.glob("moxi_turns-*.whl"))
    turns = turns.rename(other / turns.name)
    uv.tool("install", "moxi-comms", "--with", f"moxi-turns @ {turns.as_uri()}")
    assert uv.modules(uv.bin / "moxi") == ["comms", "turns"]
    entry = uv.receipt("moxi-comms")["requirements"][1]
    assert entry == {"name": "moxi-turns", "path": str(turns)}
