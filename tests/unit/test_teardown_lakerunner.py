"""Tests for the standalone teardown-lakerunner.sh script.

The script is shell + jq + AWS CLI.  To keep the pure-data parts testable
without AWS, the script exposes:

- --internal-format-plan <state.json>
- --internal-check-yes

Both run pure data transforms with no AWS calls and no side effects.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "teardown-lakerunner.sh"


def _require_jq():
    if shutil.which("jq") is None:
        pytest.skip("jq not installed on this runner")


@pytest.fixture(autouse=True)
def _script_exists():
    if not SCRIPT.exists():
        pytest.fail(
            f"Script missing: {SCRIPT}. Implement it before running these tests."
        )


@pytest.fixture
def state_file(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "InstallIdLong": "abc123def456",
        "BucketName": "cardinal-ingest-111122223333-us-east-2-abc123def456",
        "DbSecretArn": "arn:aws:secretsmanager:us-east-2:111122223333:secret:cardinal-database-DbMasterSecret-XYZ-AbCdEf",
        "DbInstanceIdentifier": "cardinal-lakerunner-db-1abc2def",
        "LicenseSecretArn": "arn:aws:secretsmanager:us-east-2:111122223333:secret:cardinal/abc123def456/license-AbCdEf",
        "AdminApiKeySecretArn": "arn:aws:secretsmanager:us-east-2:111122223333:secret:cardinal/abc123def456/admin-api-key-AbCdEf",
    }))
    return path


def _run_format(state_path, *extra):
    return subprocess.run(
        ["sh", str(SCRIPT), "--internal-format-plan", str(state_path), *extra],
        capture_output=True, text=True,
    )


def test_format_plan_lists_every_survivor(state_file):
    _require_jq()
    result = _run_format(state_file)
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "S3 ingest bucket: cardinal-ingest-111122223333-us-east-2-abc123def456" in out
    assert "Secret (license)" in out
    assert "Secret (admin-api-key)" in out
    assert "Secret (db-master)" in out
    assert "RDS final snapshot(s) for DB instance: cardinal-lakerunner-db-1abc2def" in out


def test_format_plan_marks_skipped_with_keep_bucket(state_file):
    _require_jq()
    result = _run_format(state_file, "--keep-bucket")
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "S3 ingest bucket: SKIPPED via --keep-bucket" in out
    # Other survivors are still listed.
    assert "Secret (license)" in out
    assert "RDS final snapshot(s) for DB instance" in out


def test_format_plan_marks_skipped_with_keep_secrets(state_file):
    _require_jq()
    result = _run_format(state_file, "--keep-secrets")
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "Retained secrets: SKIPPED via --keep-secrets" in out
    assert "S3 ingest bucket: cardinal-ingest" in out


def test_format_plan_marks_skipped_with_keep_snapshot(state_file):
    _require_jq()
    result = _run_format(state_file, "--keep-snapshot")
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "RDS final snapshot: SKIPPED via --keep-snapshot" in out
    assert "S3 ingest bucket: cardinal-ingest" in out


def test_format_plan_marks_skipped_with_keep_data(state_file):
    """--keep-data is shorthand for all three keep-* flags."""
    _require_jq()
    result = _run_format(state_file, "--keep-data")
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "S3 ingest bucket: SKIPPED" in out
    assert "Retained secrets: SKIPPED" in out
    assert "RDS final snapshot: SKIPPED" in out


def test_format_plan_handles_missing_state_fields(tmp_path):
    """If the script could not discover (e.g.) the bucket name, the plan
    should still render with a clear (not found) marker rather than an empty
    line that the operator might miss."""
    _require_jq()
    state = tmp_path / "partial.json"
    state.write_text(json.dumps({
        "InstallIdLong": "abc123def456",
        "BucketName": "",
        "LicenseSecretArn": "",
        "AdminApiKeySecretArn": "",
        "DbSecretArn": "",
        "DbInstanceIdentifier": "",
    }))
    result = _run_format(state)
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "S3 ingest bucket: (not found)" in out
    assert "Secret (license): (not found)" in out
    assert "RDS final snapshot(s) for DB instance: (not found)" in out


def test_format_plan_fails_on_missing_state_file(tmp_path):
    _require_jq()
    missing = tmp_path / "nope.json"
    result = subprocess.run(
        ["sh", str(SCRIPT), "--internal-format-plan", str(missing)],
        capture_output=True, text=True,
    )
    assert result.returncode == 2, (
        f"expected exit 2, got {result.returncode}: {result.stderr}"
    )
