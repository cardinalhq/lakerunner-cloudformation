"""Tests for the cardinal-deployer-role standalone template."""

import json

import pytest

from cardinal_cfn import cardinal_deployer


@pytest.fixture
def td():
    return json.loads(cardinal_deployer.build().to_json())


def test_role_name_parameter(td):
    p = td["Parameters"]["RoleName"]
    assert p["Default"] == "cardinal-cfn-deployer"
    assert p["Type"] == "String"


def test_single_role_resource(td):
    roles = [r for r in td["Resources"].values() if r["Type"] == "AWS::IAM::Role"]
    assert len(roles) == 1


def test_role_assumes_from_cloudformation(td):
    role = next(r for r in td["Resources"].values() if r["Type"] == "AWS::IAM::Role")
    statements = role["Properties"]["AssumeRolePolicyDocument"]["Statement"]
    principals = [s["Principal"]["Service"] for s in statements]
    assert "cloudformation.amazonaws.com" in principals


def test_outputs(td):
    for n in ("DeployerRoleArn", "DeployerRoleName", "ExampleUpdateCommand"):
        assert n in td["Outputs"], f"missing output: {n}"


def test_no_template_or_lakerunner_resources(td):
    """Deployer template should only own its own role; nothing else."""
    types = {r["Type"] for r in td["Resources"].values()}
    assert types == {"AWS::IAM::Role"}, f"unexpected resource types: {types}"


def _all_actions(td):
    role = next(r for r in td["Resources"].values() if r["Type"] == "AWS::IAM::Role")
    statements = role["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
    actions = []
    for s in statements:
        a = s.get("Action", [])
        actions.extend([a] if isinstance(a, str) else a)
    return actions


def test_policy_covers_resource_types_used_by_lakerunner(td):
    """Each resource type the lakerunner templates create must have at least
    one corresponding API action in the deployer policy. This is the
    drift-detector: if someone adds a new AWS resource type to a child stack
    without updating this policy, this test fails."""
    actions = set(_all_actions(td))
    required_prefixes = {
        "AWS::ECS::Cluster": ["ecs:CreateCluster"],
        "AWS::ECS::Service": ["ecs:CreateService"],
        "AWS::ECS::TaskDefinition": ["ecs:RegisterTaskDefinition"],
        "AWS::IAM::Role": ["iam:CreateRole", "iam:PutRolePolicy"],
        "AWS::EC2::SecurityGroup": ["ec2:CreateSecurityGroup"],
        "AWS::EC2::SecurityGroupIngress": ["ec2:AuthorizeSecurityGroupIngress"],
        "AWS::ElasticLoadBalancingV2::LoadBalancer":
            ["elasticloadbalancing:CreateLoadBalancer"],
        "AWS::ElasticLoadBalancingV2::Listener":
            ["elasticloadbalancing:CreateListener"],
        "AWS::ElasticLoadBalancingV2::ListenerRule":
            ["elasticloadbalancing:CreateRule"],
        "AWS::ElasticLoadBalancingV2::TargetGroup":
            ["elasticloadbalancing:CreateTargetGroup"],
        "AWS::RDS::DBInstance": ["rds:CreateDBInstance"],
        "AWS::RDS::DBSubnetGroup": ["rds:CreateDBSubnetGroup"],
        "AWS::S3::Bucket": ["s3:CreateBucket"],
        "AWS::SecretsManager::Secret": ["secretsmanager:CreateSecret"],
        "AWS::SSM::Parameter": ["ssm:PutParameter"],
        "AWS::SQS::Queue": ["sqs:CreateQueue"],
        "AWS::SQS::QueuePolicy": ["sqs:SetQueueAttributes"],
        "AWS::Lambda::Function": ["lambda:CreateFunction"],
        "AWS::Logs::LogGroup": ["logs:CreateLogGroup"],
        "AWS::ServiceDiscovery::PrivateDnsNamespace":
            ["servicediscovery:CreatePrivateDnsNamespace"],
        "AWS::ServiceDiscovery::Service": ["servicediscovery:CreateService"],
        "AWS::CloudFormation::Stack": ["cloudformation:CreateStack"],
    }
    for cfn_type, needed in required_prefixes.items():
        for action in needed:
            assert action in actions, (
                f"policy missing {action} required by {cfn_type}"
            )


def test_policy_includes_passrole(td):
    """ECS/Lambda task roles get attached at deploy time -> iam:PassRole required."""
    assert "iam:PassRole" in _all_actions(td)
