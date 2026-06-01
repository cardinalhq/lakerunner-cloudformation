"""services-control.yaml nested stack: lakerunner control-plane ECS service.

Owns a SINGLE ECS Fargate service running one task with four containers:

- admin-api (ALB-attached on dedicated 9443 listener; path catch-all `/*`,
  also registered in Cloud Map as `admin-api.<namespace>:9091` so peers
  inside the cluster — currently just maestro — can reach it without
  hairpinning through the ALB and tripping the self-signed cert path)
- sweeper (internal)
- monitoring (internal; gRPC port not exposed on the ALB; drives ECS
  autoscaling of the process-* services via ecs:UpdateService)
- alert-evaluator (internal)

These four control-plane components are tiny (~1-3 millicores, ~20-25 MiB
each). Four separate Fargate tasks would each pay the 0.25 vCPU / 0.5 GB
per-task floor; co-locating them in one task cuts that ~4x and means one task
to place instead of four (a real win under spot scarcity). The task runs at a
small fixed shape (256 CPU / 512 MiB); their combined real usage is ~7m / ~90Mi.
All four containers are essential. CPU/memory/replicas are not exposed as
deployment-time tunables.
"""

from troposphere import (
    GetAtt,
    Output,
    Parameter,
    Ref,
    Sub,
    Template,
)
from troposphere.ecs import (
    ContainerDefinition,
    Environment,
    LogConfiguration,
    PortMapping,
    RuntimePlatform,
    Secret,
    TaskDefinition,
)
from troposphere.servicediscovery import (
    DnsConfig,
    DnsRecord,
    Service as DiscoveryService,
)

from cardinal_cfn.children import services_common
from cardinal_cfn.defaults import load_defaults
from cardinal_cfn.images import add_image_override
from cardinal_cfn.naming import cardinal_tags
from cardinal_cfn.parameters import (
    add_install_id_parameters,
    add_parameter_group_metadata,
)

# Task-level shape for the merged control service. Fargate's smallest valid
# size; the four containers' combined steady-state usage is ~7m CPU / ~90Mi.
CONTROL_TASK_CPU = "256"
CONTROL_TASK_MEMORY = "512"


def build() -> Template:
    t = Template()
    t.set_description(
        "Cardinal services-control: a single ECS service running one task with "
        "four containers — admin-api (ALB-attached), sweeper, monitoring, and "
        "alert-evaluator."
    )

    defaults = load_defaults()
    admin_cfg = defaults["services"]["lakerunner-admin-api"]
    sweeper_cfg = defaults["services"]["lakerunner-sweeper"]
    monitoring_cfg = defaults["services"]["lakerunner-monitoring"]
    alert_cfg = defaults["services"]["lakerunner-alert-evaluator"]

    admin_container_port = int(admin_cfg["ingress"]["container_port"])
    admin_health_path = admin_cfg["ingress"].get("health_check_path", "/healthz")

    add_install_id_parameters(t)

    # ---------------------------------------------------------------------
    # Cross-stack inputs (forwarded from root)
    # ---------------------------------------------------------------------
    t.add_parameter(Parameter("ClusterArn", Type="String", Description="ECS cluster ARN."))
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
    t.add_parameter(
        Parameter("HttpsListenerArn", Type="String", Description="ARN of the ALB HTTPS listener.")
    )
    t.add_parameter(
        Parameter(
            "AdminHttpsListenerArn",
            Type="String",
            Description=(
                "ARN of the dedicated admin-api HTTPS listener (port 9443). "
                "admin-api owns the catch-all on this listener so the "
                "container sees unprefixed paths."
            ),
        )
    )
    t.add_parameter(
        Parameter(
            "AdminApiKeySecretArn",
            Type="String",
            Description=(
                "ARN of the admin-api initial-key secret. Forwarded into the "
                "admin-api container as ADMIN_INITIAL_API_KEY so the binary "
                "seeds its first valid admin key on startup."
            ),
        )
    )
    t.add_parameter(
        Parameter("VpcId", Type="AWS::EC2::VPC::Id", Description="VPC ID (forwarded from root).")
    )
    t.add_parameter(
        Parameter(
            "ServiceNamespaceName",
            Type="String",
            Description="Cloud Map private DNS namespace name (e.g. cardinal-<id>.local).",
        )
    )
    t.add_parameter(
        Parameter(
            "ServiceNamespaceId",
            Type="String",
            Description=(
                "Cloud Map private DNS namespace ID. Used to register "
                "admin-api so in-cluster peers (maestro) can reach it at "
                "admin-api.<namespace>:9091 without going through the ALB."
            ),
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

    # Inputs the monitoring service uses to drive ECS-based autoscaling of the
    # process-* services. ClusterName is the ECS cluster name (not ARN) — both
    # the autoscaler's ECS_CLUSTER env var and the IAM resource ARNs need the
    # name form. The three service-name inputs come from services-process stack
    # outputs; the three replica inputs are the per-service max replicas (the
    # Process*Replicas parameters, also exposed on services-process). The
    # process-* services are created at one replica there; the autoscaler then
    # scales them up to this max, so it tracks whatever the customer set at
    # deploy time.
    t.add_parameter(
        Parameter(
            "ClusterName",
            Type="String",
            Description="ECS cluster name (not ARN), used by the monitoring autoscaler.",
        )
    )
    t.add_parameter(
        Parameter(
            "ProcessLogsServiceName",
            Type="String",
            Description="ECS service name for lakerunner-process-logs.",
        )
    )
    t.add_parameter(
        Parameter(
            "ProcessMetricsServiceName",
            Type="String",
            Description="ECS service name for lakerunner-process-metrics.",
        )
    )
    t.add_parameter(
        Parameter(
            "ProcessTracesServiceName",
            Type="String",
            Description="ECS service name for lakerunner-process-traces.",
        )
    )
    t.add_parameter(
        Parameter(
            "ProcessLogsReplicas",
            Type="Number",
            Description="Maximum desired replicas for lakerunner-process-logs.",
        )
    )
    t.add_parameter(
        Parameter(
            "ProcessMetricsReplicas",
            Type="Number",
            Description="Maximum desired replicas for lakerunner-process-metrics.",
        )
    )
    t.add_parameter(
        Parameter(
            "ProcessTracesReplicas",
            Type="Number",
            Description="Maximum desired replicas for lakerunner-process-traces.",
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
                    "HttpsListenerArn",
                    "VpcId",
                    "DbEndpoint",
                    "DbPort",
                    "DbSecretArn",
                    "BucketName",
                    "LicenseSecretArn",
                    "MigrationComplete",
                    "ClusterName",
                    "ProcessLogsServiceName",
                    "ProcessMetricsServiceName",
                    "ProcessTracesServiceName",
                    "ProcessLogsReplicas",
                    "ProcessMetricsReplicas",
                    "ProcessTracesReplicas",
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
    # Per-container env. Each container keeps EXACTLY the env it had when these
    # were four separate services: base DB env + per-component OTel env +
    # YAML-declared env + any component-specific extras.
    # ---------------------------------------------------------------------
    # admin-api: ALB-attached, owns the admin-key secret.
    admin_env = (
        list(base_env)
        + services_common.lakerunner_otel_env(service_key="admin-api")
        + _service_specific_env(admin_cfg)
    )
    # Seed the lakerunner admin-api binary's first valid admin key. Without
    # this, every Authorization: Bearer <key> request fails validation
    # because the configdb has no admin keys and the binary won't accept
    # the one we want to use.
    admin_secrets = list(base_secrets) + [
        Secret(
            Name="ADMIN_INITIAL_API_KEY",
            ValueFrom=Sub("${AdminApiKeySecretArn}:key::"),
        ),
    ]

    sweeper_env = (
        list(base_env)
        + services_common.lakerunner_otel_env(service_key="sweeper")
        + _service_specific_env(sweeper_cfg)
    )

    # alert-evaluator reaches query-api over the in-cluster Cloud Map DNS.
    # Container port is 8080 (matches lakerunner-query-api.ingress.container_port).
    alert_env = (
        list(base_env)
        + services_common.lakerunner_otel_env(service_key="alert-evaluator")
        + _service_specific_env(alert_cfg)
        + [
            Environment(
                Name="ALERT_EVALUATOR_QUERY_API_URL",
                Value=Sub("http://query-api.${ServiceNamespaceName}:8080"),
            ),
        ]
    )

    # The monitoring container drives ECS autoscaling of the process-* services.
    # Env vars match cmd/monitoring.go + config/autoscaler.go in the lakerunner
    # repo; the shared TaskRole (ControlRole) carries the ecs:UpdateService /
    # DescribeServices grant it needs.
    # The lakerunner binary defaults Autoscaler.ObserveOnly=true and per-service
    # MinReplicas=0, which would log decisions but never scale and would scale
    # services to zero on idle. Override both: the customer set max replicas to
    # opt into actual scaling, and 1 is the documented floor (lakerunner
    # docs/guides/admin/autoscaling.md: "Set to 1 to prevent scale-to-zero").
    monitoring_env = (
        list(base_env)
        + services_common.lakerunner_otel_env(service_key="monitoring")
        + _service_specific_env(monitoring_cfg)
        + [
            Environment(Name="LAKERUNNER_AUTOSCALER_ENABLED", Value="true"),
            Environment(Name="LAKERUNNER_AUTOSCALER_OBSERVE_ONLY", Value="false"),
            Environment(Name="LAKERUNNER_AUTOSCALER_PLATFORM", Value="ecs"),
            Environment(Name="ECS_CLUSTER", Value=Ref("ClusterName")),
            Environment(
                Name="LAKERUNNER_AUTOSCALER_SERVICES_LOGS_DEPLOYMENT",
                Value=Ref("ProcessLogsServiceName"),
            ),
            Environment(Name="LAKERUNNER_AUTOSCALER_SERVICES_LOGS_MIN_REPLICAS", Value="1"),
            Environment(
                Name="LAKERUNNER_AUTOSCALER_SERVICES_LOGS_MAX_REPLICAS",
                Value=Ref("ProcessLogsReplicas"),
            ),
            Environment(
                Name="LAKERUNNER_AUTOSCALER_SERVICES_METRICS_DEPLOYMENT",
                Value=Ref("ProcessMetricsServiceName"),
            ),
            Environment(Name="LAKERUNNER_AUTOSCALER_SERVICES_METRICS_MIN_REPLICAS", Value="1"),
            Environment(
                Name="LAKERUNNER_AUTOSCALER_SERVICES_METRICS_MAX_REPLICAS",
                Value=Ref("ProcessMetricsReplicas"),
            ),
            Environment(
                Name="LAKERUNNER_AUTOSCALER_SERVICES_TRACES_DEPLOYMENT",
                Value=Ref("ProcessTracesServiceName"),
            ),
            Environment(Name="LAKERUNNER_AUTOSCALER_SERVICES_TRACES_MIN_REPLICAS", Value="1"),
            Environment(
                Name="LAKERUNNER_AUTOSCALER_SERVICES_TRACES_MAX_REPLICAS",
                Value=Ref("ProcessTracesReplicas"),
            ),
        ]
    )

    # ---------------------------------------------------------------------
    # Per-container log groups. KEEP the four familiar /cardinal/<svc> paths so
    # operators' log queries don't change — one log group per container, four
    # resources, one task.
    # ---------------------------------------------------------------------
    admin_lg = t.add_resource(services_common.build_log_group(service_key="admin-api"))
    sweeper_lg = t.add_resource(services_common.build_log_group(service_key="sweeper"))
    monitoring_lg = t.add_resource(services_common.build_log_group(service_key="monitoring"))
    alert_lg = t.add_resource(services_common.build_log_group(service_key="alert-evaluator"))

    # ---------------------------------------------------------------------
    # ALB attachment for admin-api: dedicated HTTPS listener (9443) so the
    # lakerunner binary's mux sees request paths verbatim (no /admin/ prefix to
    # strip). This is the only rule on that listener; the listener's default
    # action is a 503 fixed response. The ECS Service's LoadBalancers block
    # (below) targets the admin-api CONTAINER inside the merged task.
    # ---------------------------------------------------------------------
    admin_tg = t.add_resource(
        services_common.build_target_group(
            service_key="admin-api",
            vpc_id_param="VpcId",
            port=admin_container_port,
            health_check_path=admin_health_path,
        )
    )
    admin_listener_rule = t.add_resource(
        services_common.build_listener_rule(
            service_key="admin-api-https",
            target_group_ref=admin_tg,
            listener_arn_param="AdminHttpsListenerArn",
            path_patterns=["/*"],
        )
    )

    # Register admin-api in Cloud Map so peers in the cluster (maestro) can
    # reach it at http://admin-api.<namespace>:9091 without hairpinning out
    # to the ALB's 9443 HTTPS listener. The ALB attachment stays for the
    # external/admin-UI path; only the in-cluster maestro -> admin-api hop
    # switches to the direct name (see children/maestro.py).
    admin_discovery = t.add_resource(
        DiscoveryService(
            "AdminApiDiscoveryService",
            Name="admin-api",
            NamespaceId=Ref("ServiceNamespaceId"),
            DnsConfig=DnsConfig(
                DnsRecords=[DnsRecord(Type="A", TTL="10")],
                RoutingPolicy="MULTIVALUE",
            ),
        )
    )

    # ---------------------------------------------------------------------
    # One task definition, four essential containers. admin-api alone exposes a
    # PortMapping (it's the LB target); the other three have no ports / no LB.
    # ---------------------------------------------------------------------
    control_task = t.add_resource(
        TaskDefinition(
            "ControlTaskDef",
            RequiresCompatibilities=["FARGATE"],
            NetworkMode="awsvpc",
            RuntimePlatform=RuntimePlatform(
                CpuArchitecture="ARM64",
                OperatingSystemFamily="LINUX",
            ),
            Cpu=CONTROL_TASK_CPU,
            Memory=CONTROL_TASK_MEMORY,
            ExecutionRoleArn=Ref("ExecutionRoleArn"),
            TaskRoleArn=Ref("TaskRoleArn"),
            ContainerDefinitions=[
                _container(
                    name="admin-api",
                    config=admin_cfg,
                    image_ref=image_ref,
                    environment=admin_env,
                    secrets=admin_secrets,
                    log_group_ref=admin_lg,
                    container_port=admin_container_port,
                ),
                _container(
                    name="sweeper",
                    config=sweeper_cfg,
                    image_ref=image_ref,
                    environment=sweeper_env,
                    secrets=base_secrets,
                    log_group_ref=sweeper_lg,
                ),
                _container(
                    name="monitoring",
                    config=monitoring_cfg,
                    image_ref=image_ref,
                    environment=monitoring_env,
                    secrets=base_secrets,
                    log_group_ref=monitoring_lg,
                ),
                _container(
                    name="alert-evaluator",
                    config=alert_cfg,
                    image_ref=image_ref,
                    environment=alert_env,
                    secrets=base_secrets,
                    log_group_ref=alert_lg,
                ),
            ],
            Tags=cardinal_tags(component="compute", role="control"),
        )
    )

    # ---------------------------------------------------------------------
    # The single control service. LoadBalancers targets the admin-api container;
    # ServiceRegistries puts the task's IP in Cloud Map as admin-api.<ns>.
    # fallback capacity: spot-preferred with an on-demand FARGATE fallback so a
    # transient FARGATE_SPOT shortage can't fail the one task a deploy needs.
    # ---------------------------------------------------------------------
    control_service = t.add_resource(
        services_common.build_ecs_service(
            service_key="control",
            cluster_arn_param="ClusterArn",
            task_definition_ref=control_task,
            desired_count=1,
            subnets_csv_param="PrivateSubnetsCsv",
            security_group_id_param="TaskSecurityGroupId",
            target_group_ref=admin_tg,
            container_name="admin-api",
            container_port=admin_container_port,
            service_registry_ref=admin_discovery,
            listener_rule_refs=[admin_listener_rule],
            capacity="fallback",
        )
    )
    t.add_output(Output("ControlServiceName", Value=GetAtt(control_service, "Name")))

    return t


def _container(
    *,
    name: str,
    config: dict,
    image_ref,
    environment: list,
    secrets: list,
    log_group_ref,
    container_port: int | None = None,
) -> ContainerDefinition:
    """One essential container in the merged control task.

    Each container keeps its own command (from cardinal-defaults.yaml), env,
    secrets, and a LogConfiguration pointing at its own /cardinal/<name> log
    group. container_port is set only for admin-api (the LB target); the other
    three expose no ports.
    """
    kwargs = dict(
        Name=name,
        Image=image_ref,
        Essential=True,
        Environment=environment,
        Secrets=secrets,
        LogConfiguration=LogConfiguration(
            LogDriver="awslogs",
            Options={
                "awslogs-group": Ref(log_group_ref),
                "awslogs-region": Ref("AWS::Region"),
                "awslogs-stream-prefix": name,
            },
        ),
    )
    command = config.get("command")
    if command:
        kwargs["Command"] = command
    if container_port is not None:
        kwargs["PortMappings"] = [PortMapping(ContainerPort=container_port, Protocol="tcp")]
    return ContainerDefinition(**kwargs)


def _service_specific_env(service_cfg: dict) -> list:
    """Convert the YAML environment dict into a list of ECS Environment objects."""
    env = service_cfg.get("environment") or {}
    return [Environment(Name=k, Value=str(v)) for k, v in env.items()]


if __name__ == "__main__":
    print(build().to_yaml())
