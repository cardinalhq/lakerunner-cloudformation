"""services-control.yaml nested stack: lakerunner control-plane ECS services.

Owns four ECS Fargate services:

- admin-api (ALB-attached on dedicated 9443 listener; path catch-all `/*`,
  also registered in Cloud Map as `admin-api.<namespace>:9091` so peers
  inside the cluster — currently just maestro — can reach it without
  hairpinning through the ALB and tripping the self-signed cert path)
- sweeper (internal)
- monitoring (internal; gRPC port not exposed on the ALB)
- alert-evaluator (internal)

These services run at fixed shape; CPU, memory, and replicas all come from
cardinal-defaults.yaml. No per-service tunables are exposed at deployment time.
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
from troposphere.servicediscovery import (
    DnsConfig,
    DnsRecord,
    Service as DiscoveryService,
)

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
        "Cardinal services-control: lakerunner admin-api (ALB-attached), "
        "sweeper, monitoring, and alert-evaluator ECS services."
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
    t.add_parameter(Parameter("QueueUrl", Type="String", Description="URL of the ingest SQS queue."))
    t.add_parameter(Parameter("QueueArn", Type="String", Description="ARN of the ingest SQS queue."))
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
                    "QueueUrl",
                    "QueueArn",
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
        Environment(Name="LRDB_SQS_QUEUE_URL", Value=Ref("QueueUrl")),
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
    # admin-api: ALB-attached
    # ---------------------------------------------------------------------
    admin_lg = t.add_resource(services_common.build_log_group(service_key="admin-api"))
    admin_tg = t.add_resource(
        services_common.build_target_group(
            service_key="admin-api",
            vpc_id_param="VpcId",
            port=admin_container_port,
            health_check_path=admin_health_path,
        )
    )
    # admin-api gets a dedicated HTTPS listener (9443) so the lakerunner
    # binary's mux sees request paths verbatim (no /admin/ prefix to strip).
    # This is the only rule on that listener; the listener's default action
    # is a 503 fixed response.
    admin_listener_rule = t.add_resource(
        services_common.build_listener_rule(
            service_key="admin-api-https",
            target_group_ref=admin_tg,
            listener_arn_param="AdminHttpsListenerArn",
            path_patterns=["/*"],
        )
    )
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
    admin_task = t.add_resource(
        services_common.build_task_definition(
            service_key="admin-api",
            image_ref=image_ref,
            cpu=admin_cfg["cpu"],
            memory_mib=admin_cfg["memory_mib"],
            command=admin_cfg.get("command"),
            execution_role_arn_param="ExecutionRoleArn",
            task_role_arn=Ref("TaskRoleArn"),
            environment=admin_env,
            secrets=admin_secrets,
            log_group_ref=admin_lg,
            container_port=admin_container_port,
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
    admin_service = t.add_resource(
        services_common.build_ecs_service(
            service_key="admin-api",
            cluster_arn_param="ClusterArn",
            task_definition_ref=admin_task,
            desired_count=int(admin_cfg["replicas"]),
            subnets_csv_param="PrivateSubnetsCsv",
            security_group_id_param="TaskSecurityGroupId",
            target_group_ref=admin_tg,
            container_name="admin-api",
            container_port=admin_container_port,
            service_registry_ref=admin_discovery,
            listener_rule_refs=[admin_listener_rule],
        )
    )
    t.add_output(Output("AdminApiServiceName", Value=GetAtt(admin_service, "Name")))

    # ---------------------------------------------------------------------
    # Internal services (no ALB attachment)
    # ---------------------------------------------------------------------
    # alert-evaluator reaches query-api over the in-cluster Cloud Map DNS.
    # Container port is 8080 (matches lakerunner-query-api.ingress.container_port).
    alert_extra_env = [
        Environment(
            Name="ALERT_EVALUATOR_QUERY_API_URL",
            Value=Sub("http://query-api.${ServiceNamespaceName}:8080"),
        ),
    ]

    # The monitoring service drives ECS autoscaling of the process-* services.
    # Env vars match cmd/monitoring.go + config/autoscaler.go in the lakerunner
    # repo; IAM is scoped per-service to UpdateService/DescribeServices.
    # The lakerunner binary defaults Autoscaler.ObserveOnly=true and per-service
    # MinReplicas=0, which would log decisions but never scale and would scale
    # services to zero on idle. Override both: the customer set max replicas to
    # opt into actual scaling, and 1 is the documented floor (lakerunner
    # docs/guides/admin/autoscaling.md: "Set to 1 to prevent scale-to-zero").
    monitoring_extra_env = [
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
    internal_services = [
        {
            "service_key": "sweeper",
            "config": sweeper_cfg,
            "output_name": "SweeperServiceName",
            "extra_env": [],
        },
        {
            "service_key": "monitoring",
            "config": monitoring_cfg,
            "output_name": "MonitoringServiceName",
            "extra_env": monitoring_extra_env,
        },
        {
            "service_key": "alert-evaluator",
            "config": alert_cfg,
            "output_name": "AlertEvaluatorServiceName",
            "extra_env": alert_extra_env,
        },
    ]

    for spec in internal_services:
        ecs_service = _build_internal_service_block(
            t,
            service_key=spec["service_key"],
            config=spec["config"],
            image_ref=image_ref,
            base_env=base_env,
            base_secrets=base_secrets,
            extra_env=spec["extra_env"],
        )
        t.add_output(Output(spec["output_name"], Value=GetAtt(ecs_service, "Name")))

    return t


def _build_internal_service_block(
    t: Template,
    *,
    service_key: str,
    config: dict,
    image_ref,
    base_env: list,
    base_secrets: list,
    extra_env: list | None = None,
):
    """Wire up log group, task def, and ECS service for one internal service."""
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
            cpu=config["cpu"],
            memory_mib=config["memory_mib"],
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
            desired_count=int(config["replicas"]),
            subnets_csv_param="PrivateSubnetsCsv",
            security_group_id_param="TaskSecurityGroupId",
            container_name=service_key,
        )
    )


def _service_specific_env(service_cfg: dict) -> list:
    """Convert the YAML environment dict into a list of ECS Environment objects."""
    env = service_cfg.get("environment") or {}
    return [Environment(Name=k, Value=str(v)) for k, v in env.items()]


if __name__ == "__main__":
    print(build().to_yaml())
