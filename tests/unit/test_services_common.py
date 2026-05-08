"""Tests for the shared service-builder helpers."""

import json

import pytest

from cardinal_cfn.children import services_common


def test_build_log_group_uses_cardinal_naming_contract():
    lg = services_common.build_log_group(service_key="query-api")
    rendered = json.loads(json.dumps(lg, default=lambda o: o.to_dict()))
    name = rendered["Properties"].get("LogGroupName")
    assert name == "/cardinal/query-api"


def test_build_listener_rule_uses_registered_priority():
    rule = services_common.build_listener_rule(
        service_key="query-api",
        target_group_ref="QueryApiTargetGroup",
        listener_arn_param="HttpsListenerArn",
        path_patterns=["/api/v1/*"],
    )
    rendered = json.loads(json.dumps(rule, default=lambda o: o.to_dict()))
    assert rendered["Properties"]["Priority"] == 100


def test_build_listener_rule_unknown_service_raises():
    with pytest.raises(KeyError):
        services_common.build_listener_rule(
            service_key="not-registered",
            target_group_ref="x",
            listener_arn_param="HttpsListenerArn",
            path_patterns=["/x"],
        )


def test_build_task_role_no_longer_exposed():
    """Per the Phase 2 prereqs-split refactor, services no longer create
    per-service IAM roles; all share a customer-supplied TaskRoleArn."""
    assert not hasattr(services_common, "build_task_role")


def test_build_task_definition_accepts_int_cpu_memory():
    from troposphere import Ref
    from troposphere.logs import LogGroup

    lg = LogGroup("L", LogGroupName="x")
    td = services_common.build_task_definition(
        service_key="query-api",
        image_ref="image",
        cpu=1024,
        memory_mib=2048,
        command=["/app/bin/lakerunner"],
        execution_role_arn_param="ExecutionRoleArn",
        task_role_arn=Ref("TaskRoleArn"),
        environment=[],
        log_group_ref=lg,
    )
    rendered = json.loads(json.dumps(td, default=lambda o: o.to_dict()))
    assert rendered["Properties"]["Cpu"] == "1024"
    assert rendered["Properties"]["Memory"] == "2048"
    assert rendered["Properties"]["TaskRoleArn"] == {"Ref": "TaskRoleArn"}


def test_build_task_definition_accepts_ref_cpu_memory():
    from troposphere import Ref
    from troposphere.logs import LogGroup

    lg = LogGroup("L", LogGroupName="x")
    td = services_common.build_task_definition(
        service_key="query-api",
        image_ref="image",
        cpu=Ref("QueryApiCpu"),
        memory_mib=Ref("QueryApiMemory"),
        execution_role_arn_param="ExecutionRoleArn",
        task_role_arn=Ref("TaskRoleArn"),
        environment=[],
        log_group_ref=lg,
    )
    rendered = json.loads(json.dumps(td, default=lambda o: o.to_dict()))
    assert rendered["Properties"]["Cpu"] == {"Ref": "QueryApiCpu"}
    assert rendered["Properties"]["Memory"] == {"Ref": "QueryApiMemory"}


def test_build_ecs_service_has_circuit_breaker_and_rolling_deploy():
    svc = services_common.build_ecs_service(
        service_key="query-api",
        cluster_arn_param="ClusterArn",
        task_definition_ref="MyTaskDef",
        desired_count=2,
        subnets_csv_param="PrivateSubnetsCsv",
        security_group_id_param="TaskSecurityGroupId",
        container_name="query-api",
    )
    rendered = json.loads(json.dumps(svc, default=lambda o: o.to_dict()))
    dc = rendered["Properties"].get("DeploymentConfiguration", {})
    assert dc.get("MinimumHealthyPercent") == 50
    assert dc.get("MaximumPercent") == 200
    assert dc.get("DeploymentCircuitBreaker", {}).get("Enable") is True
    assert dc.get("DeploymentCircuitBreaker", {}).get("Rollback") is True
