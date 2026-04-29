"""Tests for the storage nested-stack template."""

import json

import pytest

from cardinal_cfn.children import storage


@pytest.fixture
def template_dict():
    return json.loads(storage.build().to_json())


def test_required_parameters(template_dict):
    for name in ("InstallIdShort", "InstallIdLong"):
        assert name in template_dict["Parameters"]


def test_creates_s3_bucket_and_queue(template_dict):
    resources = template_dict["Resources"]
    buckets = [r for r in resources.values() if r["Type"] == "AWS::S3::Bucket"]
    queues = [r for r in resources.values() if r["Type"] == "AWS::SQS::Queue"]
    assert len(buckets) == 1
    assert len(queues) == 1


def test_bucket_name_uses_install_id_long(template_dict):
    bucket = next(r for r in template_dict["Resources"].values() if r["Type"] == "AWS::S3::Bucket")
    name = bucket["Properties"]["BucketName"]
    assert "InstallIdLong" in json.dumps(name)
    assert "AccountId" in json.dumps(name)
    assert "Region" in json.dumps(name)


def test_bucket_retained_on_delete(template_dict):
    bucket_def = next(
        v for v in template_dict["Resources"].values() if v["Type"] == "AWS::S3::Bucket"
    )
    assert bucket_def.get("DeletionPolicy") == "Retain"
    assert bucket_def.get("UpdateReplacePolicy") == "Retain"


def test_bucket_has_three_notification_prefixes(template_dict):
    bucket_props = next(
        v["Properties"] for v in template_dict["Resources"].values() if v["Type"] == "AWS::S3::Bucket"
    )
    notif = bucket_props["NotificationConfiguration"]["QueueConfigurations"]
    prefixes = []
    for cfg in notif:
        rules = cfg["Filter"]["S3Key"]["Rules"]
        prefixes.extend([r["Value"] for r in rules if r["Name"] == "prefix"])
    assert set(prefixes) == {"otel-raw/", "logs-raw/", "metrics-raw/"}


def test_bucket_depends_on_queue_policy(template_dict):
    """S3 validates queue write permissions when the bucket is created.

    Without a DependsOn on the queue policy, CFN sometimes builds the bucket
    first and fails with 'Unable to validate destination configurations'.
    """
    bucket = next(
        v for v in template_dict["Resources"].values() if v["Type"] == "AWS::S3::Bucket"
    )
    depends_on = bucket.get("DependsOn")
    if isinstance(depends_on, str):
        depends_on = [depends_on]
    assert depends_on and "IngestQueuePolicy" in depends_on, (
        f"IngestBucket must DependsOn IngestQueuePolicy; got {depends_on!r}"
    )


def test_bucket_notification_targets_rendered_queue(template_dict):
    """Each NotificationConfiguration entry must reference the queue we just created."""
    bucket_props = next(
        v["Properties"] for v in template_dict["Resources"].values() if v["Type"] == "AWS::S3::Bucket"
    )
    queue_logical_ids = [
        k for k, v in template_dict["Resources"].items() if v["Type"] == "AWS::SQS::Queue"
    ]
    assert len(queue_logical_ids) == 1
    queue_arn = {"Fn::GetAtt": [queue_logical_ids[0], "Arn"]}
    for cfg in bucket_props["NotificationConfiguration"]["QueueConfigurations"]:
        assert cfg["Queue"] == queue_arn, (
            f"queue config must target rendered queue ARN, got {cfg['Queue']!r}"
        )


def test_queue_policy_allows_only_s3_with_account_condition(template_dict):
    """The SQS policy must scope the s3:SendMessage permission to this account."""
    policies = [
        r for r in template_dict["Resources"].values() if r["Type"] == "AWS::SQS::QueuePolicy"
    ]
    assert len(policies) == 1
    statement = policies[0]["Properties"]["PolicyDocument"]["Statement"][0]
    assert statement["Effect"] == "Allow"
    assert statement["Principal"] == {"Service": "s3.amazonaws.com"}
    assert "sqs:SendMessage" in statement["Action"]
    cond = statement.get("Condition", {})
    assert cond.get("StringEquals", {}).get("aws:SourceAccount") == {"Ref": "AWS::AccountId"}, (
        "missing aws:SourceAccount condition — would let any account's S3 publish"
    )


def test_outputs_required(template_dict):
    for n in ("BucketName", "BucketArn", "QueueUrl", "QueueArn"):
        assert n in template_dict["Outputs"]
