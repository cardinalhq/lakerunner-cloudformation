"""cardinal-remote-collector.yaml: otel collector in a remote account.

Standalone root template deployed via the AWS console in the second account.
Receives OTLP, assumes the main-account writer role, and writes telemetry to the
main-account remote-ingest bucket. The customer brings VpcId, PrivateSubnetsCsv,
and ClusterArn; this stack creates the ALB, security groups, roles, log group,
and the otel ECS service.

Design: docs/superpowers/specs/2026-05-29-cross-account-remote-ingest-design.md
"""

from troposphere import (
    GetAtt,
    Output,
    Parameter,
    Ref,
    Split,
    Sub,
    Tags,
    Template,
)
from troposphere.ec2 import SecurityGroup, SecurityGroupRule
from troposphere.ecs import (
    AwsvpcConfiguration,
    ContainerDefinition,
    DeploymentCircuitBreaker,
    DeploymentConfiguration,
    Environment,
    LoadBalancer as EcsLoadBalancer,
    LogConfiguration,
    NetworkConfiguration,
    PortMapping,
    Service,
    TaskDefinition,
)
from troposphere.elasticloadbalancingv2 import (
    Action,
    Listener,
    LoadBalancer,
    Matcher,
    TargetGroup,
)
from troposphere.iam import Policy, Role
from troposphere.logs import LogGroup

from cardinal_cfn.defaults import load_defaults, load_remote_otel_default_config
from cardinal_cfn.images import add_image_override

_SERVICE_KEY = "otel-grpc"
_OTLP_HTTP_PORT = 4318
_HEALTH_PORT = 13133


def _tags(*, component: str) -> Tags:
    return Tags(
        Name=f"cardinal-remote-collector-{component}",
        Project="cardinal",
        Application="cardinal-lakerunner",
        Component=component,
        ManagedBy="cardinal-cfn",
    )


def build() -> Template:
    t = Template()
    t.set_description(
        "Cardinal remote collector: an ALB-fronted cardinalhq-otel-collector in a "
        "remote account that assumes a main-account writer role to write telemetry "
        "to the main-account remote-ingest bucket."
    )

    defaults = load_defaults()
    otel_cfg = defaults["otel"]["otel-gateway"]

    # Customer-supplied
    t.add_parameter(Parameter("VpcId", Type="AWS::EC2::VPC::Id", Description="Customer VPC ID."))
    t.add_parameter(Parameter(
        "PrivateSubnetsCsv",
        Type="String",
        Description="Comma-separated private subnet IDs (>=2 AZs) for the internal ALB and the collector ENIs.",
    ))
    t.add_parameter(Parameter("ClusterArn", Type="String", Description="Customer ECS cluster ARN."))

    # From the main-account cardinal-remote-ingest stack outputs
    t.add_parameter(Parameter("WriterRoleArn", Type="String", Description="Writer role ARN to assume (remote-ingest WriterRoleArn output)."))
    t.add_parameter(Parameter("BucketName", Type="String", Description="Main-account remote-ingest bucket name."))
    t.add_parameter(Parameter(
        "BucketRegion",
        Type="String",
        Description="Bucket region (the main/lakerunner region). NOT the remote account's region.",
    ))
    t.add_parameter(Parameter("OrgId", Type="String", Description="Lakerunner organization_id (match the remote-ingest OrgId)."))
    t.add_parameter(Parameter("CollectorName", Type="String", Default="lakerunner", Description="Collector name (match the remote-ingest CollectorName)."))
    t.add_parameter(Parameter(
        "OtlpIngressCidr",
        Type="String",
        Default="10.0.0.0/8",
        Description="Source CIDR allowed to reach the internal ALB on 4318. Narrow to your sender/VPC CIDR.",
    ))

    image_ref = add_image_override(
        t,
        name="OtelImage",
        default=defaults["images"]["otel"],
        description="Container image for the cardinalhq-otel-collector service.",
    )
    t.add_parameter(Parameter("OtelReplicas", Type="Number", Default=str(otel_cfg["replicas"]), Description="Desired replicas."))
    t.add_parameter(Parameter("OtelCpu", Type="String", Default=str(otel_cfg["cpu"]), Description="Fargate CPU units."))
    t.add_parameter(Parameter("OtelMemory", Type="String", Default=str(otel_cfg["memory_mib"]), Description="Fargate memory (MiB)."))

    # ------------------------------------------------------------------ SGs
    alb_sg = t.add_resource(SecurityGroup(
        "AlbSecurityGroup",
        GroupDescription="cardinal remote collector ALB; OTLP/HTTP 4318 ingress.",
        VpcId=Ref("VpcId"),
        SecurityGroupIngress=[SecurityGroupRule(
            IpProtocol="tcp", FromPort=_OTLP_HTTP_PORT, ToPort=_OTLP_HTTP_PORT,
            CidrIp=Ref("OtlpIngressCidr"),
            Description="OTLP/HTTP from senders",
        )],
        SecurityGroupEgress=[SecurityGroupRule(
            IpProtocol="-1", CidrIp="0.0.0.0/0", Description="All egress",
        )],
        Tags=_tags(component="alb-sg"),
    ))
    task_sg = t.add_resource(SecurityGroup(
        "TaskSecurityGroup",
        GroupDescription="cardinal remote collector tasks; 4318 from ALB only.",
        VpcId=Ref("VpcId"),
        SecurityGroupIngress=[SecurityGroupRule(
            IpProtocol="tcp", FromPort=_OTLP_HTTP_PORT, ToPort=_OTLP_HTTP_PORT,
            SourceSecurityGroupId=Ref(alb_sg),
            Description="OTLP/HTTP from the ALB",
        )],
        SecurityGroupEgress=[SecurityGroupRule(
            IpProtocol="-1", CidrIp="0.0.0.0/0", Description="All egress",
        )],
        Tags=_tags(component="task-sg"),
    ))

    # ------------------------------------------------------------------ ALB
    alb = t.add_resource(LoadBalancer(
        "Alb",
        Scheme="internal",
        Type="application",
        Subnets=Split(",", Ref("PrivateSubnetsCsv")),
        SecurityGroups=[Ref(alb_sg)],
        Tags=_tags(component="alb"),
    ))
    target_group = t.add_resource(TargetGroup(
        "OtelTargetGroup",
        Port=_OTLP_HTTP_PORT,
        Protocol="HTTP",
        TargetType="ip",
        VpcId=Ref("VpcId"),
        HealthCheckPath="/",
        HealthCheckPort=str(_HEALTH_PORT),
        HealthCheckProtocol="HTTP",
        Matcher=Matcher(HttpCode="200"),
        Tags=_tags(component="otel-tg"),
    ))
    listener = t.add_resource(Listener(
        "OtelHttpListener",
        LoadBalancerArn=Ref(alb),
        Port=_OTLP_HTTP_PORT,
        Protocol="HTTP",
        DefaultActions=[Action(Type="forward", TargetGroupArn=Ref(target_group))],
    ))

    # ------------------------------------------------------------------ Roles
    exec_role = t.add_resource(Role(
        "ExecutionRole",
        AssumeRolePolicyDocument={
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }],
        },
        ManagedPolicyArns=[
            "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy",
        ],
        Tags=_tags(component="exec-role"),
    ))
    task_role = t.add_resource(Role(
        "TaskRole",
        RoleName=Sub("cardinal-remote-otel-${AWS::Region}"),
        AssumeRolePolicyDocument={
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }],
        },
        Policies=[Policy(
            PolicyName="cardinal-remote-otel-assume-writer",
            PolicyDocument={
                "Version": "2012-10-17",
                "Statement": [{
                    "Effect": "Allow",
                    "Action": "sts:AssumeRole",
                    "Resource": Ref("WriterRoleArn"),
                }],
            },
        )],
        Tags=_tags(component="task-role"),
    ))

    log_group = t.add_resource(LogGroup(
        "OtelLogGroup",
        LogGroupName="/cardinal/otel-grpc",
        RetentionInDays=14,
        DeletionPolicy="Delete",
        UpdateReplacePolicy="Delete",
        Tags=_tags(component="otel-logs"),
    ))

    # ------------------------------------------------------------ Task def
    env = [
        Environment(Name="CHQ_COLLECTOR_CONFIG_YAML", Value=load_remote_otel_default_config()),
        Environment(Name="LRDB_S3_BUCKET", Value=Ref("BucketName")),
        Environment(Name="LRDB_S3_REGION", Value=Ref("BucketRegion")),
        Environment(Name="LRDB_S3_ROLE_ARN", Value=Ref("WriterRoleArn")),
        Environment(Name="ORG", Value=Ref("OrgId")),
        Environment(Name="COLLECTOR", Value=Ref("CollectorName")),
    ] + [Environment(Name=k, Value=str(v)) for k, v in (otel_cfg.get("environment") or {}).items()]

    task_def = t.add_resource(TaskDefinition(
        "OtelTaskDef",
        RequiresCompatibilities=["FARGATE"],
        NetworkMode="awsvpc",
        Cpu=Ref("OtelCpu"),
        Memory=Ref("OtelMemory"),
        ExecutionRoleArn=GetAtt(exec_role, "Arn"),
        TaskRoleArn=GetAtt(task_role, "Arn"),
        ContainerDefinitions=[ContainerDefinition(
            Name=_SERVICE_KEY,
            Image=image_ref,
            Essential=True,
            Command=otel_cfg.get("command"),
            Environment=env,
            PortMappings=[PortMapping(ContainerPort=_OTLP_HTTP_PORT, Protocol="tcp")],
            LogConfiguration=LogConfiguration(
                LogDriver="awslogs",
                Options={
                    "awslogs-group": Ref(log_group),
                    "awslogs-region": Ref("AWS::Region"),
                    "awslogs-stream-prefix": _SERVICE_KEY,
                },
            ),
        )],
        Tags=_tags(component="otel-taskdef"),
    ))

    # ------------------------------------------------------------- Service
    t.add_resource(Service(
        "OtelService",
        Cluster=Ref("ClusterArn"),
        LaunchType="FARGATE",
        DesiredCount=Ref("OtelReplicas"),
        TaskDefinition=Ref(task_def),
        DependsOn=[listener.title],
        NetworkConfiguration=NetworkConfiguration(
            AwsvpcConfiguration=AwsvpcConfiguration(
                Subnets=Split(",", Ref("PrivateSubnetsCsv")),
                SecurityGroups=[Ref(task_sg)],
                AssignPublicIp="DISABLED",
            )
        ),
        DeploymentConfiguration=DeploymentConfiguration(
            MinimumHealthyPercent=50,
            MaximumPercent=200,
            DeploymentCircuitBreaker=DeploymentCircuitBreaker(Enable=True, Rollback=True),
        ),
        LoadBalancers=[EcsLoadBalancer(
            ContainerName=_SERVICE_KEY,
            ContainerPort=_OTLP_HTTP_PORT,
            TargetGroupArn=Ref(target_group),
        )],
        Tags=_tags(component="otel-service"),
    ))

    t.add_output(Output("OtelAlbDnsName", Value=GetAtt(alb, "DNSName")))
    t.add_output(Output(
        "OtelExternalUrl",
        Value=Sub(f"http://${{Dns}}:{_OTLP_HTTP_PORT}", Dns=GetAtt(alb, "DNSName")),
    ))

    return t


if __name__ == "__main__":
    print(build().to_yaml(), end="")
