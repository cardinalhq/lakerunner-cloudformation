"""deploy-lakerunner-services.sh stops the lrdb writers around the migration.

Runs the generated driver end to end against a fake `aws` on PATH that
answers from canned data and logs every call, then asserts the order: the
process/control services scale to zero (autoscaling suspended) before the
change set executes, and are restored after the stack wait -- whether it
succeeded or failed.
"""

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "deploy-lakerunner-services.sh"

SERVICES = {
    "arn:aws:ecs:us-east-1:111111111111:service/cl/process-logs": 3,
    "arn:aws:ecs:us-east-1:111111111111:service/cl/pubsub-sqs": 2,
    "arn:aws:ecs:us-east-1:111111111111:service/cl/control": 1,
}
PROCESS_LOGS_SUSPENDED = {
    "DynamicScalingInSuspended": False,
    "DynamicScalingOutSuspended": False,
    "ScheduledScalingSuspended": False,
}

FAKE_AWS = r'''#!/usr/bin/env python3
import json, os, sys

args = sys.argv[1:]
with open(os.environ["FAKE_AWS_LOG"], "a") as f:
    f.write(json.dumps(args) + "\n")

def opt(name):
    return args[args.index(name) + 1] if name in args else ""

services = json.loads(os.environ["FAKE_SERVICES"])
cmd = " ".join(args[:2])
query = opt("--query")

if cmd == "sts get-caller-identity":
    print("111111111111")
elif cmd == "cloudformation describe-stacks":
    name = opt("--stack-name")
    if name == "sat-base":
        print(json.dumps([
            {"OutputKey": "RawQueueUrl", "OutputValue": "https://sqs/q"},
            {"OutputKey": "LakerunnerAccessRoleArn", "OutputValue": "arn:aws:iam::1:role/r"},
        ]))
    elif name == "svc":
        if "StackStatus" in query:
            print("UPDATE_COMPLETE")
        elif "Parameters" in query:
            print(json.dumps([{"ParameterKey": "LakerunnerMigrateForceDirty", "ParameterValue": "true"}]))
        else:
            print("")
    elif name in ("infra-base", "infra-rds"):
        print("[]")
    else:
        sys.exit(255)
elif cmd == "cloudformation get-template-summary":
    print(json.dumps([{"ParameterKey": "LakerunnerMigrateForceDirty", "DefaultValue": "false"}]))
elif cmd == "cloudformation create-change-set":
    with open(opt("--parameters")[len("file://"):]) as src, open(os.environ["FAKE_AWS_LOG"] + ".params", "w") as dst:
        dst.write(src.read())
elif cmd == "cloudformation describe-change-set":
    if query == "Status":
        print("CREATE_COMPLETE")
    elif query == "StatusReason":
        print("None")
    elif "Migration" in query:
        print(os.environ.get("FAKE_MIGRATION_ACTION", ""))
    elif query == "ChangeSetId":
        print("arn:cs")
elif cmd == "cloudformation wait":
    if args[2] == "stack-update-complete" and os.environ.get("FAKE_STACK_FAILS"):
        sys.exit(255)
elif cmd == "cloudformation describe-stack-resource":
    print({"Process": "arn:stack/proc", "Control": "arn:stack/ctrl"}.get(opt("--logical-resource-id"), "None"))
elif cmd == "cloudformation list-stack-resources":
    tier = {"arn:stack/proc": "proc", "arn:stack/ctrl": "ctrl"}[opt("--stack-name")]
    print("\t".join(s for s in services if (s.endswith("/control")) == (tier == "ctrl")))
elif cmd == "ecs describe-services":
    print(services[opt("--services")])
elif cmd == "application-autoscaling describe-scalable-targets":
    if opt("--resource-ids") == "service/cl/process-logs":
        print(os.environ["FAKE_SUSPENDED"])
    else:
        print("null")
'''


def _require_tools():
    if shutil.which("jq") is None:
        pytest.skip("jq not installed on this runner")


def _run(tmp_path, fake_source=FAKE_AWS, **env_overrides):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "aws"
    fake.write_text(fake_source)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "aws.log"
    log.write_text("")
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "FAKE_AWS_LOG": str(log),
        "FAKE_SERVICES": json.dumps(SERVICES),
        "FAKE_SUSPENDED": json.dumps(PROCESS_LOGS_SUSPENDED),
        "FAKE_MIGRATION_ACTION": "Modify",
        "STACK_NAME": "svc",
        "REGION": "us-east-1",
        "INFRA_BASE_STACK": "infra-base",
        "INFRA_RDS_STACK": "infra-rds",
        "SATELLITE_INFRA_BASE_STACK": "sat-base",
        "CLUSTER_ARN": "arn:aws:ecs:us-east-1:111111111111:cluster/cl",
        "CLUSTER_NAME": "cl",
        "VPC_ID": "vpc-1",
        "PRIVATE_SUBNETS": "subnet-1",
        "ORGANIZATION_ID": "00000000-0000-0000-0000-000000000001",
        "DEX_ADMIN_PASSWORD_HASH": "$2y$10$hash",
        "CERTIFICATE_ARN": "arn:aws:acm:us-east-1:1:certificate/c",
    }
    env.update(env_overrides)
    result = subprocess.run(["sh", str(SCRIPT)], env=env, capture_output=True, text=True)
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    return result, calls


def _index(calls, pred):
    return next(i for i, c in enumerate(calls) if pred(c))


def _is(c, *prefix):
    return c[:len(prefix)] == list(prefix)


def _opt(c, name):
    return c[c.index(name) + 1]


def _desired_updates(calls):
    return [(i, _opt(c, "--service").rsplit("/", 1)[1], _opt(c, "--desired-count"))
            for i, c in enumerate(calls) if _is(c, "ecs", "update-service")]


def _suspend_calls(calls):
    return [(i, _opt(c, "--suspended-state"))
            for i, c in enumerate(calls) if _is(c, "application-autoscaling", "register-scalable-target")]


@pytest.mark.parametrize("stack_fails", [False, True])
def test_writers_stop_before_execute_and_restore_after_wait(tmp_path, stack_fails):
    _require_tools()
    env = {"FAKE_STACK_FAILS": "1"} if stack_fails else {}
    result, calls = _run(tmp_path, **env)
    assert (result.returncode != 0) == stack_fails, result.stderr

    execute = _index(calls, lambda c: _is(c, "cloudformation", "execute-change-set"))
    stack_wait = _index(calls, lambda c: _is(c, "cloudformation", "wait", "stack-update-complete"))
    updates = _desired_updates(calls)

    downs = [(i, name) for i, name, count in updates if count == "0"]
    assert sorted(name for _, name in downs) == ["control", "process-logs", "pubsub-sqs"]
    assert all(i < execute for i, _ in downs)
    stops_waited = [i for i, c in enumerate(calls) if _is(c, "ecs", "wait", "services-stable")]
    assert len(stops_waited) == 3 and all(i < execute for i in stops_waited)

    ups = {name: (i, count) for i, name, count in updates if count != "0"}
    assert {name: count for name, (_, count) in ups.items()} == {
        "process-logs": "3", "pubsub-sqs": "2", "control": "1"}
    assert all(i > stack_wait for i, _ in ups.values())

    # Only the autoscaled service is suspended, then restored to its exact
    # prior state after the stack wait.
    (suspend_at, suspend), (restore_at, restore) = _suspend_calls(calls)
    assert suspend_at < execute and "DynamicScalingOutSuspended=true" in suspend
    assert restore_at > stack_wait and json.loads(restore) == PROCESS_LOGS_SUSPENDED


def test_auto_leaves_writers_running_when_migration_untouched(tmp_path):
    _require_tools()
    result, calls = _run(tmp_path, FAKE_MIGRATION_ACTION="")
    assert result.returncode == 0, result.stderr
    assert _index(calls, lambda c: _is(c, "cloudformation", "execute-change-set"))
    assert _desired_updates(calls) == []
    assert _suspend_calls(calls) == []


def test_always_stops_writers_even_when_migration_untouched(tmp_path):
    _require_tools()
    result, calls = _run(tmp_path, FAKE_MIGRATION_ACTION="", MIGRATION_SCALE_DOWN="always")
    assert result.returncode == 0, result.stderr
    assert len([u for u in _desired_updates(calls) if u[2] == "0"]) == 3


def test_never_leaves_writers_running(tmp_path):
    _require_tools()
    result, calls = _run(tmp_path, MIGRATION_SCALE_DOWN="never")
    assert result.returncode == 0, result.stderr
    assert _desired_updates(calls) == []


def test_failed_stop_wait_restores_and_does_not_execute(tmp_path):
    _require_tools()
    fake = FAKE_AWS.replace(
        'elif cmd == "ecs describe-services":',
        'elif cmd == "ecs wait":\n    sys.exit(255)\nelif cmd == "ecs describe-services":')
    result, calls = _run(tmp_path, fake_source=fake)
    assert result.returncode != 0
    assert not any(_is(c, "cloudformation", "execute-change-set") for c in calls)
    restored = {name: count for _, name, count in _desired_updates(calls) if count != "0"}
    assert restored == {"process-logs": "3", "pubsub-sqs": "2", "control": "1"}
    assert json.loads(_suspend_calls(calls)[-1][1]) == PROCESS_LOGS_SUSPENDED


@pytest.mark.parametrize("value", ["true", "false", None])
def test_force_dirty_param_follows_env(tmp_path, value):
    _require_tools()
    env = {"MIGRATION_SCALE_DOWN": "never"}
    if value is not None:
        env["MIGRATION_FORCE_DIRTY"] = value
    result, _ = _run(tmp_path, **env)
    assert result.returncode == 0, result.stderr
    params = {p["ParameterKey"]: p for p in json.loads((tmp_path / "aws.log.params").read_text())}
    if value is None:
        assert params["LakerunnerMigrateForceDirty"] == {
            "ParameterKey": "LakerunnerMigrateForceDirty", "UsePreviousValue": True}
    else:
        assert params["LakerunnerMigrateForceDirty"]["ParameterValue"] == value


def test_rejects_bad_scale_down_mode(tmp_path):
    result, calls = _run(tmp_path, MIGRATION_SCALE_DOWN="sometimes")
    assert result.returncode == 2
    assert calls == []
