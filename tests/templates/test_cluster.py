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


def test_no_internally_managed_security_group(template_dict):
    """cluster takes the task SG as a parameter, never creates one."""
    resources = template_dict["Resources"]
    sgs = [r for r in resources.values() if r["Type"] == "AWS::EC2::SecurityGroup"]
    assert len(sgs) == 0


def test_no_internally_managed_iam_role(template_dict):
    """cluster takes the execution role as a parameter, never creates one."""
    resources = template_dict["Resources"]
    roles = [r for r in resources.values() if r["Type"] == "AWS::IAM::Role"]
    assert len(roles) == 0


def test_cluster_is_named_cardinal(template_dict):
    """The cluster physical name is part of the IAM cookbook contract."""
    resources = template_dict["Resources"]
    cluster_def = next(
        v for v in resources.values() if v["Type"] == "AWS::ECS::Cluster"
    )
    assert cluster_def["Properties"]["ClusterName"] == "cardinal"


def test_outputs_required_values(template_dict):
    outputs = template_dict["Outputs"]
    for name in ("ClusterArn", "ClusterName", "BaseLogGroupName"):
        assert name in outputs, f"missing output: {name}"


def test_no_legacy_outputs(template_dict):
    """Phase 2 spec drops the previously-emitted role/SG outputs."""
    outputs = template_dict["Outputs"]
    assert "TaskSecurityGroupId" not in outputs
    assert "ExecutionRoleArn" not in outputs


def test_ecs_cluster_uses_delete_policy(template_dict):
    resources = template_dict["Resources"]
    cluster_resources = [
        (k, v) for k, v in resources.items() if v["Type"] == "AWS::ECS::Cluster"
    ]
    _, cluster_def = cluster_resources[0]
    assert cluster_def.get("DeletionPolicy") == "Delete"
    assert cluster_def.get("UpdateReplacePolicy") == "Delete"
