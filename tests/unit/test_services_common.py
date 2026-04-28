"""Tests for the shared service-builder helpers."""

import json

import pytest

from cardinal_cfn.children import services_common


def test_build_log_group_uses_install_id_in_name():
    lg = services_common.build_log_group(service_key="query-api")
    rendered = json.loads(json.dumps(lg, default=lambda o: o.to_dict()))
    name = rendered["Properties"].get("LogGroupName")
    assert name is not None
    assert "InstallIdShort" in json.dumps(name)
    assert "query-api" in json.dumps(name)


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


def test_build_task_role_returns_role_with_inline_policy():
    role = services_common.build_task_role(
        service_key="query-worker",
        statements=[{"Effect": "Allow", "Action": "s3:GetObject", "Resource": "*"}],
    )
    rendered = json.loads(json.dumps(role, default=lambda o: o.to_dict()))
    assert rendered["Type"] == "AWS::IAM::Role"
    policies = rendered["Properties"].get("Policies", [])
    assert len(policies) >= 1


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
