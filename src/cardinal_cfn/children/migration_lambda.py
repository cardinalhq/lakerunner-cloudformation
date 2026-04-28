"""Source code for the migration custom-resource Lambda.

Embedded in migration.yaml as Code.ZipFile. Behavior contract:
- PhysicalResourceId is stable: cardinal-migration-<InstallIdLong>.
- Trigger property MigrationVersion: image digest. Tag is rejected.
- Create runs the migrator ECS task; success on exit-code 0.
- Update reruns the migrator only if MigrationVersion changed.
- Delete is a no-op.
"""

SOURCE = '''\
import json
import os
import time
import urllib.request

import boto3


ecs = boto3.client("ecs")


def _send(event, context, status, reason="", physical_id=None, data=None):
    body = json.dumps({
        "Status": status,
        "Reason": reason or f"see CloudWatch log {context.log_stream_name}",
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
    if not response.get("tasks"):
        raise RuntimeError("migration task failed to launch: " + json.dumps(response, default=str))
    task_arn = response["tasks"][0]["taskArn"]

    deadline = time.time() + 50 * 60
    while time.time() < deadline:
        desc = ecs.describe_tasks(cluster=cluster_arn, tasks=[task_arn])
        if not desc["tasks"]:
            raise RuntimeError("migration task disappeared")
        task = desc["tasks"][0]
        if task["lastStatus"] == "STOPPED":
            for c in task.get("containers", []):
                if c.get("exitCode") not in (0, None):
                    raise RuntimeError(f"migration container exit {c.get(\'exitCode\')}: {c.get(\'reason\')}")
            return
        time.sleep(15)
    raise TimeoutError("migration task did not finish within 50 minutes")


def lambda_handler(event, context):
    physical_id = _physical_id(event)
    try:
        if event["RequestType"] == "Delete":
            _send(event, context, "SUCCESS", physical_id=physical_id)
            return
        if event["RequestType"] == "Update":
            old = event.get("OldResourceProperties", {}).get("MigrationVersion")
            new = event["ResourceProperties"]["MigrationVersion"]
            if old == new:
                _send(event, context, "SUCCESS", physical_id=physical_id, reason="MigrationVersion unchanged")
                return
        _run_migration(event)
        _send(event, context, "SUCCESS", physical_id=physical_id)
    except Exception as exc:
        _send(event, context, "FAILED", reason=str(exc), physical_id=physical_id)
'''
