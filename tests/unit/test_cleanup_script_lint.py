"""Lint the inline cleanup shell body."""

import shutil
import subprocess

import pytest

from cardinal_cfn.cleanup_script import SCRIPT


def _write_script(tmp_path):
    path = tmp_path / "cleanup.sh"
    path.write_text(SCRIPT)
    return path


def test_script_parses_with_posix_sh(tmp_path):
    """The shell body must be syntactically valid POSIX sh."""
    path = _write_script(tmp_path)
    result = subprocess.run(["sh", "-n", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, f"sh -n failed: {result.stderr}"


def test_script_passes_shellcheck(tmp_path):
    """Optional shellcheck pass. Skipped if shellcheck is not on PATH."""
    if shutil.which("shellcheck") is None:
        pytest.skip("shellcheck not installed on this runner")
    path = _write_script(tmp_path)
    result = subprocess.run(
        ["shellcheck", "-s", "sh", "-S", "warning", str(path)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"shellcheck failed:\n{result.stdout}\n{result.stderr}"
    )
