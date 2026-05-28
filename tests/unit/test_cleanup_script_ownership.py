"""Fixture tests for the ownership_ok shell helper in cleanup_script.SCRIPT."""

import json
import subprocess
import textwrap

from cardinal_cfn.cleanup_script import SCRIPT


_PRELUDE = textwrap.dedent(
    """
    log() { :; }
    ownership_ok() {
        printf '%s' "$1" | python3 -c '
import json, sys
tags = json.load(sys.stdin)
got = {t["Key"]: t["Value"] for t in tags}
ok = (
    got.get("Application") == "cardinal-lakerunner"
    and got.get("ManagedBy") == "cardinal-data-setup-script"
)
sys.exit(0 if ok else 1)
'
    }
    """
)


def _run(tags):
    payload = json.dumps(tags)
    result = subprocess.run(
        ["sh", "-c", f"{_PRELUDE}\nownership_ok '{payload}'"],
        capture_output=True,
        text=True,
    )
    return result.returncode


def test_ownership_ok_with_both_tags():
    assert _run([
        {"Key": "Application", "Value": "cardinal-lakerunner"},
        {"Key": "ManagedBy",   "Value": "cardinal-data-setup-script"},
    ]) == 0


def test_ownership_rejects_missing_application():
    assert _run([
        {"Key": "ManagedBy", "Value": "cardinal-data-setup-script"},
    ]) == 1


def test_ownership_rejects_missing_managed_by():
    assert _run([
        {"Key": "Application", "Value": "cardinal-lakerunner"},
    ]) == 1


def test_ownership_rejects_wrong_application():
    assert _run([
        {"Key": "Application", "Value": "rogue"},
        {"Key": "ManagedBy",   "Value": "cardinal-data-setup-script"},
    ]) == 1


def test_ownership_rejects_wrong_managed_by():
    assert _run([
        {"Key": "Application", "Value": "cardinal-lakerunner"},
        {"Key": "ManagedBy",   "Value": "someone-else"},
    ]) == 1


def test_ownership_rejects_empty():
    assert _run([]) == 1


def test_helper_string_is_present_in_main_script():
    """Regression: if the helper drifts, our standalone copy is stale."""
    assert "ownership_ok()" in SCRIPT
    assert "cardinal-data-setup-script" in SCRIPT
