"""Tests for services-process."""

import json

import pytest

from cardinal_cfn.children import services_process


@pytest.fixture
def td():
    return json.loads(services_process.build().to_json())


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------


def test_required_cross_stack_parameters(td):
    for n in (
        "InstallIdShort",
        "InstallIdLong",
        "ClusterArn",
        "TaskSecurityGroupId",
        "ExecutionRoleArn",
        "PrivateSubnetsCsv",
        "DbEndpoint",
        "DbPort",
        "DbSecretArn",
        "BucketName",
        "QueueUrl",
        "QueueArn",
        "LicenseSecretArn",
        "InternalServiceKeysSecretArn",
        "ApiKeysParamName",
        "StorageProfilesParamName",
        "MigrationComplete",
        "LakerunnerImage",
    ):
        assert n in td["Parameters"], f"missing parameter: {n}"


def test_no_alb_parameters(td):
    """services-process does not attach to the ALB and must not declare ALB inputs."""
    for n in ("HttpsListenerArn", "VpcId"):
        assert n not in td["Parameters"], f"unexpected ALB parameter present: {n}"


def test_per_service_tunable_parameters(td):
    for n in (
        "ProcessLogsReplicas",
        "ProcessLogsMemory",
        "ProcessMetricsReplicas",
        "ProcessMetricsMemory",
        "ProcessTracesReplicas",
        "ProcessTracesMemory",
        "PubsubSqsReplicas",
    ):
        assert n in td["Parameters"], f"missing parameter: {n}"


def test_no_cpu_parameters_for_process_or_pubsub(td):
    """Per the project CLAUDE.md table: CPU is YAML-only for these services."""
    for n in (
        "ProcessLogsCpu",
        "ProcessMetricsCpu",
        "ProcessTracesCpu",
        "PubsubSqsCpu",
        "PubsubSqsMemory",
    ):
        assert n not in td["Parameters"], f"unexpected parameter: {n}"


# ---------------------------------------------------------------------------
# Core resources
# ---------------------------------------------------------------------------


def test_creates_four_ecs_services(td):
    services = [r for r in td["Resources"].values() if r["Type"] == "AWS::ECS::Service"]
    assert len(services) == 4


def test_no_target_groups(td):
    tgs = [r for r in td["Resources"].values()
           if r["Type"] == "AWS::ElasticLoadBalancingV2::TargetGroup"]
    assert len(tgs) == 0


def test_no_listener_rules(td):
    rules = [r for r in td["Resources"].values()
             if r["Type"] == "AWS::ElasticLoadBalancingV2::ListenerRule"]
    assert len(rules) == 0


def test_no_service_attaches_load_balancers(td):
    services = [r for r in td["Resources"].values() if r["Type"] == "AWS::ECS::Service"]
    for svc in services:
        lbs = svc["Properties"].get("LoadBalancers")
        assert not lbs, f"unexpected LoadBalancers on ECS service: {lbs!r}"


def test_expected_service_logical_ids_present(td):
    expected_suffixes = {
        "ProcessLogsService",
        "ProcessMetricsService",
        "ProcessTracesService",
        "PubsubSqsService",
    }
    found = {
        logical_id
        for logical_id, res in td["Resources"].items()
        if res["Type"] == "AWS::ECS::Service"
    }
    assert expected_suffixes <= found, f"missing services; got: {found}"


# ---------------------------------------------------------------------------
# Log groups
# ---------------------------------------------------------------------------


def test_each_service_has_unique_log_group_with_14_day_retention(td):
    log_groups = [r for r in td["Resources"].values() if r["Type"] == "AWS::Logs::LogGroup"]
    assert len(log_groups) == 4
    for lg in log_groups:
        assert lg["Properties"]["RetentionInDays"] == 14
    names = {json.dumps(lg["Properties"].get("LogGroupName")) for lg in log_groups}
    assert len(names) == 4, "log groups must have distinct names"


# ---------------------------------------------------------------------------
# IAM task roles (one per service)
# ---------------------------------------------------------------------------


def test_each_service_has_its_own_task_role(td):
    roles = [r for r in td["Resources"].values() if r["Type"] == "AWS::IAM::Role"]
    assert len(roles) == 4


# ---------------------------------------------------------------------------
# Task definitions
# ---------------------------------------------------------------------------


def test_four_task_definitions(td):
    task_defs = [r for r in td["Resources"].values() if r["Type"] == "AWS::ECS::TaskDefinition"]
    assert len(task_defs) == 4


def test_task_definition_memory_uses_parameter_refs_for_process_services(td):
    """Process services pull Memory from CFN parameters."""
    task_defs = {
        logical_id: r
        for logical_id, r in td["Resources"].items()
        if r["Type"] == "AWS::ECS::TaskDefinition"
    }

    expected_mem_refs = {
        "ProcessLogsTaskDef": "ProcessLogsMemory",
        "ProcessMetricsTaskDef": "ProcessMetricsMemory",
        "ProcessTracesTaskDef": "ProcessTracesMemory",
    }
    for logical_id, expected_ref in expected_mem_refs.items():
        td_res = task_defs[logical_id]
        mem = td_res["Properties"]["Memory"]
        assert isinstance(mem, dict) and mem.get("Ref") == expected_ref, (
            f"{logical_id} Memory should be Ref({expected_ref}); got {mem!r}"
        )


def test_pubsub_sqs_task_definition_uses_yaml_defaults(td):
    """pubsub-sqs has no Cpu/Memory parameter; both come from cardinal-defaults.yaml."""
    task_defs = {
        logical_id: r
        for logical_id, r in td["Resources"].items()
        if r["Type"] == "AWS::ECS::TaskDefinition"
    }
    pubsub = task_defs["PubsubSqsTaskDef"]
    cpu = pubsub["Properties"]["Cpu"]
    mem = pubsub["Properties"]["Memory"]
    # Neither should be a Ref; both are stringified ints from YAML.
    assert not isinstance(cpu, dict), f"PubsubSqs Cpu unexpectedly templated: {cpu!r}"
    assert not isinstance(mem, dict), f"PubsubSqs Memory unexpectedly templated: {mem!r}"


def test_desired_count_uses_replicas_param(td):
    services = {
        logical_id: r
        for logical_id, r in td["Resources"].items()
        if r["Type"] == "AWS::ECS::Service"
    }
    expected_refs = {
        "ProcessLogsService": "ProcessLogsReplicas",
        "ProcessMetricsService": "ProcessMetricsReplicas",
        "ProcessTracesService": "ProcessTracesReplicas",
        "PubsubSqsService": "PubsubSqsReplicas",
    }
    for logical_id, expected_ref in expected_refs.items():
        svc = services[logical_id]
        dc = svc["Properties"]["DesiredCount"]
        assert isinstance(dc, dict) and dc.get("Ref") == expected_ref, (
            f"{logical_id} DesiredCount should be Ref({expected_ref}); got {dc!r}"
        )


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------


def test_outputs_required(td):
    for n in (
        "ProcessLogsServiceName",
        "ProcessMetricsServiceName",
        "ProcessTracesServiceName",
        "PubsubSqsServiceName",
    ):
        assert n in td["Outputs"], f"missing output: {n}"
