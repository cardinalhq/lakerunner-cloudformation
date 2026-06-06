"""Tests for the maestro nested stack."""

import json

import pytest

from cardinal_cfn.children import maestro


@pytest.fixture
def td():
    return json.loads(maestro.build().to_json())


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------


def test_required_cross_stack_parameters(td):
    for n in (
        "InstallIdShort",
        "InstallIdLong",
        "ClusterArn",
        "TaskSecurityGroupId",
        "ExecutionRoleArn",
        "TaskRoleArn",
        "PrivateSubnetsCsv",
        "VpcId",
        "HttpsListenerArn",
        "AlbDnsName",
        "ServiceNamespaceName",
        "DbEndpoint",
        "DbPort",
        "DbSecretArn",
        "LicenseSecretArn",
        "MigrationComplete",
    ):
        assert n in td["Parameters"], f"missing parameter: {n}"


def test_no_storage_profile_or_api_keys_params(td):
    """The migrator seeds configdb from these SSM parameters; maestro reads
    profiles from configdb at runtime, so these are not threaded here."""
    for n in ("ApiKeysParamName", "StorageProfilesParamName"):
        assert n not in td["Parameters"], f"unexpected parameter: {n}"


def test_image_override_parameters(td):
    for n in ("MaestroImage", "DexImage", "DbInitImage"):
        assert n in td["Parameters"], f"missing parameter: {n}"


def test_tunable_parameters(td):
    for n in ("MaestroTaskCpu", "MaestroTaskMemory", "DexClientId"):
        assert n in td["Parameters"], f"missing parameter: {n}"


def test_dex_client_id_default(td):
    assert td["Parameters"]["DexClientId"]["Default"] == "maestro-ui"


# ---------------------------------------------------------------------------
# Core resources
# ---------------------------------------------------------------------------


def test_creates_one_ecs_service(td):
    services = [r for r in td["Resources"].values() if r["Type"] == "AWS::ECS::Service"]
    assert len(services) == 1


def test_creates_one_task_definition(td):
    task_defs = [r for r in td["Resources"].values() if r["Type"] == "AWS::ECS::TaskDefinition"]
    assert len(task_defs) == 1


def test_task_definition_has_expected_containers(td):
    task_def = next(
        r for r in td["Resources"].values() if r["Type"] == "AWS::ECS::TaskDefinition"
    )
    containers = task_def["Properties"]["ContainerDefinitions"]
    names = {c["Name"] for c in containers}
    assert names == {"db-init", "mcp-gateway", "wait-for-mcp", "maestro", "dex"}


def test_db_init_is_not_essential(td):
    task_def = next(
        r for r in td["Resources"].values() if r["Type"] == "AWS::ECS::TaskDefinition"
    )
    db_init = next(
        c for c in task_def["Properties"]["ContainerDefinitions"] if c["Name"] == "db-init"
    )
    assert db_init["Essential"] is False


def test_maestro_and_dex_are_essential(td):
    task_def = next(
        r for r in td["Resources"].values() if r["Type"] == "AWS::ECS::TaskDefinition"
    )
    for name in ("maestro", "dex"):
        container = next(
            c for c in task_def["Properties"]["ContainerDefinitions"] if c["Name"] == name
        )
        assert container["Essential"] is True, f"{name} must be essential"


def test_maestro_depends_on_db_init(td):
    task_def = next(
        r for r in td["Resources"].values() if r["Type"] == "AWS::ECS::TaskDefinition"
    )
    maestro_container = next(
        c for c in task_def["Properties"]["ContainerDefinitions"] if c["Name"] == "maestro"
    )
    deps = maestro_container.get("DependsOn", [])
    assert {"ContainerName": "db-init", "Condition": "SUCCESS"} in deps


def test_three_log_groups(td):
    log_groups = [r for r in td["Resources"].values() if r["Type"] == "AWS::Logs::LogGroup"]
    assert len(log_groups) == 3
    for lg in log_groups:
        assert lg["Properties"]["RetentionInDays"] == 14
    names = {json.dumps(lg["Properties"].get("LogGroupName")) for lg in log_groups}
    assert len(names) == 3, "log groups must have distinct names"


def test_no_internally_managed_iam_task_role(td):
    """Phase 2: maestro shares the customer-supplied TaskRoleArn parameter."""
    roles = [r for r in td["Resources"].values() if r["Type"] == "AWS::IAM::Role"]
    assert len(roles) == 0


def test_task_definition_uses_task_role_param(td):
    task_def = next(
        r for r in td["Resources"].values() if r["Type"] == "AWS::ECS::TaskDefinition"
    )
    assert task_def["Properties"]["TaskRoleArn"] == {"Ref": "TaskRoleArn"}


# ---------------------------------------------------------------------------
# ALB plumbing
# ---------------------------------------------------------------------------


def test_two_target_groups(td):
    tgs = [
        r for r in td["Resources"].values()
        if r["Type"] == "AWS::ElasticLoadBalancingV2::TargetGroup"
    ]
    assert len(tgs) == 2


def test_two_listener_rules_with_correct_priorities(td):
    rules = [
        r for r in td["Resources"].values()
        if r["Type"] == "AWS::ElasticLoadBalancingV2::ListenerRule"
    ]
    priorities = sorted(r["Properties"]["Priority"] for r in rules)
    assert priorities == [210, 49999]


def test_listener_rule_path_patterns(td):
    rules = [
        r for r in td["Resources"].values()
        if r["Type"] == "AWS::ElasticLoadBalancingV2::ListenerRule"
    ]
    by_priority = {r["Properties"]["Priority"]: r for r in rules}

    maestro_conds = by_priority[49999]["Properties"]["Conditions"]
    found_maestro = False
    for c in maestro_conds:
        if c.get("Field") == "path-pattern":
            assert c["PathPatternConfig"]["Values"] == ["/*"]
            found_maestro = True
    assert found_maestro

    dex_conds = by_priority[210]["Properties"]["Conditions"]
    found_dex = False
    for c in dex_conds:
        if c.get("Field") == "path-pattern":
            assert c["PathPatternConfig"]["Values"] == ["/dex/*"]
            found_dex = True
    assert found_dex


def test_ecs_service_has_two_load_balancers(td):
    svc = next(r for r in td["Resources"].values() if r["Type"] == "AWS::ECS::Service")
    lbs = svc["Properties"].get("LoadBalancers")
    assert isinstance(lbs, list)
    assert len(lbs) == 2
    names = {lb["ContainerName"] for lb in lbs}
    assert names == {"maestro", "dex"}
    ports = {lb["ContainerPort"] for lb in lbs}
    assert ports == {4200, 5556}


# ---------------------------------------------------------------------------
# DB secret
# ---------------------------------------------------------------------------


def test_no_internally_managed_maestro_db_secret(td):
    """maestro connects with the master DB secret (infra-setup output); the
    stack manages no secret of its own."""
    secrets = [
        r for r in td["Resources"].values()
        if r["Type"] == "AWS::SecretsManager::Secret"
    ]
    assert len(secrets) == 0


# ---------------------------------------------------------------------------
# Container env / secrets
# ---------------------------------------------------------------------------


def _container(td, name):
    task_def = next(
        r for r in td["Resources"].values() if r["Type"] == "AWS::ECS::TaskDefinition"
    )
    return next(
        c for c in task_def["Properties"]["ContainerDefinitions"] if c["Name"] == name
    )


def test_db_init_secrets_use_master_db_credentials(td):
    """db-init only creates the database, so it needs just the master creds."""
    db_init = _container(td, "db-init")
    secret_names = {s["Name"] for s in db_init.get("Secrets", [])}
    assert {"LRDB_USER", "LRDB_PASSWORD"} <= secret_names
    assert "MAESTRO_DB_PASSWORD" not in secret_names


def test_maestro_container_env_has_db_settings(td):
    maestro_c = _container(td, "maestro")
    env_names = {e["Name"] for e in maestro_c.get("Environment", [])}
    for n in (
        "MAESTRO_DB_HOST",
        "MAESTRO_DB_PORT",
        "MAESTRO_DB_NAME",
        "MAESTRO_DB_SSLMODE",
        "DEX_ISSUER_URL",
        "DEX_CLIENT_ID",
    ):
        assert n in env_names, f"maestro env missing {n}"


def test_maestro_container_has_bootstrap_env(td):
    """Org/owner/datasource seed contract consumed by the conductor feature."""
    maestro_c = _container(td, "maestro")
    env = {e["Name"]: e["Value"] for e in maestro_c.get("Environment", [])}
    assert env["MAESTRO_BOOTSTRAP_ORG_ID"] == {"Ref": "OrganizationId"}
    assert env["MAESTRO_BOOTSTRAP_ORG_NAME"] == {"Ref": "OrgName"}
    assert env["MAESTRO_BOOTSTRAP_OWNER_EMAIL"] == {"Ref": "DexAdminEmail"}
    assert (
        env["MAESTRO_BOOTSTRAP_LAKERUNNER_QUERY_API_URL"]["Fn::Sub"]
        == "http://query-api.${ServiceNamespaceName}:8080"
    )
    assert (
        env["MAESTRO_BOOTSTRAP_LAKERUNNER_ADMIN_API_URL"]["Fn::Sub"]
        == "http://admin-api.${ServiceNamespaceName}:9091"
    )


def test_maestro_container_has_bootstrap_bucket_env(td):
    """Bucket coordinates so a fixed maestro version can re-insert the
    organization_buckets join row after provision_org (v1.45.9 wipes it
    without re-inserting; deploy-time ensure-storage-profile only runs
    on stack-image change)."""
    maestro_c = _container(td, "maestro")
    env = {e["Name"]: e["Value"] for e in maestro_c.get("Environment", [])}
    assert env["MAESTRO_BOOTSTRAP_BUCKET_NAME"] == {"Ref": "BucketName"}
    assert env["MAESTRO_BOOTSTRAP_BUCKET_REGION"] == {"Ref": "AWS::Region"}
    assert env["MAESTRO_BOOTSTRAP_BUCKET_CLOUD_PROVIDER"] == "aws"
    assert env["MAESTRO_BOOTSTRAP_BUCKET_COLLECTOR_NAME"] == "lakerunner"


def test_maestro_container_admin_api_key_from_secret(td):
    """The seeded datasource's admin key is the same cardinal-admin-key secret
    that admin-api validates via ADMIN_INITIAL_API_KEY."""
    maestro_c = _container(td, "maestro")
    secrets = {s["Name"]: s["ValueFrom"] for s in maestro_c.get("Secrets", [])}
    assert "AdminApiKeySecretArn" in td["Parameters"]
    assert (
        secrets["MAESTRO_BOOTSTRAP_LAKERUNNER_ADMIN_API_KEY"]["Fn::Sub"]
        == "${AdminApiKeySecretArn}:key::"
    )


def test_maestro_container_db_user_and_password_from_master_secret(td):
    """User and password both come from the master DB secret (no maestro role)."""
    maestro_c = _container(td, "maestro")
    secrets = {s["Name"]: s["ValueFrom"] for s in maestro_c.get("Secrets", [])}
    assert "MAESTRO_DB_USER" in secrets
    assert "MAESTRO_DB_PASSWORD" in secrets
    for name in ("MAESTRO_DB_USER", "MAESTRO_DB_PASSWORD"):
        assert secrets[name]["Fn::Sub"].startswith("${DbSecretArn}")


def test_mcp_gateway_container_has_license_data(td):
    """mcp-gateway's license-go loader honors LICENSE_DATA env var;
    without it the container falls through to /app/license/license.json
    and the task fails at startup."""
    mcp = _container(td, "mcp-gateway")
    secret_names = {s["Name"] for s in mcp.get("Secrets", [])}
    assert "LICENSE_DATA" in secret_names


def test_maestro_container_has_license_data(td):
    maestro_c = _container(td, "maestro")
    secret_names = {s["Name"] for s in maestro_c.get("Secrets", [])}
    assert "LICENSE_DATA" in secret_names


def test_dex_container_env_has_oidc_inputs(td):
    # dex v0.3.0 renders its own config from these env vars (no dex-init).
    dex_c = _container(td, "dex")
    env_names = {e["Name"] for e in dex_c.get("Environment", [])}
    assert {
        "DEX_ISSUER_URL",
        "DEX_REDIRECT_URI",
        "DEX_CLIENT_ID",
        "DEX_PORT",
        "DEX_ADMIN_EMAIL",
        "DEX_ADMIN_HASH",
    } <= env_names


def test_dex_renders_config_in_image(td):
    # No dex-init sidecar: nothing mounts /etc/dex (a mount shadows the baked
    # template) and the dex container keeps the image's gomplate entrypoint/CMD.
    dex_c = _container(td, "dex")
    assert "Command" not in dex_c
    mount_paths = {m["ContainerPath"] for m in dex_c.get("MountPoints", [])}
    assert "/etc/dex" not in mount_paths


def test_task_definition_has_no_volumes(td):
    task_def = next(
        r for r in td["Resources"].values() if r["Type"] == "AWS::ECS::TaskDefinition"
    )
    assert not task_def["Properties"].get("Volumes")


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------


def test_outputs_maestro_url(td):
    assert "MaestroUrl" in td["Outputs"]


def test_outputs_dex_url(td):
    assert "DexUrl" in td["Outputs"]


def test_outputs_service_name(td):
    assert "MaestroServiceName" in td["Outputs"]


# ---------------------------------------------------------------------------
# Security: AssignPublicIp DISABLED
# ---------------------------------------------------------------------------


def test_all_services_disable_public_ip(td):
    services = [r for r in td["Resources"].values() if r["Type"] == "AWS::ECS::Service"]
    assert services
    for svc in services:
        awsvpc = svc["Properties"]["NetworkConfiguration"]["AwsvpcConfiguration"]
        assert awsvpc["AssignPublicIp"] == "DISABLED", (
            f"AssignPublicIp must be DISABLED; got {awsvpc['AssignPublicIp']!r}"
        )


def test_service_is_pure_on_demand(td):
    # Maestro is a deploy-critical singleton: pure on-demand FARGATE so its one
    # task always places during a rolling deploy. No FARGATE_SPOT.
    svc = next(r for r in td["Resources"].values() if r["Type"] == "AWS::ECS::Service")
    items = svc["Properties"]["CapacityProviderStrategy"]
    assert items == [{"CapacityProvider": "FARGATE", "Weight": 1}]
    assert all("Base" not in i for i in items)
