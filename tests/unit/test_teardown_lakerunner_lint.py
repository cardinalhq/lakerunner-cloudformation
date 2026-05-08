"""Lint tests for the teardown-lakerunner.sh script.

Mirrors test_upgrade_lakerunner_lint.py: shellcheck-clean, parses, fails
loudly without required args.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "teardown-lakerunner.sh"


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
    """A bare invocation should hit the validation branch and exit code 2,
    not blow up with a syntax error."""
    result = subprocess.run(
        ["sh", str(SCRIPT)],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"},
    )
    assert result.returncode == 2, (
        f"expected exit 2, got {result.returncode}: {result.stderr}"
    )


def test_help_flag_prints_usage_and_exits_zero():
    result = subprocess.run(
        ["sh", str(SCRIPT), "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "Usage:" in result.stdout
    assert "--yes" in result.stdout
    assert "--keep-data" in result.stdout


# ---------------------------------------------------------------------------
# Safety: --yes is required to actually destroy anything.  The script's
# pure-data --internal-check-yes hook reports whether --yes was passed; use
# it to confirm the gate exists in the parsed-args layer.
# ---------------------------------------------------------------------------

def test_yes_flag_required_for_destruction():
    no_yes = subprocess.run(
        ["sh", str(SCRIPT), "--internal-check-yes"],
        capture_output=True, text=True,
    )
    assert no_yes.returncode == 0, no_yes.stderr
    assert no_yes.stdout.strip() == "missing-yes"

    with_yes = subprocess.run(
        ["sh", str(SCRIPT), "--internal-check-yes", "--yes"],
        capture_output=True, text=True,
    )
    assert with_yes.returncode == 0, with_yes.stderr
    assert with_yes.stdout.strip() == "ok"


# ---------------------------------------------------------------------------
# AWS CLI accepts --role-arn only on cloudformation subcommands in this
# script — and the deployer role's trust policy only allows
# cloudformation.amazonaws.com to assume it, so passing --role-arn to s3api
# / secretsmanager / rds calls would fail twice over.  Guard it.
# ---------------------------------------------------------------------------

def test_nested_stack_logical_ids_match_root_template():
    """The teardown script's retained-resource discovery references nested
    stack logical ids under the root.  The infra-script pivot moves the
    database/storage/config/cluster children entirely outside CFN (their
    work now lives in scripts/data-setup.sh), so the script's references
    to those stack ids are now legacy: they exist for installs deployed
    before the refactor and are tolerated by the script's
    silent-empty-on-missing path.  This test simply asserts the script's
    referenced ids are a subset of the legacy + current set."""
    import re

    from cardinal_cfn.root import build as build_root

    root = build_root()
    current_nested_ids = {
        name
        for name, res in root.resources.items()
        if res.resource_type == "AWS::CloudFormation::Stack"
    }
    legacy_nested_ids = {
        "StorageStack",
        "DatabaseStack",
        "ConfigStack",
        "ClusterStack",
    }
    allowed_ids = current_nested_ids | legacy_nested_ids

    raw = SCRIPT.read_text()
    referenced = set(re.findall(r'get_nested_stack_id\s+"\$stack_name"\s+"([^"]+)"', raw))
    unknown = referenced - allowed_ids
    assert not unknown, (
        f"teardown script references nested logical ids that exist in neither "
        f"the current root template nor the legacy retain set: {sorted(unknown)}"
    )


def test_role_arn_only_passed_to_cloudformation_calls():
    """Collapse backslash-continued lines first, then check that every
    `--role-arn` occurrence is either docs, a variable assignment, the arg
    parser, or a real `aws cloudformation ...` invocation."""
    raw = SCRIPT.read_text()
    # Join shell line-continuations so multi-line AWS calls become one logical line.
    joined = raw.replace("\\\n", " ")
    offenders = []
    for i, line in enumerate(joined.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if "--role-arn" not in stripped:
            continue
        if "aws cloudformation" in stripped:
            continue
        if (
            "deployer_role_arn=" in stripped
            or '"--deployer-role-arn"' in stripped
            or "Role ARN" in stripped
        ):
            continue
        offenders.append((i, stripped))
    assert not offenders, (
        "--role-arn appears on a non-cloudformation AWS CLI call:\n"
        + "\n".join(f"  {i}: {ln}" for i, ln in offenders)
    )
