"""Shared helpers for building ECS service resources.

Each helper constructs and returns a single troposphere object.
The caller is responsible for adding it to a template.
"""

from troposphere import GetAtt, Ref, Split, Sub
from troposphere.ecs import (
    AwsvpcConfiguration,
    ContainerDefinition,
    DeploymentCircuitBreaker,
    DeploymentConfiguration,
    Environment,
    LoadBalancer as EcsLoadBalancer,
    LogConfiguration,
    NetworkConfiguration,
    Secret as EcsSecret,
    Service,
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
from troposphere.iam import Policy, Role
from troposphere.logs import LogGroup

from cardinal_cfn.listener_priorities import priority_for
from cardinal_cfn.naming import cardinal_tags
from cardinal_cfn.policies import apply_policy


def build_log_group(*, service_key: str, retention_days: int = 14) -> LogGroup:
    """Per-service CloudWatch log group named `/cardinal/${InstallIdShort}/<service-key>`."""
    lg = LogGroup(
        _resource_title(service_key, "LogGroup"),
        LogGroupName=Sub(f"/cardinal/${{InstallIdShort}}/{service_key}"),
        RetentionInDays=retention_days,
        Tags=cardinal_tags(component="compute", role=service_key),
    )
    apply_policy(lg, "log-group")
    return lg


def build_task_role(*, service_key: str, statements: list) -> Role:
    """Per-service IAM task role with the provided inline policy statements."""
    return Role(
        _resource_title(service_key, "TaskRole"),
        AssumeRolePolicyDocument={
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                }
            ],
        },
        Policies=[
            Policy(
                PolicyName=f"cardinal-{service_key}-task-policy",
                PolicyDocument={
                    "Version": "2012-10-17",
                    "Statement": statements,
                },
            )
        ],
        Tags=cardinal_tags(component="compute", role=f"{service_key}-task-role"),
    )


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
    service_config: dict,
    image_ref,
    execution_role_arn_param: str,
    task_role_ref,
    environment: list,
    secrets: list = None,
    log_group_ref,
) -> TaskDefinition:
    """ECS Fargate task definition for a service."""
    container_kwargs = dict(
        Name=service_key,
        Image=image_ref,
        Essential=True,
        Environment=environment,
        LogConfiguration=LogConfiguration(
            LogDriver="awslogs",
            Options={
                "awslogs-group": Ref(log_group_ref),
                "awslogs-region": {"Ref": "AWS::Region"},
                "awslogs-stream-prefix": service_key,
            },
        ),
    )
    if service_config.get("command"):
        container_kwargs["Command"] = service_config["command"]
    if secrets:
        container_kwargs["Secrets"] = secrets

    return TaskDefinition(
        _resource_title(service_key, "TaskDef"),
        RequiresCompatibilities=["FARGATE"],
        NetworkMode="awsvpc",
        Cpu=str(service_config["cpu"]),
        Memory=str(service_config["memory_mib"]),
        ExecutionRoleArn=Ref(execution_role_arn_param),
        TaskRoleArn=GetAtt(task_role_ref, "Arn"),
        ContainerDefinitions=[ContainerDefinition(**container_kwargs)],
        Tags=cardinal_tags(component="compute", role=service_key),
    )


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
    container_port: int = None,
) -> Service:
    """ECS Fargate Service with rolling deploy + circuit breaker."""
    kwargs = dict(
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

    return Service(_resource_title(service_key, "Service"), **kwargs)


def _resource_title(service_key: str, suffix: str) -> str:
    """Convert a service key like 'query-api' to a CFN logical ID like 'QueryApiService'."""
    return "".join(part.capitalize() for part in service_key.replace("-", " ").split()) + suffix
