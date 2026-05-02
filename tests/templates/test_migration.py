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
              "DbEndpoint", "DbSecretArn", "LakerunnerImage"):
        assert n in template_dict["Parameters"], f"missing parameter: {n}"


def test_migration_image_params_are_unified(template_dict):
    """Spec: migrator and lakerunner tasks share LakerunnerImage; the legacy
    MigrationImage / MigrationImageDigest parameters must not reappear."""
    params = template_dict["Parameters"]
    assert "MigrationImage" not in params
    assert "MigrationImageDigest" not in params


def test_creates_lambda_and_custom_resource(template_dict):
    types = [r["Type"] for r in template_dict["Resources"].values()]
    assert "AWS::Lambda::Function" in types
    assert any(t == "AWS::CloudFormation::CustomResource" or t == "Custom::MigrationRunner"
               for t in types)


def test_creates_task_definition(template_dict):
    types = [r["Type"] for r in template_dict["Resources"].values()]
    assert "AWS::ECS::TaskDefinition" in types


def test_custom_resource_uses_lakerunner_image_as_trigger(template_dict):
    """MigrationVersion must be wired to LakerunnerImage so any image change
    reruns the migrator and the migrator can never drift from the tasks."""
    cr = next(
        r for r in template_dict["Resources"].values()
        if r["Type"] == "AWS::CloudFormation::CustomResource" or r["Type"] == "Custom::MigrationRunner"
    )
    mv = cr["Properties"].get("MigrationVersion")
    assert mv == {"Ref": "LakerunnerImage"}, (
        f"MigrationVersion must be Ref(LakerunnerImage); got {mv!r}"
    )


def test_migrator_container_uses_lakerunner_image(template_dict):
    """The migrator container must run the same image as the lakerunner tasks."""
    task_def = next(
        r for r in template_dict["Resources"].values() if r["Type"] == "AWS::ECS::TaskDefinition"
    )
    container = next(
        c for c in task_def["Properties"]["ContainerDefinitions"]
        if c["Name"] == "migrator"
    )
    assert container["Image"] == {"Ref": "LakerunnerImage"}, (
        f"migrator container must use Ref(LakerunnerImage); got {container['Image']!r}"
    )


def test_migrator_task_uses_ssl_required(template_dict):
    """Database connections must always require SSL."""
    task_def = next(
        r for r in template_dict["Resources"].values() if r["Type"] == "AWS::ECS::TaskDefinition"
    )
    container = next(
        c for c in task_def["Properties"]["ContainerDefinitions"]
        if c["Name"] == "migrator"
    )
    env = {e["Name"]: e["Value"] for e in container["Environment"]}
    assert env.get("LRDB_SSLMODE") == "require", (
        f"LRDB_SSLMODE must be 'require'; got {env.get('LRDB_SSLMODE')!r}"
    )
    assert env.get("CONFIGDB_SSLMODE") == "require", (
        f"CONFIGDB_SSLMODE must be 'require'; got {env.get('CONFIGDB_SSLMODE')!r}"
    )


def test_migration_lambda_source_disables_public_ip(template_dict):
    """The migration Lambda runs the migrator ECS task; it must not assign public IPs."""
    fn = next(
        r for r in template_dict["Resources"].values() if r["Type"] == "AWS::Lambda::Function"
    )
    src = fn["Properties"]["Code"]["ZipFile"]
    assert '"assignPublicIp": "DISABLED"' in src, "Lambda must call run_task with assignPublicIp=DISABLED"
