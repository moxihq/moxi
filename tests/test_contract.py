"""The module contract check over the core's own repo: what the commit hook runs.

The core passes its own check by order (it is built before any hook runs it), so its suite
runs the check over the working tree and, staged, over git's index.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from moxi.contract import check

ROOT = Path(__file__).resolve().parents[1]


def _in_git() -> bool:
    if shutil.which("git") is None:
        return False
    done = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--is-inside-work-tree"],
                          capture_output=True, text=True, check=False)
    return done.returncode == 0 and done.stdout.strip() == "true"


def test_the_core_keeps_the_contract():
    assert [str(finding) for finding in check(ROOT)] == []


@pytest.mark.skipif(not _in_git(), reason="not a git checkout, so there is no index")
def test_the_core_keeps_the_contract_as_staged():
    assert [str(finding) for finding in check(ROOT, staged=True)] == []
