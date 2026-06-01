"""Lint tests for the generic chained deploy driver and its per-stack wrappers.

These tests soft-skip the shellcheck step when shellcheck is not on PATH so
that contributors and CI runners without it installed are not blocked.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"

SCRIPTS = [
    SCRIPTS_DIR / "deploy-stack.sh",
    SCRIPTS_DIR / "deploy-lakerunner-infra-base.sh",
    SCRIPTS_DIR / "deploy-lakerunner-infra-rds.sh",
    SCRIPTS_DIR / "deploy-satellite-infra-base.sh",
    SCRIPTS_DIR / "deploy-satellite-services.sh",
    SCRIPTS_DIR / "deploy-lakerunner-services.sh",
]

# Minimal PATH so a bare invocation reaches the validation branch the same way
# it would on a CI runner.
TEST_PATH = "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_script_is_executable(script):
    assert script.exists(), f"missing {script}"
    assert script.stat().st_mode & 0o111, f"{script.name} should be executable"


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_syntax_clean(script):
    """`sh -n` must parse the script without errors."""
    result = subprocess.run(
        ["sh", "-n", str(script)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"sh -n reported a syntax error in {script.name}:\n{result.stderr}"
    )


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_no_args_prints_usage_and_fails(script):
    """A bare invocation should print usage and exit non-zero (validation
    failure), not blow up with a syntax error or silently succeed."""
    result = subprocess.run(
        ["sh", str(script)],
        capture_output=True,
        text=True,
        env={"PATH": TEST_PATH},
    )
    assert result.returncode != 0, (
        f"{script.name} should exit non-zero with no args, got 0"
    )
    assert "Usage:" in result.stderr, (
        f"{script.name} should print usage to stderr with no args:\n{result.stderr}"
    )


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_shellcheck_clean(script):
    if shutil.which("shellcheck") is None:
        pytest.skip("shellcheck not installed on this runner")
    result = subprocess.run(
        ["shellcheck", str(script)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"shellcheck reported issues in {script.name}:\n{result.stdout}\n{result.stderr}"
    )


# ---------------------------------------------------------------------------
# The generic driver wraps only create-change-set in cfntool() (the sole call
# that accepts --role-arn).  Mirrors the guard in test_deploy_lakerunner_lint.
# ---------------------------------------------------------------------------

ALLOWED_CFNTOOL_WRAPPED_SUBCOMMANDS = {"create-change-set"}


def test_cfntool_wrapper_only_wraps_role_capable_subcommands():
    script = SCRIPTS_DIR / "deploy-stack.sh"
    offenders = []
    for i, line in enumerate(script.read_text().splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if not stripped.startswith("cfntool "):
            continue
        token = stripped.split()[1] if len(stripped.split()) > 1 else ""
        if not token:
            continue
        if token not in ALLOWED_CFNTOOL_WRAPPED_SUBCOMMANDS:
            offenders.append((i, stripped))
    assert not offenders, (
        "cfntool() wrapper used on subcommand(s) that don't accept --role-arn:\n"
        + "\n".join(f"  {i}: {ln}" for i, ln in offenders)
        + f"\nAllowed: {sorted(ALLOWED_CFNTOOL_WRAPPED_SUBCOMMANDS)}"
    )


def test_satellite_infra_base_maps_lakerunner_principal():
    """The one non-automatic --map: LakerunnerPrincipal <- ProcessRoleArn."""
    text = (SCRIPTS_DIR / "deploy-satellite-infra-base.sh").read_text()
    assert "LakerunnerPrincipal=ProcessRoleArn" in text


def test_lakerunner_services_computes_pubsub_sqs_env():
    """lakerunner-services must compute PubsubSqsEnv from the three satellite
    outputs and pass it as an explicit --param."""
    text = (SCRIPTS_DIR / "deploy-lakerunner-services.sh").read_text()
    for token in ("RawQueueUrl", "LakerunnerAccessRoleArn",
                  "SQS_QUEUE_URL=", "SQS_REGION=", "SQS_ROLE_ARN=",
                  "PubsubSqsEnv="):
        assert token in text, f"deploy-lakerunner-services.sh missing {token}"


def test_resolver_precedence_param_over_upstream(tmp_path):
    """End-to-end of the pure resolver via the internal hook: --param-class
    upstream value wins, a default fills gaps, and an unsatisfied required
    parameter fails."""
    summary = tmp_path / "summary.json"
    summary.write_text(
        '[{"ParameterKey":"A"},'
        '{"ParameterKey":"B","DefaultValue":"bdef"},'
        '{"ParameterKey":"C"}]'
    )
    upstream = tmp_path / "upstream.json"
    upstream.write_text('{"A":"aval"}')
    current = tmp_path / "current.json"
    current.write_text("[]")

    result = subprocess.run(
        [
            "sh",
            str(SCRIPTS_DIR / "deploy-stack.sh"),
            "--internal-resolve-params",
            str(summary),
            str(upstream),
            str(current),
        ],
        capture_output=True,
        text=True,
        env={"PATH": TEST_PATH},
    )
    # C is required and unsatisfied -> exit 2.
    assert result.returncode == 2, (
        f"expected exit 2 for unresolved required param, got "
        f"{result.returncode}:\n{result.stdout}\n{result.stderr}"
    )
    assert "C" in result.stderr


def test_resolver_resolves_when_all_satisfied(tmp_path):
    summary = tmp_path / "summary.json"
    summary.write_text(
        '[{"ParameterKey":"A"},{"ParameterKey":"B","DefaultValue":"bdef"}]'
    )
    upstream = tmp_path / "upstream.json"
    upstream.write_text('{"A":"aval"}')
    current = tmp_path / "current.json"
    current.write_text("[]")

    result = subprocess.run(
        [
            "sh",
            str(SCRIPTS_DIR / "deploy-stack.sh"),
            "--internal-resolve-params",
            str(summary),
            str(upstream),
            str(current),
        ],
        capture_output=True,
        text=True,
        env={"PATH": TEST_PATH},
    )
    assert result.returncode == 0, result.stderr
    assert '"ParameterValue": "aval"' in result.stdout
    assert '"ParameterValue": "bdef"' in result.stdout
