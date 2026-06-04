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


def test_bucket_public_access_block_opt_in(td):
    # PublicAccessBlock is opt-in (default off): wrapped in an Fn::If keyed on
    # AddRawBucketPublicAccessBlock, falling back to NoValue.
    pab = td["Resources"]["RawIngestBucket"]["Properties"][
        "PublicAccessBlockConfiguration"
    ]["Fn::If"]
    assert pab[0] == "AddRawBucketPublicAccessBlock"
    assert pab[1] == {
        "BlockPublicAcls": True,
        "BlockPublicPolicy": True,
        "IgnorePublicAcls": True,
        "RestrictPublicBuckets": True,
    }
    assert pab[2] == {"Ref": "AWS::NoValue"}
    p = td["Parameters"]["ConfigureBucketPublicAccessBlock"]
    assert p["Default"] == "false"
    assert set(p["AllowedValues"]) == {"false", "true"}


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


def test_role_named_cardinal_satellite_access(td):
    """Fixed name lets the lakerunner process tier scope cross-account
    sts:AssumeRole to the cardinal-satellite-access* pattern."""
    assert (
        td["Resources"]["LakerunnerAccessRole"]["Properties"]["RoleName"]
        == "cardinal-satellite-access"
    )


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
    # No s3:PutObject: as of lakerunner v1.40.4 the trace ingest worklane
    # honors the read/write storage-profile split (like logs/metrics) and
    # writes cooked segments to the cooked bucket, not back to this source
    # bucket. The poller still needs DeleteObject for delete_sources cleanup.
    # See the role comment in satellite_infra_base.py.
    stmts = td["Resources"]["LakerunnerAccessRole"]["Properties"]["Policies"][
        0
    ]["PolicyDocument"]["Statement"]
    s3 = next(s for s in stmts if s["Sid"] == "RawBucketReadDelete")
    assert set(s3["Action"]) == {
        "s3:GetObject",
        "s3:DeleteObject",
        "s3:ListBucket",
        "s3:GetBucketLocation",
    }
    assert "s3:PutObject" not in s3["Action"]


def test_role_can_consume_only_its_queue(td):
    stmts = td["Resources"]["LakerunnerAccessRole"]["Properties"]["Policies"][
        0
    ]["PolicyDocument"]["Statement"]
    sqs = next(s for s in stmts if s["Sid"] == "RawQueueConsume")
    assert sqs["Resource"] == {"Fn::GetAtt": ["RawIngestQueue", "Arn"]}
    assert "sqs:ReceiveMessage" in sqs["Action"]
    assert "sqs:DeleteMessage" in sqs["Action"]


def test_outputs_present(td):
    for o in (
        "RawBucketName",
        "RawQueueUrl",
        "RawQueueArn",
        "LakerunnerAccessRoleArn",
        "Region",
    ):
        assert o in td["Outputs"], f"missing output: {o}"


def test_pull_model_no_remote_notification_target(td):
    """Pull invariant: the bucket notifies only its own in-stack queue;
    no resource targets a remote/central queue, SNS topic, or Lambda, and
    there is no outbound push to the Lakerunner account."""
    notif = td["Resources"]["RawIngestBucket"]["Properties"][
        "NotificationConfiguration"
    ]
    assert "LambdaConfigurations" not in notif
    assert "TopicConfigurations" not in notif
    qcfg = notif["QueueConfigurations"]
    assert all(
        c["Queue"] == {"Fn::GetAtt": ["RawIngestQueue", "Arn"]} for c in qcfg
    )
