"""Lint tests for the generic chained deploy driver and its per-stack wrappers.

These tests soft-skip the shellcheck step when shellcheck is not on PATH so
that contributors and CI runners without it installed are not blocked.
"""

import json
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
def test_missing_required_env_prints_usage_and_fails(script):
    """With every required env var cleared, the script should print usage and
    exit non-zero (validation failure), not blow up with a syntax error or
    silently succeed.  The scripts are env-var driven (no flags), so a clean
    env that omits the required vars is the failure path.

    A minimal env (PATH only) clears STACK_NAME/REGION/VERSION/etc. for free.
    """
    result = subprocess.run(
        ["sh", str(script)],
        capture_output=True,
        text=True,
        env={"PATH": TEST_PATH},
    )
    assert result.returncode != 0, (
        f"{script.name} should exit non-zero with required env vars unset, got 0"
    )
    combined = result.stdout + result.stderr
    assert ("REQUIRED" in combined.upper()) or ("missing required" in combined), (
        f"{script.name} should print usage (a REQUIRED section) or a "
        f"'missing required' line to stderr when required env vars are unset:\n"
        f"{combined}"
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


def test_lakerunner_services_passes_queue_params():
    """lakerunner-services must read the group-0 queue inputs from the satellite
    outputs and pass them as plain QueueUrl/QueueRoleArn params (no shell blob)."""
    text = (SCRIPTS_DIR / "deploy-lakerunner-services.sh").read_text()
    for token in ("RawQueueUrl", "LakerunnerAccessRoleArn",
                  "QueueUrl=", "QueueRoleArn="):
        assert token in text, f"deploy-lakerunner-services.sh missing {token}"
    assert "PubsubSqsEnv" not in text, "old shell-blob PubsubSqsEnv still present"


def test_lakerunner_services_requires_dex_admin_password_hash():
    """Maestro/DEX will not start without DEX_ADMIN_PASSWORD_HASH, so the
    wrapper must validate it as a REQUIRED env var (collect-all-missing)."""
    text = (SCRIPTS_DIR / "deploy-lakerunner-services.sh").read_text()
    assert 'missing="$missing DEX_ADMIN_PASSWORD_HASH"' in text, (
        "DEX_ADMIN_PASSWORD_HASH is not in the required-vars validation"
    )


def test_lakerunner_services_supports_alb_scheme():
    """The services wrapper forwards an optional ALB_SCHEME -> AlbScheme param,
    mirroring the infra-base wrapper."""
    text = (SCRIPTS_DIR / "deploy-lakerunner-services.sh").read_text()
    assert "AlbScheme=$ALB_SCHEME" in text


def test_lakerunner_services_drops_otel_replicas():
    """The lakerunner-services template no longer has an OtelReplicas param;
    the wrapper must not pass it (dead plumbing)."""
    text = (SCRIPTS_DIR / "deploy-lakerunner-services.sh").read_text()
    assert "OTEL_REPLICAS" not in text
    assert "OtelReplicas" not in text


def test_lakerunner_services_cleanup_cert_propagates_status():
    """The cleanup_cert EXIT trap must preserve the child's exit status: it
    captures $? first, then rm -rf, then exits with the captured status -- so a
    FAILED deploy (non-zero child) does not false-green to 0 via rm's success.

    Verified behaviorally with a tiny sh harness reproducing the trap idiom:
    the body exits 3 and the harness must exit 3, not 0.
    """
    text = (SCRIPTS_DIR / "deploy-lakerunner-services.sh").read_text()
    # Static: the corrected idiom is present (capture-first, clear, re-exit).
    assert "status=$?" in text
    assert "trap - EXIT" in text
    assert 'exit "$status"' in text

    # Behavioral: the same trap idiom must propagate a non-zero child status.
    harness = (
        "cleanup() { status=$?; rm -rf /tmp/nonexistent-xyzzy; "
        'trap - EXIT; exit "$status"; }\n'
        "trap cleanup EXIT INT TERM HUP\n"
        "exit 3\n"
    )
    result = subprocess.run(
        ["sh", "-c", harness], capture_output=True, text=True
    )
    assert result.returncode == 3, (
        f"trap idiom must propagate the child's non-zero status, got "
        f"{result.returncode}"
    )


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


# ---------------------------------------------------------------------------
# FILE_PARAMS: a ParamName=/path entry resolves to the file's full (possibly
# multi-line / PEM) content.  Exercised via the --internal-build-upstream hook
# which builds the merged upstream-values object from the env without AWS.
# ---------------------------------------------------------------------------

PEM_CONTENT = (
    "-----BEGIN CERTIFICATE-----\n"
    "line-two-with-special=chars;and:more\n"
    "line-three\n"
    "-----END CERTIFICATE-----\n"
)


def _build_upstream(env_extra):
    env = {"PATH": TEST_PATH}
    env.update(env_extra)
    return subprocess.run(
        ["sh", str(SCRIPTS_DIR / "deploy-stack.sh"), "--internal-build-upstream"],
        capture_output=True,
        text=True,
        env=env,
    )


def test_file_params_resolves_multiline_file_content(tmp_path):
    pem = tmp_path / "cert.pem"
    pem.write_text(PEM_CONTENT)

    result = _build_upstream({"FILE_PARAMS": f"CertificateBody={pem}"})
    assert result.returncode == 0, result.stderr

    merged = json.loads(result.stdout)
    assert merged["CertificateBody"] == PEM_CONTENT, (
        f"FILE_PARAMS value should equal the file content verbatim, got:\n"
        f"{merged.get('CertificateBody')!r}"
    )


def test_file_params_unreadable_file_fails(tmp_path):
    result = _build_upstream(
        {"FILE_PARAMS": f"CertificateBody={tmp_path / 'does-not-exist.pem'}"}
    )
    assert result.returncode == 2, (
        f"expected exit 2 for unreadable FILE_PARAMS file, got "
        f"{result.returncode}:\n{result.stdout}\n{result.stderr}"
    )
    assert "FILE_PARAMS" in result.stderr


def test_params_wins_over_file_params_for_same_key(tmp_path):
    pem = tmp_path / "cert.pem"
    pem.write_text(PEM_CONTENT)

    result = _build_upstream(
        {
            "FILE_PARAMS": f"CertificateBody={pem}",
            "PARAMS": "CertificateBody=literal-wins",
        }
    )
    assert result.returncode == 0, result.stderr
    merged = json.loads(result.stdout)
    assert merged["CertificateBody"] == "literal-wins", (
        "PARAMS should win over FILE_PARAMS for the same key"
    )
