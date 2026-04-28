"""Tests for services-control."""

import json

import pytest

from cardinal_cfn.children import services_control


@pytest.fixture
def td():
    return json.loads(services_control.build().to_json())


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
        "HttpsListenerArn",
        "VpcId",
        "DbEndpoint",
        "DbPort",
        "DbSecretArn",
        "BucketName",
        "QueueUrl",
        "QueueArn",
        "LicenseSecretArn",
        "InternalServiceKeysSecretArn",
        "ApiKeysParamName",
        "StorageProfilesParamName",
        "MigrationComplete",
        "LakerunnerImage",
    ):
        assert n in td["Parameters"], f"missing parameter: {n}"


def test_no_per_service_tunable_parameters(td):
    """Control-plane services run at fixed shape from cardinal-defaults.yaml."""
    for n in (
        "AdminApiReplicas",
        "AdminApiCpu",
        "AdminApiMemory",
        "SweeperReplicas",
        "SweeperCpu",
        "SweeperMemory",
        "MonitoringReplicas",
        "MonitoringCpu",
        "MonitoringMemory",
        "AlertEvaluatorReplicas",
        "AlertEvaluatorCpu",
        "AlertEvaluatorMemory",
    ):
        assert n not in td["Parameters"], f"unexpected parameter: {n}"


# ---------------------------------------------------------------------------
# Core resources
# ---------------------------------------------------------------------------


def test_creates_four_ecs_services(td):
    services = [r for r in td["Resources"].values() if r["Type"] == "AWS::ECS::Service"]
    assert len(services) == 4


def test_expected_service_logical_ids_present(td):
    expected_suffixes = {
        "AdminApiService",
        "SweeperService",
        "MonitoringService",
        "AlertEvaluatorService",
    }
    found = {
        logical_id
        for logical_id, res in td["Resources"].items()
        if res["Type"] == "AWS::ECS::Service"
    }
    assert expected_suffixes <= found, f"missing services; got: {found}"


def test_only_one_target_group_and_one_listener_rule(td):
    tgs = [r for r in td["Resources"].values()
           if r["Type"] == "AWS::ElasticLoadBalancingV2::TargetGroup"]
    rules = [r for r in td["Resources"].values()
             if r["Type"] == "AWS::ElasticLoadBalancingV2::ListenerRule"]
    assert len(tgs) == 1
    assert len(rules) == 1


def test_admin_api_listener_rule_priority(td):
    rules = [r for r in td["Resources"].values()
             if r["Type"] == "AWS::ElasticLoadBalancingV2::ListenerRule"]
    assert any(r["Properties"]["Priority"] == 110 for r in rules)


def test_admin_api_listener_rule_path_pattern(td):
    rule = next(r for r in td["Resources"].values()
                if r["Type"] == "AWS::ElasticLoadBalancingV2::ListenerRule")
    conditions = rule["Properties"]["Conditions"]
    found = False
    for cond in conditions:
        if cond.get("Field") == "path-pattern":
            values = cond["PathPatternConfig"]["Values"]
            assert values == ["/admin/*"]
            found = True
    assert found, "expected a path-pattern condition"


# ---------------------------------------------------------------------------
# ALB attachment differentiation
# ---------------------------------------------------------------------------


def _service_by_logical_id(td, suffix):
    for logical_id, res in td["Resources"].items():
        if res["Type"] == "AWS::ECS::Service" and logical_id.endswith(suffix):
            return res
    raise AssertionError(f"no ECS service with logical id ending in {suffix}")


def test_admin_api_service_has_load_balancers(td):
    svc = _service_by_logical_id(td, "AdminApiService")
    assert "LoadBalancers" in svc["Properties"]
    assert len(svc["Properties"]["LoadBalancers"]) == 1
    lb = svc["Properties"]["LoadBalancers"][0]
    assert lb["ContainerName"] == "admin-api"
    assert lb["ContainerPort"] == 9091


def test_internal_services_have_no_load_balancers(td):
    for suffix in ("SweeperService", "MonitoringService", "AlertEvaluatorService"):
        svc = _service_by_logical_id(td, suffix)
        assert "LoadBalancers" not in svc["Properties"], (
            f"{suffix} unexpectedly has LoadBalancers"
        )


# ---------------------------------------------------------------------------
# Log groups + IAM task roles (one per service)
# ---------------------------------------------------------------------------


def test_each_service_has_unique_log_group_with_14_day_retention(td):
    log_groups = [r for r in td["Resources"].values() if r["Type"] == "AWS::Logs::LogGroup"]
    assert len(log_groups) == 4
    for lg in log_groups:
        assert lg["Properties"]["RetentionInDays"] == 14
    names = {json.dumps(lg["Properties"].get("LogGroupName")) for lg in log_groups}
    assert len(names) == 4, "log groups must have distinct names"


def test_each_service_has_its_own_task_role(td):
    roles = [r for r in td["Resources"].values() if r["Type"] == "AWS::IAM::Role"]
    assert len(roles) == 4


# ---------------------------------------------------------------------------
# Task definitions
# ---------------------------------------------------------------------------


def test_four_task_definitions(td):
    task_defs = [r for r in td["Resources"].values() if r["Type"] == "AWS::ECS::TaskDefinition"]
    assert len(task_defs) == 4


def test_task_definition_cpu_and_memory_come_from_yaml(td):
    """All four control-plane services use YAML-fixed Cpu and Memory."""
    task_defs = [r for r in td["Resources"].values() if r["Type"] == "AWS::ECS::TaskDefinition"]
    for td_res in task_defs:
        cpu = td_res["Properties"]["Cpu"]
        mem = td_res["Properties"]["Memory"]
        assert not isinstance(cpu, dict), f"Cpu unexpectedly templated: {cpu!r}"
        assert not isinstance(mem, dict), f"Memory unexpectedly templated: {mem!r}"


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------


def test_outputs_required(td):
    for n in (
        "AdminApiServiceName",
        "SweeperServiceName",
        "MonitoringServiceName",
        "AlertEvaluatorServiceName",
    ):
        assert n in td["Outputs"], f"missing output: {n}"
