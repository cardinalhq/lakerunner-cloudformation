"""Lint tests for the deploy-lakerunner.sh script.

These tests soft-skip when their respective tools are not on PATH so that
contributors and CI runners without shellcheck installed are not blocked.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "deploy-lakerunner.sh"


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


def test_review_in_progress_handling_present():
    """When --no-execute is used, the prior change set leaves the stack in
    REVIEW_IN_PROGRESS.  Without explicit handling the next invocation
    auto-detects mode=update and fails because install-only params can't
    UsePreviousValue.  The script must auto-recover by deleting the
    REVIEW_IN_PROGRESS stack and re-entering CREATE mode.  Discovered in
    a real test-account install on 2026-05-04."""
    text = SCRIPT.read_text()
    assert "REVIEW_IN_PROGRESS" in text, (
        "deploy script must handle REVIEW_IN_PROGRESS state explicitly"
    )
    assert "delete-stack" in text and "stack-delete-complete" in text, (
        "REVIEW_IN_PROGRESS recovery must delete the stale stack"
    )


def test_rollback_complete_handling_present():
    """A failed initial CREATE leaves the stack in ROLLBACK_COMPLETE.
    CloudFormation refuses any UPDATE against such a stack, so the deploy
    script must auto-recover the same way it does for REVIEW_IN_PROGRESS:
    delete the empty stack and re-enter CREATE mode."""
    text = SCRIPT.read_text()
    assert "ROLLBACK_COMPLETE" in text, (
        "deploy script must handle ROLLBACK_COMPLETE state explicitly"
    )


# ---------------------------------------------------------------------------
# AWS CLI accepts --role-arn only on create-change-set in this script.  Wrapping
# any other subcommand in `cfntool ...` causes the AWS CLI to error out with
# "Unknown options: --role-arn".  This regression bit us twice: once on
# get-template-summary, once on execute-change-set.  Guard it.
# ---------------------------------------------------------------------------

ALLOWED_CFNTOOL_WRAPPED_SUBCOMMANDS = {"create-change-set"}


def test_cfntool_wrapper_only_wraps_role_capable_subcommands():
    text = SCRIPT.read_text().splitlines()
    offenders = []
    for i, line in enumerate(text, 1):
        stripped = line.strip()
        # Skip comments, the function definition itself, and the inline help.
        if stripped.startswith("#"):
            continue
        if not stripped.startswith("cfntool "):
            continue
        # Extract the first token after `cfntool `.
        token = stripped.split()[1] if len(stripped.split()) > 1 else ""
        # Skip lines that aren't actually CFN subcommands (e.g. nothing).
        if not token:
            continue
        if token not in ALLOWED_CFNTOOL_WRAPPED_SUBCOMMANDS:
            offenders.append((i, stripped))
    assert not offenders, (
        "cfntool() wrapper used on subcommand(s) that don't accept --role-arn:\n"
        + "\n".join(f"  {i}: {ln}" for i, ln in offenders)
        + f"\nAllowed: {sorted(ALLOWED_CFNTOOL_WRAPPED_SUBCOMMANDS)}"
    )
