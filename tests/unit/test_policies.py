"""Tests for lifecycle policy helpers."""

import pytest

from cardinal_cfn.policies import POLICIES, apply_policy


class FakeResource:
    """Stand-in for a troposphere resource to exercise apply_policy."""

    def __init__(self):
        self.DeletionPolicy = None
        self.UpdateReplacePolicy = None


def test_policies_table_covers_data_resources():
    expected_kinds = {
        "rds-instance",
        "s3-ingest-bucket",
        "db-master-secret",
        "internal-service-keys-secret",
        "admin-api-key-secret",
        "sqs-ingest-queue",
        "alb",
        "log-group",
    }
    assert expected_kinds.issubset(POLICIES.keys())


def test_apply_policy_for_rds_uses_snapshot():
    r = FakeResource()
    apply_policy(r, "rds-instance")
    assert r.DeletionPolicy == "Snapshot"
    assert r.UpdateReplacePolicy == "Snapshot"


def test_apply_policy_for_ingest_bucket_retains():
    r = FakeResource()
    apply_policy(r, "s3-ingest-bucket")
    assert r.DeletionPolicy == "Retain"
    assert r.UpdateReplacePolicy == "Retain"


def test_apply_policy_for_internal_keys_deletes():
    r = FakeResource()
    apply_policy(r, "internal-service-keys-secret")
    assert r.DeletionPolicy == "Delete"
    assert r.UpdateReplacePolicy == "Delete"


def test_apply_policy_unknown_kind_raises():
    r = FakeResource()
    with pytest.raises(ValueError):
        apply_policy(r, "made-up-kind")
