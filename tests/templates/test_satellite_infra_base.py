"""Tests for the cardinal-satellite-infra-base standalone template."""

import json

import pytest

from cardinal_cfn import satellite_infra_base


@pytest.fixture
def td():
    return json.loads(satellite_infra_base.build().to_json())


def test_required_parameters(td):
    for n in (
        "LakerunnerPrincipal",
        "ExternalId",
        "RawBucketName",
        "RawBucketLifecycleDays",
    ):
        assert n in td["Parameters"], f"missing parameter: {n}"


def test_description_mentions_pull_model(td):
    desc = td["Description"].lower()
    assert "pull" in desc
    assert "nothing pushes" in desc


def test_queue_is_delete_policy(td):
    q = td["Resources"]["RawIngestQueue"]
    assert q["DeletionPolicy"] == "Delete"
    assert q["UpdateReplacePolicy"] == "Delete"


def test_queue_policy_allows_s3_same_account_only(td):
    stmt = td["Resources"]["RawIngestQueuePolicy"]["Properties"][
        "PolicyDocument"
    ]["Statement"][0]
    assert stmt["Principal"] == {"Service": "s3.amazonaws.com"}
    assert "sqs:SendMessage" in stmt["Action"]
    assert stmt["Condition"]["StringEquals"]["aws:SourceAccount"] == {
        "Ref": "AWS::AccountId"
    }
    assert "aws:SourceArn" in stmt["Condition"]["ArnLike"]


def test_bucket_is_delete_policy(td):
    b = td["Resources"]["RawIngestBucket"]
    assert b["DeletionPolicy"] == "Delete"
    assert b["UpdateReplacePolicy"] == "Delete"


def test_bucket_blocks_public_access(td):
    pab = td["Resources"]["RawIngestBucket"]["Properties"][
        "PublicAccessBlockConfiguration"
    ]
    assert pab == {
        "BlockPublicAcls": True,
        "BlockPublicPolicy": True,
        "IgnorePublicAcls": True,
        "RestrictPublicBuckets": True,
    }


def test_bucket_is_encrypted(td):
    enc = td["Resources"]["RawIngestBucket"]["Properties"]["BucketEncryption"]
    rule = enc["ServerSideEncryptionConfiguration"][0]
    assert rule["ServerSideEncryptionByDefault"]["SSEAlgorithm"] == "AES256"


def test_bucket_notifies_its_own_queue(td):
    qcfg = td["Resources"]["RawIngestBucket"]["Properties"][
        "NotificationConfiguration"
    ]["QueueConfigurations"][0]
    assert qcfg["Event"] == "s3:ObjectCreated:*"
    assert qcfg["Queue"] == {"Fn::GetAtt": ["RawIngestQueue", "Arn"]}


def test_bucket_depends_on_queue_policy(td):
    assert td["Resources"]["RawIngestBucket"]["DependsOn"] == "RawIngestQueuePolicy"


def test_bucket_lifecycle_uses_parameter(td):
    rule = td["Resources"]["RawIngestBucket"]["Properties"][
        "LifecycleConfiguration"
    ]["Rules"][0]
    assert rule["ExpirationInDays"] == {"Ref": "RawBucketLifecycleDays"}
    assert rule["AbortIncompleteMultipartUpload"]["DaysAfterInitiation"] == 1


def test_role_trusts_lakerunner_principal(td):
    trust = td["Resources"]["LakerunnerAccessRole"]["Properties"][
        "AssumeRolePolicyDocument"
    ]["Statement"][0]
    assert trust["Principal"] == {"AWS": {"Ref": "LakerunnerPrincipal"}}
    assert trust["Action"] == "sts:AssumeRole"


def test_role_external_id_is_conditional(td):
    trust = td["Resources"]["LakerunnerAccessRole"]["Properties"][
        "AssumeRolePolicyDocument"
    ]["Statement"][0]
    assert trust["Condition"] == {
        "Fn::If": [
            "HasExternalId",
            {"StringEquals": {"sts:ExternalId": {"Ref": "ExternalId"}}},
            {"Ref": "AWS::NoValue"},
        ]
    }


def test_role_can_read_and_delete_raw(td):
    stmts = td["Resources"]["LakerunnerAccessRole"]["Properties"]["Policies"][
        0
    ]["PolicyDocument"]["Statement"]
    s3 = next(s for s in stmts if s["Sid"] == "RawBucketReadDelete")
    assert {"s3:GetObject", "s3:DeleteObject", "s3:ListBucket"}.issubset(
        set(s3["Action"])
    )


def test_role_can_consume_only_its_queue(td):
    stmts = td["Resources"]["LakerunnerAccessRole"]["Properties"]["Policies"][
        0
    ]["PolicyDocument"]["Statement"]
    sqs = next(s for s in stmts if s["Sid"] == "RawQueueConsume")
    assert sqs["Resource"] == {"Fn::GetAtt": ["RawIngestQueue", "Arn"]}
    assert "sqs:ReceiveMessage" in sqs["Action"]
    assert "sqs:DeleteMessage" in sqs["Action"]
