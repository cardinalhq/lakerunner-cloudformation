"""Tests for the migration nested-stack template (no-Lambda ECS-service form)."""

import json

import pytest

from cardinal_cfn.children import migration


@pytest.fixture
def template_dict():
    return json.loads(migration.build().to_json())


def _types(td):
    return [r["Type"] for r in td["Resources"].values()]


def _task_def(td):
    return next(r for r in td["Resources"].values()
                if r["Type"] == "AWS::ECS::TaskDefinition")


def _containers(td):
    return {c["Name"]: c for c in _task_def(td)["Properties"]["ContainerDefinitions"]}


def _service(td):
    return next(r for r in td["Resources"].values()
                if r["Type"] == "AWS::ECS::Service")


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------


def test_required_parameters(template_dict):
    for n in ("InstallIdShort", "InstallIdLong", "ClusterArn", "ClusterName",
              "TaskSecurityGroupId", "ExecutionRoleArn", "TaskRoleArn",
              "PrivateSubnetsCsv", "DbEndpoint", "DbSecretArn",
              "LakerunnerImage", "DbInitImage"):
        assert n in template_dict["Parameters"], f"missing parameter: {n}"


def test_no_org_content_params(template_dict):
    """Org content is Maestro-owned; the migration child takes no SSM-seed or
    org/bucket params."""
    params = template_dict["Parameters"]
    for gone in ("StorageProfilesParamName", "ApiKeysParamName",
                 "OrgId", "IngestBucketName"):
        assert gone not in params, f"{gone} should be removed from migration child"


def test_migrator_does_not_import_ssm(template_dict):
    """The migrator seeds nothing from SSM; Maestro provisions org content via
    /api/v1/provision. With empty configdb the binary's initializeIfNeededFunc
    is a no-op."""
    migrator = _containers(template_dict)["migrator"]
    env = {e["Name"]: e["Value"] for e in migrator["Environment"]}
    assert "STORAGE_PROFILE_FILE" not in env
    assert "API_KEYS_FILE" not in env
    secrets = {s["Name"] for s in migrator.get("Secrets", [])}
    assert "STORAGE_PROFILES_YAML" not in secrets
    assert "API_KEYS_YAML" not in secrets


def test_no_ensure_storage_profile_container(template_dict):
    """The canonical-profile upsert sidecar is gone; Maestro owns the row."""
    assert "ensure-storage-profile" not in _containers(template_dict)


def test_migration_image_params_are_unified(template_dict):
    """migrator and lakerunner tasks share LakerunnerImage; the legacy
    MigrationImage / MigrationImageDigest parameters must not reappear."""
    params = template_dict["Parameters"]
    assert "MigrationImage" not in params
    assert "MigrationImageDigest" not in params


def test_no_migration_lambda_role_parameter(template_dict):
    """The Lambda is gone, so its role parameter must be too."""
    assert "MigrationLambdaRoleArn" not in template_dict["Parameters"]


# ---------------------------------------------------------------------------
# No Lambda, no custom resource
# ---------------------------------------------------------------------------


def test_no_lambda_or_custom_resource(template_dict):
    types = _types(template_dict)
    assert "AWS::Lambda::Function" not in types, "migration must not create a Lambda"
    assert not any(t == "AWS::CloudFormation::CustomResource" or t.startswith("Custom::")
                   for t in types), "migration must not use a custom resource"


# ---------------------------------------------------------------------------
# Task definition: configdb-init -> migrator -> keepalive
# ---------------------------------------------------------------------------


def test_creates_task_definition(template_dict):
    assert "AWS::ECS::TaskDefinition" in _types(template_dict)


def test_three_containers_present(template_dict):
    assert set(_containers(template_dict)) == {
        "configdb-init", "migrator", "keepalive",
    }


def test_exactly_one_essential_container(template_dict):
    essential = [c["Name"] for c in _task_def(template_dict)["Properties"]["ContainerDefinitions"]
                 if c.get("Essential")]
    assert essential == ["keepalive"], f"only keepalive should be essential; got {essential}"


def test_container_ordering_chain(template_dict):
    """configdb-init -> migrator -> keepalive. keepalive (the only essential
    container) waits on migrator=SUCCESS, so the task -- and thus the nested
    stack -- only stabilizes after migrations succeed."""
    cs = _containers(template_dict)
    assert "DependsOn" not in cs["configdb-init"]
    assert cs["migrator"]["DependsOn"] == [
        {"ContainerName": "configdb-init", "Condition": "COMPLETE"}
    ]
    assert cs["keepalive"]["DependsOn"] == [
        {"ContainerName": "migrator", "Condition": "SUCCESS"}
    ]


def test_migrator_container_uses_lakerunner_image(template_dict):
    assert _containers(template_dict)["migrator"]["Image"] == {"Ref": "LakerunnerImage"}


def test_migrator_container_is_non_essential(template_dict):
    """It runs to completion and exits; the task keeps running via keepalive."""
    assert _containers(template_dict)["migrator"].get("Essential") is False


def test_keepalive_idles(template_dict):
    keepalive = _containers(template_dict)["keepalive"]
    assert keepalive["Image"] == {"Ref": "DbInitImage"}
    assert keepalive.get("Essential") is True
    assert any("sleep" in part for part in keepalive["Command"]), keepalive["Command"]


def test_migrator_task_uses_ssl_required(template_dict):
    env = {e["Name"]: e["Value"] for e in _containers(template_dict)["migrator"]["Environment"]}
    assert env.get("LRDB_SSLMODE") == "require"
    assert env.get("CONFIGDB_SSLMODE") == "require"


# ---------------------------------------------------------------------------
# Migration ECS service
# ---------------------------------------------------------------------------


def test_creates_ecs_service(template_dict):
    assert "AWS::ECS::Service" in _types(template_dict)


def test_service_runs_one_fargate_task_no_lb(template_dict):
    svc = _service(template_dict)["Properties"]
    assert "LaunchType" not in svc
    assert svc["DesiredCount"] == 1
    assert svc["TaskDefinition"] == {"Ref": "MigratorTaskDef"}
    assert "LoadBalancers" not in svc
    assert "ServiceRegistries" not in svc


def test_service_is_pure_on_demand(template_dict):
    # Singleton: pure on-demand FARGATE so the migrator's one task always places
    # during a rolling deploy. No FARGATE_SPOT (it can't guarantee placement).
    svc = _service(template_dict)["Properties"]
    items = svc["CapacityProviderStrategy"]
    assert items == [{"CapacityProvider": "FARGATE", "Weight": 1}]
    assert all("Base" not in i for i in items)


def test_service_disables_public_ip(template_dict):
    awsvpc = _service(template_dict)["Properties"]["NetworkConfiguration"]["AwsvpcConfiguration"]
    assert awsvpc["AssignPublicIp"] == "DISABLED"
    assert awsvpc["SecurityGroups"] == [{"Ref": "TaskSecurityGroupId"}]


def test_service_has_circuit_breaker(template_dict):
    dc = _service(template_dict)["Properties"]["DeploymentConfiguration"]
    assert dc["DeploymentCircuitBreaker"] == {"Enable": True, "Rollback": True}


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def test_output_is_migration_service_arn(template_dict):
    outs = template_dict["Outputs"]
    assert set(outs) == {"MigrationServiceArn"}
    assert outs["MigrationServiceArn"]["Value"] == {"Ref": "MigratorService"}
    # the old custom-resource output name must not linger
    assert "MigrationCustomResourceRef" not in outs
