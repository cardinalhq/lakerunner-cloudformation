"""Shared helpers for building ECS service resources.

Each helper constructs and returns a single troposphere object.
The caller is responsible for adding it to a template.
"""

from troposphere import GetAtt, Ref, Split
from troposphere.ecs import (
    AwsvpcConfiguration,
    ContainerDefinition,
    DeploymentCircuitBreaker,
    DeploymentConfiguration,
    LoadBalancer as EcsLoadBalancer,
    LogConfiguration,
    NetworkConfiguration,
    PortMapping,
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
) -> TargetGroup:
    """ALB target group for a service that attaches to the ALB."""
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
    if container_port is not None:
        container_kwargs["PortMappings"] = [
            PortMapping(ContainerPort=container_port, Protocol="tcp")
        ]

    return TaskDefinition(
        _resource_title(service_key, "TaskDef"),
        RequiresCompatibilities=["FARGATE"],
        NetworkMode="awsvpc",
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
    """
    kwargs: dict = dict(
        Cluster=Ref(cluster_arn_param),
        LaunchType="FARGATE",
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
