"""Tests for the alb nested-stack template."""

import json

import pytest

from cardinal_cfn.children import alb


@pytest.fixture
def template_dict():
    return json.loads(alb.build().to_json())


def test_required_parameters(template_dict):
    for n in ("InstallIdShort", "InstallIdLong", "VpcId", "PublicSubnetsCsv",
              "PrivateSubnetsCsv", "AlbScheme", "TaskSecurityGroupId"):
        assert n in template_dict["Parameters"], f"missing parameter: {n}"


def test_creates_load_balancer(template_dict):
    lbs = [r for r in template_dict["Resources"].values()
           if r["Type"] == "AWS::ElasticLoadBalancingV2::LoadBalancer"]
    assert len(lbs) == 1


def test_creates_https_listeners_on_443_and_9443(template_dict):
    listeners = [r for r in template_dict["Resources"].values()
                 if r["Type"] == "AWS::ElasticLoadBalancingV2::Listener"]
    assert len(listeners) == 2
    ports = sorted(l["Properties"]["Port"] for l in listeners)
    assert ports == [443, 9443]


def test_creates_alb_to_task_ingress(template_dict):
    ingresses = [r for r in template_dict["Resources"].values()
                 if r["Type"] == "AWS::EC2::SecurityGroupIngress"]
    assert len(ingresses) >= 1


def test_alb_uses_delete_policy(template_dict):
    alb_def = next(
        v for v in template_dict["Resources"].values()
        if v["Type"] == "AWS::ElasticLoadBalancingV2::LoadBalancer"
    )
    assert alb_def.get("DeletionPolicy") == "Delete"


def test_outputs_required(template_dict):
    for n in (
        "AlbArn",
        "AlbDnsName",
        "AlbSecurityGroupId",
        "HttpsListenerArn",
        "AdminHttpsListenerArn",
    ):
        assert n in template_dict["Outputs"]
