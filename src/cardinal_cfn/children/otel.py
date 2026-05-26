"""otel.yaml nested stack: cardinalhq-otel-collector ECS service.

Owns a SINGLE ECS Fargate service running the cardinalhq-otel-collector
image. The collector listens on the canonical OTLP ports (gRPC 4317 and
HTTP 4318) and writes telemetry directly to the ingest S3 bucket; it has
no database dependency and does not consume from the ingest SQS queue.

The collector is always attached to the shared ALB, exposing OTLP/HTTP
on its own dedicated plain-HTTP listener at port 4318: this stack creates
a TargetGroup + ListenerRule (priority 300, "/v1/*") on the OTel listener
and wires the ECS Service's LoadBalancers block to it. The listener is
HTTP (not HTTPS) because the ALB is internal-scheme and external senders
arrive over VPC peering / TGW / VPN -- TLS at the ALB would only force
those senders to install the ALB cert. Health checks hit the collector's
health_check extension on 13133. The gRPC receiver (4317) is not
ALB-exposed but stays reachable task-to-task via Cloud Map. The ALB
security group is customer-supplied and may differ from the task security
group; the customer owns the ALB-to-task ingress rule on 4318 and any
peer-source ingress rule on the ALB itself.

Config injection is intentionally minimal: the image ships with a baked-in
config (`/etc/otel/config.yaml`, referenced by the default command) and
this stack threads an OtelConfigYaml parameter through to an
OTEL_CONFIG_OVERRIDE environment variable. Customers wanting a fuller
config-injection mechanism (SSM-backed config, init-container rendering,
etc.) can layer it on later.
"""

from troposphere import (
    GetAtt,
    Equals,
    If,
    Not,
    Output,
    Parameter,
    Ref,
    Split,
    Sub,
    Template,
)
from troposphere.ecs import (
    AwsvpcConfiguration,
    DeploymentCircuitBreaker,
    DeploymentConfiguration,
    Environment,
    LoadBalancer as EcsLoadBalancer,
    NetworkConfiguration,
    Secret,
    Service,
    ServiceRegistry,
)
from troposphere.servicediscovery import (
    DnsConfig,
    DnsRecord,
    Service as DiscoveryService,
)

from cardinal_cfn.children import services_common
from cardinal_cfn.defaults import load_defaults, load_otel_default_config
from cardinal_cfn.images import add_image_override
from cardinal_cfn.naming import cardinal_tags
from cardinal_cfn.parameters import (
    add_install_id_parameters,
    add_parameter_group_metadata,
)


# Service key used for naming (log group, role, target group, listener rule)
# and as the ECS container name. Listener rule priority is registered under
# "otel-grpc" in listener_priorities; build_task_definition uses this same
# string as the ContainerDefinition Name, which the ECS Service LoadBalancer
# entry must reference.
_SERVICE_KEY = "otel-grpc"

# OTLP ports. The ALB exposes OTLP/HTTP (4318): it's plain HTTP, so a standard
# HTTP target group + the "/v1/*" listener rule (OTLP HTTP's canonical path
# prefix: /v1/traces|metrics|logs) work without gRPC/HTTP-2 complications. The
# gRPC receiver (4317) stays reachable task-to-task via Cloud Map but is not
# ALB-exposed. The collector's health_check extension serves HTTP 200 at "/"
# on 13133, so the target group health-checks that port (not the OTLP port,
# which has no plain-HTTP health path).
_OTLP_HTTP_PORT = 4318
_HEALTH_PORT = 13133


def build() -> Template:
    t = Template()
    t.set_description(
        "Cardinal otel: cardinalhq-otel-collector ECS Fargate service, always "
        "attached to the shared ALB HTTPS listener."
    )

    defaults = load_defaults()
    otel_cfg = defaults["otel"]["otel-gateway"]

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
        Parameter("BucketName", Type="String", Description="Name of the ingest S3 bucket.")
    )
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
            "OtelHttpListenerArn",
            Type="String",
            Description="ARN of the ALB OTel listener (plain HTTP on port 4318).",
        )
    )
    t.add_parameter(
        Parameter(
            "AlbDnsName",
            Type="String",
            Description=(
                "AWS-assigned ALB DNS name (e.g. internal-cardinal-...elb.amazonaws.com). "
                "Surfaced as an output for callers that prefer the ALB path over "
                "Cloud Map service discovery."
            ),
        )
    )
    t.add_parameter(
        Parameter("VpcId", Type="AWS::EC2::VPC::Id", Description="VPC ID (forwarded from root).")
    )
    t.add_parameter(
        Parameter(
            "ServiceNamespaceId",
            Type="String",
            Description=(
                "Cloud Map private DNS namespace ID. Used to register the "
                "collector at cardinal-otel.<namespace> for task-to-task "
                "discovery by lakerunner self-telemetry."
            ),
        )
    )
    t.add_parameter(
        Parameter(
            "ServiceNamespaceName",
            Type="String",
            Description=(
                "Cloud Map private DNS namespace name (e.g. cardinal.local). "
                "Used only to compose the OtelInternalUrl output."
            ),
        )
    )

    # ---------------------------------------------------------------------
    # Image override
    # ---------------------------------------------------------------------
    image_ref = add_image_override(
        t,
        name="OtelImage",
        default=defaults["images"]["otel"],
        description="Container image for the cardinalhq-otel-collector service.",
    )

    # ---------------------------------------------------------------------
    # OTEL tunables (defaults from cardinal-defaults.yaml otel.otel-gateway)
    # ---------------------------------------------------------------------
    t.add_parameter(
        Parameter(
            "OtelReplicas",
            Type="Number",
            Default=str(otel_cfg["replicas"]),
            Description="Desired replicas for the otel-gateway service.",
        )
    )
    t.add_parameter(
        Parameter(
            "OtelCpu",
            Type="String",
            Default=str(otel_cfg["cpu"]),
            Description="Fargate CPU units for the otel-gateway service.",
        )
    )
    t.add_parameter(
        Parameter(
            "OtelMemory",
            Type="String",
            Default=str(otel_cfg["memory_mib"]),
            Description="Fargate memory (MiB) for the otel-gateway service.",
        )
    )
    # The cardinalhq-otel-collector image's run-with-env-config wrapper reads
    # the collector YAML from CHQ_COLLECTOR_CONFIG_YAML at task start. We
    # ship a sensible default (cardinal-otel-config.yaml) and pass it via
    # If(HasOtelConfigOverride, Ref(OtelConfigYaml), default).
    t.add_parameter(
        Parameter(
            "OtelConfigYaml",
            Type="String",
            Default="",
            Description=(
                "Optional inline OTEL collector config YAML. Empty uses the "
                "default ingest-to-S3 pipeline shipped with this stack."
            ),
        )
    )

    # ---------------------------------------------------------------------
    # Conditions
    # ---------------------------------------------------------------------
    t.add_condition(
        "HasOtelConfigOverride", Not(Equals(Ref("OtelConfigYaml"), ""))
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
                    "BucketName",
                    "QueueArn",
                    "LicenseSecretArn",
                    "OtelHttpListenerArn",
                    "AlbDnsName",
                    "VpcId",
                    "ServiceNamespaceId",
                    "ServiceNamespaceName",
                ],
            },
            {
                "label": "OTEL tunables",
                "parameters": [
                    "OtelReplicas",
                    "OtelCpu",
                    "OtelMemory",
                    "OtelConfigYaml",
                ],
            },
            {
                "label": "Image overrides",
                "parameters": ["OtelImage"],
            },
        ],
    )

    # ---------------------------------------------------------------------
    # Log group, task role, task definition
    # ---------------------------------------------------------------------
    log_group = t.add_resource(services_common.build_log_group(service_key=_SERVICE_KEY))

    storage_profiles = defaults.get("storage_profiles") or []
    default_org = (
        storage_profiles[0].get("organization_id")
        if storage_profiles else "default"
    )
    default_collector = (
        otel_cfg.get("collector_name")
        or (storage_profiles[0].get("collector_name") if storage_profiles else None)
        or "lakerunner"
    )

    env = [
        Environment(
            Name="CHQ_COLLECTOR_CONFIG_YAML",
            Value=If(
                "HasOtelConfigOverride",
                Ref("OtelConfigYaml"),
                load_otel_default_config(),
            ),
        ),
        Environment(Name="LRDB_S3_BUCKET", Value=Ref("BucketName")),
        Environment(Name="LRDB_S3_REGION", Value=Ref("AWS::Region")),
        Environment(Name="ORG", Value=default_org),
        Environment(Name="COLLECTOR", Value=default_collector),
    ] + _service_specific_env(otel_cfg)

    secrets = [
        Secret(Name="LICENSE_DATA", ValueFrom=Ref("LicenseSecretArn")),
    ]

    task_def = t.add_resource(
        services_common.build_task_definition(
            service_key=_SERVICE_KEY,
            image_ref=image_ref,
            cpu=Ref("OtelCpu"),
            memory_mib=Ref("OtelMemory"),
            command=otel_cfg.get("command"),
            execution_role_arn_param="ExecutionRoleArn",
            task_role_arn=Ref("TaskRoleArn"),
            environment=env,
            secrets=secrets,
            log_group_ref=log_group,
            container_port=_OTLP_HTTP_PORT,
        )
    )

    # ---------------------------------------------------------------------
    # ALB attachment (TargetGroup + ListenerRule). Traffic targets OTLP/HTTP
    # (4318) and the "/v1/*" rule matches /v1/traces|metrics|logs. Health
    # checks hit the collector's health_check extension on 13133 (HTTP 200 at
    # "/"); the OTLP port has no plain-HTTP health path.
    # ---------------------------------------------------------------------
    target_group = services_common.build_target_group(
        service_key=_SERVICE_KEY,
        vpc_id_param="VpcId",
        port=_OTLP_HTTP_PORT,
        health_check_path="/",
        health_check_port=_HEALTH_PORT,
    )
    t.add_resource(target_group)

    listener_rule = services_common.build_listener_rule(
        service_key=_SERVICE_KEY,
        target_group_ref=target_group,
        listener_arn_param="OtelHttpListenerArn",
        path_patterns=["/v1/*"],
    )
    t.add_resource(listener_rule)

    # ---------------------------------------------------------------------
    # Cloud Map registration so other tasks can reach the collector at
    # cardinal-otel.<namespace>:4318 (OTLP/HTTP) without going through the
    # ALB. Used by
    # lakerunner self-telemetry.
    # ---------------------------------------------------------------------
    otel_discovery = t.add_resource(
        DiscoveryService(
            "OtelDiscoveryService",
            Name="cardinal-otel",
            NamespaceId=Ref("ServiceNamespaceId"),
            DnsConfig=DnsConfig(
                DnsRecords=[DnsRecord(Type="A", TTL="10")],
                RoutingPolicy="MULTIVALUE",
            ),
        )
    )

    # ---------------------------------------------------------------------
    # ECS Service (inlined, not via build_ecs_service helper, so the
    # LoadBalancers block can reference the always-present TargetGroup).
    # ---------------------------------------------------------------------
    service = t.add_resource(
        Service(
            "OtelGrpcService",
            Cluster=Ref("ClusterArn"),
            LaunchType="FARGATE",
            DesiredCount=Ref("OtelReplicas"),
            TaskDefinition=Ref(task_def),
            NetworkConfiguration=NetworkConfiguration(
                AwsvpcConfiguration=AwsvpcConfiguration(
                    Subnets=Split(",", Ref("PrivateSubnetsCsv")),
                    SecurityGroups=[Ref("TaskSecurityGroupId")],
                    AssignPublicIp="DISABLED",
                )
            ),
            DeploymentConfiguration=DeploymentConfiguration(
                MinimumHealthyPercent=50,
                MaximumPercent=200,
                DeploymentCircuitBreaker=DeploymentCircuitBreaker(
                    Enable=True,
                    Rollback=True,
                ),
            ),
            LoadBalancers=[
                EcsLoadBalancer(
                    ContainerName=_SERVICE_KEY,
                    ContainerPort=_OTLP_HTTP_PORT,
                    TargetGroupArn=Ref(target_group),
                )
            ],
            ServiceRegistries=[
                ServiceRegistry(RegistryArn=GetAtt(otel_discovery, "Arn")),
            ],
            Tags=cardinal_tags(component="compute", role=_SERVICE_KEY),
        )
    )

    # ---------------------------------------------------------------------
    # Outputs
    # ---------------------------------------------------------------------
    # In-cluster OTLP/HTTP URL via Cloud Map service discovery. Reaches the
    # task ENI directly, bypassing the ALB.
    t.add_output(
        Output(
            "OtelInternalUrl",
            Value=Sub(
                f"http://cardinal-otel.${{ServiceNamespaceName}}:{_OTLP_HTTP_PORT}"
            ),
        )
    )
    # AWS-assigned ALB DNS name -- publicly resolvable but resolves to private
    # IPs (the ALB is internal-scheme). Useful for callers with VPC reachability
    # (peering / TGW / VPN) that arrive via the ALB plain-HTTP OTel listener.
    t.add_output(Output("OtelAlbDnsName", Value=Ref("AlbDnsName")))
    t.add_output(
        Output(
            "OtelExternalUrl",
            Value=Sub(f"http://${{AlbDnsName}}:{_OTLP_HTTP_PORT}"),
        )
    )
    t.add_output(Output("OtelServiceName", Value=GetAtt(service, "Name")))

    return t


def _service_specific_env(service_cfg: dict) -> list:
    """Convert the YAML environment dict into a list of ECS Environment objects."""
    env = service_cfg.get("environment") or {}
    return [Environment(Name=k, Value=str(v)) for k, v in env.items()]


if __name__ == "__main__":
    print(build().to_yaml())
