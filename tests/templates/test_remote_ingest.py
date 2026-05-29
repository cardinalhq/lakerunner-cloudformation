"""Tests for the cardinal-remote-ingest standalone template."""

import json

import pytest

from cardinal_cfn import remote_ingest


@pytest.fixture
def td():
    return json.loads(remote_ingest.build().to_json())


def test_parameters(td):
    for n in ("RemoteAccountId", "OrgId", "QueueArn", "BucketName",
              "CollectorName", "RemoteOtelRoleNamePattern",
              "IngestBucketLifecycleDays"):
        assert n in td["Parameters"], f"missing parameter: {n}"


def test_remote_account_id_is_12_digits(td):
    assert td["Parameters"]["RemoteAccountId"]["AllowedPattern"] == r"^[0-9]{12}$"


def test_creates_one_bucket_retained_owner_enforced(td):
    buckets = [r for r in td["Resources"].values() if r["Type"] == "AWS::S3::Bucket"]
    assert len(buckets) == 1
    b = buckets[0]
    assert b["DeletionPolicy"] == "Retain"
    assert b["UpdateReplacePolicy"] == "Retain"
    rule = b["Properties"]["OwnershipControls"]["Rules"][0]
    assert rule["ObjectOwnership"] == "BucketOwnerEnforced"
    pab = b["Properties"]["PublicAccessBlockConfiguration"]
    assert all(pab[k] is True for k in (
        "BlockPublicAcls", "BlockPublicPolicy", "IgnorePublicAcls", "RestrictPublicBuckets"
    ))


def test_bucket_notifies_queue(td):
    b = next(r for r in td["Resources"].values() if r["Type"] == "AWS::S3::Bucket")
    qc = b["Properties"]["NotificationConfiguration"]["QueueConfigurations"][0]
    assert qc["Event"] == "s3:ObjectCreated:*"
    assert qc["Queue"] == {"Ref": "QueueArn"}


def test_writer_role_trusts_remote_account_root_with_name_condition(td):
    role = next(r for r in td["Resources"].values() if r["Type"] == "AWS::IAM::Role")
    stmt = role["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]
    assert stmt["Principal"]["AWS"] == {
        "Fn::Sub": "arn:${AWS::Partition}:iam::${RemoteAccountId}:root"
    }
    assert stmt["Action"] == "sts:AssumeRole"
    assert stmt["Condition"]["ArnLike"]["aws:PrincipalArn"] == {
        "Fn::Sub": "arn:${AWS::Partition}:iam::${RemoteAccountId}:role/${RemoteOtelRoleNamePattern}"
    }


def test_writer_role_can_put_to_bucket(td):
    role = next(r for r in td["Resources"].values() if r["Type"] == "AWS::IAM::Role")
    doc = role["Properties"]["Policies"][0]["PolicyDocument"]
    actions = doc["Statement"][0]["Action"]
    assert "s3:PutObject" in actions
    assert "s3:AbortMultipartUpload" in actions


def test_outputs(td):
    for n in ("BucketName", "BucketArn", "BucketRegion", "WriterRoleArn",
              "StorageProfileSnippet"):
        assert n in td["Outputs"], f"missing output: {n}"


def test_storage_profile_snippet_uses_region_and_bucket(td):
    snippet = json.dumps(td["Outputs"]["StorageProfileSnippet"]["Value"])
    assert "organization_id: ${OrgId}" in snippet
    assert "use_path_style: true" in snippet


def test_no_ecs_or_sqs_resources(td):
    """This template only owns the bucket + writer role; the queue lives in infra."""
    for r in td["Resources"].values():
        assert r["Type"] not in ("AWS::SQS::Queue", "AWS::ECS::Service")
