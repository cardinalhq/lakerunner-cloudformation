"""Driver internal-hook tests (pure data transforms; no AWS calls)."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "cleanup-lakerunner.sh"


def _require_python3():
    if shutil.which("python3") is None:
        pytest.skip("python3 not on PATH")


def _run(*args):
    return subprocess.run(["sh", str(SCRIPT), *args], capture_output=True, text=True)


def test_help_exits_zero():
    result = _run("--help")
    assert result.returncode == 0
    assert "Usage: cleanup-lakerunner.sh" in result.stdout


def test_missing_args_exits_2():
    result = _run("--region", "us-east-1", "--yes")
    assert result.returncode == 2
    assert "missing required" in result.stderr.lower()


def test_no_yes_prints_plan_and_exits_2():
    result = _run(
        "--region", "us-east-1",
        "--version", "v0.0.46",
        "--cluster-name", "the-cluster",
        "--private-subnets", "subnet-aaa,subnet-bbb",
        "--task-sg-id", "sg-ccc",
        "--cleanup-task-role-arn", "arn:aws:iam::1:role/task",
        "--cleanup-execution-role-arn", "arn:aws:iam::1:role/exec",
        "--deployer-role-arn", "arn:aws:iam::1:role/dep",
    )
    assert result.returncode == 2
    assert "Re-run with --yes" in result.stderr
    assert "the-cluster" in result.stderr
    assert "cardinal-lakerunner" in result.stderr
    assert "cardinal-cleanup" in result.stderr


def test_unknown_arg_exits_2():
    result = _run("--bogus", "value")
    assert result.returncode == 2
    assert "unknown argument" in result.stderr.lower()


def test_internal_plan_text_pure_transform():
    _require_python3()
    payload = json.dumps({
        "region": "us-east-2",
        "cluster": "prod-cluster",
        "lakerunner_stack": "cardinal-lakerunner",
        "cleanup_stack": "cardinal-cleanup",
    })
    result = _run("--internal-plan-text", payload)
    assert result.returncode == 0, result.stderr
    assert "us-east-2" in result.stdout
    assert "prod-cluster" in result.stdout
    assert "cardinal-lakerunner" in result.stdout
    assert "cardinal-cleanup" in result.stdout
