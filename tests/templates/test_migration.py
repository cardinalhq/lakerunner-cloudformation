"""Tests for the migration nested-stack template."""

import json

import pytest

from cardinal_cfn.children import migration


@pytest.fixture
def template_dict():
    return json.loads(migration.build().to_json())


def test_required_parameters(template_dict):
    for n in ("InstallIdShort", "InstallIdLong", "ClusterArn", "ClusterName",
              "TaskSecurityGroupId", "ExecutionRoleArn", "PrivateSubnetsCsv",
              "DbEndpoint", "DbSecretArn", "MigrationImage", "MigrationImageDigest"):
        assert n in template_dict["Parameters"], f"missing parameter: {n}"


def test_creates_lambda_and_custom_resource(template_dict):
    types = [r["Type"] for r in template_dict["Resources"].values()]
    assert "AWS::Lambda::Function" in types
    assert any(t == "AWS::CloudFormation::CustomResource" or t == "Custom::MigrationRunner"
               for t in types)


def test_creates_task_definition(template_dict):
    types = [r["Type"] for r in template_dict["Resources"].values()]
    assert "AWS::ECS::TaskDefinition" in types


def test_custom_resource_passes_migration_version(template_dict):
    cr = next(
        r for r in template_dict["Resources"].values()
        if r["Type"] == "AWS::CloudFormation::CustomResource" or r["Type"] == "Custom::MigrationRunner"
    )
    assert "MigrationVersion" in cr["Properties"]
