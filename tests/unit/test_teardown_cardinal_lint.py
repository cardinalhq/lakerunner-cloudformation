"""Lint tests for the per-stack teardown-cardinal.sh script.

Mirrors test_teardown_lakerunner_lint.py: shellcheck-clean, parses, fails
loudly without REGION, and gates destruction behind CONFIRM=DELETE.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "dev-scripts" / "teardown-cardinal.sh"


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


def test_no_region_fails_loudly():
    """Bare invocation (no REGION) hits validation and exits 2, not a crash."""
    result = subprocess.run(
        ["sh", str(SCRIPT)],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"},
    )
    assert result.returncode == 2, (
        f"expected exit 2, got {result.returncode}: {result.stderr}"
    )


def test_help_prints_usage_and_exits_zero():
    result = subprocess.run(
        ["sh", str(SCRIPT), "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "Usage:" in result.stdout
    assert "CONFIRM=DELETE" in result.stdout


def test_confirm_required_for_destruction():
    """With REGION set but CONFIRM unset, the script prints the plan and exits
    0 WITHOUT touching AWS (the plan branch precedes any AWS call) — proving the
    destruction gate. A minimal PATH ensures it cannot reach a real `aws`."""
    result = subprocess.run(
        ["sh", str(SCRIPT)],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "REGION": "us-east-1"},
    )
    assert result.returncode == 0, (
        f"expected exit 0 (plan mode), got {result.returncode}: {result.stderr}"
    )
    assert "PLAN" in result.stderr
    assert "CONFIRM=DELETE" in result.stderr
