"""services-control.yaml nested stack: lakerunner control-plane ECS service.

Owns a SINGLE ECS Fargate service running one task with four containers:

- admin-api (ALB-attached on dedicated 9443 listener; path catch-all `/*`,
  also registered in Cloud Map as `admin-api.<namespace>:9091` so peers
  inside the cluster — currently just maestro — can reach it without
  hairpinning through the ALB and tripping the self-signed cert path)
- sweeper (internal)
- monitoring (internal; gRPC port not exposed on the ALB)
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
            "ClusterName",
            Type="String",
            Description="ECS cluster name (not ARN), used in OTel resource attributes.",
        )
    )
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
                    "ClusterName",
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
    # All four containers share the merged task's network namespace, so they
    # CANNOT all bind the default health port (8090). Give each a DISTINCT
    # HEALTH_CHECK_PORT. admin-api keeps 8090 (the ALB-probed one); the other
    # three move to 8091/8092/8093 to avoid a port collision in the namespace.
    admin_env = (
        list(base_env)
        + services_common.lakerunner_otel_env(service_key="admin-api")
        + _service_specific_env(admin_cfg)
        + [Environment(Name="HEALTH_CHECK_PORT", Value="8090")]
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
        + [Environment(Name="HEALTH_CHECK_PORT", Value="8091")]
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
            Environment(Name="HEALTH_CHECK_PORT", Value="8093"),
        ]
    )

    # The monitoring container no longer drives autoscaling -- the process-*
    # services scale on CPU via native ECS Application Auto Scaling (see
    # services-process), mirroring the Kubernetes HPA. monitoring keeps only its
    # base/OTel env and a distinct health-check port.
    monitoring_env = (
        list(base_env)
        + services_common.lakerunner_otel_env(service_key="monitoring")
        + _service_specific_env(monitoring_cfg)
        + [
            Environment(Name="HEALTH_CHECK_PORT", Value="8092"),
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
            # lakerunner v1.39: /healthz lives on the dedicated health server
            # (port 8090), not the 9091 API port. Probe 8090.
            health_check_port=8090,
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
                    health_check_port=8090,
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
    # ondemand capacity: the merged control service is a deploy-critical
    # singleton (desired=1), so its one task runs pure on-demand FARGATE — a
    # transient FARGATE_SPOT shortage must never fail the task a deploy needs.
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
            capacity="ondemand",
            # Margin for admin-api's health server (8090) to come up before the
            # ALB starts failing the task.
            health_check_grace_period=60,
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
    health_check_port: int | None = None,
) -> ContainerDefinition:
    """One essential container in the merged control task.

    Each container keeps its own command (from cardinal-defaults.yaml), env,
    secrets, and a LogConfiguration pointing at its own /cardinal/<name> log
    group. container_port is set only for admin-api (the LB target); the other
    three expose no ports. health_check_port adds an additional PortMapping for
    the dedicated health server (admin-api only, port 8090, ALB-probed).
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
    port_mappings = []
    if container_port is not None:
        port_mappings.append(PortMapping(ContainerPort=container_port, Protocol="tcp"))
    if health_check_port is not None:
        port_mappings.append(PortMapping(ContainerPort=health_check_port, Protocol="tcp"))
    if port_mappings:
        kwargs["PortMappings"] = port_mappings
    return ContainerDefinition(**kwargs)


def _service_specific_env(service_cfg: dict) -> list:
    """Convert the YAML environment dict into a list of ECS Environment objects."""
    env = service_cfg.get("environment") or {}
    return [Environment(Name=k, Value=str(v)) for k, v in env.items()]


if __name__ == "__main__":
    print(build().to_yaml())
