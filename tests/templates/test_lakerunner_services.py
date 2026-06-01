"""Tests for the standalone cardinal-lakerunner-services root template."""

import json

import pytest

from cardinal_cfn import lakerunner_services


@pytest.fixture
def td():
    return json.loads(lakerunner_services.build().to_json())


def _nested_logical_ids(td):
    return {k for k, v in td["Resources"].items()
            if v["Type"] == "AWS::CloudFormation::Stack"}


def test_no_security_child(td):
    """No nested stack should be the Security child."""
    assert "Security" not in _nested_logical_ids(td)
    for r in td["Resources"].values():
        if r["Type"] != "AWS::CloudFormation::Stack":
            continue
        url = json.dumps(r["Properties"]["TemplateURL"])
        assert "security.yaml" not in url, "security child must be removed"


def test_creates_no_iam_or_sg(td):
    """The template's own Resources create no IAM roles and no security groups.

    Everything role/SG-shaped arrives as a parameter; the only non-nested-stack
    resource is the Cloud Map private DNS namespace.
    """
    for k, r in td["Resources"].items():
        assert r["Type"] != "AWS::IAM::Role", f"{k} is an IAM role"
        assert r["Type"] != "AWS::EC2::SecurityGroup", f"{k} is a security group"


def test_role_and_sg_params_present(td):
    sg_params = [
        "AlbSecurityGroupId",
        "MigrationSecurityGroupId",
        "QuerySecurityGroupId",
        "ProcessSecurityGroupId",
        "ControlSecurityGroupId",
        "OtelSecurityGroupId",
        "MaestroSecurityGroupId",
    ]
    role_params = [
        "ExecutionRoleArn",
        "MigrationRoleArn",
        "QueryRoleArn",
        "ProcessRoleArn",
        "ControlRoleArn",
        "OtelRoleArn",
        "MaestroRoleArn",
    ]
    for n in sg_params:
        assert n in td["Parameters"], f"missing SG param: {n}"
        assert td["Parameters"][n]["Type"] == "AWS::EC2::SecurityGroup::Id"
    for n in role_params:
        assert n in td["Parameters"], f"missing role param: {n}"
        assert td["Parameters"][n]["Type"] == "String"


def test_data_plane_params(td):
    params = td["Parameters"]
    for n in (
        "CookedBucketName",
        "DbEndpoint",
        "DbPort",
        "DbName",
        "DbMasterSecretArn",
        "LicenseSecretArn",
        "AdminKeySecretArn",
        "StorageProfilesParamName",
        "ApiKeysParamName",
    ):
        assert n in params, f"missing data-plane param: {n}"
    # Removed/renamed sources
    assert "IngestBucketName" not in params
    assert "RdsSecurityGroupId" not in params
    # SQS is optional in v1.
    for n in ("QueueUrl", "QueueArn"):
        assert n in params, f"missing queue param: {n}"
        assert params[n]["Default"] == ""


def test_children_present(td):
    nested = _nested_logical_ids(td)
    expected = {
        "Cert",
        "Alb",
        "Migration",
        "Query",
        "Process",
        "Control",
        "Otel",
        "Maestro",
    }
    assert nested == expected
    assert "Security" not in nested


def test_cooked_bucket_wired_to_children(td):
    """At least one child receives Ref CookedBucketName for its bucket param."""
    ref = {"Ref": "CookedBucketName"}
    migration = td["Resources"]["Migration"]["Properties"]["Parameters"]
    assert migration["IngestBucketName"] == ref
    otel = td["Resources"]["Otel"]["Properties"]["Parameters"]
    assert otel["BucketName"] == ref
