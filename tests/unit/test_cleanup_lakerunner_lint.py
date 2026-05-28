"""Lint the cleanup-lakerunner.sh driver script."""

import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "cleanup-lakerunner.sh"


def test_script_exists_and_is_executable():
    assert SCRIPT.exists()
    assert SCRIPT.stat().st_mode & 0o111


def test_sh_n_passes():
    result = subprocess.run(["sh", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, f"sh -n failed: {result.stderr}"


def test_shellcheck_passes():
    if shutil.which("shellcheck") is None:
        pytest.skip("shellcheck not installed on this runner")
    result = subprocess.run(
        ["shellcheck", "-s", "sh", "-S", "warning", str(SCRIPT)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"shellcheck failed:\n{result.stdout}\n{result.stderr}"
    )
