"""Tests for the lrdev-vpc standalone template."""

import json

import pytest

from cardinal_cfn import lrdev_vpc


@pytest.fixture
def td():
    return json.loads(lrdev_vpc.build().to_json())


def test_required_parameters(td):
    for n in ("VpcCidr", "EnvironmentName", "CreateNatGateway", "CreateInterfaceEndpoints"):
        assert n in td["Parameters"], f"missing parameter: {n}"


def test_environment_default_is_lrdev(td):
    assert td["Parameters"]["EnvironmentName"]["Default"] == "lrdev"


def test_create_nat_gateway_default_yes(td):
    assert td["Parameters"]["CreateNatGateway"]["Default"] == "Yes"
    assert td["Parameters"]["CreateNatGateway"]["AllowedValues"] == ["Yes", "No"]


def test_create_interface_endpoints_default_no(td):
    assert td["Parameters"]["CreateInterfaceEndpoints"]["Default"] == "No"
    assert td["Parameters"]["CreateInterfaceEndpoints"]["AllowedValues"] == ["Yes", "No"]


def test_outputs(td):
    for n in ("VpcId", "PrivateSubnetsCsv", "PublicSubnetsCsv", "VpcEndpointSecurityGroupId"):
        assert n in td["Outputs"], f"missing output: {n}"


def test_outputs_have_no_export(td):
    """Standalone template: customer copies values manually, no exports needed."""
    for name, out in td["Outputs"].items():
        assert "Export" not in out, f"output {name} should not have an Export"


def test_creates_vpc_and_subnets(td):
    vpcs = [r for r in td["Resources"].values() if r["Type"] == "AWS::EC2::VPC"]
    subnets = [r for r in td["Resources"].values() if r["Type"] == "AWS::EC2::Subnet"]
    assert len(vpcs) == 1
    assert len(subnets) == 6  # 3 public + 3 private


def test_creates_internet_gateway(td):
    igws = [r for r in td["Resources"].values() if r["Type"] == "AWS::EC2::InternetGateway"]
    attachments = [
        r for r in td["Resources"].values()
        if r["Type"] == "AWS::EC2::VPCGatewayAttachment"
    ]
    assert len(igws) == 1
    assert len(attachments) == 1


def test_nat_gateway_conditional(td):
    """NAT Gateway only when CreateNatGateway=Yes."""
    nats = [r for r in td["Resources"].values() if r["Type"] == "AWS::EC2::NatGateway"]
    assert len(nats) == 1
    assert nats[0].get("Condition") is not None


def test_nat_eip_conditional(td):
    eips = [r for r in td["Resources"].values() if r["Type"] == "AWS::EC2::EIP"]
    assert len(eips) == 1
    assert eips[0].get("Condition") is not None


def test_creates_route_tables(td):
    rts = [r for r in td["Resources"].values() if r["Type"] == "AWS::EC2::RouteTable"]
    # one public, one private
    assert len(rts) == 2


def test_s3_gateway_endpoint(td):
    endpoints = [
        r for r in td["Resources"].values() if r["Type"] == "AWS::EC2::VPCEndpoint"
    ]
    gateway_eps = [
        e for e in endpoints if e["Properties"].get("VpcEndpointType") == "Gateway"
    ]
    assert len(gateway_eps) == 1
    # S3 endpoint is unconditional
    assert gateway_eps[0].get("Condition") is None


def test_interface_endpoints_conditional(td):
    """Interface endpoints (Secrets Manager, Logs, ECS, ECR API/DKR) gated."""
    endpoints = [
        r for r in td["Resources"].values() if r["Type"] == "AWS::EC2::VPCEndpoint"
    ]
    interface_eps = [
        e for e in endpoints if e["Properties"].get("VpcEndpointType") == "Interface"
    ]
    assert len(interface_eps) == 5  # secretsmanager, logs, ecs, ecr.api, ecr.dkr
    for ep in interface_eps:
        assert ep.get("Condition") is not None, "interface endpoint must be conditional"


def test_vpc_endpoint_security_group(td):
    sgs = [r for r in td["Resources"].values() if r["Type"] == "AWS::EC2::SecurityGroup"]
    assert len(sgs) == 1
    sg = sgs[0]
    ingress = sg["Properties"]["SecurityGroupIngress"]
    assert any(rule["FromPort"] == 443 and rule["ToPort"] == 443 for rule in ingress)


def test_vpc_has_dns_support_and_hostnames(td):
    vpc = next(r for r in td["Resources"].values() if r["Type"] == "AWS::EC2::VPC")
    assert vpc["Properties"]["EnableDnsSupport"] is True
    assert vpc["Properties"]["EnableDnsHostnames"] is True


def test_public_subnets_map_public_ip(td):
    subnets = [
        r for r in td["Resources"].values()
        if r["Type"] == "AWS::EC2::Subnet" and r["Properties"].get("MapPublicIpOnLaunch")
    ]
    assert len(subnets) == 3


def test_subnets_span_three_az_indices(td):
    """Each AZ index 0, 1, 2 must appear among both public and private subnets."""
    subnets = {k: v for k, v in td["Resources"].items() if v["Type"] == "AWS::EC2::Subnet"}
    public = [s for s in subnets.values() if s["Properties"].get("MapPublicIpOnLaunch")]
    private = [s for s in subnets.values() if not s["Properties"].get("MapPublicIpOnLaunch")]
    assert len(public) == 3
    assert len(private) == 3
    for group_name, group in (("public", public), ("private", private)):
        az_indices = {s["Properties"]["AvailabilityZone"]["Fn::Select"][0] for s in group}
        assert az_indices == {0, 1, 2}, f"{group_name} subnets missing expected AZ indices"


def test_outputs_list_three_subnets_each(td):
    """PublicSubnetsCsv and PrivateSubnetsCsv must each Join 3 subnet refs."""
    for output_name in ("PublicSubnetsCsv", "PrivateSubnetsCsv"):
        join_values = td["Outputs"][output_name]["Value"]["Fn::Join"][1]
        assert len(join_values) == 3, f"{output_name} should reference 3 subnets"


def test_no_install_id_parameters(td):
    """Standalone template should not require InstallId parameters."""
    for n in ("InstallIdShort", "InstallIdLong"):
        assert n not in td["Parameters"], f"{n} should not be a parameter"
