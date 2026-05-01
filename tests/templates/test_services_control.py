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
        "ClusterName",
        "ProcessLogsServiceName",
        "ProcessMetricsServiceName",
        "ProcessTracesServiceName",
        "ProcessLogsReplicas",
        "ProcessMetricsReplicas",
        "ProcessTracesReplicas",
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


def test_admin_api_listener_rule_uses_dedicated_listener(td):
    rule = next(r for r in td["Resources"].values()
                if r["Type"] == "AWS::ElasticLoadBalancingV2::ListenerRule")
    assert rule["Properties"]["Priority"] == 1
    assert rule["Properties"]["ListenerArn"] == {"Ref": "AdminHttpsListenerArn"}


def test_admin_api_listener_rule_is_catch_all(td):
    rule = next(r for r in td["Resources"].values()
                if r["Type"] == "AWS::ElasticLoadBalancingV2::ListenerRule")
    conditions = rule["Properties"]["Conditions"]
    found = False
    for cond in conditions:
        if cond.get("Field") == "path-pattern":
            values = cond["PathPatternConfig"]["Values"]
            assert values == ["/*"]
            found = True
    assert found, "expected a path-pattern condition"


def test_admin_https_listener_arn_parameter(td):
    assert "AdminHttpsListenerArn" in td["Parameters"]


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


# ---------------------------------------------------------------------------
# Security: AssignPublicIp DISABLED + LRDB_SSLMODE require
# ---------------------------------------------------------------------------


def test_all_services_disable_public_ip(td):
    services = [r for r in td["Resources"].values() if r["Type"] == "AWS::ECS::Service"]
    for svc in services:
        awsvpc = svc["Properties"]["NetworkConfiguration"]["AwsvpcConfiguration"]
        assert awsvpc["AssignPublicIp"] == "DISABLED", (
            f"AssignPublicIp must be DISABLED; got {awsvpc['AssignPublicIp']!r}"
        )


def test_all_task_definitions_set_ssl_required(td):
    task_defs = [r for r in td["Resources"].values() if r["Type"] == "AWS::ECS::TaskDefinition"]
    for tdef in task_defs:
        for container in tdef["Properties"]["ContainerDefinitions"]:
            env = {e["Name"]: e["Value"] for e in container.get("Environment", [])}
            assert env.get("LRDB_SSLMODE") == "require", (
                f"{container.get('Name')} LRDB_SSLMODE must be 'require'; got {env.get('LRDB_SSLMODE')!r}"
            )


# ---------------------------------------------------------------------------
# B → C boundary
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Monitoring autoscaler wiring
# ---------------------------------------------------------------------------


def _task_def_for(td, container_name):
    for res in td["Resources"].values():
        if res["Type"] != "AWS::ECS::TaskDefinition":
            continue
        for container in res["Properties"]["ContainerDefinitions"]:
            if container["Name"] == container_name:
                return res, container
    raise AssertionError(f"no task definition with container {container_name!r}")


def test_monitoring_container_has_autoscaler_env(td):
    _, container = _task_def_for(td, "monitoring")
    env = {e["Name"]: e["Value"] for e in container.get("Environment", [])}
    assert env.get("LAKERUNNER_AUTOSCALER_ENABLED") == "true"
    assert env.get("LAKERUNNER_AUTOSCALER_PLATFORM") == "ecs"
    assert env.get("ECS_CLUSTER") == {"Ref": "ClusterName"}
    expected = {
        "LAKERUNNER_AUTOSCALER_SERVICES_LOGS_DEPLOYMENT": "ProcessLogsServiceName",
        "LAKERUNNER_AUTOSCALER_SERVICES_LOGS_MAX_REPLICAS": "ProcessLogsReplicas",
        "LAKERUNNER_AUTOSCALER_SERVICES_METRICS_DEPLOYMENT": "ProcessMetricsServiceName",
        "LAKERUNNER_AUTOSCALER_SERVICES_METRICS_MAX_REPLICAS": "ProcessMetricsReplicas",
        "LAKERUNNER_AUTOSCALER_SERVICES_TRACES_DEPLOYMENT": "ProcessTracesServiceName",
        "LAKERUNNER_AUTOSCALER_SERVICES_TRACES_MAX_REPLICAS": "ProcessTracesReplicas",
    }
    for env_name, param_name in expected.items():
        assert env.get(env_name) == {"Ref": param_name}, (
            f"{env_name} must Ref {param_name}; got {env.get(env_name)!r}"
        )


def test_only_monitoring_has_autoscaler_env(td):
    """Sweeper / admin-api / alert-evaluator must not see the autoscaler env."""
    for container_name in ("admin-api", "sweeper", "alert-evaluator"):
        _, container = _task_def_for(td, container_name)
        env_names = {e["Name"] for e in container.get("Environment", [])}
        leaked = {n for n in env_names if n.startswith("LAKERUNNER_AUTOSCALER_")}
        leaked.update(env_names & {"ECS_CLUSTER"})
        assert not leaked, f"{container_name} unexpectedly has {leaked}"


def _task_role_for(td, service_key):
    """Return the IAM role whose logical id starts with the title-cased service_key."""
    title = "".join(p.capitalize() for p in service_key.split("-")) + "TaskRole"
    return td["Resources"][title]


def _flatten_actions(stmt_actions):
    return [stmt_actions] if isinstance(stmt_actions, str) else list(stmt_actions)


def test_monitoring_task_role_grants_ecs_update(td):
    role = _task_role_for(td, "monitoring")
    statements = role["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
    found = False
    for stmt in statements:
        actions = _flatten_actions(stmt.get("Action", []))
        if "ecs:UpdateService" in actions and "ecs:DescribeServices" in actions:
            found = True
            resources = stmt["Resource"]
            assert isinstance(resources, list) and len(resources) == 3
            joined = json.dumps(resources)
            for param in (
                "ProcessLogsServiceName",
                "ProcessMetricsServiceName",
                "ProcessTracesServiceName",
            ):
                assert param in joined, f"{param} not referenced in monitoring ECS resource ARNs"
            assert "ClusterName" in joined
    assert found, "monitoring role missing ecs:DescribeServices/UpdateService"


def test_other_roles_lack_ecs_update(td):
    """Only monitoring should see ecs:UpdateService."""
    for service_key in ("admin-api", "sweeper", "alert-evaluator"):
        role = _task_role_for(td, service_key)
        statements = role["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
        for stmt in statements:
            actions = _flatten_actions(stmt.get("Action", []))
            assert "ecs:UpdateService" not in actions, (
                f"{service_key} unexpectedly has ecs:UpdateService"
            )


# ---------------------------------------------------------------------------
# B → C boundary
# ---------------------------------------------------------------------------


def test_no_shared_resources_in_services_control(td):
    forbidden = {
        "AWS::ECS::Cluster",
        "AWS::ElasticLoadBalancingV2::LoadBalancer",
        "AWS::ElasticLoadBalancingV2::Listener",
        "AWS::SecretsManager::Secret",
        "AWS::SSM::Parameter",
        "AWS::S3::Bucket",
        "AWS::SQS::Queue",
        "AWS::SQS::QueuePolicy",
        "AWS::RDS::DBInstance",
        "AWS::RDS::DBSubnetGroup",
        "AWS::EC2::SecurityGroup",
    }
    found = {r["Type"] for r in td["Resources"].values()} & forbidden
    assert not found, f"services-control must not own shared resources; found {found}"
