"""Tests for services-control."""

import json

import pytest

from cardinal_cfn.children import services_control


@pytest.fixture
def td():
    return json.loads(services_control.build().to_json())


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
        "DbEndpoint",
        "DbPort",
        "DbSecretArn",
        "BucketName",
        "LicenseSecretArn",
        "MigrationComplete",
        "LakerunnerImage",
        "ClusterName",
        "ProcessLogsServiceName",
        "ProcessMetricsServiceName",
        "ProcessTracesServiceName",
        "ProcessLogsReplicas",
        "ProcessMetricsReplicas",
        "ProcessTracesReplicas",
    ):
        assert n in td["Parameters"], f"missing parameter: {n}"


def test_no_vestigial_queue_parameters(td):
    """The control tier does not consume SQS; the old queue plumbing (only ever
    fed the dead LRDB_SQS_QUEUE_URL env) was purged."""
    for n in ("QueueUrl", "QueueArn"):
        assert n not in td["Parameters"], f"vestigial queue parameter present: {n}"


def test_no_storage_profile_or_api_keys_params(td):
    """The migrator seeds configdb from these SSM parameters; service tasks
    read profiles from configdb at runtime, so these are not threaded here."""
    for n in ("ApiKeysParamName", "StorageProfilesParamName"):
        assert n not in td["Parameters"], f"unexpected parameter: {n}"


def test_no_per_service_tunable_parameters(td):
    """Control-plane services run at fixed shape from cardinal-defaults.yaml."""
    for n in (
        "AdminApiReplicas",
        "AdminApiCpu",
        "AdminApiMemory",
        "SweeperReplicas",
        "SweeperCpu",
        "SweeperMemory",
        "MonitoringReplicas",
        "MonitoringCpu",
        "MonitoringMemory",
        "AlertEvaluatorReplicas",
        "AlertEvaluatorCpu",
        "AlertEvaluatorMemory",
    ):
        assert n not in td["Parameters"], f"unexpected parameter: {n}"


# ---------------------------------------------------------------------------
# Core resources
# ---------------------------------------------------------------------------


def test_creates_single_merged_ecs_service(td):
    """The four control-plane singletons are co-located in ONE ECS service
    running ONE task with four containers — cutting the per-task Fargate floor
    ~4x and leaving one task to place instead of four."""
    services = [r for r in td["Resources"].values() if r["Type"] == "AWS::ECS::Service"]
    assert len(services) == 1


def test_control_service_logical_id_present(td):
    found = {
        logical_id
        for logical_id, res in td["Resources"].items()
        if res["Type"] == "AWS::ECS::Service"
    }
    assert found == {"ControlService"}, f"expected only ControlService; got: {found}"


def test_single_task_has_four_essential_containers(td):
    task_defs = [r for r in td["Resources"].values() if r["Type"] == "AWS::ECS::TaskDefinition"]
    assert len(task_defs) == 1
    containers = task_defs[0]["Properties"]["ContainerDefinitions"]
    names = {c["Name"] for c in containers}
    assert names == {"admin-api", "sweeper", "monitoring", "alert-evaluator"}
    for c in containers:
        assert c["Essential"] is True, f"{c['Name']} must be essential"


def test_merged_task_uses_fixed_minimum_shape(td):
    """256 CPU / 512 MiB — Fargate's floor; the four containers' combined real
    usage is ~7m / ~90Mi. ARM64/LINUX, awsvpc, FARGATE."""
    task = next(r for r in td["Resources"].values() if r["Type"] == "AWS::ECS::TaskDefinition")
    props = task["Properties"]
    assert props["Cpu"] == "256"
    assert props["Memory"] == "512"
    assert props["NetworkMode"] == "awsvpc"
    assert props["RequiresCompatibilities"] == ["FARGATE"]
    assert props["RuntimePlatform"]["CpuArchitecture"] == "ARM64"
    assert props["RuntimePlatform"]["OperatingSystemFamily"] == "LINUX"


def test_merged_task_uses_shared_exec_and_control_roles(td):
    task = next(r for r in td["Resources"].values() if r["Type"] == "AWS::ECS::TaskDefinition")
    props = task["Properties"]
    assert props["ExecutionRoleArn"] == {"Ref": "ExecutionRoleArn"}
    assert props["TaskRoleArn"] == {"Ref": "TaskRoleArn"}


def test_only_one_target_group_and_one_listener_rule(td):
    tgs = [r for r in td["Resources"].values()
           if r["Type"] == "AWS::ElasticLoadBalancingV2::TargetGroup"]
    rules = [r for r in td["Resources"].values()
             if r["Type"] == "AWS::ElasticLoadBalancingV2::ListenerRule"]
    assert len(tgs) == 1
    assert len(rules) == 1


def test_admin_api_listener_rule_uses_dedicated_listener(td):
    rule = next(r for r in td["Resources"].values()
                if r["Type"] == "AWS::ElasticLoadBalancingV2::ListenerRule")
    assert rule["Properties"]["Priority"] == 1
    assert rule["Properties"]["ListenerArn"] == {"Ref": "AdminHttpsListenerArn"}


def test_admin_api_listener_rule_is_catch_all(td):
    rule = next(r for r in td["Resources"].values()
                if r["Type"] == "AWS::ElasticLoadBalancingV2::ListenerRule")
    conditions = rule["Properties"]["Conditions"]
    found = False
    for cond in conditions:
        if cond.get("Field") == "path-pattern":
            values = cond["PathPatternConfig"]["Values"]
            assert values == ["/*"]
            found = True
    assert found, "expected a path-pattern condition"


def test_admin_https_listener_arn_parameter(td):
    assert "AdminHttpsListenerArn" in td["Parameters"]


# ---------------------------------------------------------------------------
# Cloud Map registration for admin-api
# ---------------------------------------------------------------------------


def test_service_namespace_id_parameter_present(td):
    """ServiceNamespaceId is forwarded from root so admin-api can register
    a Cloud Map A record at admin-api.<namespace>:9091."""
    assert "ServiceNamespaceId" in td["Parameters"]


def test_admin_api_discovery_service_registered(td):
    """admin-api gets a ServiceDiscovery::Service so peers in the cluster
    (maestro) can reach it without going through the ALB."""
    discoveries = {
        logical_id: res
        for logical_id, res in td["Resources"].items()
        if res["Type"] == "AWS::ServiceDiscovery::Service"
    }
    assert "AdminApiDiscoveryService" in discoveries
    props = discoveries["AdminApiDiscoveryService"]["Properties"]
    assert props["Name"] == "admin-api"
    assert props["NamespaceId"] == {"Ref": "ServiceNamespaceId"}
    records = props["DnsConfig"]["DnsRecords"]
    assert any(r["Type"] == "A" for r in records)


def test_control_service_has_service_registries(td):
    """The merged control ECS service registers in Cloud Map (its admin-api
    container's IP) so peers in the cluster (maestro) can reach it without
    going through the ALB."""
    svc = next(
        res
        for logical_id, res in td["Resources"].items()
        if res["Type"] == "AWS::ECS::Service" and logical_id == "ControlService"
    )
    assert "ServiceRegistries" in svc["Properties"]
    arn = svc["Properties"]["ServiceRegistries"][0]["RegistryArn"]
    assert arn == {"Fn::GetAtt": ["AdminApiDiscoveryService", "Arn"]}


# ---------------------------------------------------------------------------
# ALB attachment differentiation
# ---------------------------------------------------------------------------


def _control_service(td):
    return td["Resources"]["ControlService"]


def test_control_service_load_balancer_targets_admin_api_container(td):
    """The single merged service's LoadBalancers block targets the admin-api
    CONTAINER (and only it) inside the task; the other three containers have no
    ports and no LB attachment."""
    svc = _control_service(td)
    assert "LoadBalancers" in svc["Properties"]
    assert len(svc["Properties"]["LoadBalancers"]) == 1
    lb = svc["Properties"]["LoadBalancers"][0]
    assert lb["ContainerName"] == "admin-api"
    assert lb["ContainerPort"] == 9091


def test_only_admin_api_container_exposes_a_port(td):
    task = next(r for r in td["Resources"].values() if r["Type"] == "AWS::ECS::TaskDefinition")
    for c in task["Properties"]["ContainerDefinitions"]:
        if c["Name"] == "admin-api":
            assert c.get("PortMappings") == [{"ContainerPort": 9091, "Protocol": "tcp"}]
        else:
            assert "PortMappings" not in c, f"{c['Name']} unexpectedly has a port"


# ---------------------------------------------------------------------------
# Log groups + IAM task roles (one per service)
# ---------------------------------------------------------------------------


def test_each_container_has_unique_log_group_with_14_day_retention(td):
    """Per-container log groups are KEPT (one task, four groups) so operators'
    familiar /cardinal/<svc> log paths don't change."""
    log_groups = [r for r in td["Resources"].values() if r["Type"] == "AWS::Logs::LogGroup"]
    assert len(log_groups) == 4
    for lg in log_groups:
        assert lg["Properties"]["RetentionInDays"] == 14
    names = {lg["Properties"]["LogGroupName"] for lg in log_groups}
    assert names == {
        "/cardinal/admin-api",
        "/cardinal/sweeper",
        "/cardinal/monitoring",
        "/cardinal/alert-evaluator",
    }


def test_each_container_logs_to_its_own_group(td):
    """Each container's awslogs LogConfiguration points at its own group."""
    task = next(r for r in td["Resources"].values() if r["Type"] == "AWS::ECS::TaskDefinition")
    for c in task["Properties"]["ContainerDefinitions"]:
        opts = c["LogConfiguration"]["Options"]
        assert opts["awslogs-group"] == {"Ref": _log_group_id(c["Name"])}
        assert opts["awslogs-stream-prefix"] == c["Name"]


def _log_group_id(container_name):
    return "".join(p.capitalize() for p in container_name.split("-")) + "LogGroup"


def test_no_internally_managed_iam_roles(td):
    """Phase 2: all services share the customer-supplied TaskRoleArn parameter."""
    roles = [r for r in td["Resources"].values() if r["Type"] == "AWS::IAM::Role"]
    assert len(roles) == 0


# ---------------------------------------------------------------------------
# Task definitions
# ---------------------------------------------------------------------------


def test_single_task_definition(td):
    task_defs = [r for r in td["Resources"].values() if r["Type"] == "AWS::ECS::TaskDefinition"]
    assert len(task_defs) == 1


def test_task_definition_cpu_and_memory_are_fixed(td):
    """The merged control task uses a fixed minimum Cpu/Memory (not templated)."""
    task = next(r for r in td["Resources"].values() if r["Type"] == "AWS::ECS::TaskDefinition")
    cpu = task["Properties"]["Cpu"]
    mem = task["Properties"]["Memory"]
    assert not isinstance(cpu, dict), f"Cpu unexpectedly templated: {cpu!r}"
    assert not isinstance(mem, dict), f"Memory unexpectedly templated: {mem!r}"


def test_admin_api_container_has_admin_key_secret(td):
    """The admin-api container keeps its ADMIN_INITIAL_API_KEY secret sourced
    from the AdminApiKeySecretArn parameter."""
    _, container = _task_def_for(td, "admin-api")
    secrets = {s["Name"]: s["ValueFrom"] for s in container.get("Secrets", [])}
    assert "ADMIN_INITIAL_API_KEY" in secrets
    assert secrets["ADMIN_INITIAL_API_KEY"] == {
        "Fn::Sub": "${AdminApiKeySecretArn}:key::"
    }


def test_only_admin_api_container_has_admin_key_secret(td):
    for name in ("sweeper", "monitoring", "alert-evaluator"):
        _, container = _task_def_for(td, name)
        names = {s["Name"] for s in container.get("Secrets", [])}
        assert "ADMIN_INITIAL_API_KEY" not in names, f"{name} unexpectedly has admin key"


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------


def test_outputs_reduced_to_single_control_service_name(td):
    """The four per-service-name outputs collapse to one ControlServiceName.
    The root consumes none of the old per-service outputs, so this is safe."""
    assert "ControlServiceName" in td["Outputs"]
    for n in (
        "AdminApiServiceName",
        "SweeperServiceName",
        "MonitoringServiceName",
        "AlertEvaluatorServiceName",
    ):
        assert n not in td["Outputs"], f"vestigial per-service output present: {n}"


# ---------------------------------------------------------------------------
# Security: AssignPublicIp DISABLED + LRDB_SSLMODE require
# ---------------------------------------------------------------------------


def test_all_services_disable_public_ip(td):
    services = [r for r in td["Resources"].values() if r["Type"] == "AWS::ECS::Service"]
    for svc in services:
        awsvpc = svc["Properties"]["NetworkConfiguration"]["AwsvpcConfiguration"]
        assert awsvpc["AssignPublicIp"] == "DISABLED", (
            f"AssignPublicIp must be DISABLED; got {awsvpc['AssignPublicIp']!r}"
        )


def test_all_task_definitions_set_ssl_required(td):
    task_defs = [r for r in td["Resources"].values() if r["Type"] == "AWS::ECS::TaskDefinition"]
    for tdef in task_defs:
        for container in tdef["Properties"]["ContainerDefinitions"]:
            env = {e["Name"]: e["Value"] for e in container.get("Environment", [])}
            assert env.get("LRDB_SSLMODE") == "require", (
                f"{container.get('Name')} LRDB_SSLMODE must be 'require'; got {env.get('LRDB_SSLMODE')!r}"
            )


def test_no_dead_sqs_env(td):
    """LRDB_SQS_QUEUE_URL is a dead env name (the binary reads SQS_QUEUE_URL),
    and control tasks never consumed SQS -- it must not appear anywhere."""
    task_defs = [r for r in td["Resources"].values() if r["Type"] == "AWS::ECS::TaskDefinition"]
    for tdef in task_defs:
        for container in tdef["Properties"]["ContainerDefinitions"]:
            names = {e["Name"] for e in container.get("Environment", [])}
            assert "LRDB_SQS_QUEUE_URL" not in names, (
                f"{container.get('Name')} still has dead LRDB_SQS_QUEUE_URL env"
            )


# ---------------------------------------------------------------------------
# B → C boundary
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Monitoring autoscaler wiring
# ---------------------------------------------------------------------------


def _task_def_for(td, container_name):
    for res in td["Resources"].values():
        if res["Type"] != "AWS::ECS::TaskDefinition":
            continue
        for container in res["Properties"]["ContainerDefinitions"]:
            if container["Name"] == container_name:
                return res, container
    raise AssertionError(f"no task definition with container {container_name!r}")


def test_monitoring_container_has_autoscaler_env(td):
    _, container = _task_def_for(td, "monitoring")
    env = {e["Name"]: e["Value"] for e in container.get("Environment", [])}
    assert env.get("LAKERUNNER_AUTOSCALER_ENABLED") == "true"
    # Without ObserveOnly=false the autoscaler defaults to compute-don't-write,
    # which would silently no-op the whole feature.
    assert env.get("LAKERUNNER_AUTOSCALER_OBSERVE_ONLY") == "false"
    assert env.get("LAKERUNNER_AUTOSCALER_PLATFORM") == "ecs"
    assert env.get("ECS_CLUSTER") == {"Ref": "ClusterName"}
    expected = {
        "LAKERUNNER_AUTOSCALER_SERVICES_LOGS_DEPLOYMENT": "ProcessLogsServiceName",
        "LAKERUNNER_AUTOSCALER_SERVICES_LOGS_MAX_REPLICAS": "ProcessLogsReplicas",
        "LAKERUNNER_AUTOSCALER_SERVICES_METRICS_DEPLOYMENT": "ProcessMetricsServiceName",
        "LAKERUNNER_AUTOSCALER_SERVICES_METRICS_MAX_REPLICAS": "ProcessMetricsReplicas",
        "LAKERUNNER_AUTOSCALER_SERVICES_TRACES_DEPLOYMENT": "ProcessTracesServiceName",
        "LAKERUNNER_AUTOSCALER_SERVICES_TRACES_MAX_REPLICAS": "ProcessTracesReplicas",
    }
    for env_name, param_name in expected.items():
        assert env.get(env_name) == {"Ref": param_name}, (
            f"{env_name} must Ref {param_name}; got {env.get(env_name)!r}"
        )
    for signal in ("LOGS", "METRICS", "TRACES"):
        key = f"LAKERUNNER_AUTOSCALER_SERVICES_{signal}_MIN_REPLICAS"
        assert env.get(key) == "1", f"{key} must default to 1, got {env.get(key)!r}"


def test_only_monitoring_has_autoscaler_env(td):
    """Sweeper / admin-api / alert-evaluator must not see the autoscaler env."""
    for container_name in ("admin-api", "sweeper", "alert-evaluator"):
        _, container = _task_def_for(td, container_name)
        env_names = {e["Name"] for e in container.get("Environment", [])}
        leaked = {n for n in env_names if n.startswith("LAKERUNNER_AUTOSCALER_")}
        leaked.update(env_names & {"ECS_CLUSTER"})
        assert not leaked, f"{container_name} unexpectedly has {leaked}"


# Phase 2: per-service IAM roles are gone — every service shares the
# customer-supplied TaskRoleArn parameter, so monitoring's ECS UpdateService
# permission must be granted on that role by the customer (documented in
# required-roles.md).


# ---------------------------------------------------------------------------
# B → C boundary
# ---------------------------------------------------------------------------


def test_no_shared_resources_in_services_control(td):
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
    assert not found, f"services-control must not own shared resources; found {found}"


def test_control_service_has_ondemand_fallback(td):
    # The merged control service is a deploy-critical singleton: spot-preferred
    # with an on-demand FARGATE fallback so a transient FARGATE_SPOT shortage
    # can't fail the one task a rolling deploy needs.
    svc = td["Resources"]["ControlService"]
    strategy = {s["CapacityProvider"] for s in svc["Properties"]["CapacityProviderStrategy"]}
    assert strategy == {"FARGATE_SPOT", "FARGATE"}
