"""services-process.yaml nested stack: lakerunner process-* and pubsub-sqs services.

Owns nine ECS Fargate services that ingest from SQS and write to S3:

- pubsub-sqs (signal_type: common, fixed shape, replicas-only tunable)
- process-<signal>-<role>, one per PROCESS_ROLES entry (lakerunner #1966: a
  worker runs exactly one role, named by LAKERUNNER_PROCESS_ROLE; there is no
  combined mode). Every role of a signal shares that signal's Process*Memory /
  Process*MinReplicas / Process*Replicas tunables.

None of these services attach to the ALB. The process-* services are created
at one replica (min_replicas) and scale on CPU via native ECS Application Auto
Scaling target-tracking (ECSServiceAverageCPUUtilization at cpu_target_percent,
mirroring the Kubernetes HPA) up to the Process*Replicas cap -- launching at the
max would triple the steady-state Fargate footprint on every deploy. CPU values
come from cardinal-defaults.yaml directly.
"""

from troposphere import (
    GetAtt,
    Output,
    Parameter,
    Ref,
    Sub,
    Template,
)
from troposphere.applicationautoscaling import (
    PredefinedMetricSpecification,
    ScalableTarget,
    ScalingPolicy,
    TargetTrackingScalingPolicyConfiguration,
)
from troposphere.ecs import Environment, Secret

from cardinal_cfn.children import services_common
from cardinal_cfn.defaults import load_defaults
from cardinal_cfn.images import add_image_override
from cardinal_cfn.parameters import (
    add_install_id_parameters,
    add_parameter_group_metadata,
)

# The role table is fixed by the lakerunner binary (internal/metricsprocessing/
# process_role.go); the helm chart renders the same set.
PROCESS_ROLES = {
    "logs": ["ingest", "compact"],
    "metrics": ["partition", "materialize", "compact", "rollup"],
    "traces": ["ingest", "compact"],
}

# shortcut: the seeded metric partition layout is 16 partitions and changes only
# by hand; the materialize role may never run more owners than partitions, so
# its autoscaler is capped here and the binary checks the layout on start.
# Upgrade to a parameter when repartitioning lands in lakerunner.
METRIC_PARTITIONS = 16


def build() -> Template:
    t = Template()
    t.set_description(
        "Cardinal services-process: lakerunner pubsub-sqs and one "
        "process-<signal>-<role> ECS service per worker role. None attach to the ALB."
    )

    defaults = load_defaults()
    lakerunner_capacity = defaults.get("lakerunner_capacity", "ondemand")
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
    t.add_parameter(
        Parameter(
            "QueueUrl",
            Type="String",
            Default="",
            Description=(
                "SQS queue URL for the pubsub-sqs primary queue. "
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
                "primary queue and its bucket."
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
    # per-service blocks below) and the ECS CPU target-tracking policy scales
    # up to this value under load.
    # ---------------------------------------------------------------------
    t.add_parameter(
        Parameter(
            "ProcessLogsReplicas",
            Type="Number",
            Default=str(_max_replicas(logs_cfg)),
            Description=(
                "Maximum replicas the CPU autoscaler may scale each "
                "lakerunner-process-logs-<role> service to. Each is created at "
                "min_replicas and scales up to this cap under load."
            ),
        )
    )
    t.add_parameter(
        Parameter(
            "ProcessLogsMemory",
            Type="String",
            Default=str(logs_cfg["memory_mib"]),
            Description="Fargate memory (MiB) for every lakerunner-process-logs-<role> task.",
        )
    )
    t.add_parameter(
        Parameter(
            "ProcessMetricsReplicas",
            Type="Number",
            Default=str(_max_replicas(metrics_cfg)),
            Description=(
                "Maximum replicas the CPU autoscaler may scale each "
                "lakerunner-process-metrics-<role> service to. Each is created at "
                "min_replicas and scales up to this cap under load."
            ),
        )
    )
    t.add_parameter(
        Parameter(
            "ProcessMetricsMemory",
            Type="String",
            Default=str(metrics_cfg["memory_mib"]),
            Description="Fargate memory (MiB) for every lakerunner-process-metrics-<role> task.",
        )
    )
    t.add_parameter(
        Parameter(
            "ProcessTracesReplicas",
            Type="Number",
            Default=str(_max_replicas(traces_cfg)),
            Description=(
                "Maximum replicas the CPU autoscaler may scale each "
                "lakerunner-process-traces-<role> service to. Each is created at "
                "min_replicas and scales up to this cap under load."
            ),
        )
    )
    t.add_parameter(
        Parameter(
            "ProcessTracesMemory",
            Type="String",
            Default=str(traces_cfg["memory_mib"]),
            Description="Fargate memory (MiB) for every lakerunner-process-traces-<role> task.",
        )
    )
    # Autoscaler floor for process-*; each service is also created at this count.
    for prefix, cfg in (
        ("ProcessLogs", logs_cfg),
        ("ProcessMetrics", metrics_cfg),
        ("ProcessTraces", traces_cfg),
    ):
        t.add_parameter(
            Parameter(
                f"{prefix}MinReplicas",
                Type="Number",
                MinValue=1,
                Default=str(_min_replicas(cfg)),
                Description=(
                    "Minimum replicas the CPU autoscaler keeps running for each "
                    f"role service of this signal. Must not exceed {prefix}Replicas."
                ),
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
                "parameters": ["ProcessLogsMinReplicas", "ProcessLogsReplicas", "ProcessLogsMemory"],
            },
            {
                "label": "Process Metrics tunables",
                "parameters": ["ProcessMetricsMinReplicas", "ProcessMetricsReplicas", "ProcessMetricsMemory"],
            },
            {
                "label": "Process Traces tunables",
                "parameters": ["ProcessTracesMinReplicas", "ProcessTracesReplicas", "ProcessTracesMemory"],
            },
            {
                "label": "Pubsub-SQS tunables",
                "parameters": ["PubsubSqsReplicas", "QueueUrl", "QueueRoleArn"],
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
    # process-* services are created at Process*MinReplicas; a native ECS
    # Application Auto Scaling target-tracking policy (CPU) then scales them up
    # to Process*Replicas. Starting at the max would launch ~3x the steady-state
    # task count on every deploy (and can blow the account's Fargate vCPU
    # quota). pubsub-sqs has no autoscaler, so its DesiredCount is the
    # PubsubSqsReplicas parameter directly.
    # ---------------------------------------------------------------------
    # Temporary hack until tracked fields get a Maestro UI: override the
    # default set of log fields rolled up into the log_field_values fast
    # tag-value lookup table at ingest. Only the logs ingest role reads this
    # (the lone GetLogTrackedFields caller is the log-ingest worklane). Per-org
    # admin-API config still wins.
    #
    # LAKERUNNER_FEATURES_LOG_EMBEDDED_C2_INDEX turns on the LKIDX004 C2
    # text index inside every log .lkrn bundle at ingest. Default ON as
    # of lakerunner v1.90.0 — turning it on changes what a segment IS,
    # not just what it costs (newly written segments carry a C2 root
    # older readers can't open). See kb/specs/lkrn-format/embedded-c2-admission.md
    # in the lakerunner repo.
    logs_ingest_env = [
        Environment(
            Name="LAKERUNNER_LOG_TRACKED_FIELDS",
            Value="service_name,environment_type,installation,proc_name,partition_id",
        ),
        Environment(
            Name="LAKERUNNER_FEATURES_LOG_EMBEDDED_C2_INDEX",
            Value="true",
        ),
    ]
    signal_cfgs = {"logs": logs_cfg, "metrics": metrics_cfg, "traces": traces_cfg}
    services = []
    for signal, roles in PROCESS_ROLES.items():
        prefix = f"Process{signal.capitalize()}"
        for role in roles:
            extra_env = [Environment(Name="LAKERUNNER_PROCESS_ROLE", Value=role)]
            if (signal, role) == ("logs", "ingest"):
                extra_env += logs_ingest_env
            materialize = (signal, role) == ("metrics", "materialize")
            if materialize:
                # The binary refuses to start when this disagrees with the
                # active layout, so a hand-grown layout fails loudly.
                extra_env.append(
                    Environment(Name="LAKERUNNER_METRIC_PARTITIONS", Value=str(METRIC_PARTITIONS))
                )
            services.append(
                {
                    "service_key": f"process-{signal}-{role}",
                    "config": signal_cfgs[signal],
                    "cpu": signal_cfgs[signal]["cpu"],
                    "memory_mib": Ref(f"{prefix}Memory"),
                    "desired_count": Ref(f"{prefix}MinReplicas"),
                    "output_name": f"{prefix}{role.capitalize()}ServiceName",
                    "min_replicas_param": f"{prefix}MinReplicas",
                    "max_replicas_param": f"{prefix}Replicas",
                    "max_capacity": METRIC_PARTITIONS if materialize else None,
                    "extra_env": extra_env,
                }
            )
    services.append(
        {
            "service_key": "pubsub-sqs",
            "config": pubsub_cfg,
            "cpu": pubsub_cfg["cpu"],
            "memory_mib": pubsub_cfg["memory_mib"],
            "desired_count": Ref("PubsubSqsReplicas"),
            "output_name": "PubsubSqsServiceName",
            # Deploy-critical singleton (no autoscaler; desired=PubsubSqsReplicas,
            # default 1): pure on-demand FARGATE so a transient FARGATE_SPOT
            # shortage can't block its one task and trip the deploy circuit
            # breaker. process-* default to fallback (Base=1 + spot scale-out).
            "capacity": "ondemand",
            # pubsub-sqs alone consumes SQS. The lakerunner binary reads the
            # primary queue from three plain env vars. The image is distroless
            # (no shell), so these must be real container env vars. The other
            # three process-* services never read SQS.
            "extra_env": [
                Environment(Name="SQS_QUEUE_URL", Value=Ref("QueueUrl")),
                Environment(Name="SQS_REGION", Value=Ref("AWS::Region")),
                Environment(Name="SQS_ROLE_ARN", Value=Ref("QueueRoleArn")),
            ],
        }
    )

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
            # Autoscaled workers (process-<signal>-<role>) take the
            # build-time "lakerunner_capacity" knob (default "fallback": Base=1
            # on-demand guarantees the first NEW task of a rolling upgrade places
            # even in a transient FARGATE_SPOT shortage, while scale-out replicas
            # ride cheap spot; "ondemand" disables spot). pubsub-sqs overrides
            # this to "ondemand" (deploy-critical singleton, not autoscaled).
            capacity=spec.get("capacity", lakerunner_capacity),
        )
        t.add_output(Output(spec["output_name"], Value=GetAtt(ecs_service, "Name")))

        # process-* services scale on CPU via native ECS Application Auto
        # Scaling, mirroring the Kubernetes HPA (target_percent CPU). pubsub-sqs
        # has no max_replicas_param and is left at its literal DesiredCount.
        if spec.get("max_replicas_param"):
            _add_cpu_autoscaling(
                t,
                ecs_service=ecs_service,
                service_key=spec["service_key"],
                config=spec["config"],
                min_replicas_param=spec["min_replicas_param"],
                max_replicas_param=spec["max_replicas_param"],
                max_capacity=spec.get("max_capacity"),
            )

    return t


def _add_cpu_autoscaling(
    t: Template,
    *,
    ecs_service,
    service_key: str,
    config: dict,
    min_replicas_param: str,
    max_replicas_param: str,
    max_capacity: int | None = None,
):
    """Attach a CPU target-tracking autoscaler to a process-* ECS service.

    `max_capacity` replaces the Process*Replicas cap with a literal (the
    materialize role's partition count).

    Uses native ECS Application Auto Scaling (no RoleARN -> the account's
    AWSServiceRoleForApplicationAutoScaling_ECSService service-linked role,
    created automatically on first use). The target percent comes from the
    service's cardinal-defaults.yaml autoscaling block and matches the
    Kubernetes HPA we run elsewhere.
    """
    title = services_common._resource_title(service_key, "")
    scalable_target = t.add_resource(
        ScalableTarget(
            f"{title}ScalableTarget",
            DependsOn=[ecs_service.title],
            ServiceNamespace="ecs",
            ScalableDimension="ecs:service:DesiredCount",
            ResourceId=Sub(
                "service/${ClusterName}/${ServiceName}",
                ServiceName=GetAtt(ecs_service, "Name"),
            ),
            MinCapacity=Ref(min_replicas_param),
            MaxCapacity=max_capacity if max_capacity is not None else Ref(max_replicas_param),
        )
    )
    t.add_resource(
        ScalingPolicy(
            f"{title}CpuScalingPolicy",
            PolicyName=Sub("${ServiceName}-cpu", ServiceName=GetAtt(ecs_service, "Name")),
            PolicyType="TargetTrackingScaling",
            ScalingTargetId=Ref(scalable_target),
            TargetTrackingScalingPolicyConfiguration=TargetTrackingScalingPolicyConfiguration(
                PredefinedMetricSpecification=PredefinedMetricSpecification(
                    PredefinedMetricType="ECSServiceAverageCPUUtilization",
                ),
                TargetValue=float(config["autoscaling"]["cpu_target_percent"]),
            ),
        )
    )


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
    capacity: str = "fallback",
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
    """Process*MinReplicas default for an autoscaling-eligible service.

    The service is created at this count; the CPU target-tracking policy scales
    it up to the Process*Replicas cap under load. Falls back to `replicas` if
    autoscaling is absent.
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
