"""Tests for services-query."""

import json

import pytest

from cardinal_cfn.children import services_query


@pytest.fixture
def td():
    return json.loads(services_query.build().to_json())


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
        "TaskRoleArn",
        "PrivateSubnetsCsv",
        "HttpsListenerArn",
        "VpcId",
        "LakerunnerImage",
        "DbEndpoint",
        "DbPort",
        "DbSecretArn",
        "BucketName",
        "QueueUrl",
        "QueueArn",
        "LicenseSecretArn",
        "MigrationComplete",
    ):
        assert n in td["Parameters"], f"missing parameter: {n}"


def test_no_storage_profile_or_api_keys_params(td):
    """The migrator seeds configdb from these SSM parameters; service tasks
    read profiles from configdb at runtime, so these are not threaded here."""
    for n in ("ApiKeysParamName", "StorageProfilesParamName"):
        assert n not in td["Parameters"], f"unexpected parameter: {n}"


def test_per_service_tunable_parameters(td):
    for n in (
        "QueryApiReplicas",
        "QueryApiCpu",
        "QueryApiMemory",
        "QueryWorkerReplicas",
        "QueryWorkerCpu",
        "QueryWorkerMemory",
    ):
        assert n in td["Parameters"], f"missing parameter: {n}"


# ---------------------------------------------------------------------------
# Core resources
# ---------------------------------------------------------------------------


def test_creates_two_ecs_services(td):
    services = [r for r in td["Resources"].values() if r["Type"] == "AWS::ECS::Service"]
    assert len(services) == 2


def test_creates_target_group_and_listener_rule_for_query_api(td):
    tgs = [r for r in td["Resources"].values()
           if r["Type"] == "AWS::ElasticLoadBalancingV2::TargetGroup"]
    rules = [r for r in td["Resources"].values()
             if r["Type"] == "AWS::ElasticLoadBalancingV2::ListenerRule"]
    assert len(tgs) >= 1
    assert len(rules) >= 1
    assert any(r["Properties"]["Priority"] == 100 for r in rules)


def test_query_worker_does_not_attach_to_alb(td):
    """query-worker is internal; no listener rule should reference its target group."""
    worker_tg_logical_ids = {
        logical_id
        for logical_id, res in td["Resources"].items()
        if res["Type"] == "AWS::ElasticLoadBalancingV2::TargetGroup"
        and "Worker" in logical_id
    }
    rules = [r for r in td["Resources"].values()
             if r["Type"] == "AWS::ElasticLoadBalancingV2::ListenerRule"]
    for rule in rules:
        for action in rule["Properties"].get("Actions", []):
            tg_arn = action.get("TargetGroupArn")
            if isinstance(tg_arn, dict) and "Ref" in tg_arn:
                assert tg_arn["Ref"] not in worker_tg_logical_ids, (
                    f"query-worker target group is attached to a listener rule: {tg_arn!r}"
                )


def test_only_one_listener_rule_and_one_target_group(td):
    tgs = [r for r in td["Resources"].values()
           if r["Type"] == "AWS::ElasticLoadBalancingV2::TargetGroup"]
    rules = [r for r in td["Resources"].values()
             if r["Type"] == "AWS::ElasticLoadBalancingV2::ListenerRule"]
    assert len(tgs) == 1
    assert len(rules) == 1


def test_listener_rule_uses_query_api_path_pattern(td):
    rule = next(r for r in td["Resources"].values()
                if r["Type"] == "AWS::ElasticLoadBalancingV2::ListenerRule")
    conditions = rule["Properties"]["Conditions"]
    found = False
    for cond in conditions:
        if cond.get("Field") == "path-pattern":
            values = cond["PathPatternConfig"]["Values"]
            assert values == ["/api/v1/query/*"]
            found = True
    assert found, "expected a path-pattern condition"


# ---------------------------------------------------------------------------
# Log groups
# ---------------------------------------------------------------------------


def test_each_service_has_unique_log_group_with_14_day_retention(td):
    log_groups = [r for r in td["Resources"].values() if r["Type"] == "AWS::Logs::LogGroup"]
    assert len(log_groups) == 2
    for lg in log_groups:
        assert lg["Properties"]["RetentionInDays"] == 14
    names = {json.dumps(lg["Properties"].get("LogGroupName")) for lg in log_groups}
    assert len(names) == 2, "log groups must have distinct names"


# ---------------------------------------------------------------------------
# IAM task roles (one per service)
# ---------------------------------------------------------------------------


def test_no_internally_managed_iam_roles(td):
    """Phase 2: all services share the customer-supplied TaskRoleArn parameter."""
    roles = [r for r in td["Resources"].values() if r["Type"] == "AWS::IAM::Role"]
    assert len(roles) == 0


# ---------------------------------------------------------------------------
# query-api LB attachment / query-worker non-attachment
# ---------------------------------------------------------------------------


def _service_by_logical_id(td, suffix):
    for logical_id, res in td["Resources"].items():
        if res["Type"] == "AWS::ECS::Service" and logical_id.endswith(suffix):
            return res
    raise AssertionError(f"no ECS service with logical id ending in {suffix}")


def test_query_api_service_has_load_balancers(td):
    svc = _service_by_logical_id(td, "QueryApiService")
    assert "LoadBalancers" in svc["Properties"]
    assert len(svc["Properties"]["LoadBalancers"]) == 1


def test_query_worker_service_has_no_load_balancers(td):
    svc = _service_by_logical_id(td, "QueryWorkerService")
    assert "LoadBalancers" not in svc["Properties"]


# ---------------------------------------------------------------------------
# Task-to-task ingress for query-worker port
# ---------------------------------------------------------------------------


def test_task_definition_cpu_and_memory_are_parameter_refs(td):
    """Cpu/Memory must serialize as Ref intrinsics, not Python repr strings."""
    task_defs = [r for r in td["Resources"].values() if r["Type"] == "AWS::ECS::TaskDefinition"]
    assert len(task_defs) == 2
    seen = set()
    for td_res in task_defs:
        cpu = td_res["Properties"]["Cpu"]
        mem = td_res["Properties"]["Memory"]
        assert isinstance(cpu, dict) and "Ref" in cpu, f"Cpu should be a Ref, got {cpu!r}"
        assert isinstance(mem, dict) and "Ref" in mem, f"Memory should be a Ref, got {mem!r}"
        seen.add(cpu["Ref"])
        seen.add(mem["Ref"])
    assert {"QueryApiCpu", "QueryApiMemory", "QueryWorkerCpu", "QueryWorkerMemory"} <= seen


def test_no_security_group_ingress_resources(td):
    # Phase 2: lakerunner stack must not create or mutate any SGs.
    # The customer-supplied TaskSgId already includes self-ingress
    # covering query-api -> query-worker traffic.
    ingresses = [r for r in td["Resources"].values()
                 if r["Type"] == "AWS::EC2::SecurityGroupIngress"]
    assert ingresses == [], (
        "services_query must not create SecurityGroupIngress resources; "
        "the customer-supplied TaskSgId already permits intra-cluster traffic."
    )


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------


def test_outputs_required(td):
    for n in ("QueryApiServiceName", "QueryWorkerServiceName"):
        assert n in td["Outputs"], f"missing output: {n}"


# ---------------------------------------------------------------------------
# Security: AssignPublicIp DISABLED + LRDB_SSLMODE require
# ---------------------------------------------------------------------------


def test_all_services_disable_public_ip(td):
    """Hard security requirement: no Fargate task may receive a public IP."""
    services = [r for r in td["Resources"].values() if r["Type"] == "AWS::ECS::Service"]
    for svc in services:
        awsvpc = svc["Properties"]["NetworkConfiguration"]["AwsvpcConfiguration"]
        assert awsvpc["AssignPublicIp"] == "DISABLED", (
            f"AssignPublicIp must be DISABLED; got {awsvpc['AssignPublicIp']!r}"
        )


def test_all_task_definitions_set_ssl_required(td):
    """Database connections must always require SSL."""
    task_defs = [r for r in td["Resources"].values() if r["Type"] == "AWS::ECS::TaskDefinition"]
    for tdef in task_defs:
        for container in tdef["Properties"]["ContainerDefinitions"]:
            env = {e["Name"]: e["Value"] for e in container.get("Environment", [])}
            assert env.get("LRDB_SSLMODE") == "require", (
                f"{container.get('Name')} LRDB_SSLMODE must be 'require'; got {env.get('LRDB_SSLMODE')!r}"
            )


# ---------------------------------------------------------------------------
# B → C boundary: per-service stack must own only per-service resources.
# ---------------------------------------------------------------------------


def test_no_shared_resources_in_services_query(td):
    """services-query owns only per-service resources; shared ones live elsewhere."""
    forbidden = {
        "AWS::ECS::Cluster",
        "AWS::ElasticLoadBalancingV2::LoadBalancer",
        "AWS::ElasticLoadBalancingV2::Listener",
        "AWS::SecretsManager::Secret",
        "AWS::SSM::Parameter",
        "AWS::S3::Bucket",
        "AWS::SQS::Queue",
        "AWS::SQS::QueuePolicy",
        "AWS::RDS::DBInstance",
        "AWS::RDS::DBSubnetGroup",
        "AWS::EC2::SecurityGroup",
    }
    found = {r["Type"] for r in td["Resources"].values()} & forbidden
    assert not found, f"services-query must not own shared resources; found {found}"
