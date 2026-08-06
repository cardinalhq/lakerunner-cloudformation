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


def _notification_prefixes(td):
    return [
        c["Filter"]["S3Key"]["Rules"][0]["Value"]
        for c in td["Resources"]["RawIngestBucket"]["Properties"][
            "NotificationConfiguration"
        ]["QueueConfigurations"]
    ]


def test_notifications_are_prefix_filtered(td):
    """Every producer prefix is notified, and only via a prefix rule -- an
    unfiltered configuration would hand Firehose's error-output blobs to the
    poller, which cannot parse them."""
    cfgs = td["Resources"]["RawIngestBucket"]["Properties"][
        "NotificationConfiguration"
    ]["QueueConfigurations"]
    assert len(cfgs) == len(satellite_infra_base.INGEST_PREFIXES)
    for c in cfgs:
        assert c["Filter"]["S3Key"]["Rules"][0]["Name"] == "prefix"
    assert _notification_prefixes(td) == list(
        satellite_infra_base.INGEST_PREFIXES
    )


def test_ingest_prefixes_cover_known_producers(td):
    prefixes = _notification_prefixes(td)
    # otel-collector (satellite-services) and the CloudWatch metric stream
    # (satellite-cwmetrics) both write into this one bucket.
    assert "otel-raw/" in prefixes
    assert "cwmetrics-raw/" in prefixes


def test_ingest_prefixes_do_not_overlap(td):
    """S3 rejects overlapping prefix filters for the same event type."""
    prefixes = _notification_prefixes(td)
    for a in prefixes:
        for b in prefixes:
            if a is not b:
                assert not a.startswith(b), f"{a!r} overlaps {b!r}"


def test_firehose_error_prefix_is_not_notified(td):
    """Error output lands under cwmetrics-errors/, which must not match any
    notification prefix."""
    for prefix in _notification_prefixes(td):
        assert not "cwmetrics-errors/".startswith(prefix)


def test_queue_has_dlq_redrive(td):
    redrive = td["Resources"]["RawIngestQueue"]["Properties"]["RedrivePolicy"]
    assert redrive["deadLetterTargetArn"] == {
        "Fn::GetAtt": ["RawIngestDlq", "Arn"]
    }
    assert redrive["maxReceiveCount"] == satellite_infra_base.MAX_RECEIVE_COUNT


def test_dlq_retains_long_enough_to_inspect(td):
    dlq = td["Resources"]["RawIngestDlq"]
    assert dlq["Properties"]["MessageRetentionPeriod"] == 1209600
    assert dlq["DeletionPolicy"] == "Delete"


def test_dlq_has_no_redrive_allow_policy(td):
    """Pinning sourceQueueArns would make the two queues reference each other
    and CloudFormation would reject the cycle."""
    assert "RedriveAllowPolicy" not in td["Resources"]["RawIngestDlq"]["Properties"]


def test_visibility_timeout_exceeds_processing_budget(td):
    """A too-short visibility timeout redelivers in-flight messages and burns
    redrive attempts, DLQ-ing healthy work."""
    vt = td["Resources"]["RawIngestQueue"]["Properties"]["VisibilityTimeout"]
    assert vt == satellite_infra_base.QUEUE_VISIBILITY_TIMEOUT
    assert vt > 30


def test_queues_are_encrypted(td):
    for name in ("RawIngestQueue", "RawIngestDlq"):
        assert td["Resources"][name]["Properties"]["SqsManagedSseEnabled"] is True


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
    sts:AssumeRole to the cardinal-satellite-access* pattern. NameSuffix
    (blank default) appends -<suffix> while still matching that pattern;
    blank resolves to the original name so existing stacks never change."""
    assert td["Resources"]["LakerunnerAccessRole"]["Properties"]["RoleName"] == {
        "Fn::If": [
            "HasNameSuffix",
            {"Fn::Sub": "cardinal-satellite-access-${NameSuffix}"},
            "cardinal-satellite-access",
        ]
    }


def test_name_suffix_blank_default_and_bounded(td):
    p = td["Parameters"]["NameSuffix"]
    assert p["Default"] == ""
    # Max 16 chars so the default bucket name stays within S3's 63-char cap
    # (18 prefix + 12 account + 1 + 14 longest region + 1 + 16 = 62).
    assert p["AllowedPattern"] == r"^$|^[a-z0-9]([a-z0-9-]{0,14}[a-z0-9])?$"


def test_default_bucket_name_gets_suffix(td):
    name = td["Resources"]["RawIngestBucket"]["Properties"]["BucketName"]
    assert name == {
        "Fn::If": [
            "UseDefaultBucketName",
            {
                "Fn::If": [
                    "HasNameSuffix",
                    {
                        "Fn::Sub": "cardinal-otel-raw-${AWS::AccountId}"
                        "-${AWS::Region}-${NameSuffix}"
                    },
                    {"Fn::Sub": "cardinal-otel-raw-${AWS::AccountId}-${AWS::Region}"},
                ]
            },
            {"Ref": "RawBucketName"},
        ]
    }


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
        "RawDlqArn",
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
