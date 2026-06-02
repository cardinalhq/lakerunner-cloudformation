"""Shared helpers for building ECS service resources.

Each helper constructs and returns a single troposphere object.
The caller is responsible for adding it to a template.
"""

from troposphere import GetAtt, Ref, Split, Sub
from troposphere.ecs import (
    AwsvpcConfiguration,
    CapacityProviderStrategyItem,
    ContainerDefinition,
    DeploymentCircuitBreaker,
    DeploymentConfiguration,
    Environment,
    LoadBalancer as EcsLoadBalancer,
    LogConfiguration,
    NetworkConfiguration,
    PortMapping,
    RuntimePlatform,
    Service,
    ServiceRegistry,
    TaskDefinition,
)
from troposphere.elasticloadbalancingv2 import (
    Condition as AlbCondition,
    ListenerRule,
    ListenerRuleAction,
    Matcher,
    PathPatternConfig,
    TargetGroup,
)
from troposphere.logs import LogGroup

from cardinal_cfn.listener_priorities import priority_for
from cardinal_cfn.naming import cardinal_tags
from cardinal_cfn.policies import apply_policy


def lakerunner_otel_env(*, service_key: str) -> list:
    """OTel env vars wired to the in-cluster otel-collector.

    Mirrors the helm chart pattern: OTEL_SERVICE_NAME=lakerunner-<component>,
    ENABLE_OTLP_TELEMETRY toggles the actual exporter, OTEL_EXPORTER_OTLP_ENDPOINT
    points at the collector's Cloud Map DNS name, and OTEL_RESOURCE_ATTRIBUTES
    carries ecs.cluster.name. All four env vars are unconditionally set; the
    ENABLE_OTLP_TELEMETRY flag is what actually starts/stops exporting.

    Expects the calling stack to declare these parameters:
      - SelfTelemetryEndpoint (String): the OTLP gRPC URL, or "" when disabled.
      - SelfTelemetryEnabled  (String): "true" or "false".
      - ClusterName           (String): forwarded from the root.
    """
    return [
        Environment(Name="OTEL_SERVICE_NAME", Value=f"lakerunner-{service_key}"),
        Environment(Name="OTEL_EXPORTER_OTLP_ENDPOINT", Value=Ref("SelfTelemetryEndpoint")),
        Environment(Name="ENABLE_OTLP_TELEMETRY", Value=Ref("SelfTelemetryEnabled")),
        Environment(
            Name="OTEL_RESOURCE_ATTRIBUTES",
            Value=Sub("ecs.cluster.name=${ClusterName}"),
        ),
    ]


def build_log_group(*, service_key: str, retention_days: int = 14) -> LogGroup:
    """Per-service CloudWatch log group named `/cardinal/<service-key>`.

    Bare ``/cardinal/<svc>`` matches the IAM cookbook's ``/cardinal/*`` glob
    on TaskRole CW Logs grants.
    """
    lg = LogGroup(
        _resource_title(service_key, "LogGroup"),
        LogGroupName=f"/cardinal/{service_key}",
        RetentionInDays=retention_days,
        Tags=cardinal_tags(component="compute", role=service_key),
    )
    apply_policy(lg, "log-group")
    return lg


def build_target_group(
    *,
    service_key: str,
    vpc_id_param: str,
    port: int,
    health_check_path: str = "/healthz",
    health_check_port: int | None = None,
) -> TargetGroup:
    """ALB target group for a service that attaches to the ALB.

    ``health_check_port`` overrides the port the ALB probes when the health
    endpoint lives on a different port than the traffic port (e.g. the otel
    collector serves OTLP on 4318 but its health_check extension on 13133).
    Defaults to the traffic port.
    """
    kwargs = {}
    if health_check_port is not None:
        kwargs["HealthCheckPort"] = str(health_check_port)
    return TargetGroup(
        _resource_title(service_key, "TargetGroup"),
        Port=port,
        Protocol="HTTP",
        TargetType="ip",
        VpcId=Ref(vpc_id_param),
        HealthCheckPath=health_check_path,
        HealthCheckProtocol="HTTP",
        Matcher=Matcher(HttpCode="200"),
        Tags=cardinal_tags(component="networking", role=f"{service_key}-tg"),
        **kwargs,
    )


def build_listener_rule(
    *,
    service_key: str,
    target_group_ref,
    listener_arn_param: str,
    path_patterns: list,
) -> ListenerRule:
    """ListenerRule using priority_for(service_key). Raises KeyError for unknown services."""
    return ListenerRule(
        _resource_title(service_key, "ListenerRule"),
        ListenerArn=Ref(listener_arn_param),
        Priority=priority_for(service_key),
        Conditions=[
            AlbCondition(
                Field="path-pattern",
                PathPatternConfig=PathPatternConfig(Values=path_patterns),
            )
        ],
        Actions=[
            ListenerRuleAction(
                Type="forward",
                TargetGroupArn=Ref(target_group_ref),
            )
        ],
    )


def build_task_definition(
    *,
    service_key: str,
    image_ref,
    cpu,
    memory_mib,
    command: list | None = None,
    execution_role_arn_param: str,
    task_role_arn,
    environment: list,
    secrets: list | None = None,
    log_group_ref,
    container_port: int | None = None,
    health_check_port: int | None = None,
) -> TaskDefinition:
    """ECS Fargate task definition for a service.

    cpu / memory_mib accept ints (from YAML defaults) or troposphere Refs
    (from CloudFormation parameters). Ints are coerced to strings; Refs and
    other troposphere objects pass through unchanged so they serialize as
    intrinsic functions.

    task_role_arn accepts either a plain string or a troposphere object
    (typically Ref('TaskRoleArn')) and is used verbatim as TaskRoleArn.

    container_port: if provided, the container exposes a tcp PortMapping at
    that port. Required whenever the corresponding ECS Service references
    the container in a LoadBalancer/TargetGroup attachment — without the
    PortMapping CFN rejects the Service with "container ... did not have a
    container port N defined."

    health_check_port: if provided, the container exposes an ADDITIONAL tcp
    PortMapping at that port (lakerunner's dedicated health server, port 8090
    as of v1.39). Needed so the ALB target group can probe the health server
    on a port distinct from the traffic port.
    """
    container_kwargs = dict(
        Name=service_key,
        Image=image_ref,
        Essential=True,
        Environment=environment,
        LogConfiguration=LogConfiguration(
            LogDriver="awslogs",
            Options={
                "awslogs-group": Ref(log_group_ref),
                "awslogs-region": Ref("AWS::Region"),
                "awslogs-stream-prefix": service_key,
            },
        ),
    )
    if command:
        container_kwargs["Command"] = command
    if secrets:
        container_kwargs["Secrets"] = secrets
    port_mappings = []
    if container_port is not None:
        port_mappings.append(PortMapping(ContainerPort=container_port, Protocol="tcp"))
    if health_check_port is not None:
        port_mappings.append(PortMapping(ContainerPort=health_check_port, Protocol="tcp"))
    if port_mappings:
        container_kwargs["PortMappings"] = port_mappings

    return TaskDefinition(
        _resource_title(service_key, "TaskDef"),
        RequiresCompatibilities=["FARGATE"],
        NetworkMode="awsvpc",
        RuntimePlatform=RuntimePlatform(
            CpuArchitecture="ARM64",
            OperatingSystemFamily="LINUX",
        ),
        Cpu=_coerce_size(cpu),
        Memory=_coerce_size(memory_mib),
        ExecutionRoleArn=Ref(execution_role_arn_param),
        TaskRoleArn=task_role_arn,
        ContainerDefinitions=[ContainerDefinition(**container_kwargs)],
        Tags=cardinal_tags(component="compute", role=service_key),
    )


def _coerce_size(value):
    """Stringify ints/floats from YAML; pass through troposphere objects (Refs, Subs)."""
    if isinstance(value, (int, float)):
        return str(value)
    return value


def capacity_provider_strategy(capacity: str = "ondemand") -> list:
    """Capacity-provider strategy for an ECS Service.

    Three modes:

    "ondemand" (deploy-critical singletons and fixed-size tiers): pure on-demand
    FARGATE for ALL replicas. The only deploy-reliable choice for a service
    where every task must place during a rolling deploy. A weight-based strategy
    does NOT give a single task failover — ECS weights only distribute MULTIPLE
    tasks across providers; for any individual task ECS picks ONE provider, and
    if FARGATE_SPOT has no capacity at that instant the task fails to place,
    tripping the deployment circuit breaker and rolling the stack back. Used by
    the migrator, the merged control service, maestro, pubsub-sqs, query-api,
    and the satellite collector.

    "fallback" (autoscaled scale-out workers): Base=1 on-demand FARGATE plus
    weighted FARGATE_SPOT (4:1) for scale-out. The Base=1 guarantees the FIRST
    replica always lands on on-demand — so a rolling deploy always has at least
    one reliable task — while replicas beyond the first ride cheap spot. Only
    one provider in a strategy may carry Base>0; it lives on FARGATE here. Used
    by process-{logs,metrics,traces} and query-worker, which autoscale and can
    tolerate a transient spot shortage on their extra replicas.

    "spot": pure FARGATE_SPOT, an explicit opt-in only. DEPLOY-UNSAFE — a single
    task can fail to place — for a customer who knowingly wants spot on a
    non-critical, cost-sensitive service. No caller uses it.
    """
    if capacity == "ondemand":
        return [
            CapacityProviderStrategyItem(CapacityProvider="FARGATE", Weight=1),
        ]
    if capacity == "fallback":
        return [
            CapacityProviderStrategyItem(
                CapacityProvider="FARGATE", Base=1, Weight=1
            ),
            CapacityProviderStrategyItem(CapacityProvider="FARGATE_SPOT", Weight=4),
        ]
    if capacity == "spot":
        return [
            CapacityProviderStrategyItem(CapacityProvider="FARGATE_SPOT", Weight=1),
        ]
    raise ValueError(f"unknown capacity mode: {capacity!r}")


def build_ecs_service(
    *,
    service_key: str,
    cluster_arn_param: str,
    task_definition_ref,
    desired_count,
    subnets_csv_param: str,
    security_group_id_param: str,
    target_group_ref=None,
    container_name: str,
    container_port: int | None = None,
    service_registry_ref=None,
    listener_rule_refs: list | None = None,
    capacity: str = "ondemand",
    health_check_grace_period: int | None = None,
) -> Service:
    """ECS Fargate Service with rolling deploy + circuit breaker.

    service_registry_ref: optional Cloud Map ServiceDiscovery::Service to attach
    so the ECS Service registers each task in private DNS for in-cluster
    routing (e.g. alert-evaluator -> query-api).

    listener_rule_refs: ListenerRule resources whose creation must precede this
    Service. ECS validates at create-time that LoadBalancer target groups are
    already attached to a listener; without an explicit DependsOn, CFN may
    create the Service before the ListenerRule attaches the TG, producing
    "target group does not have an associated load balancer" failures.

    health_check_grace_period: seconds ECS ignores ELB health-check failures
    after a task starts. Only valid on a service with a LoadBalancers block;
    gives the lakerunner health server (port 8090) a margin to come up before
    the ALB starts failing the task.
    """
    kwargs: dict = dict(
        Cluster=Ref(cluster_arn_param),
        CapacityProviderStrategy=capacity_provider_strategy(capacity),
        DesiredCount=desired_count,
        TaskDefinition=Ref(task_definition_ref),
        NetworkConfiguration=NetworkConfiguration(
            AwsvpcConfiguration=AwsvpcConfiguration(
                Subnets=Split(",", Ref(subnets_csv_param)),
                SecurityGroups=[Ref(security_group_id_param)],
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
        Tags=cardinal_tags(component="compute", role=service_key),
    )

    if target_group_ref is not None:
        kwargs["LoadBalancers"] = [
            EcsLoadBalancer(
                ContainerName=container_name,
                ContainerPort=container_port,
                TargetGroupArn=Ref(target_group_ref),
            )
        ]
        if health_check_grace_period is not None:
            kwargs["HealthCheckGracePeriodSeconds"] = health_check_grace_period

    if service_registry_ref is not None:
        kwargs["ServiceRegistries"] = [
            ServiceRegistry(RegistryArn=GetAtt(service_registry_ref, "Arn"))
        ]

    if listener_rule_refs:
        kwargs["DependsOn"] = [r.title for r in listener_rule_refs]

    return Service(_resource_title(service_key, "Service"), **kwargs)


def _resource_title(service_key: str, suffix: str) -> str:
    """Convert a service key like 'query-api' to a CFN logical ID like 'QueryApiService'."""
    return "".join(part.capitalize() for part in service_key.replace("-", " ").split()) + suffix
