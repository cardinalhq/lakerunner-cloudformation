"""Regression: every resource that cardinal-infrastructure marks Retain (or
Snapshot) must have a corresponding sweep step in cleanup_script.SCRIPT.

Without this, adding a new Retain'd resource type to the infra stack would
silently leave that resource behind after a cleanup run -- exactly the bug
that left the RdsSecurityGroup blocking `lrdev-vpc` delete the first time
the rewritten cleanup ran end-to-end.

The test does not enforce naming; it just checks that for each Retain'd
resource type, the cleanup script:

1. discovers the physical ID into a known variable, AND
2. references that variable in a delete function

A new Retain'd resource type forces the author to extend
``_RETAINED_TYPE_DISCOVERY`` here, which surfaces the missing sweep in CI.
"""

import json

import pytest

from cardinal_cfn import cardinal_infrastructure
from cardinal_cfn.cleanup_script import SCRIPT


# CloudFormation resource type -> (discovery line marker in SCRIPT,
#                                  sweep function name / call in SCRIPT).
#
# When a new resource type with Retain (or Snapshot) policy lands in
# cardinal-infrastructure, add it here and add the corresponding discovery
# + delete-function in cleanup_script.py.
_RETAINED_TYPE_DISCOVERY = {
    "AWS::S3::Bucket":              ("INGEST_BUCKET=",   "delete_ingest_bucket"),
    "AWS::SecretsManager::Secret":  ("SECRET_IDS=",      "delete_secrets"),
    "AWS::SSM::Parameter":          ("SSM_PARAMS=",      "delete_ssm"),
    "AWS::SQS::Queue":              ("INGEST_QUEUE_URL=", "delete_sqs"),
    "AWS::RDS::DBSubnetGroup":      ("DB_SUBNET_GROUP=", "delete_db_subnet_group"),
    "AWS::EC2::SecurityGroup":      ("RDS_SG_ID=",       "delete_rds_security_group"),
    # DBInstance has DeletionPolicy: Snapshot; CFN handles the snapshot+delete
    # itself during step 3, then step 4 cleans up the leftover snapshots.
    "AWS::RDS::DBInstance":         ("DB_INSTANCE_ID=",  "delete_rds_snapshots"),
}


def _retained_resource_types() -> set[str]:
    td = json.loads(cardinal_infrastructure.build().to_json())
    return {
        r["Type"]
        for r in td["Resources"].values()
        if r.get("DeletionPolicy") in ("Retain", "Snapshot")
    }


def test_every_retained_type_is_in_the_audit_table():
    """If this fails, a new Retain'd resource type was added to
    cardinal-infrastructure without a corresponding sweep entry below.
    Add it to ``_RETAINED_TYPE_DISCOVERY`` and add a discovery + delete
    function in ``cleanup_script.SCRIPT``."""
    declared = _retained_resource_types()
    covered = set(_RETAINED_TYPE_DISCOVERY.keys())
    new = declared - covered
    assert not new, (
        f"new Retain'd resource type(s) in cardinal-infrastructure: {sorted(new)!r}. "
        f"Add a discovery + sweep step in cleanup_script.py and an entry in "
        f"tests/templates/test_cleanup_covers_retained_resources.py."
    )
    stale = covered - declared
    assert not stale, (
        f"entries in _RETAINED_TYPE_DISCOVERY no longer exist in "
        f"cardinal-infrastructure: {sorted(stale)!r}"
    )


@pytest.mark.parametrize("resource_type", sorted(_RETAINED_TYPE_DISCOVERY.keys()))
def test_cleanup_script_discovers_and_sweeps_retained_type(resource_type):
    discovery_marker, sweep_marker = _RETAINED_TYPE_DISCOVERY[resource_type]
    assert discovery_marker in SCRIPT, (
        f"cleanup_script.SCRIPT does not assign discovery variable matching "
        f"{discovery_marker!r} for {resource_type}"
    )
    assert sweep_marker in SCRIPT, (
        f"cleanup_script.SCRIPT does not contain sweep function "
        f"{sweep_marker!r} for {resource_type}"
    )
