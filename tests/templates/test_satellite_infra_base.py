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
