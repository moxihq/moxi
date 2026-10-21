"""`moxi install` end to end: the real installer in a uv tool environment, offline.

The core wheel is this checkout's; the modules are fake wheels kept outside the
find-links directory, so only `--from` reaches them, as before PyPI holds the modules.
"""

from __future__ import annotations

import json
import subprocess
import tomllib
from pathlib import Path

import pytest


@pytest.fixture
def tool(tmp_path, uv_env, wheels):
    index = tmp_path / "index"      # what the tool's find-links reaches
    direct = tmp_path / "direct"    # reached only by --from
    log = tmp_path / "setup.log"
    wheels.core(index)
    wheels.build(index, "websockets", "15.0", {"websockets/__init__.py": ""})

    def setup(name: str) -> dict[str, str]:
        return {f"moxi_{name}/setup.py":
                f"def run():\n    with open({str(log)!r}, 'a') as fh:\n"
                f"        fh.write({name + chr(10)!r})\n    print('set up {name}')\n"}

    wheels.module(direct, "turns", requires=("moxi>=0.1,<0.2",),
                  extras={"codex": ["websockets>=13"]},
                  optional={"codex": "Reaches Codex sessions."},
                  setup="moxi_turns.setup:run", files=setup("turns"))
    wheels.module(direct, "cache", requires=("moxi>=0.1,<0.2",),
                  scripts={"moxi": "moxi.cli:main"},
                  setup="moxi_cache.setup:run", files=setup("cache"))
    comms_cli = ("def main(argv=None, prog=None):\n"
                 "    print(prog, *argv)\n    return 0\n")
    wheels.module(direct, "comms", requires=("moxi>=0.1,<0.2", "moxi-turns>=0.1,<0.2"),
                  extras={"cache": ["moxi-cache>=0.1,<0.2"],
                          "codex": ["moxi-turns[codex]>=0.1,<0.2"]},
                  optional={"cache": "Holds a cold send.", "codex": "Delivers to Codex."},
                  scripts={"moxi": "moxi.cli:main", "moxi-comms-daemon": "moxi.cli:main"},
                  setup="moxi_comms.setup:run", cli="moxi_comms.cli:main",
                  files={**setup("comms"), "moxi_comms/cli.py": comms_cli})

    done = subprocess.run(["uv", "tool", "install", "moxi", "--no-index",
                           "--find-links", str(index)],
                          env=uv_env, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr

    class Tool:
        bin = Path(uv_env["UV_TOOL_BIN_DIR"])
        prefix = Path(uv_env["UV_TOOL_DIR"]) / "moxi"

        def frm(self, name: str) -> str:
            wheel = next(direct.glob(f"moxi_{name}-*.whl"))
            return f"moxi-{name} @ {wheel.as_uri()}"

        def moxi(self, *args: str, code: int = 0) -> str:
            # The tool's own options must carry the re-runs: the environment sets none.
            env = {k: v for k, v in uv_env.items()
                   if k not in ("UV_TOOL_DIR", "UV_TOOL_BIN_DIR")}
            done = subprocess.run([str(self.bin / "moxi"), *args], env=env,
                                  capture_output=True, text=True, timeout=300,
                                  stdin=subprocess.DEVNULL)
            assert done.returncode == code, done.stdout + done.stderr
            return done.stdout + done.stderr

        def receipt(self) -> dict:
            return tomllib.loads((self.prefix / "uv-receipt.toml").read_text())["tool"]

        def modules(self) -> dict[str, dict]:
            info = json.loads(self.moxi("modules", "--json"))
            return {m["name"]: m for m in info["modules"]}

        def setups(self) -> list[str]:
            return log.read_text().split() if log.exists() else []

    return Tool()


def test_install_in_a_uv_tool_environment(tool):
    # The core alone: nothing installed yet.
    assert tool.modules() == {}

    # comms from git-like URLs, with its whole closure the environment lacks.
    out = tool.moxi("install", "comms", "--none",
                    "--from", tool.frm("comms"), "--from", tool.frm("turns"))
    assert sorted(tool.modules()) == ["comms", "turns"]
    receipt = tool.receipt()
    assert [r["name"] for r in receipt["requirements"]] == ["moxi", "moxi-comms",
                                                            "moxi-turns"]
    assert "path" in receipt["requirements"][1]
    assert receipt["options"]["find-links"]          # passed back, not dropped
    assert tool.setups() == ["turns", "comms"]       # required before requiring
    assert "comms [cache] not installed: Holds a cold send." in out
    assert "add it later with: moxi install comms --with cache" in out
    assert "turns offers codex too: moxi install turns" in out

    # cache beside comms goes in as comms' extra, so comms' range applies.
    tool.moxi("install", "cache", "--from", tool.frm("cache"))
    found = tool.modules()
    assert sorted(found) == ["cache", "comms", "turns"]
    comms = next(r for r in tool.receipt()["requirements"] if r["name"] == "moxi-comms")
    assert comms["extras"] == ["cache"] and "path" in comms
    assert tool.setups()[-1] == "cache"
    assert [o["installed"] for o in found["comms"]["optional"]] == [True, False]

    # An extra later: comms is present, so its setup runs again; the re-run keeps cache.
    out = tool.moxi("install", "comms", "--with", "codex")
    assert "comms is already installed" in out
    comms = next(r for r in tool.receipt()["requirements"] if r["name"] == "moxi-comms")
    assert comms["extras"] == ["cache", "codex"]
    found = tool.modules()
    assert sorted(found) == ["cache", "comms", "turns"]
    assert [o["installed"] for o in found["comms"]["optional"]] == [True, True]
    assert found["turns"]["optional"][0]["installed"] is True
    assert "now on: comms[codex]: Delivers to Codex." in out
    assert tool.setups()[-1] == "comms"

    # Dispatch, in the installed environment.
    assert tool.moxi("comms", "send", "x").strip() == "moxi comms send x"
    assert "the shortcut" not in tool.moxi("modules")


def test_install_names_the_closure_when_a_member_is_left_out(tool):
    out = tool.moxi("install", "comms", "--none", "--from", tool.frm("comms"), code=1)
    assert "the install failed" in out
    assert "one --from for each Moxi distribution the module needs" in out
    assert tool.modules() == {}


def test_install_in_a_plain_environment(tool, tmp_path, uv_env):
    venv = tmp_path / "venv"
    index = tmp_path / "index"
    for args in (["venv", str(venv)],
                 ["pip", "install", "--python", str(venv / "bin" / "python"),
                  "--no-index", "--find-links", str(index), "moxi"]):
        done = subprocess.run(["uv", *args], env=uv_env, capture_output=True, text=True,
                              timeout=120)
        assert done.returncode == 0, done.stderr
    done = subprocess.run([str(venv / "bin" / "moxi"), "install", "comms", "--none",
                           "--from", tool.frm("comms"), "--from", tool.frm("turns")],
                          env=uv_env, capture_output=True, text=True, timeout=300,
                          stdin=subprocess.DEVNULL)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "uv pip install --python" in done.stdout
    assert "installed comms" in done.stdout and "installed turns" in done.stdout
