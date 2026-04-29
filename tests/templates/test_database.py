"""Tests for the database nested-stack template."""

import json

import pytest

from cardinal_cfn.children import database


@pytest.fixture
def template_dict():
    return json.loads(database.build().to_json())


def test_required_parameters(template_dict):
    params = template_dict["Parameters"]
    for name in (
        "InstallIdShort",
        "InstallIdLong",
        "VpcId",
        "TaskSecurityGroupId",
        "PrivateSubnetsCsv",
        "DbInstanceClass",
    ):
        assert name in params, f"missing parameter: {name}"


def test_private_subnets_is_string_csv(template_dict):
    """List parameters must be passed as comma-separated strings to nested stacks."""
    assert template_dict["Parameters"]["PrivateSubnetsCsv"]["Type"] == "String"


def test_creates_rds_instance(template_dict):
    rds = [r for r in template_dict["Resources"].values() if r["Type"] == "AWS::RDS::DBInstance"]
    assert len(rds) == 1


def test_rds_uses_snapshot_policy(template_dict):
    rds = [(k, v) for k, v in template_dict["Resources"].items() if v["Type"] == "AWS::RDS::DBInstance"]
    _, rds_def = rds[0]
    assert rds_def.get("DeletionPolicy") == "Snapshot"
    assert rds_def.get("UpdateReplacePolicy") == "Snapshot"


def test_db_master_secret_retained(template_dict):
    secrets = [(k, v) for k, v in template_dict["Resources"].items() if v["Type"] == "AWS::SecretsManager::Secret"]
    assert len(secrets) >= 1
    _, sec_def = secrets[0]
    assert sec_def.get("DeletionPolicy") == "Retain"


def test_outputs_required_values(template_dict):
    for name in ("DbEndpoint", "DbPort", "DbSecretArn", "DbName"):
        assert name in template_dict["Outputs"], f"missing output: {name}"


def test_no_explicit_db_instance_identifier(template_dict):
    """Spec: never set DBInstanceIdentifier — blocks in-place upgrades."""
    rds_props = next(
        v["Properties"] for v in template_dict["Resources"].values() if v["Type"] == "AWS::RDS::DBInstance"
    )
    assert "DBInstanceIdentifier" not in rds_props


def test_db_instance_has_dedicated_security_group(template_dict):
    """RDS must reference a dedicated SG; relying on the VPC default SG breaks task connectivity."""
    rds_props = next(
        v["Properties"] for v in template_dict["Resources"].values() if v["Type"] == "AWS::RDS::DBInstance"
    )
    sgs = rds_props.get("VPCSecurityGroups")
    assert sgs and len(sgs) == 1, f"DBInstance must declare exactly one VPCSecurityGroups entry; got {sgs!r}"
    assert sgs[0] == {"Ref": "DbSecurityGroup"}, f"DBInstance must reference DbSecurityGroup; got {sgs[0]!r}"


def test_db_security_group_present(template_dict):
    sgs = [r for r in template_dict["Resources"].values() if r["Type"] == "AWS::EC2::SecurityGroup"]
    assert len(sgs) == 1
    assert sgs[0]["Properties"]["VpcId"] == {"Ref": "VpcId"}


def test_db_ingress_only_from_task_sg_on_5432(template_dict):
    """Postgres 5432 ingress must be limited to the ECS task security group."""
    ingresses = [
        r for r in template_dict["Resources"].values()
        if r["Type"] == "AWS::EC2::SecurityGroupIngress"
    ]
    assert len(ingresses) == 1, f"expected exactly one ingress rule; got {len(ingresses)}"
    props = ingresses[0]["Properties"]
    assert props["FromPort"] == 5432
    assert props["ToPort"] == 5432
    assert props["IpProtocol"] == "tcp"
    assert props["SourceSecurityGroupId"] == {"Ref": "TaskSecurityGroupId"}
    assert props["GroupId"] == {"Ref": "DbSecurityGroup"}
