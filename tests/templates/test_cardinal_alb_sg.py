"""Tests for the cardinal-alb-sg standalone template."""

import json

import pytest

from cardinal_cfn import cardinal_alb_sg


@pytest.fixture
def td():
    return json.loads(cardinal_alb_sg.build().to_json())


def test_required_parameters(td):
    for n in ("VpcId", "SecurityGroupName"):
        assert n in td["Parameters"], f"missing parameter: {n}"


def test_security_group_name_default(td):
    assert td["Parameters"]["SecurityGroupName"]["Default"] == "cardinal-alb-sg"


def _sg(td):
    sgs = [r for r in td["Resources"].values() if r["Type"] == "AWS::EC2::SecurityGroup"]
    assert len(sgs) == 1
    return sgs[0]


def test_group_name_is_parameterized(td):
    assert _sg(td)["Properties"]["GroupName"] == {"Ref": "SecurityGroupName"}


def test_ingress_is_https_from_rfc1918(td):
    ingress = _sg(td)["Properties"]["SecurityGroupIngress"]
    cidrs = {r["CidrIp"] for r in ingress}
    assert cidrs == {"10.0.0.0/8", "172.16.0.0/12"}
    for r in ingress:
        assert r["IpProtocol"] == "tcp"
        assert r["FromPort"] == 443
        assert r["ToPort"] == 443


def test_egress_is_all_traffic_anywhere(td):
    egress = _sg(td)["Properties"]["SecurityGroupEgress"]
    assert len(egress) == 1
    assert egress[0]["IpProtocol"] == "-1"
    assert egress[0]["CidrIp"] == "0.0.0.0/0"


def test_output_exposes_group_id(td):
    assert "AlbSecurityGroupId" in td["Outputs"]
    assert td["Outputs"]["AlbSecurityGroupId"]["Value"] == {
        "Fn::GetAtt": ["AlbSecurityGroup", "GroupId"]
    }


def test_outputs_have_no_export(td):
    for name, out in td["Outputs"].items():
        assert "Export" not in out, f"output {name} should not have an Export"
