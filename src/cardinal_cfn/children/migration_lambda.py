"""Source code for the migration custom-resource Lambda.

Embedded in migration.yaml as Code.ZipFile. Behavior contract:
- PhysicalResourceId is stable: cardinal-migration-<InstallIdLong>.
- Trigger property MigrationVersion: the LakerunnerImage value (tag or
  image@sha256:...). Any change reruns the migrator. Mutable tags like :latest
  are not supported by Cardinal policy and the Lambda treats them like any
  other string — equal value, no rerun.
- Create runs the migrator ECS task; success on exit-code 0.
- Update reruns the migrator only if MigrationVersion changed.
- Delete is a no-op.
- Lambda Timeout is 900s (AWS max). Polling deadline is set below that so we
  always send a CFN response — orphan tasks beat hung stacks.
"""

SOURCE = '''\
import json
import os
import time
import traceback
import urllib.request

import boto3


ecs = boto3.client("ecs")

MAX_REASON_LEN = 1000


def _send(event, context, status, reason="", physical_id=None, data=None):
    body = json.dumps({
        "Status": status,
        "Reason": (reason or f"see CloudWatch log {context.log_stream_name}")[:MAX_REASON_LEN],
        "PhysicalResourceId": physical_id or event.get("PhysicalResourceId") or "cardinal-migration-unknown",
        "StackId": event["StackId"],
        "RequestId": event["RequestId"],
        "LogicalResourceId": event["LogicalResourceId"],
        "Data": data or {},
    }).encode("utf-8")
    req = urllib.request.Request(
        url=event["ResponseURL"],
        data=body,
        method="PUT",
        headers={"content-type": "", "content-length": str(len(body))},
    )
    urllib.request.urlopen(req).read()


def _physical_id(event):
    install_id_long = event["ResourceProperties"]["InstallIdLong"]
    return f"cardinal-migration-{install_id_long}"


def _run_migration(event):
    props = event["ResourceProperties"]
    cluster_arn = props["ClusterArn"]
    task_definition = props["TaskDefinitionArn"]
    subnets = props["PrivateSubnetIds"]
    security_groups = [props["TaskSecurityGroupId"]]

    response = ecs.run_task(
        cluster=cluster_arn,
        taskDefinition=task_definition,
        launchType="FARGATE",
        networkConfiguration={
            "awsvpcConfiguration": {
                "subnets": subnets,
                "securityGroups": security_groups,
                "assignPublicIp": "DISABLED",
            }
        },
    )
    failures = response.get("failures") or []
    if failures:
        raise RuntimeError("migration ecs.run_task failures: " + json.dumps(failures, default=str))
    if not response.get("tasks"):
        raise RuntimeError("migration task failed to launch: " + json.dumps(response, default=str))
    task_arn = response["tasks"][0]["taskArn"]

    # Lambda Timeout is 900s; stay safely below it so we still respond to CFN.
    deadline = time.time() + 14 * 60
    while time.time() < deadline:
        desc = ecs.describe_tasks(cluster=cluster_arn, tasks=[task_arn])
        if not desc["tasks"]:
            raise RuntimeError("migration task disappeared from ECS")
        task = desc["tasks"][0]
        if task["lastStatus"] == "STOPPED":
            stop_code = task.get("stopCode")
            stopped_reason = task.get("stoppedReason")
            essential = next(
                (c for c in task.get("containers", []) if c.get("name") == "migrator"),
                None,
            )
            if essential is None:
                raise RuntimeError(
                    f"migration task stopped with no migrator container: "
                    f"stopCode={stop_code} reason={stopped_reason}"
                )
            exit_code = essential.get("exitCode")
            if exit_code != 0:
                raise RuntimeError(
                    f"migration container exited with exitCode={exit_code!r} "
                    f"reason={essential.get('reason')!r} "
                    f"stopCode={stop_code} stoppedReason={stopped_reason}"
                )
            return
        time.sleep(15)
    raise TimeoutError(
        "migration task did not finish within Lambda budget; "
        f"task {task_arn} may still be running"
    )


def lambda_handler(event, context):
    physical_id = _physical_id(event)
    try:
        if event["RequestType"] == "Delete":
            _send(event, context, "SUCCESS", physical_id=physical_id)
            return
        new = event["ResourceProperties"].get("MigrationVersion")
        if not new:
            raise ValueError("MigrationVersion property is required")
        if event["RequestType"] == "Update":
            old = event.get("OldResourceProperties", {}).get("MigrationVersion")
            if old == new:
                _send(event, context, "SUCCESS", physical_id=physical_id,
                      reason="MigrationVersion unchanged")
                return
        _run_migration(event)
        _send(event, context, "SUCCESS", physical_id=physical_id)
    except Exception as exc:
        # Print before _send so the original cause survives in CloudWatch even
        # if the response delivery itself fails.
        print(f"MIGRATION FAILED: {exc!r}", flush=True)
        traceback.print_exc()
        try:
            _send(event, context, "FAILED", reason=str(exc),
                  physical_id=physical_id)
        except Exception as send_exc:
            print(f"FAILED to notify CFN: {send_exc!r}", flush=True)
            raise
'''
