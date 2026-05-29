"""Tests for the cardinal-remote-collector standalone template (remote account)."""

import json

import pytest

from cardinal_cfn import remote_collector


@pytest.fixture
def td():
    return json.loads(remote_collector.build().to_json())


def test_customer_supplied_parameters(td):
    for n in ("VpcId", "PrivateSubnetsCsv", "ClusterArn", "WriterRoleArn",
              "BucketName", "BucketRegion", "OrgId", "CollectorName",
              "OtlpIngressCidr"):
        assert n in td["Parameters"], f"missing parameter: {n}"


def test_no_license_secret_anywhere(td):
    """The remote collector runs receive->S3 only; it needs no license."""
    blob = json.dumps(td)
    assert "LICENSE_DATA" not in blob
    assert "LicenseSecretArn" not in td["Parameters"]
    task_def = next(r for r in td["Resources"].values() if r["Type"] == "AWS::ECS::TaskDefinition")
    container = task_def["Properties"]["ContainerDefinitions"][0]
    assert "Secrets" not in container or container["Secrets"] == []


def test_creates_internal_alb_on_4318(td):
    albs = [r for r in td["Resources"].values()
            if r["Type"] == "AWS::ElasticLoadBalancingV2::LoadBalancer"]
    assert len(albs) == 1
    assert albs[0]["Properties"]["Scheme"] == "internal"
    listeners = [r for r in td["Resources"].values()
                 if r["Type"] == "AWS::ElasticLoadBalancingV2::Listener"]
    assert len(listeners) == 1
    assert listeners[0]["Properties"]["Port"] == 4318
    assert listeners[0]["Properties"]["Protocol"] == "HTTP"


def test_creates_two_security_groups(td):
    sgs = [r for r in td["Resources"].values() if r["Type"] == "AWS::EC2::SecurityGroup"]
    assert len(sgs) == 2


def test_task_role_name_matches_writer_trust_pattern(td):
    """Task role name must start with cardinal-remote-otel- so the main writer
    role's trust condition matches it."""
    roles = [r for r in td["Resources"].values() if r["Type"] == "AWS::IAM::Role"]
    task_roles = [
        r for r in roles
        if any(
            "sts:AssumeRole" in json.dumps(p["PolicyDocument"])
            and "WriterRoleArn" in json.dumps(p["PolicyDocument"])
            for p in r["Properties"].get("Policies", [])
        )
    ]
    assert len(task_roles) == 1
    name = task_roles[0]["Properties"]["RoleName"]
    assert name == {"Fn::Sub": "cardinal-remote-otel-${AWS::Region}"}


def test_task_role_can_assume_writer_role(td):
    roles = [r for r in td["Resources"].values() if r["Type"] == "AWS::IAM::Role"]
    found = False
    for r in roles:
        for p in r["Properties"].get("Policies", []):
            doc = json.dumps(p["PolicyDocument"])
            if "sts:AssumeRole" in doc and "WriterRoleArn" in doc:
                found = True
    assert found, "no role grants sts:AssumeRole on WriterRoleArn"


def test_collector_env_uses_bucket_region_and_role(td):
    task_def = next(r for r in td["Resources"].values() if r["Type"] == "AWS::ECS::TaskDefinition")
    container = task_def["Properties"]["ContainerDefinitions"][0]
    env = {e["Name"]: e["Value"] for e in container["Environment"]}
    assert env["LRDB_S3_BUCKET"] == {"Ref": "BucketName"}
    assert env["LRDB_S3_REGION"] == {"Ref": "BucketRegion"}
    assert env["LRDB_S3_ROLE_ARN"] == {"Ref": "WriterRoleArn"}
    assert env["ORG"] == {"Ref": "OrgId"}
    assert "CHQ_COLLECTOR_CONFIG_YAML" in env


def test_service_disables_public_ip(td):
    svc = next(r for r in td["Resources"].values() if r["Type"] == "AWS::ECS::Service")
    awsvpc = svc["Properties"]["NetworkConfiguration"]["AwsvpcConfiguration"]
    assert awsvpc["AssignPublicIp"] == "DISABLED"


def test_no_cloud_map_registration(td):
    """Self-telemetry discovery is a main-account concern; not here."""
    assert not [r for r in td["Resources"].values()
                if r["Type"] == "AWS::ServiceDiscovery::Service"]


def test_outputs(td):
    for n in ("OtelAlbDnsName", "OtelExternalUrl"):
        assert n in td["Outputs"], f"missing output: {n}"
