"""Tests for the OTEL collector nested stack."""

import json

import pytest

from cardinal_cfn.children import otel


@pytest.fixture
def td():
    return json.loads(otel.build().to_json())


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
        "BucketName",
        "QueueArn",
        "LicenseSecretArn",
        "OtelHttpListenerArn",
        "AlbDnsName",
        "VpcId",
    ):
        assert n in td["Parameters"], f"missing parameter: {n}"


def test_no_db_parameters(td):
    """OTEL collector writes to S3, not the database."""
    for n in ("DbEndpoint", "DbPort", "DbSecretArn"):
        assert n not in td["Parameters"], f"unexpected DB parameter: {n}"


def test_no_storage_profile_or_api_keys_params(td):
    """The migrator seeds configdb from these SSM parameters; the OTEL
    collector reads profiles from configdb at runtime, so these are not
    threaded here."""
    for n in ("ApiKeysParamName", "StorageProfilesParamName"):
        assert n not in td["Parameters"], f"unexpected parameter: {n}"


def test_no_migration_complete_parameter(td):
    """OTEL has no DB dependency, so MigrationComplete is not threaded in."""
    assert "MigrationComplete" not in td["Parameters"]


def test_tunable_parameters_present(td):
    for n in ("OtelReplicas", "OtelCpu", "OtelMemory", "OtelImage", "OtelConfigYaml"):
        assert n in td["Parameters"], f"missing parameter: {n}"


def test_no_expose_on_alb_parameter(td):
    """ALB attachment is unconditional; the toggle no longer exists."""
    assert "OtelExposeOnAlb" not in td["Parameters"]


# ---------------------------------------------------------------------------
# Conditions
# ---------------------------------------------------------------------------


def test_no_expose_on_alb_condition(td):
    assert "ExposeOtelOnAlb" not in td.get("Conditions", {})


# ---------------------------------------------------------------------------
# Core resources
# ---------------------------------------------------------------------------


def test_creates_one_ecs_service(td):
    services = [r for r in td["Resources"].values() if r["Type"] == "AWS::ECS::Service"]
    assert len(services) == 1


def test_creates_one_log_group(td):
    log_groups = [r for r in td["Resources"].values() if r["Type"] == "AWS::Logs::LogGroup"]
    assert len(log_groups) == 1
    assert log_groups[0]["Properties"]["RetentionInDays"] == 14


def test_no_internally_managed_iam_role(td):
    """Phase 2: otel uses the customer-supplied TaskRoleArn parameter."""
    roles = [r for r in td["Resources"].values() if r["Type"] == "AWS::IAM::Role"]
    assert len(roles) == 0


def test_creates_one_task_definition(td):
    task_defs = [r for r in td["Resources"].values() if r["Type"] == "AWS::ECS::TaskDefinition"]
    assert len(task_defs) == 1


def test_target_group_and_listener_rule_unconditional(td):
    tgs = [
        r for r in td["Resources"].values()
        if r["Type"] == "AWS::ElasticLoadBalancingV2::TargetGroup"
    ]
    rules = [
        r for r in td["Resources"].values()
        if r["Type"] == "AWS::ElasticLoadBalancingV2::ListenerRule"
    ]
    assert len(tgs) == 1
    assert len(rules) == 1
    assert "Condition" not in tgs[0]
    assert "Condition" not in rules[0]


def test_target_group_serves_otlp_http_health_checks_13133(td):
    """Traffic on OTLP/HTTP 4318; health check on the health_check extension
    (13133), since 4317 is gRPC and 4318 has no plain-HTTP health path."""
    tg = next(
        r for r in td["Resources"].values()
        if r["Type"] == "AWS::ElasticLoadBalancingV2::TargetGroup"
    )
    props = tg["Properties"]
    assert props["Port"] == 4318
    assert props["Protocol"] == "HTTP"
    assert props["HealthCheckPort"] == "13133"
    assert props["HealthCheckPath"] == "/"
    assert props["HealthCheckProtocol"] == "HTTP"


def test_listener_rule_priority_is_300(td):
    rule = next(
        r for r in td["Resources"].values()
        if r["Type"] == "AWS::ElasticLoadBalancingV2::ListenerRule"
    )
    assert rule["Properties"]["Priority"] == 300


def test_ecs_service_load_balancers_unconditional(td):
    svc = next(r for r in td["Resources"].values() if r["Type"] == "AWS::ECS::Service")
    lbs = svc["Properties"].get("LoadBalancers")
    assert isinstance(lbs, list), f"expected a plain list for LoadBalancers, got {lbs!r}"
    assert len(lbs) == 1
    assert lbs[0]["ContainerPort"] == 4318


# ---------------------------------------------------------------------------
# Container env / config override
# ---------------------------------------------------------------------------


def test_container_has_collector_config_env(td):
    """run-with-env-config in the image reads YAML from CHQ_COLLECTOR_CONFIG_YAML."""
    task_def = next(
        r for r in td["Resources"].values() if r["Type"] == "AWS::ECS::TaskDefinition"
    )
    container = task_def["Properties"]["ContainerDefinitions"][0]
    env_names = {e["Name"] for e in container.get("Environment", [])}
    assert "CHQ_COLLECTOR_CONFIG_YAML" in env_names
    for n in ("LRDB_S3_BUCKET", "LRDB_S3_REGION", "ORG", "COLLECTOR"):
        assert n in env_names, f"missing env var {n}"


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------


def test_outputs_endpoints(td):
    for n in ("OtelInternalUrl", "OtelExternalUrl", "OtelAlbDnsName"):
        assert n in td["Outputs"], f"missing output: {n}"


def test_external_url_is_plain_http_on_4318(td):
    """The OTel ALB listener is HTTP (not HTTPS); the URL must match."""
    assert td["Outputs"]["OtelExternalUrl"]["Value"] == {
        "Fn::Sub": "http://${AlbDnsName}:4318"
    }


def test_outputs_service_name(td):
    assert "OtelServiceName" in td["Outputs"]


# ---------------------------------------------------------------------------
# Security: AssignPublicIp DISABLED
# ---------------------------------------------------------------------------


def test_otel_service_disables_public_ip(td):
    services = [r for r in td["Resources"].values() if r["Type"] == "AWS::ECS::Service"]
    assert services
    for svc in services:
        awsvpc = svc["Properties"]["NetworkConfiguration"]["AwsvpcConfiguration"]
        assert awsvpc["AssignPublicIp"] == "DISABLED", (
            f"AssignPublicIp must be DISABLED; got {awsvpc['AssignPublicIp']!r}"
        )
