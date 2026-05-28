"""Fixture tests for the JSON parsing inside discover_services."""

import json
import subprocess
import textwrap


# This is the literal python -c fragment used inside discover_services. If
# the shell version changes, update here and assert presence in SCRIPT.
PYSRC = textwrap.dedent(
    """
    import json, sys
    data = json.load(sys.stdin)
    for r in data.get("StackResourceSummaries", []):
        if r["ResourceType"] == "AWS::ECS::Service" and r.get("PhysicalResourceId"):
            print("SERVICE\\t" + r["PhysicalResourceId"])
        elif r["ResourceType"] == "AWS::CloudFormation::Stack" and r.get("PhysicalResourceId"):
            print("STACK\\t" + r["PhysicalResourceId"])
    """
).strip()


def _parse(payload):
    result = subprocess.run(
        ["python3", "-c", PYSRC],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return [line.split("\t", 1) for line in result.stdout.strip().splitlines() if line]


def test_extracts_service_arn():
    parsed = _parse({"StackResourceSummaries": [{
        "ResourceType": "AWS::ECS::Service",
        "PhysicalResourceId": "arn:aws:ecs:us-east-1:111:service/c/svc-A",
    }]})
    assert parsed == [["SERVICE", "arn:aws:ecs:us-east-1:111:service/c/svc-A"]]


def test_extracts_nested_stack():
    parsed = _parse({"StackResourceSummaries": [{
        "ResourceType": "AWS::CloudFormation::Stack",
        "PhysicalResourceId": (
            "arn:aws:cloudformation:us-east-1:111:stack/"
            "cardinal-lakerunner-ServicesQuery-X/Y"
        ),
    }]})
    assert parsed == [[
        "STACK",
        "arn:aws:cloudformation:us-east-1:111:stack/"
        "cardinal-lakerunner-ServicesQuery-X/Y",
    ]]


def test_mixed_resources():
    parsed = _parse({"StackResourceSummaries": [
        {"ResourceType": "AWS::ECS::Service",
         "PhysicalResourceId": "arn:aws:ecs:us-east-1:111:service/c/svc-A"},
        {"ResourceType": "AWS::ECS::TaskDefinition",
         "PhysicalResourceId": "arn:aws:ecs:us-east-1:111:task-definition/family:1"},
        {"ResourceType": "AWS::CloudFormation::Stack",
         "PhysicalResourceId": "arn:aws:cloudformation:us-east-1:111:stack/child/uuid"},
        {"ResourceType": "AWS::ECS::Service",
         "PhysicalResourceId": "arn:aws:ecs:us-east-1:111:service/c/svc-B"},
    ]})
    assert parsed == [
        ["SERVICE", "arn:aws:ecs:us-east-1:111:service/c/svc-A"],
        ["STACK",   "arn:aws:cloudformation:us-east-1:111:stack/child/uuid"],
        ["SERVICE", "arn:aws:ecs:us-east-1:111:service/c/svc-B"],
    ]


def test_skips_resources_without_physical_id():
    parsed = _parse({"StackResourceSummaries": [
        {"ResourceType": "AWS::ECS::Service", "PhysicalResourceId": None},
    ]})
    assert parsed == []


def test_empty_summary():
    assert _parse({"StackResourceSummaries": []}) == []
    assert _parse({}) == []
