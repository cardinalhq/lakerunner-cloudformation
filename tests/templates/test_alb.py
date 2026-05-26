"""Tests for the alb nested-stack template."""

import json

import pytest

from cardinal_cfn.children import alb


@pytest.fixture
def template_dict():
    return json.loads(alb.build().to_json())


def test_required_parameters(template_dict):
    for n in ("InstallIdShort", "InstallIdLong", "VpcId",
              "PrivateSubnetsCsv", "AlbSgId"):
        assert n in template_dict["Parameters"], f"missing parameter: {n}"


def test_no_internally_managed_security_group(template_dict):
    """alb takes SGs as parameters, never creates them."""
    sgs = [r for r in template_dict["Resources"].values()
           if r["Type"] == "AWS::EC2::SecurityGroup"]
    assert len(sgs) == 0


def test_no_public_subnet_or_alb_scheme_parameters(template_dict):
    for n in ("PublicSubnetsCsv", "AlbScheme"):
        assert n not in template_dict["Parameters"], (
            f"parameter {n} should have been removed"
        )


def test_alb_is_internal(template_dict):
    alb_def = next(
        v for v in template_dict["Resources"].values()
        if v["Type"] == "AWS::ElasticLoadBalancingV2::LoadBalancer"
    )
    assert alb_def["Properties"]["Scheme"] == "internal"
    subnets = alb_def["Properties"]["Subnets"]
    assert subnets == {"Fn::Split": [",", {"Ref": "PrivateSubnetsCsv"}]}


def test_creates_load_balancer(template_dict):
    lbs = [r for r in template_dict["Resources"].values()
           if r["Type"] == "AWS::ElasticLoadBalancingV2::LoadBalancer"]
    assert len(lbs) == 1


def test_creates_listeners_on_443_9443_and_4318(template_dict):
    listeners = [r for r in template_dict["Resources"].values()
                 if r["Type"] == "AWS::ElasticLoadBalancingV2::Listener"]
    assert len(listeners) == 3
    by_port = {l["Properties"]["Port"]: l["Properties"]["Protocol"] for l in listeners}
    assert by_port == {443: "HTTPS", 9443: "HTTPS", 4318: "HTTP"}


def test_no_ingress_resources_internally_managed(template_dict):
    """ALB-to-task ingress is now arranged by the customer outside the stack."""
    ingresses = [r for r in template_dict["Resources"].values()
                 if r["Type"] == "AWS::EC2::SecurityGroupIngress"]
    assert len(ingresses) == 0


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
        "HttpsListenerArn",
        "AdminHttpsListenerArn",
        "OtelHttpListenerArn",
    ):
        assert n in template_dict["Outputs"]


def test_no_alb_security_group_output(template_dict):
    """The ALB SG ID is supplied by the caller, so the stack does not surface it."""
    assert "AlbSecurityGroupId" not in template_dict["Outputs"]
