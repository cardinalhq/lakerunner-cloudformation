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


def test_outputs_required(template_dict):
    for n in ("BucketName", "BucketArn", "QueueUrl", "QueueArn"):
        assert n in template_dict["Outputs"]
