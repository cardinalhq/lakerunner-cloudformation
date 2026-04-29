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
        "PrivateSubnetsCsv",
        "VpcId",
        "HttpsListenerArn",
        "AlbDnsName",
        "DbEndpoint",
        "DbPort",
        "DbSecretArn",
        "LicenseSecretArn",
        "InternalServiceKeysSecretArn",
        "ApiKeysParamName",
        "StorageProfilesParamName",
        "MigrationComplete",
    ):
        assert n in td["Parameters"], f"missing parameter: {n}"


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
    assert names == {"db-init", "mcp-gateway", "wait-for-mcp", "maestro", "dex-init", "dex"}


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


def test_four_log_groups(td):
    log_groups = [r for r in td["Resources"].values() if r["Type"] == "AWS::Logs::LogGroup"]
    assert len(log_groups) == 4
    for lg in log_groups:
        assert lg["Properties"]["RetentionInDays"] == 14
    names = {json.dumps(lg["Properties"].get("LogGroupName")) for lg in log_groups}
    assert len(names) == 4, "log groups must have distinct names"


def test_one_iam_task_role(td):
    roles = [r for r in td["Resources"].values() if r["Type"] == "AWS::IAM::Role"]
    assert len(roles) == 1


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


def test_one_maestro_db_secret(td):
    secrets = [
        r for r in td["Resources"].values()
        if r["Type"] == "AWS::SecretsManager::Secret"
    ]
    assert len(secrets) == 1
    s = secrets[0]
    gss = s["Properties"]["GenerateSecretString"]
    assert gss["GenerateStringKey"] == "password"
    assert "maestro" in gss["SecretStringTemplate"]


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


def test_db_init_secrets_include_master_db_user_password_and_maestro_password(td):
    db_init = _container(td, "db-init")
    secret_names = {s["Name"] for s in db_init.get("Secrets", [])}
    assert {"LRDB_USER", "LRDB_PASSWORD", "MAESTRO_DB_PASSWORD"} <= secret_names


def test_maestro_container_env_has_db_settings(td):
    maestro_c = _container(td, "maestro")
    env_names = {e["Name"] for e in maestro_c.get("Environment", [])}
    for n in (
        "MAESTRO_DB_HOST",
        "MAESTRO_DB_PORT",
        "MAESTRO_DB_NAME",
        "MAESTRO_DB_USER",
        "MAESTRO_DB_SSLMODE",
        "DEX_ISSUER_URL",
        "DEX_CLIENT_ID",
    ):
        assert n in env_names, f"maestro env missing {n}"


def test_maestro_container_password_secret_present(td):
    maestro_c = _container(td, "maestro")
    secret_names = {s["Name"] for s in maestro_c.get("Secrets", [])}
    assert "MAESTRO_DB_PASSWORD" in secret_names


def test_dex_init_container_env_has_issuer_url(td):
    dex_init_c = _container(td, "dex-init")
    env_names = {e["Name"] for e in dex_init_c.get("Environment", [])}
    assert "DEX_ISSUER_URL" in env_names


def test_dex_depends_on_dex_init(td):
    dex_c = _container(td, "dex")
    deps = dex_c.get("DependsOn", [])
    assert {"ContainerName": "dex-init", "Condition": "SUCCESS"} in deps


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------


def test_outputs_maestro_url(td):
    assert "MaestroUrl" in td["Outputs"]


def test_outputs_dex_url(td):
    assert "DexUrl" in td["Outputs"]


def test_outputs_service_name(td):
    assert "MaestroServiceName" in td["Outputs"]


def test_outputs_db_secret_arn(td):
    assert "MaestroDbSecretArn" in td["Outputs"]


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
