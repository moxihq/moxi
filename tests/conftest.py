"""Fixtures: a throwaway site directory that holds fake distributions for discovery tests."""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

import moxi.modules

ANNOUNCEMENT_DEFAULTS = {
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


def announcement(drop: tuple[str, ...] = (), **names: str) -> str:
    """The source of an announcement module; each keyword is a name's Python expression."""
    fields = {**ANNOUNCEMENT_DEFAULTS, **names}
    return "".join(f"{k} = {v}\n" for k, v in fields.items() if k not in drop)


class Site:
    """A directory on sys.path where a test installs fake distributions."""

    def __init__(self, root: Path):
        self.root = root
        self.packages: set[str] = set()

    def write(self, relpath: str, text: str) -> Path:
        path = self.root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text))
        top = relpath.split("/")[0].removesuffix(".py")
        if not top.endswith(".dist-info"):
            self.packages.add(top)
        return path

    def distribution(self, name: str, version: str = "0.1.0", *,
                     entry_points: dict[str, str] | None = None,
                     requires: tuple[str, ...] = (),
                     extras: dict[str, list[str]] | None = None,
                     direct_url: dict | None = None) -> None:
        info = f"{name.replace('-', '_')}-{version}.dist-info"
        self.write(f"{info}/METADATA",
                   metadata(name, version, requires=requires, extras=extras))
        if entry_points:
            lines = "".join(f"{k} = {v}\n" for k, v in entry_points.items())
            self.write(f"{info}/entry_points.txt", f"[moxi.modules]\n{lines}")
        if direct_url is not None:
            import json

            self.write(f"{info}/direct_url.json", json.dumps(direct_url))

    def module(self, name: str, *, dist: str | None = None, version: str = "0.1.0",
               package: str | None = None, drop: tuple[str, ...] = (),
               requires: tuple[str, ...] = (),
               extras: dict[str, list[str]] | None = None,
               direct_url: dict | None = None,
               **names: str) -> str:
        """A whole fake module: its distribution, package and announcement."""
        package = package or f"moxi_{name}"
        self.write(f"{package}/__init__.py", "")
        self.write(f"{package}/module.py", announcement(drop, **names))
        self.distribution(dist or f"moxi-{name}", version,
                          entry_points={name: f"{package}.module"},
                          requires=requires, extras=extras, direct_url=direct_url)
        return package


def metadata(name: str, version: str, *, requires: tuple[str, ...] = (),
             extras: dict[str, list[str]] | None = None) -> str:
    """A METADATA file's text, with Requires-Dist and Provides-Extra lines."""
    lines = ["Metadata-Version: 2.1", f"Name: {name}", f"Version: {version}"]
    lines += [f"Requires-Dist: {req}" for req in requires]
    for extra, reqs in (extras or {}).items():
        lines.append(f"Provides-Extra: {extra}")
        lines += [f'Requires-Dist: {req}; extra == "{extra}"' for req in reqs]
    return "\n".join(lines) + "\n"


@pytest.fixture
def site(tmp_path, monkeypatch):
    root = tmp_path / "site"
    root.mkdir()
    monkeypatch.syspath_prepend(str(root))
    moxi.modules.reset()
    s = Site(root)
    yield s
    moxi.modules.reset()
    for mod in list(sys.modules):
        if mod.split(".")[0] in s.packages:
            del sys.modules[mod]


# -- local wheels and an offline uv, for the uv tests ---------------------------------------

def build_wheel(out: Path, name: str, version: str, files: dict[str, str], *,
                requires: tuple[str, ...] = (), extras: dict[str, list[str]] | None = None,
                scripts: dict[str, str] | None = None,
                entry_points: dict[str, dict[str, str]] | None = None) -> Path:
    """Write a pure-Python wheel by hand: no build backend, no index."""
    import base64
    import hashlib
    import zipfile

    norm = name.replace("-", "_")
    info = f"{norm}-{version}.dist-info"
    files = dict(files)
    files[f"{info}/METADATA"] = metadata(name, version, requires=requires, extras=extras)
    files[f"{info}/WHEEL"] = ("Wheel-Version: 1.0\nGenerator: moxi-tests\n"
                              "Root-Is-Purelib: true\nTag: py3-none-any\n")
    groups = dict(entry_points or {})
    if scripts:
        groups["console_scripts"] = scripts
    if groups:
        files[f"{info}/entry_points.txt"] = "".join(
            f"[{group}]\n" + "".join(f"{k} = {v}\n" for k, v in items.items())
            for group, items in groups.items())
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{norm}-{version}-py3-none-any.whl"
    record = []
    with zipfile.ZipFile(path, "w") as whl:
        for rel, text in files.items():
            data = text.encode()
            whl.writestr(rel, data)
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
            record.append(f"{rel},sha256={digest.decode()},{len(data)}")
        record.append(f"{info}/RECORD,,")
        whl.writestr(f"{info}/RECORD", "\n".join(record) + "\n")
    return path


def core_wheel(out: Path, version: str = "0.1.0") -> Path:
    """This checkout's `moxi` package as a wheel, with its console script."""
    src = Path(__file__).resolve().parents[1] / "src" / "moxi"
    files = {f"moxi/{p.name}": p.read_text() for p in sorted(src.glob("*.py"))}
    return build_wheel(out, "moxi", version, files, scripts={"moxi": "moxi.cli:main"})


@pytest.fixture
def uv_env(tmp_path):
    """Environment variables for an offline uv that touches nothing outside `tmp_path`."""
    import os
    import shutil

    if shutil.which("uv") is None:
        pytest.skip("uv is not on PATH")
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("UV_", "VIRTUAL_ENV", "PYTHON"))}
    env.update(UV_TOOL_DIR=str(tmp_path / "tools"), UV_TOOL_BIN_DIR=str(tmp_path / "bin"),
               UV_CACHE_DIR=str(tmp_path / "uv-cache"), UV_OFFLINE="1",
               UV_PYTHON_DOWNLOADS="never", UV_NO_CONFIG="1", UV_NO_PROGRESS="1")
    return env


def module_wheel(out: Path, name: str, *, requires: tuple[str, ...] = (),
                 extras: dict[str, list[str]] | None = None,
                 optional: dict[str, str] | None = None,
                 scripts: dict[str, str] | None = None, setup: str | None = None,
                 files: dict[str, str] | None = None, cli: str | None = None,
                 version: str = "0.1.0") -> Path:
    """A fake module's wheel: its package, a real announcement and its entry point."""
    pkg = f"moxi_{name}"
    fields = {"SUMMARY": repr(f"The {name} module."), "OPTIONAL": repr(optional or {}),
              "SETUP": repr(setup), "CLI": repr(cli)}
    body = {f"{pkg}/__init__.py": "", f"{pkg}/module.py": announcement(**fields)}
    body.update(files or {})
    return build_wheel(out, f"moxi-{name}", version, body, requires=requires,
                       extras=extras, scripts=scripts,
                       entry_points={"moxi.modules": {name: f"{pkg}.module"}})


@pytest.fixture
def wheels():
    """The wheel builders, for test files (which import nothing from this one)."""
    import types

    return types.SimpleNamespace(build=build_wheel, core=core_wheel, module=module_wheel)
