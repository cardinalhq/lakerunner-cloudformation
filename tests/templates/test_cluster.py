"""Tests for the cluster nested-stack template."""

import json

import pytest

from cardinal_cfn.children import cluster


@pytest.fixture
def template_dict():
    return json.loads(cluster.build().to_json())


def test_declares_install_id_parameters(template_dict):
    params = template_dict["Parameters"]
    assert "InstallIdShort" in params
    assert "InstallIdLong" in params


def test_declares_vpc_id_parameter(template_dict):
    assert template_dict["Parameters"]["VpcId"]["Type"] == "AWS::EC2::VPC::Id"


def test_creates_ecs_cluster(template_dict):
    resources = template_dict["Resources"]
    cluster_resources = [
        r for r in resources.values() if r["Type"] == "AWS::ECS::Cluster"
    ]
    assert len(cluster_resources) == 1


def test_creates_task_security_group(template_dict):
    resources = template_dict["Resources"]
    sgs = [r for r in resources.values() if r["Type"] == "AWS::EC2::SecurityGroup"]
    assert len(sgs) == 1
    sg = sgs[0]
    name_tag = next(t for t in sg["Properties"]["Tags"] if t["Key"] == "Name")
    assert "task-sg" in str(name_tag["Value"]) or "InstallIdShort" in str(name_tag["Value"])


def test_creates_execution_role(template_dict):
    resources = template_dict["Resources"]
    roles = [r for r in resources.values() if r["Type"] == "AWS::IAM::Role"]
    assert len(roles) >= 1
    exec_roles = [
        r for r in roles
        if "ecs-tasks.amazonaws.com" in json.dumps(r["Properties"].get("AssumeRolePolicyDocument", {}))
    ]
    assert len(exec_roles) >= 1


def test_outputs_required_values(template_dict):
    outputs = template_dict["Outputs"]
    for name in ("ClusterArn", "ClusterName", "TaskSecurityGroupId", "ExecutionRoleArn", "BaseLogGroupName"):
        assert name in outputs, f"missing output: {name}"


def test_ecs_cluster_uses_delete_policy(template_dict):
    resources = template_dict["Resources"]
    cluster_resources = [
        (k, v) for k, v in resources.items() if v["Type"] == "AWS::ECS::Cluster"
    ]
    _, cluster_def = cluster_resources[0]
    assert cluster_def.get("DeletionPolicy") == "Delete"
    assert cluster_def.get("UpdateReplacePolicy") == "Delete"
