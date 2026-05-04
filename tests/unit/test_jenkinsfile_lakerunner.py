"""Lint and structural smoke tests for the deploy Jenkinsfile.

These tests soft-skip when Groovy is not installed, so contributors and CI
runners without a Groovy parser are not blocked.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
JENKINSFILE = REPO_ROOT / "jenkins" / "Jenkinsfile.lakerunner"


def test_jenkinsfile_exists():
    assert JENKINSFILE.exists(), f"missing {JENKINSFILE}"


def test_jenkinsfile_balanced_braces():
    """Cheap structural check that catches gross truncation or typos."""
    text = JENKINSFILE.read_text()
    # Quoted braces don't count - strip strings before counting. Quick-and-dirty.
    cleaned = []
    in_single = False
    in_double = False
    triple_double = False
    i = 0
    while i < len(text):
        if not in_single and not in_double and text[i:i + 3] == '"""':
            triple_double = not triple_double
            i += 3
            continue
        if triple_double:
            i += 1
            continue
        ch = text[i]
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif not in_single and not in_double:
            cleaned.append(ch)
        i += 1
    cleaned_text = "".join(cleaned)
    assert cleaned_text.count("{") == cleaned_text.count("}"), (
        f"unbalanced braces in {JENKINSFILE.name}"
    )


def test_jenkinsfile_required_blocks_present():
    text = JENKINSFILE.read_text()
    for marker in [
        "pipeline {",
        "parameters {",
        "stage('Plan')",
        "stage('Apply')",
        "stage('Approval')",
        "post {",
        "scripts/deploy-lakerunner.sh",
        "AmazonWebServicesCredentialsBinding",
        # Install-mode params must be exposed.
        "VpcId",
        "PrivateSubnets",
        "LicenseData",
        "DexAdminPasswordHash",
    ]:
        assert marker in text, f"Jenkinsfile missing expected block: {marker}"


def test_groovy_parse_if_available():
    if shutil.which("groovy") is None:
        pytest.skip("groovy not installed on this runner")
    # `groovy -e` evaluates a snippet; we can't easily do a parse-only mode for
    # a Jenkinsfile (which depends on the Jenkins DSL).  Instead, wrap the file
    # in a function so Groovy parses it without trying to execute the pipeline
    # DSL methods.
    wrapper = f"""
def pipeline(Closure c) {{}}
def parameters(Closure c) {{}}
{JENKINSFILE.read_text()}
"""
    result = subprocess.run(
        ["groovy", "-e", wrapper],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"groovy parse failed:\n{result.stdout}\n{result.stderr}"
    )
