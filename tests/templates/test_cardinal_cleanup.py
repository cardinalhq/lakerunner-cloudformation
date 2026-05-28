"""Tests for the cardinal-cleanup standalone template."""

import json

import pytest

from cardinal_cfn import cardinal_cleanup


@pytest.fixture
def td():
    return json.loads(cardinal_cleanup.build().to_json())


def test_required_parameters(td):
    for n in (
        "LakerunnerStackName",
        "CleanupTaskRoleArn",
        "CleanupExecutionRoleArn",
        "ClusterName",
        "DeployerRoleArn",
    ):
        assert n in td["Parameters"], f"missing parameter: {n}"


def test_lakerunner_stack_name_default(td):
    assert td["Parameters"]["LakerunnerStackName"]["Default"] == "cardinal-lakerunner"


def test_only_two_resources_present(td):
    """No IAM, no networking, no services -- only log group + task definition."""
    assert set(td["Resources"]) == {"CleanupLogGroup", "CleanupTaskDefinition"}


def test_no_iam_resources(td):
    for name, r in td["Resources"].items():
        assert not r["Type"].startswith("AWS::IAM::"), (
            f"resource {name} ({r['Type']}) violates the no-IAM rule"
        )


def test_log_group_properties(td):
    lg = td["Resources"]["CleanupLogGroup"]
    assert lg["Type"] == "AWS::Logs::LogGroup"
    assert lg["DeletionPolicy"] == "Delete"
    assert lg["UpdateReplacePolicy"] == "Delete"
    props = lg["Properties"]
    assert props["RetentionInDays"] == 7
    # LogGroupName is a !Sub with the stack-name suffix.
    sub = props["LogGroupName"]
    assert "Fn::Sub" in sub
    assert sub["Fn::Sub"] == "/aws/ecs/cardinal-cleanup/${AWS::StackName}"


def test_task_definition_shape(td):
    tdef = td["Resources"]["CleanupTaskDefinition"]
    assert tdef["Type"] == "AWS::ECS::TaskDefinition"
    props = tdef["Properties"]
    assert props["Family"] == "cardinal-cleanup"
    assert props["RequiresCompatibilities"] == ["FARGATE"]
    assert props["NetworkMode"] == "awsvpc"
    assert props["Cpu"] == "512"
    assert props["Memory"] == "1024"
    assert props["TaskRoleArn"] == {"Ref": "CleanupTaskRoleArn"}
    assert props["ExecutionRoleArn"] == {"Ref": "CleanupExecutionRoleArn"}


def test_single_container(td):
    containers = td["Resources"]["CleanupTaskDefinition"]["Properties"]["ContainerDefinitions"]
    assert len(containers) == 1
    c = containers[0]
    assert c["Name"] == "cleanup"
    assert c["Image"] == "public.ecr.aws/aws-cli/aws-cli:latest"
    assert c["Essential"] is True


def test_script_in_entrypoint_not_command(td):
    """The vetted shell must live in EntryPoint so RunTask command overrides
    cannot substitute it (containerOverrides has no entryPoint override)."""
    c = td["Resources"]["CleanupTaskDefinition"]["Properties"]["ContainerDefinitions"][0]
    assert c["EntryPoint"][:2] == ["/bin/sh", "-c"]
    script_body = c["EntryPoint"][2]
    assert "drain_services" in script_body
    assert "delete_lakerunner_stack" in script_body
    assert "empty_ingest_bucket" in script_body
    assert "delete_infra_stack" in script_body
    assert "delete_ingest_bucket" in script_body
    assert "delete_secrets" in script_body
    assert "delete_rds_snapshots" in script_body
    assert "self_delete" in script_body
    assert c["Command"] == []


def test_environment_pins_required_vars(td):
    c = td["Resources"]["CleanupTaskDefinition"]["Properties"]["ContainerDefinitions"][0]
    env_pairs = {e["Name"]: e["Value"] for e in c["Environment"]}
    assert env_pairs == {
        "AWS_REGION":            {"Ref": "AWS::Region"},
        "AWS_ACCOUNT_ID":        {"Ref": "AWS::AccountId"},
        "CLUSTER_NAME":          {"Ref": "ClusterName"},
        "LAKERUNNER_STACK_NAME": {"Ref": "LakerunnerStackName"},
        "INFRA_STACK_NAME":      {"Ref": "InfraStackName"},
        "CLEANUP_STACK_NAME":    {"Ref": "AWS::StackName"},
        "DEPLOYER_ROLE_ARN":     {"Ref": "DeployerRoleArn"},
    }


def test_log_configuration_targets_log_group(td):
    c = td["Resources"]["CleanupTaskDefinition"]["Properties"]["ContainerDefinitions"][0]
    lc = c["LogConfiguration"]
    assert lc["LogDriver"] == "awslogs"
    assert lc["Options"]["awslogs-group"] == {"Ref": "CleanupLogGroup"}
    assert lc["Options"]["awslogs-region"] == {"Ref": "AWS::Region"}
    assert lc["Options"]["awslogs-stream-prefix"] == "cleanup"


def test_outputs(td):
    for n in ("TaskDefinitionArn", "LogGroupName"):
        assert n in td["Outputs"], f"missing output: {n}"
    assert td["Outputs"]["TaskDefinitionArn"]["Value"] == {"Ref": "CleanupTaskDefinition"}
    assert td["Outputs"]["LogGroupName"]["Value"] == {"Ref": "CleanupLogGroup"}
