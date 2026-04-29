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
        "PrivateSubnetsCsv",
        "BucketName",
        "QueueArn",
        "LicenseSecretArn",
        "InternalServiceKeysSecretArn",
        "ApiKeysParamName",
        "StorageProfilesParamName",
        "HttpsListenerArn",
        "VpcId",
    ):
        assert n in td["Parameters"], f"missing parameter: {n}"


def test_no_db_parameters(td):
    """OTEL collector writes to S3, not the database."""
    for n in ("DbEndpoint", "DbPort", "DbSecretArn"):
        assert n not in td["Parameters"], f"unexpected DB parameter: {n}"


def test_no_migration_complete_parameter(td):
    """OTEL has no DB dependency, so MigrationComplete is not threaded in."""
    assert "MigrationComplete" not in td["Parameters"]


def test_tunable_parameters_present(td):
    for n in ("OtelReplicas", "OtelCpu", "OtelMemory", "OtelImage", "OtelConfigYaml"):
        assert n in td["Parameters"], f"missing parameter: {n}"


def test_expose_on_alb_parameter(td):
    p = td["Parameters"]["OtelExposeOnAlb"]
    assert p["Type"] == "String"
    assert p["AllowedValues"] == ["Yes", "No"]
    assert p["Default"] == "No"


# ---------------------------------------------------------------------------
# Conditions
# ---------------------------------------------------------------------------


def test_expose_on_alb_condition_defined(td):
    conds = td.get("Conditions", {})
    assert "ExposeOtelOnAlb" in conds
    assert conds["ExposeOtelOnAlb"] == {"Fn::Equals": [{"Ref": "OtelExposeOnAlb"}, "Yes"]}


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


def test_creates_one_iam_role(td):
    roles = [r for r in td["Resources"].values() if r["Type"] == "AWS::IAM::Role"]
    assert len(roles) == 1


def test_creates_one_task_definition(td):
    task_defs = [r for r in td["Resources"].values() if r["Type"] == "AWS::ECS::TaskDefinition"]
    assert len(task_defs) == 1


def test_target_group_and_listener_rule_have_condition(td):
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
    assert tgs[0].get("Condition") == "ExposeOtelOnAlb"
    assert rules[0].get("Condition") == "ExposeOtelOnAlb"


def test_listener_rule_priority_is_300(td):
    rule = next(
        r for r in td["Resources"].values()
        if r["Type"] == "AWS::ElasticLoadBalancingV2::ListenerRule"
    )
    assert rule["Properties"]["Priority"] == 300


def test_ecs_service_load_balancers_is_conditional(td):
    svc = next(r for r in td["Resources"].values() if r["Type"] == "AWS::ECS::Service")
    lbs = svc["Properties"].get("LoadBalancers")
    assert isinstance(lbs, dict), f"expected an Fn::If for LoadBalancers, got {lbs!r}"
    assert "Fn::If" in lbs
    branches = lbs["Fn::If"]
    assert branches[0] == "ExposeOtelOnAlb"
    # Non-attached branch must be AWS::NoValue.
    assert branches[2] == {"Ref": "AWS::NoValue"}


# ---------------------------------------------------------------------------
# Container env / config override
# ---------------------------------------------------------------------------


def test_container_has_otel_config_override_env(td):
    task_def = next(
        r for r in td["Resources"].values() if r["Type"] == "AWS::ECS::TaskDefinition"
    )
    container = task_def["Properties"]["ContainerDefinitions"][0]
    env_names = {e["Name"] for e in container.get("Environment", [])}
    assert "OTEL_CONFIG_OVERRIDE" in env_names


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------


def test_outputs_endpoint(td):
    assert "OtelEndpoint" in td["Outputs"]


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
