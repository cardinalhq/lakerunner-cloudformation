"""Lint tests for the upgrade-lakerunner.sh script.

These tests soft-skip when their respective tools are not on PATH so that
contributors and CI runners without shellcheck installed are not blocked.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "upgrade-lakerunner.sh"


def test_script_is_executable():
    assert SCRIPT.exists(), f"missing {SCRIPT}"
    assert SCRIPT.stat().st_mode & 0o111, "script should be executable"


def test_shellcheck_clean():
    if shutil.which("shellcheck") is None:
        pytest.skip("shellcheck not installed on this runner")
    result = subprocess.run(
        ["shellcheck", str(SCRIPT)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"shellcheck reported issues:\n{result.stdout}\n{result.stderr}"
    )


def test_runs_with_no_args_and_fails_loudly():
    """A bare invocation should hit the 'required flags missing' branch and
    exit code 2, not blow up with a syntax error."""
    result = subprocess.run(
        ["sh", str(SCRIPT)],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"},
    )
    # Exit 2 means we made it past the parser into the validation branch.
    # (Or exit 2 from preflight if a tool is missing on the runner; either is
    # an expected validation-class failure.)
    assert result.returncode == 2, (
        f"expected exit 2, got {result.returncode}: {result.stderr}"
    )
