"""The new-module skeleton: a new repo's first commit keeps the contract and loads.

The skeleton is a private path of this repo, so a tree of the public paths alone skips
these tests.
"""

import subprocess
import sys
from pathlib import Path

import pytest

import moxi.modules as modules
from moxi.contract import check

SKELETON = Path(__file__).resolve().parents[1] / "skeleton"

pytestmark = pytest.mark.skipif(not (SKELETON / "render.py").is_file(),
                                reason="the skeleton is a private path, absent here")


def render(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SKELETON / "render.py"), *args],
                          capture_output=True, text=True, check=False)


@pytest.fixture
def sample(tmp_path) -> Path:
    repo = tmp_path / "sample"
    done = render("sample", str(repo))
    assert done.returncode == 0, done.stderr
    return repo


def test_the_skeleton_writes_what_a_first_commit_holds(sample):
    files = sorted(str(p.relative_to(sample)) for p in sample.rglob("*") if p.is_file())
    assert files == [".gitignore", ".public-paths", "pyproject.toml",
                     "src/moxi_sample/__init__.py", "src/moxi_sample/module.py",
                     "tests/test_contract.py"]


def test_the_skeleton_keeps_the_contract(sample):
    assert check(sample) == []
    subprocess.run(["git", "-C", str(sample), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(sample), "add", "."], check=True)
    assert check(sample, staged=True) == []


def test_public_paths_places_every_file(sample):
    lines = [line.split(" ", 1) for line in
             (sample / ".public-paths").read_text().splitlines()
             if line and not line.startswith("#")]
    placed = {path: kind for kind, path in lines}
    assert placed[".public-paths"] == "private"
    for path in sample.rglob("*"):
        if path.is_file():
            rel = path.relative_to(sample)
            covering = [p for p in placed if rel == Path(p) or Path(p) in rel.parents]
            assert len(covering) == 1, rel


def test_the_announcement_loads_and_offers_nothing(sample, site):
    for path in (sample / "src").rglob("*.py"):
        site.write(str(path.relative_to(sample / "src")), path.read_text())
    site.distribution("moxi-sample", entry_points={"sample": "moxi_sample.module"})
    found = modules.load("sample")
    assert found.state == modules.PRESENT, found.reason
    assert found.interfaces == frozenset() and found.api is None and found.cli is None
    assert found.optional == {} and found.summary == "Moxi's sample module."


def test_the_skeleton_refuses_a_verb_a_bad_name_and_an_existing_file(sample, tmp_path):
    assert "umbrella's own verbs" in render("check", str(tmp_path / "c")).stderr
    assert "not a Moxi module name" in render("Bad-Name", str(tmp_path / "b")).stderr
    again = render("sample", str(sample))
    assert again.returncode == 2 and "overwrites nothing" in again.stderr
