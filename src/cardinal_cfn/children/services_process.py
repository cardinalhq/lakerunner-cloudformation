"""services-process.yaml nested stack: lakerunner process-* and pubsub-sqs services.

Owns four ECS Fargate services that ingest from SQS and write to S3:

- pubsub-sqs (signal_type: common, fixed shape, replicas-only tunable)
- process-logs (signal_type: logs, replicas + memory tunable)
- process-metrics (signal_type: metrics, replicas + memory tunable)
- process-traces (signal_type: traces, replicas + memory tunable)

None of these services attach to the ALB. The process-* services are created
at one replica (min_replicas); the monitoring service in services-control
scales them up to the Process*Replicas cap via ecs:UpdateService -- launching
at the max would triple the steady-state Fargate footprint on every deploy.
CPU values come from cardinal-defaults.yaml directly.
"""

from troposphere import (
    GetAtt,
    Output,
    Parameter,
    Ref,
    Sub,
    Template,
)
from troposphere.ecs import Environment, Secret

from cardinal_cfn.children import services_common
from cardinal_cfn.defaults import load_defaults
from cardinal_cfn.images import add_image_override
from cardinal_cfn.parameters import (
    add_install_id_parameters,
    add_parameter_group_metadata,
)


def build() -> Template:
    t = Template()
    t.set_description(
        "Cardinal services-process: lakerunner pubsub-sqs and process-logs/"
        "metrics/traces ECS services. None attach to the ALB."
    )

    defaults = load_defaults()
    pubsub_cfg = defaults["services"]["lakerunner-pubsub-sqs"]
    logs_cfg = defaults["services"]["lakerunner-process-logs"]
    metrics_cfg = defaults["services"]["lakerunner-process-metrics"]
    traces_cfg = defaults["services"]["lakerunner-process-traces"]

    add_install_id_parameters(t)

    # ---------------------------------------------------------------------
    # Cross-stack inputs (forwarded from root)
    # ---------------------------------------------------------------------
    t.add_parameter(Parameter("ClusterArn", Type="String", Description="ECS cluster ARN."))
    t.add_parameter(Parameter("ClusterName", Type="String", Description="ECS cluster name."))
    t.add_parameter(
        Parameter(
            "TaskSecurityGroupId",
            Type="AWS::EC2::SecurityGroup::Id",
            Description="ECS task security group ID from the cluster stack.",
        )
    )
    t.add_parameter(
        Parameter("ExecutionRoleArn", Type="String", Description="ECS task execution role ARN.")
    )
    t.add_parameter(
        Parameter("TaskRoleArn", Type="String", Description="ECS task role ARN (shared across all services).")
    )
    t.add_parameter(
        Parameter(
            "PrivateSubnetsCsv",
            Type="String",
            Description="Comma-separated private subnet IDs.",
        )
    )
    t.add_parameter(Parameter("DbEndpoint", Type="String", Description="RDS endpoint hostname."))
    t.add_parameter(Parameter("DbPort", Type="String", Default="5432", Description="RDS port."))
    t.add_parameter(
        Parameter("DbSecretArn", Type="String", Description="ARN of the DB master secret.")
    )
    t.add_parameter(
        Parameter("BucketName", Type="String", Description="Name of the ingest S3 bucket.")
    )
    # SQS inputs for the pubsub-sqs service's primary queue (group 0). The
    # lakerunner binary reads SQS_QUEUE_URL / SQS_REGION / SQS_ROLE_ARN as three
    # plain env vars; an empty queue URL idles the service. Multi-account
    # fan-out (numbered SQS_*_N groups) will later use ECS environmentFiles from
    # S3; this only handles the single primary queue.
    t.add_parameter(
        Parameter(
            "QueueUrl",
            Type="String",
            Default="",
            Description=(
                "SQS queue URL for the pubsub-sqs primary queue (group 0). "
                "Empty leaves the pubsub-sqs service idle."
            ),
        )
    )
    t.add_parameter(
        Parameter(
            "QueueRoleArn",
            Type="String",
            Default="",
            Description=(
                "IAM role ARN the pubsub-sqs service STS-assumes to read the "
                "primary queue and its bucket (group 0)."
            ),
        )
    )
    t.add_parameter(
        Parameter(
            "LicenseSecretArn",
            Type="String",
            Description="ARN of the license Secrets Manager secret.",
        )
    )
    t.add_parameter(
        Parameter(
            "SelfTelemetryEndpoint",
            Type="String",
            Default="",
            Description="OTLP gRPC URL for the in-cluster otel-collector. Empty when SelfTelemetry is disabled.",
        )
    )
    t.add_parameter(
        Parameter(
            "SelfTelemetryEnabled",
            Type="String",
            Default="false",
            AllowedValues=["true", "false"],
            Description="ENABLE_OTLP_TELEMETRY flag for lakerunner containers in this tier.",
        )
    )

    # MigrationComplete is unused inside this stack on purpose. The root passes
    # the migration-stack output through this parameter; CloudFormation cannot
    # render this nested stack until the migration stack finishes producing
    # that output, so depending on the parameter is enough.
    t.add_parameter(
        Parameter(
            "MigrationComplete",
            Type="String",
            Description=(
                "Sentinel forwarded from the migration stack output. Forces this "
                "stack to wait for migration to finish; not used inside the stack."
            ),
        )
    )

    # ---------------------------------------------------------------------
    # Image override
    # ---------------------------------------------------------------------
    image_ref = add_image_override(
        t,
        name="LakerunnerImage",
        default=defaults["images"]["lakerunner"],
        description="Container image for all lakerunner services in this tier.",
    )

    # ---------------------------------------------------------------------
    # Per-service tunables (defaults from cardinal-defaults.yaml).
    #
    # Per the project CLAUDE.md table: process-* services expose Replicas and
    # Memory as parameters; CPU stays in YAML. pubsub-sqs exposes Replicas
    # only; CPU and Memory both stay in YAML.
    #
    # For process-*, "Replicas" is the autoscaler's *max* cap, not the initial
    # desired count -- the services are created at min_replicas (see the
    # per-service blocks below) and the monitoring service scales up to this
    # value. The same Refs are forwarded to services-control as the
    # autoscaler's per-service max.
    # ---------------------------------------------------------------------
    t.add_parameter(
        Parameter(
            "ProcessLogsReplicas",
            Type="Number",
            Default=str(_max_replicas(logs_cfg)),
            Description=(
                "Maximum replicas the monitoring autoscaler may scale "
                "lakerunner-process-logs to. The service is created at "
                "min_replicas and the autoscaler scales it up to this cap "
                "under load."
            ),
        )
    )
    t.add_parameter(
        Parameter(
            "ProcessLogsMemory",
            Type="String",
            Default=str(logs_cfg["memory_mib"]),
            Description="Fargate memory (MiB) for lakerunner-process-logs.",
        )
    )
    t.add_parameter(
        Parameter(
            "ProcessMetricsReplicas",
            Type="Number",
            Default=str(_max_replicas(metrics_cfg)),
            Description=(
                "Maximum replicas the monitoring autoscaler may scale "
                "lakerunner-process-metrics to. The service is created at "
                "min_replicas and the autoscaler scales it up to this cap "
                "under load."
            ),
        )
    )
    t.add_parameter(
        Parameter(
            "ProcessMetricsMemory",
            Type="String",
            Default=str(metrics_cfg["memory_mib"]),
            Description="Fargate memory (MiB) for lakerunner-process-metrics.",
        )
    )
    t.add_parameter(
        Parameter(
            "ProcessTracesReplicas",
            Type="Number",
            Default=str(_max_replicas(traces_cfg)),
            Description=(
                "Maximum replicas the monitoring autoscaler may scale "
                "lakerunner-process-traces to. The service is created at "
                "min_replicas and the autoscaler scales it up to this cap "
                "under load."
            ),
        )
    )
    t.add_parameter(
        Parameter(
            "ProcessTracesMemory",
            Type="String",
            Default=str(traces_cfg["memory_mib"]),
            Description="Fargate memory (MiB) for lakerunner-process-traces.",
        )
    )
    t.add_parameter(
        Parameter(
            "PubsubSqsReplicas",
            Type="Number",
            Default=str(pubsub_cfg["replicas"]),
            Description="Desired replicas for lakerunner-pubsub-sqs.",
        )
    )
    t.add_parameter(
        Parameter(
            "PubsubAutoRegister",
            Type="String",
            Default="false",
            AllowedValues=["true", "false"],
            Description=(
                "Enable pubsub-sqs auto-registration of unseen satellite raw "
                "buckets. When true, the worker registers new orgs and routes "
                "cooked output to PubsubAutoRegisterWritesToInstance."
            ),
        )
    )
    t.add_parameter(
        Parameter(
            "PubsubAutoRegisterWritesToInstance",
            Type="String",
            Default="1",
            Description=(
                "Central cooked-bucket instance_num that pubsub-sqs auto-"
                "registered orgs write to. Required when PubsubAutoRegister "
                "is true."
            ),
        )
    )

    # ---------------------------------------------------------------------
    # Console parameter grouping
    # ---------------------------------------------------------------------
    add_parameter_group_metadata(
        t,
        groups=[
            {
                "label": "Cross-stack inputs",
                "parameters": [
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
                    "LicenseSecretArn",
                    "MigrationComplete",
                ],
            },
            {
                "label": "Process Logs tunables",
                "parameters": ["ProcessLogsReplicas", "ProcessLogsMemory"],
            },
            {
                "label": "Process Metrics tunables",
                "parameters": ["ProcessMetricsReplicas", "ProcessMetricsMemory"],
            },
            {
                "label": "Process Traces tunables",
                "parameters": ["ProcessTracesReplicas", "ProcessTracesMemory"],
            },
            {
                "label": "Pubsub-SQS tunables",
                "parameters": [
                    "PubsubSqsReplicas",
                    "QueueUrl",
                    "QueueRoleArn",
                    "PubsubAutoRegister",
                    "PubsubAutoRegisterWritesToInstance",
                ],
            },
            {
                "label": "Image overrides",
                "parameters": ["LakerunnerImage"],
            },
        ],
    )

    # ---------------------------------------------------------------------
    # Per-service shared environment / secrets
    # ---------------------------------------------------------------------
    base_env = [
        Environment(Name="LRDB_HOST", Value=Ref("DbEndpoint")),
        Environment(Name="LRDB_PORT", Value=Ref("DbPort")),
        Environment(Name="LRDB_DBNAME", Value="lakerunner"),
        Environment(Name="LRDB_SSLMODE", Value="require"),
        Environment(Name="LRDB_S3_BUCKET", Value=Ref("BucketName")),
        Environment(Name="CONFIGDB_HOST", Value=Ref("DbEndpoint")),
        Environment(Name="CONFIGDB_PORT", Value=Ref("DbPort")),
        Environment(Name="CONFIGDB_DBNAME", Value="configdb"),
        Environment(Name="CONFIGDB_SSLMODE", Value="require"),
    ]

    base_secrets = [
        Secret(Name="LRDB_USER", ValueFrom=Sub("${DbSecretArn}:username::")),
        Secret(Name="LRDB_PASSWORD", ValueFrom=Sub("${DbSecretArn}:password::")),
        Secret(Name="CONFIGDB_USER", ValueFrom=Sub("${DbSecretArn}:username::")),
        Secret(Name="CONFIGDB_PASSWORD", ValueFrom=Sub("${DbSecretArn}:password::")),
        Secret(Name="LICENSE_DATA", ValueFrom=Ref("LicenseSecretArn")),
    ]

    # ---------------------------------------------------------------------
    # Per-service blocks (log group, task def, ECS service).
    #
    # process-* services are created at their min replica count; the monitoring
    # service (services-control) then scales them up to Process*Replicas via
    # ecs:UpdateService. Starting at the max would launch ~3x the steady-state
    # task count on every deploy (and can blow the account's Fargate vCPU
    # quota). pubsub-sqs has no autoscaler, so its DesiredCount is the
    # PubsubSqsReplicas parameter directly.
    # ---------------------------------------------------------------------
    services = [
        {
            "service_key": "process-logs",
            "config": logs_cfg,
            "cpu": logs_cfg["cpu"],
            "memory_mib": Ref("ProcessLogsMemory"),
            "desired_count": _min_replicas(logs_cfg),
            "output_name": "ProcessLogsServiceName",
        },
        {
            "service_key": "process-metrics",
            "config": metrics_cfg,
            "cpu": metrics_cfg["cpu"],
            "memory_mib": Ref("ProcessMetricsMemory"),
            "desired_count": _min_replicas(metrics_cfg),
            "output_name": "ProcessMetricsServiceName",
        },
        {
            "service_key": "process-traces",
            "config": traces_cfg,
            "cpu": traces_cfg["cpu"],
            "memory_mib": Ref("ProcessTracesMemory"),
            "desired_count": _min_replicas(traces_cfg),
            "output_name": "ProcessTracesServiceName",
        },
        {
            "service_key": "pubsub-sqs",
            "config": pubsub_cfg,
            "cpu": pubsub_cfg["cpu"],
            "memory_mib": pubsub_cfg["memory_mib"],
            "desired_count": Ref("PubsubSqsReplicas"),
            "output_name": "PubsubSqsServiceName",
            # Singleton (no autoscaler): give it an on-demand FARGATE fallback
            # so a transient FARGATE_SPOT shortage can't block its one task and
            # trip the deploy circuit breaker. process-* stay pure spot.
            "capacity": "fallback",
            # pubsub-sqs alone consumes SQS. The lakerunner binary reads the
            # primary queue (group 0) from three plain env vars. The image is
            # distroless (no shell), so these must be real container env vars,
            # not a shell-evalable blob. The other three process-* services
            # never read SQS. Multi-account fan-out (numbered SQS_*_N groups)
            # will later use ECS environmentFiles from S3.
            "extra_env": [
                Environment(Name="SQS_QUEUE_URL", Value=Ref("QueueUrl")),
                Environment(Name="SQS_REGION", Value=Ref("AWS::Region")),
                Environment(Name="SQS_ROLE_ARN", Value=Ref("QueueRoleArn")),
                Environment(Name="LAKERUNNER_PUBSUB_AUTOREGISTER", Value=Ref("PubsubAutoRegister")),
                Environment(
                    Name="LAKERUNNER_PUBSUB_AUTOREGISTER_WRITES_TO_INSTANCE",
                    Value=Ref("PubsubAutoRegisterWritesToInstance"),
                ),
            ],
        },
    ]

    for spec in services:
        ecs_service = _build_service_block(
            t,
            service_key=spec["service_key"],
            config=spec["config"],
            image_ref=image_ref,
            cpu=spec["cpu"],
            memory_mib=spec["memory_mib"],
            desired_count=spec["desired_count"],
            base_env=base_env,
            base_secrets=base_secrets,
            extra_env=spec.get("extra_env"),
            capacity=spec.get("capacity", "spot"),
        )
        t.add_output(Output(spec["output_name"], Value=GetAtt(ecs_service, "Name")))

    return t


def _build_service_block(
    t: Template,
    *,
    service_key: str,
    config: dict,
    image_ref,
    cpu,
    memory_mib,
    desired_count,
    base_env: list,
    base_secrets: list,
    extra_env: list | None = None,
    capacity: str = "spot",
):
    """Wire up the three resources (log group, task def, service) for one service."""
    log_group = t.add_resource(services_common.build_log_group(service_key=service_key))
    env = (
        list(base_env)
        + services_common.lakerunner_otel_env(service_key=service_key)
        + _service_specific_env(config)
        + list(extra_env or [])
    )
    task_def = t.add_resource(
        services_common.build_task_definition(
            service_key=service_key,
            image_ref=image_ref,
            cpu=cpu,
            memory_mib=memory_mib,
            command=config.get("command"),
            execution_role_arn_param="ExecutionRoleArn",
            task_role_arn=Ref("TaskRoleArn"),
            environment=env,
            secrets=base_secrets,
            log_group_ref=log_group,
        )
    )
    return t.add_resource(
        services_common.build_ecs_service(
            service_key=service_key,
            cluster_arn_param="ClusterArn",
            task_definition_ref=task_def,
            desired_count=desired_count,
            subnets_csv_param="PrivateSubnetsCsv",
            security_group_id_param="TaskSecurityGroupId",
            container_name=service_key,
            capacity=capacity,
        )
    )


def _max_replicas(service_cfg: dict) -> int:
    """Autoscaler max-replica cap for an autoscaling-eligible service.

    process-* configs encode min/max under autoscaling; the Process*Replicas
    parameter default uses max_replicas. Falls back to `replicas` if
    autoscaling is absent.
    """
    autoscaling = service_cfg.get("autoscaling")
    if autoscaling and "max_replicas" in autoscaling:
        return int(autoscaling["max_replicas"])
    return int(service_cfg["replicas"])


def _min_replicas(service_cfg: dict) -> int:
    """Initial ECS DesiredCount for an autoscaling-eligible service.

    The service is created at this count; the monitoring service (in
    services-control) scales it up to the Process*Replicas cap under load.
    Falls back to `replicas` if autoscaling is absent.
    """
    autoscaling = service_cfg.get("autoscaling")
    if autoscaling and "min_replicas" in autoscaling:
        return int(autoscaling["min_replicas"])
    return int(service_cfg["replicas"])


def _service_specific_env(service_cfg: dict) -> list:
    """Convert the YAML environment dict into a list of ECS Environment objects."""
    env = service_cfg.get("environment") or {}
    return [Environment(Name=k, Value=str(v)) for k, v in env.items()]


if __name__ == "__main__":
    print(build().to_yaml())
