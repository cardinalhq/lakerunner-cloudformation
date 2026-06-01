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
              "LakerunnerImage", "DbInitImage",
              "StorageProfilesParamName", "ApiKeysParamName",
              "OrgId", "IngestBucketName"):
        assert n in template_dict["Parameters"], f"missing parameter: {n}"


def test_migrator_seeds_configdb_from_ssm(template_dict):
    """The migrator must read storage-profiles + api-keys YAML from the
    SSM parameters so initializeIfNeededFunc seeds configdb on first install
    (issue #109). YAML is injected as env vars via ECS Secrets resolution
    against the SSM parameter ARN; STORAGE_PROFILE_FILE / API_KEYS_FILE use
    the binary's `env:VAR` indirection."""
    migrator = _containers(template_dict)["migrator"]
    env = {e["Name"]: e["Value"] for e in migrator["Environment"]}
    assert env.get("STORAGE_PROFILE_FILE") == "env:STORAGE_PROFILES_YAML"
    assert env.get("API_KEYS_FILE") == "env:API_KEYS_YAML"

    secrets = {s["Name"]: s["ValueFrom"] for s in migrator["Secrets"]}
    sp_arn = secrets.get("STORAGE_PROFILES_YAML")
    ak_arn = secrets.get("API_KEYS_YAML")
    assert sp_arn is not None and "StorageProfilesParamName" in str(sp_arn), \
        f"STORAGE_PROFILES_YAML secret must reference StorageProfilesParamName: {sp_arn}"
    assert ak_arn is not None and "ApiKeysParamName" in str(ak_arn), \
        f"API_KEYS_YAML secret must reference ApiKeysParamName: {ak_arn}"


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


def test_four_containers_present(template_dict):
    assert set(_containers(template_dict)) == {
        "configdb-init", "migrator", "ensure-storage-profile", "keepalive",
    }


def test_exactly_one_essential_container(template_dict):
    essential = [c["Name"] for c in _task_def(template_dict)["Properties"]["ContainerDefinitions"]
                 if c.get("Essential")]
    assert essential == ["keepalive"], f"only keepalive should be essential; got {essential}"


def test_container_ordering_chain(template_dict):
    """configdb-init -> migrator -> ensure-storage-profile -> keepalive. The
    canonical-profile upsert sits on the keepalive critical path so any
    failure surfaces as a stack rollback, not silent log noise."""
    cs = _containers(template_dict)
    assert "DependsOn" not in cs["configdb-init"]
    assert cs["migrator"]["DependsOn"] == [
        {"ContainerName": "configdb-init", "Condition": "COMPLETE"}
    ]
    assert cs["ensure-storage-profile"]["DependsOn"] == [
        {"ContainerName": "migrator", "Condition": "SUCCESS"}
    ]
    assert cs["keepalive"]["DependsOn"] == [
        {"ContainerName": "ensure-storage-profile", "Condition": "SUCCESS"}
    ]


def test_ensure_storage_profile_is_non_essential(template_dict):
    """It runs the upsert once and exits; keepalive's DependsOn=SUCCESS is
    what gates task stability on a clean exit."""
    assert _containers(template_dict)["ensure-storage-profile"].get("Essential") is False


def test_ensure_storage_profile_upsert_is_idempotent(template_dict):
    """SQL must use ON CONFLICT DO NOTHING on both inserts so re-runs after
    operator edits never clobber existing rows."""
    cmd = " ".join(_containers(template_dict)["ensure-storage-profile"]["Command"])
    cmd_oneline = " ".join(cmd.split())  # collapse all whitespace
    assert "ON CONFLICT (bucket_name) DO NOTHING" in cmd_oneline
    assert (
        "ON CONFLICT (organization_id, bucket_id, instance_num, collector_name) DO NOTHING"
        in cmd_oneline
    )
    # collector_name must match the SSM-driven seed in cardinal_infrastructure
    assert "'lakerunner'" in cmd_oneline


def test_ensure_storage_profile_uses_configdb_credentials(template_dict):
    """Sidecar reads CONFIGDB_* env + secrets and connects with sslmode=require."""
    c = _containers(template_dict)["ensure-storage-profile"]
    env = {e["Name"]: e["Value"] for e in c["Environment"]}
    assert env["CONFIGDB_DBNAME"] == "configdb"
    assert {"Ref": "DbEndpoint"} == env["CONFIGDB_HOST"]
    assert {"Ref": "IngestBucketName"} == env["BUCKET_NAME"]
    assert {"Ref": "OrgId"} == env["ORG_ID"]
    secrets = {s["Name"] for s in c["Secrets"]}
    assert {"CONFIGDB_USER", "CONFIGDB_PASSWORD"} <= secrets
    cmd = " ".join(c["Command"])
    assert "PGSSLMODE=require" in cmd


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


def test_service_has_ondemand_fallback(template_dict):
    # Singleton: spot-preferred but with an on-demand FARGATE fallback so a
    # transient FARGATE_SPOT shortage can't fail the deploy.
    svc = _service(template_dict)["Properties"]
    providers = {s["CapacityProvider"] for s in svc["CapacityProviderStrategy"]}
    assert providers == {"FARGATE_SPOT", "FARGATE"}


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
