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
        "TaskRoleArn",
        "PrivateSubnetsCsv",
        "DbEndpoint",
        "DbPort",
        "DbSecretArn",
        "BucketName",
        "QueueUrl",
        "QueueRoleArn",
        "LicenseSecretArn",
        "MigrationComplete",
        "LakerunnerImage",
    ):
        assert n in td["Parameters"], f"missing parameter: {n}"


def test_pubsub_queue_parameters_default_empty(td):
    """The pubsub-sqs primary queue (group 0) is supplied via plain QueueUrl /
    QueueRoleArn params; an empty QueueUrl leaves the service idle."""
    for n in ("QueueUrl", "QueueRoleArn"):
        assert n in td["Parameters"], f"missing parameter: {n}"
        assert td["Parameters"][n]["Default"] == "", n


def test_old_pubsub_sqs_env_parameter_is_gone(td):
    """The shell-evalable PubsubSqsEnv blob is replaced by plain env vars."""
    assert "PubsubSqsEnv" not in td["Parameters"]


def test_no_alb_parameters(td):
    """services-process does not attach to the ALB and must not declare ALB inputs."""
    for n in ("HttpsListenerArn", "VpcId"):
        assert n not in td["Parameters"], f"unexpected ALB parameter present: {n}"


def test_no_storage_profile_or_api_keys_params(td):
    """The migrator seeds configdb from these SSM parameters; service tasks
    read profiles from configdb at runtime, so these are not threaded here."""
    for n in ("ApiKeysParamName", "StorageProfilesParamName"):
        assert n not in td["Parameters"], f"unexpected parameter: {n}"


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


def test_no_internally_managed_iam_roles(td):
    """Phase 2: all services share the customer-supplied TaskRoleArn parameter."""
    roles = [r for r in td["Resources"].values() if r["Type"] == "AWS::IAM::Role"]
    assert len(roles) == 0


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


def test_process_services_start_at_one_replica(td):
    """process-* are created at min_replicas (1); the monitoring autoscaler in
    services-control scales them up to the Process*Replicas cap. Launching at
    the max would triple the steady-state Fargate footprint on every deploy."""
    services = {
        logical_id: r
        for logical_id, r in td["Resources"].items()
        if r["Type"] == "AWS::ECS::Service"
    }
    for logical_id in ("ProcessLogsService", "ProcessMetricsService", "ProcessTracesService"):
        dc = services[logical_id]["Properties"]["DesiredCount"]
        assert dc == 1, f"{logical_id} should be created at 1 replica; got {dc!r}"


def test_pubsub_sqs_desired_count_uses_replicas_param(td):
    """pubsub-sqs has no autoscaler, so its DesiredCount is the parameter."""
    services = {
        logical_id: r
        for logical_id, r in td["Resources"].items()
        if r["Type"] == "AWS::ECS::Service"
    }
    dc = services["PubsubSqsService"]["Properties"]["DesiredCount"]
    assert isinstance(dc, dict) and dc.get("Ref") == "PubsubSqsReplicas", (
        f"PubsubSqsService DesiredCount should be Ref(PubsubSqsReplicas); got {dc!r}"
    )


def test_process_replicas_params_default_to_autoscaler_max(td):
    """Process*Replicas defaults are the autoscaler ceiling (max_replicas from
    cardinal-defaults.yaml), not the initial desired count."""
    for n in ("ProcessLogsReplicas", "ProcessMetricsReplicas", "ProcessTracesReplicas"):
        assert td["Parameters"][n]["Default"] == "10", (
            f"{n} default should be the autoscaling max; got {td['Parameters'][n]['Default']!r}"
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


# ---------------------------------------------------------------------------
# pubsub-sqs plain SQS env vars (group 0)
# ---------------------------------------------------------------------------


def _container(td, task_def_id):
    task_def = td["Resources"][task_def_id]
    return task_def["Properties"]["ContainerDefinitions"][0]


def _env(td, task_def_id):
    container = _container(td, task_def_id)
    return {e["Name"]: e["Value"] for e in container.get("Environment", [])}


def test_pubsub_sqs_has_plain_group0_env(td):
    """pubsub-sqs sets the group-0 SQS env as three plain container env vars
    (the distroless image has no shell to eval a blob)."""
    env = _env(td, "PubsubSqsTaskDef")
    assert env["SQS_QUEUE_URL"] == {"Ref": "QueueUrl"}, env.get("SQS_QUEUE_URL")
    assert env["SQS_REGION"] == {"Ref": "AWS::Region"}, env.get("SQS_REGION")
    assert env["SQS_ROLE_ARN"] == {"Ref": "QueueRoleArn"}, env.get("SQS_ROLE_ARN")
    assert "PUBSUB_SQS_ENV" not in env, "shell-evalable blob still present"


def test_no_container_command_uses_shell_wrapper(td):
    """No container wraps its command in a shell (`sh -c ... eval`): the image
    is distroless. Commands stay the plain lakerunner invocation from defaults."""
    for tdef in td["Resources"].values():
        if tdef["Type"] != "AWS::ECS::TaskDefinition":
            continue
        for container in tdef["Properties"]["ContainerDefinitions"]:
            command = container.get("Command") or []
            assert "sh" not in command, f"{container.get('Name')} command uses sh: {command!r}"
            joined = " ".join(str(c) for c in command)
            assert "sh -c" not in joined and "eval" not in joined, (
                f"{container.get('Name')} command wrapped in a shell: {command!r}"
            )


def test_process_services_have_no_sqs_env(td):
    """process-logs/metrics/traces never consume SQS: no SQS_* env vars."""
    for task_def_id in ("ProcessLogsTaskDef", "ProcessMetricsTaskDef", "ProcessTracesTaskDef"):
        env = _env(td, task_def_id)
        for n in ("SQS_QUEUE_URL", "SQS_REGION", "SQS_ROLE_ARN", "PUBSUB_SQS_ENV"):
            assert n not in env, f"{task_def_id} unexpectedly has {n}"


# ---------------------------------------------------------------------------
# pubsub-sqs auto-registration env vars
# ---------------------------------------------------------------------------


def test_pubsub_autoregister_params_exist_with_defaults(td):
    """PubsubAutoRegister and PubsubAutoRegisterWritesToInstance are declared
    with the expected defaults."""
    params = td["Parameters"]
    assert "PubsubAutoRegister" in params
    assert params["PubsubAutoRegister"]["Default"] == "false"
    assert params["PubsubAutoRegister"]["AllowedValues"] == ["true", "false"]
    assert "PubsubAutoRegisterWritesToInstance" in params
    assert params["PubsubAutoRegisterWritesToInstance"]["Default"] == "1"


def test_pubsub_sqs_has_autoregister_env_vars(td):
    """pubsub-sqs container carries the two auto-registration env vars wired
    to their respective parameters."""
    env = _env(td, "PubsubSqsTaskDef")
    assert env.get("LAKERUNNER_PUBSUB_AUTOREGISTER") == {"Ref": "PubsubAutoRegister"}, (
        env.get("LAKERUNNER_PUBSUB_AUTOREGISTER")
    )
    assert env.get("LAKERUNNER_PUBSUB_AUTOREGISTER_WRITES_TO_INSTANCE") == (
        {"Ref": "PubsubAutoRegisterWritesToInstance"}
    ), env.get("LAKERUNNER_PUBSUB_AUTOREGISTER_WRITES_TO_INSTANCE")


def test_process_services_have_no_autoregister_env(td):
    """process-logs/metrics/traces must NOT carry the auto-registration env vars."""
    for task_def_id in ("ProcessLogsTaskDef", "ProcessMetricsTaskDef", "ProcessTracesTaskDef"):
        env = _env(td, task_def_id)
        assert "LAKERUNNER_PUBSUB_AUTOREGISTER" not in env, (
            f"{task_def_id} unexpectedly has LAKERUNNER_PUBSUB_AUTOREGISTER"
        )
        assert "LAKERUNNER_PUBSUB_AUTOREGISTER_WRITES_TO_INSTANCE" not in env, (
            f"{task_def_id} unexpectedly has LAKERUNNER_PUBSUB_AUTOREGISTER_WRITES_TO_INSTANCE"
        )


# ---------------------------------------------------------------------------
# B → C boundary
# ---------------------------------------------------------------------------


def test_no_shared_resources_in_services_process(td):
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
    assert not found, f"services-process must not own shared resources; found {found}"


def _service_strategy(td, suffix):
    res = next(
        r for lid, r in td["Resources"].items()
        if r["Type"] == "AWS::ECS::Service" and lid.endswith(suffix)
    )
    return {s["CapacityProvider"] for s in res["Properties"]["CapacityProviderStrategy"]}


def _service_items(td, suffix):
    res = next(
        r for lid, r in td["Resources"].items()
        if r["Type"] == "AWS::ECS::Service" and lid.endswith(suffix)
    )
    return res["Properties"]["CapacityProviderStrategy"]


def _assert_on_demand(items):
    # Pure on-demand FARGATE for all replicas — the only deploy-reliable choice.
    assert items == [{"CapacityProvider": "FARGATE", "Weight": 1}]
    assert all("Base" not in i for i in items)


def test_pubsub_sqs_is_pure_on_demand(td):
    # pubsub-sqs is a non-autoscaled singleton: pure on-demand FARGATE so a
    # rolling deploy always places. No FARGATE_SPOT.
    assert _service_strategy(td, "PubsubSqsService") == {"FARGATE"}
    _assert_on_demand(_service_items(td, "PubsubSqsService"))


def test_process_workers_are_pure_on_demand(td):
    # process-* run pure on-demand FARGATE. Every rolling deploy must place a
    # NEW task before draining the old, and FARGATE_SPOT can't guarantee
    # placement, so no spot tier anywhere.
    for suffix in ("ProcessLogsService", "ProcessMetricsService", "ProcessTracesService"):
        assert _service_strategy(td, suffix) == {"FARGATE"}, suffix
        _assert_on_demand(_service_items(td, suffix))
