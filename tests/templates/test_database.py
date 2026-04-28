"""Tests for the database nested-stack template."""

import json

import pytest

from cardinal_cfn.children import database


@pytest.fixture
def template_dict():
    return json.loads(database.build().to_json())


def test_required_parameters(template_dict):
    params = template_dict["Parameters"]
    for name in ("InstallIdShort", "InstallIdLong", "PrivateSubnetsCsv", "DbInstanceClass"):
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
