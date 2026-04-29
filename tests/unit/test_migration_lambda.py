"""Behavioral tests for the migration custom-resource Lambda.

The Lambda source lives as a string in cardinal_cfn.children.migration_lambda.SOURCE
and is embedded in the migration nested-stack template. We compile + run it in
an isolated namespace, stub boto3, and drive lambda_handler with synthetic
CloudFormation events to verify the behavior contract:

- Delete is a no-op SUCCESS.
- Update with unchanged MigrationVersion skips run_task and returns SUCCESS.
- Create with a non-digest MigrationVersion fails.
- Container exitCode != 0 fails (treated as migration failure).
- Container exitCode is None (e.g. ECS killed the task before the container ran) fails.
- run_task failures array (non-empty) fails.
- Stable PhysicalResourceId is cardinal-migration-<InstallIdLong>.
"""

import builtins
import json
import sys
import types
from unittest import mock

import pytest

from cardinal_cfn.children import migration_lambda


VALID_DIGEST = "sha256:" + "a" * 64
ANOTHER_DIGEST = "sha256:" + "b" * 64


@pytest.fixture
def lambda_module():
    """Load the embedded Lambda source as an importable module with stubbed boto3."""
    fake_boto3 = types.SimpleNamespace(client=lambda name: mock.MagicMock(name=f"boto3.{name}"))
    saved = sys.modules.get("boto3")
    sys.modules["boto3"] = fake_boto3
    try:
        ns = {"__name__": "migration_lambda_under_test"}
        compiled = compile(migration_lambda.SOURCE, "<migration_lambda>", "exec")
        builtins.exec(compiled, ns)
        # urlopen calls go nowhere by default; tests can override.
        ns["urllib"].request.urlopen = mock.MagicMock(name="urlopen", return_value=mock.MagicMock(read=lambda: b""))
        # Don't actually sleep during tests.
        ns["time"].sleep = lambda *_args, **_kwargs: None
        yield ns
    finally:
        if saved is None:
            sys.modules.pop("boto3", None)
        else:
            sys.modules["boto3"] = saved


def _event(request_type, *, install_id_long="abcdef012345", migration_version=VALID_DIGEST,
           old_version=None):
    event = {
        "RequestType": request_type,
        "StackId": "arn:aws:cloudformation:us-east-2:1234567890:stack/test/abc",
        "RequestId": "rid-1",
        "LogicalResourceId": "MigrationRunner",
        "ResponseURL": "https://cfn.example.com/response",
        "ResourceProperties": {
            "InstallIdLong": install_id_long,
            "MigrationVersion": migration_version,
            "ClusterArn": "arn:aws:ecs:us-east-2:1234567890:cluster/c",
            "TaskDefinitionArn": "arn:aws:ecs:us-east-2:1234567890:task-definition/t:1",
            "PrivateSubnetIds": ["subnet-1", "subnet-2"],
            "TaskSecurityGroupId": "sg-1",
        },
    }
    if old_version is not None:
        event["OldResourceProperties"] = {"MigrationVersion": old_version}
    return event


def _context():
    ctx = mock.MagicMock()
    ctx.log_stream_name = "log-stream"
    return ctx


def _last_response_payload(lambda_module):
    """Return the JSON body of the last urlopen() call as a dict."""
    urlopen = lambda_module["urllib"].request.urlopen
    request = urlopen.call_args.args[0]
    return json.loads(request.data.decode("utf-8"))


# ---------------------------------------------------------------------------
# Delete — must be a no-op success and must not call ecs.run_task.
# ---------------------------------------------------------------------------

def test_delete_is_noop_success(lambda_module):
    lambda_module["lambda_handler"](_event("Delete"), _context())
    body = _last_response_payload(lambda_module)
    assert body["Status"] == "SUCCESS"
    assert body["PhysicalResourceId"] == "cardinal-migration-abcdef012345"
    assert not lambda_module["ecs"].run_task.called


# ---------------------------------------------------------------------------
# Update with unchanged MigrationVersion — skip and report success.
# ---------------------------------------------------------------------------

def test_update_unchanged_skips_run_task(lambda_module):
    lambda_module["lambda_handler"](
        _event("Update", migration_version=VALID_DIGEST, old_version=VALID_DIGEST),
        _context(),
    )
    body = _last_response_payload(lambda_module)
    assert body["Status"] == "SUCCESS"
    assert "unchanged" in body["Reason"].lower()
    assert not lambda_module["ecs"].run_task.called


# ---------------------------------------------------------------------------
# Update with changed MigrationVersion — must run the migration.
# ---------------------------------------------------------------------------

def test_update_changed_runs_migration(lambda_module):
    ecs = lambda_module["ecs"]
    ecs.run_task.return_value = {
        "tasks": [{"taskArn": "arn:aws:ecs:us-east-2:1:task/c/t1"}],
        "failures": [],
    }
    ecs.describe_tasks.return_value = {
        "tasks": [{
            "lastStatus": "STOPPED",
            "containers": [{"name": "migrator", "exitCode": 0}],
        }],
    }
    lambda_module["lambda_handler"](
        _event("Update", migration_version=ANOTHER_DIGEST, old_version=VALID_DIGEST),
        _context(),
    )
    body = _last_response_payload(lambda_module)
    assert body["Status"] == "SUCCESS"
    assert ecs.run_task.call_count == 1


# ---------------------------------------------------------------------------
# Digest validation — non-digest values must fail.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["", "v1.2.3", ":latest", "latest", "sha256:short", None])
def test_create_rejects_non_digest_migration_version(lambda_module, bad):
    lambda_module["lambda_handler"](
        _event("Create", migration_version=bad),
        _context(),
    )
    body = _last_response_payload(lambda_module)
    assert body["Status"] == "FAILED"
    assert "sha256" in body["Reason"]
    assert not lambda_module["ecs"].run_task.called


# ---------------------------------------------------------------------------
# Container exit codes — only 0 is success.
# ---------------------------------------------------------------------------

def test_create_fails_on_nonzero_exit_code(lambda_module):
    ecs = lambda_module["ecs"]
    ecs.run_task.return_value = {
        "tasks": [{"taskArn": "arn:aws:ecs:us-east-2:1:task/c/t1"}],
        "failures": [],
    }
    ecs.describe_tasks.return_value = {
        "tasks": [{
            "lastStatus": "STOPPED",
            "stopCode": "EssentialContainerExited",
            "stoppedReason": "schema mismatch",
            "containers": [{"name": "migrator", "exitCode": 7, "reason": "boom"}],
        }],
    }
    lambda_module["lambda_handler"](_event("Create"), _context())
    body = _last_response_payload(lambda_module)
    assert body["Status"] == "FAILED"
    assert "exitCode=7" in body["Reason"]


def test_create_fails_when_exit_code_is_none(lambda_module):
    """ECS sets exitCode=None when it kills a task before the container ran (image pull failure, OOM, ENI failure)."""
    ecs = lambda_module["ecs"]
    ecs.run_task.return_value = {
        "tasks": [{"taskArn": "arn:aws:ecs:us-east-2:1:task/c/t1"}],
        "failures": [],
    }
    ecs.describe_tasks.return_value = {
        "tasks": [{
            "lastStatus": "STOPPED",
            "stopCode": "TaskFailedToStart",
            "stoppedReason": "ResourceInitializationError: image pull failed",
            "containers": [{"name": "migrator", "exitCode": None}],
        }],
    }
    lambda_module["lambda_handler"](_event("Create"), _context())
    body = _last_response_payload(lambda_module)
    assert body["Status"] == "FAILED"
    assert "exitCode=None" in body["Reason"]
    assert "TaskFailedToStart" in body["Reason"]


# ---------------------------------------------------------------------------
# run_task failures array must surface as failure.
# ---------------------------------------------------------------------------

def test_create_fails_when_run_task_returns_failures(lambda_module):
    ecs = lambda_module["ecs"]
    ecs.run_task.return_value = {
        "tasks": [{"taskArn": "arn:aws:ecs:us-east-2:1:task/c/t1"}],
        "failures": [{"reason": "RESOURCE:CPU", "arn": "..."}],
    }
    lambda_module["lambda_handler"](_event("Create"), _context())
    body = _last_response_payload(lambda_module)
    assert body["Status"] == "FAILED"
    assert "RESOURCE:CPU" in body["Reason"]


# ---------------------------------------------------------------------------
# Polling deadline must be < the Lambda's 900s timeout so we always respond.
# ---------------------------------------------------------------------------

def test_polling_deadline_is_under_lambda_timeout():
    """Lambda Timeout is 900s in migration.py; the polling loop must exit before that."""
    src = migration_lambda.SOURCE
    # Source uses `14 * 60` (840s). If anyone bumps it past 900, this test catches it.
    assert "14 * 60" in src, (
        "polling deadline must stay below the 900s Lambda timeout; "
        "found a different value in migration_lambda.SOURCE"
    )
    assert "50 * 60" not in src, (
        "old 50-minute deadline detected — Lambda would be killed before responding"
    )


# ---------------------------------------------------------------------------
# Stable physical id contract.
# ---------------------------------------------------------------------------

def test_physical_id_is_stable_across_request_types(lambda_module):
    for rt in ("Delete", "Update"):
        lambda_module["lambda_handler"](
            _event(rt, migration_version=VALID_DIGEST, old_version=VALID_DIGEST),
            _context(),
        )
        body = _last_response_payload(lambda_module)
        assert body["PhysicalResourceId"] == "cardinal-migration-abcdef012345"


# ---------------------------------------------------------------------------
# Reason is truncated to fit CFN's 1 KB limit.
# ---------------------------------------------------------------------------

def test_reason_is_truncated(lambda_module):
    ecs = lambda_module["ecs"]
    ecs.run_task.side_effect = RuntimeError("x" * 5000)
    lambda_module["lambda_handler"](_event("Create"), _context())
    body = _last_response_payload(lambda_module)
    assert body["Status"] == "FAILED"
    assert len(body["Reason"]) <= 1000
