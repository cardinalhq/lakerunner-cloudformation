"""cardinal-satellite-services: collector behind its own ALB in a satellite account.

Standalone stack (not a nested child). Deploys an otel-collector ECS Fargate
service fronted by an internal (or internet-facing) ALB with a plain-HTTP
OTLP listener on port 4318. The collector writes to the raw bucket created by
`cardinal-satellite-infra-base`.

Pull model is preserved: this stack only WRITES to its own in-account raw
bucket. The bucket -> SQS notification -> Lakerunner poller path is owned by
satellite-infra-base; the lakerunner poller in the central account consumes
from the queue cross-account. Nothing here pushes to the Lakerunner account.

Resources created:
  - CollectorExecutionRole (IAM): managed AmazonECSTaskExecutionRolePolicy +
    inline secretsmanager:GetSecretValue on LicenseSecretArn.
  - CollectorTaskRole (IAM): inline s3:PutObject/GetObject/ListBucket/
    GetBucketLocation on RawBucketName. No DeleteObject (poller deletes).
  - AlbSecurityGroup (EC2): ingress tcp 4318 from IngestSourceCidr.
  - TaskSecurityGroup (EC2): ingress tcp 4318 and 13133 from AlbSecurityGroup.
  - Alb (ELBv2): AlbScheme-driven, subnets from AlbSubnetsCsv.
  - OtelHttpListener (ELBv2 Listener): HTTP port 4318, default 404.
  - OtelGrpcTargetGroup: port 4318, health-check on 13133.
  - OtelGrpcListenerRule: path /v1/*, priority 300.
  - OtelGrpcLogGroup: /cardinal/otel-grpc.
  - OtelGrpcTaskDef: ARM64 Fargate, env vars, LICENSE_DATA secret.
  - CollectorService: FARGATE_SPOT, circuit breaker, no Cloud Map.
"""

from troposphere import (
    Equals,
    GetAtt,
    If,
    Not,
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
    Secret,
    Service,
    TaskDefinition,
)
from troposphere.elasticloadbalancingv2 import (
    Action,
    Condition as AlbCondition,
    FixedResponseConfig,
    ListenerRule,
    ListenerRuleAction,
    LoadBalancer,
    Listener,
    Matcher,
    PathPatternConfig,
    TargetGroup,
)
from troposphere.iam import Policy, Role
from troposphere.logs import LogGroup

from cardinal_cfn.defaults import load_defaults, load_otel_default_config
from cardinal_cfn.images import add_image_override
from cardinal_cfn.parameters import add_parameter_group_metadata
from cardinal_cfn.policies import apply_policy

APPLICATION = "cardinal-lakerunner"
PROJECT = "cardinal"
MANAGED_BY = "cardinal-cfn-satellite"

_SERVICE_KEY = "otel-grpc"
_OTLP_HTTP_PORT = 4318
_HEALTH_PORT = 13133


def _tags(*, component: str) -> Tags:
    return Tags(
        Application=APPLICATION,
        Project=PROJECT,
        ManagedBy=MANAGED_BY,
        Component=component,
        Name=f"cardinal-{component}",
    )


def _ecs_tasks_trust() -> dict:
    return {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "ecs-tasks.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }],
    }


def build() -> Template:
    t = Template()
    t.set_description(
        "Cardinal satellite services: otel-collector ECS Fargate service behind "
        "its own internal ALB, writing to the raw bucket from satellite-infra-base. "
        "Pull model; nothing pushes to the Lakerunner account."
    )

    defaults = load_defaults()
    otel_cfg = defaults["otel"]["otel-gateway"]

    # ------------------------------------------------------------------
    # Parameters
    # ------------------------------------------------------------------
    t.add_parameter(
        Parameter(
            "RawBucketName",
            Type="String",
            Description=(
                "Name of the raw ingest bucket (RawBucketName output of "
                "cardinal-satellite-infra-base)."
            ),
            AllowedPattern=r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$",
        )
    )
    t.add_parameter(
        Parameter(
            "LicenseSecretArn",
            Type="String",
            Description=(
                "ARN of the Cardinal license secret in this account. "
                "The collector validates it at startup."
            ),
        )
    )
    t.add_parameter(
        Parameter(
            "VpcId",
            Type="AWS::EC2::VPC::Id",
            Description="VPC the ALB and tasks are placed in.",
        )
    )
    t.add_parameter(
        Parameter(
            "AlbSubnetsCsv",
            Type="String",
            Description=(
                "Comma-separated subnet IDs for the ALB. Use private subnets "
                "when AlbScheme=internal; public subnets when internet-facing."
            ),
        )
    )
    t.add_parameter(
        Parameter(
            "TaskSubnetsCsv",
            Type="String",
            Description="Comma-separated private subnet IDs for the collector tasks.",
        )
    )
    t.add_parameter(
        Parameter(
            "EcsClusterArn",
            Type="String",
            Description="ARN of the customer-supplied ECS cluster.",
        )
    )
    t.add_parameter(
        Parameter(
            "AlbScheme",
            Type="String",
            Default="internal",
            AllowedValues=["internal", "internet-facing"],
            Description=(
                "ALB scheme. Default 'internal' keeps the ALB private; "
                "set 'internet-facing' for environments reachable from outside the VPC."
            ),
        )
    )
    t.add_parameter(
        Parameter(
            "IngestSourceCidr",
            Type="String",
            Default="10.0.0.0/8",
            AllowedPattern=r"^\d{1,3}(\.\d{1,3}){3}/\d{1,2}$",
            Description="CIDR allowed to send OTLP to the ALB SG on port 4318.",
        )
    )

    # Tunables
    t.add_parameter(
        Parameter(
            "OtelReplicas",
            Type="Number",
            Default=str(otel_cfg["replicas"]),
            Description="Desired replica count for the collector service.",
        )
    )
    t.add_parameter(
        Parameter(
            "OtelCpu",
            Type="String",
            Default=str(otel_cfg["cpu"]),
            Description="Fargate CPU units for the collector.",
        )
    )
    t.add_parameter(
        Parameter(
            "OtelMemory",
            Type="String",
            Default=str(otel_cfg["memory_mib"]),
            Description="Fargate memory (MiB) for the collector.",
        )
    )
    t.add_parameter(
        Parameter(
            "OtelConfigYaml",
            Type="String",
            Default="",
            Description=(
                "Optional inline OTEL collector config YAML. "
                "Empty uses the default ingest-to-S3 pipeline."
            ),
        )
    )

    # Image override
    image_ref = add_image_override(
        t,
        name="OtelImage",
        default=defaults["images"]["otel"],
        description="Container image for the cardinalhq-otel-collector service.",
    )

    # ------------------------------------------------------------------
    # Conditions
    # ------------------------------------------------------------------
    t.add_condition(
        "HasOtelConfigOverride", Not(Equals(Ref("OtelConfigYaml"), ""))
    )

    # ------------------------------------------------------------------
    # Console parameter grouping
    # ------------------------------------------------------------------
    add_parameter_group_metadata(
        t,
        groups=[
            {
                "label": "Inputs",
                "parameters": [
                    "RawBucketName",
                    "LicenseSecretArn",
                    "EcsClusterArn",
                ],
            },
            {
                "label": "Networking",
                "parameters": [
                    "VpcId",
                    "AlbSubnetsCsv",
                    "TaskSubnetsCsv",
                    "AlbScheme",
                    "IngestSourceCidr",
                ],
            },
            {
                "label": "Tunables",
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

    # ------------------------------------------------------------------
    # IAM: execution role
    # ------------------------------------------------------------------
    exec_role = t.add_resource(
        Role(
            "CollectorExecutionRole",
            AssumeRolePolicyDocument=_ecs_tasks_trust(),
            ManagedPolicyArns=[
                "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy",
            ],
            Policies=[
                Policy(
                    PolicyName="cardinal-collector-exec-extras",
                    PolicyDocument={
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Sid": "LicenseSecretPull",
                                "Effect": "Allow",
                                "Action": [
                                    "secretsmanager:GetSecretValue",
                                ],
                                "Resource": [Ref("LicenseSecretArn")],
                            },
                            {
                                "Sid": "CollectorLogStreams",
                                "Effect": "Allow",
                                "Action": [
                                    "logs:CreateLogStream",
                                    "logs:PutLogEvents",
                                ],
                                "Resource": Sub(
                                    "arn:${AWS::Partition}:logs:${AWS::Region}:"
                                    "${AWS::AccountId}:log-group:/cardinal/*"
                                ),
                            },
                        ],
                    },
                )
            ],
        )
    )

    # ------------------------------------------------------------------
    # IAM: task role (write-only to raw bucket; no DeleteObject)
    # ------------------------------------------------------------------
    task_role = t.add_resource(
        Role(
            "CollectorTaskRole",
            AssumeRolePolicyDocument=_ecs_tasks_trust(),
            Policies=[
                Policy(
                    PolicyName="cardinal-collector-write",
                    PolicyDocument={
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Sid": "RawBucketWrite",
                                "Effect": "Allow",
                                "Action": [
                                    "s3:PutObject",
                                    "s3:GetObject",
                                    "s3:ListBucket",
                                    "s3:GetBucketLocation",
                                ],
                                "Resource": [
                                    Sub(
                                        "arn:${AWS::Partition}:s3:::${RawBucketName}"
                                    ),
                                    Sub(
                                        "arn:${AWS::Partition}:s3:::${RawBucketName}/*"
                                    ),
                                ],
                            },
                        ],
                    },
                )
            ],
        )
    )

    # ------------------------------------------------------------------
    # Security groups
    # ------------------------------------------------------------------
    alb_sg = t.add_resource(
        SecurityGroup(
            "AlbSecurityGroup",
            GroupDescription=(
                "Cardinal satellite ALB SG. Inbound 4318 from IngestSourceCidr."
            ),
            VpcId=Ref("VpcId"),
            SecurityGroupIngress=[
                SecurityGroupRule(
                    IpProtocol="tcp",
                    FromPort=_OTLP_HTTP_PORT,
                    ToPort=_OTLP_HTTP_PORT,
                    CidrIp=Ref("IngestSourceCidr"),
                    Description="OTLP/HTTP from IngestSourceCidr",
                )
            ],
            SecurityGroupEgress=[
                SecurityGroupRule(
                    IpProtocol="-1",
                    CidrIp="0.0.0.0/0",
                    Description="All egress",
                )
            ],
            Tags=_tags(component="satellite-alb-sg"),
        )
    )

    task_sg = t.add_resource(
        SecurityGroup(
            "TaskSecurityGroup",
            GroupDescription=(
                "Cardinal satellite collector task SG. "
                "Inbound 4318 and 13133 from AlbSecurityGroup."
            ),
            VpcId=Ref("VpcId"),
            SecurityGroupIngress=[
                SecurityGroupRule(
                    IpProtocol="tcp",
                    FromPort=_OTLP_HTTP_PORT,
                    ToPort=_OTLP_HTTP_PORT,
                    SourceSecurityGroupId=Ref(alb_sg),
                    Description="OTLP/HTTP from ALB SG",
                ),
                SecurityGroupRule(
                    IpProtocol="tcp",
                    FromPort=_HEALTH_PORT,
                    ToPort=_HEALTH_PORT,
                    SourceSecurityGroupId=Ref(alb_sg),
                    Description="Health probe from ALB SG",
                ),
            ],
            SecurityGroupEgress=[
                SecurityGroupRule(
                    IpProtocol="-1",
                    CidrIp="0.0.0.0/0",
                    Description="All egress",
                )
            ],
            Tags=_tags(component="satellite-task-sg"),
        )
    )

    # ------------------------------------------------------------------
    # ALB
    # ------------------------------------------------------------------
    alb = t.add_resource(
        LoadBalancer(
            "Alb",
            Scheme=Ref("AlbScheme"),
            Subnets=Split(",", Ref("AlbSubnetsCsv")),
            SecurityGroups=[Ref(alb_sg)],
            Type="application",
            Tags=_tags(component="satellite-alb"),
        )
    )
    apply_policy(alb, "alb")

    # Plain-HTTP OTLP listener on 4318.  No cert needed — internal-scheme
    # ALB + VPC-layer reachability (TGW/peering/VPN).
    otel_listener = t.add_resource(
        Listener(
            "OtelHttpListener",
            LoadBalancerArn=Ref(alb),
            Port=_OTLP_HTTP_PORT,
            Protocol="HTTP",
            DefaultActions=[
                Action(
                    Type="fixed-response",
                    FixedResponseConfig=FixedResponseConfig(
                        StatusCode="404",
                        ContentType="text/plain",
                        MessageBody="no listener rule matched",
                    ),
                )
            ],
        )
    )

    # ------------------------------------------------------------------
    # Target group (inlined to avoid cardinal_tags / InstallIdShort in tags)
    # ------------------------------------------------------------------
    target_group = t.add_resource(
        TargetGroup(
            "OtelGrpcTargetGroup",
            Port=_OTLP_HTTP_PORT,
            Protocol="HTTP",
            TargetType="ip",
            VpcId=Ref("VpcId"),
            HealthCheckPath="/",
            HealthCheckPort=str(_HEALTH_PORT),
            HealthCheckProtocol="HTTP",
            Matcher=Matcher(HttpCode="200"),
            Tags=_tags(component="satellite-otel-tg"),
        )
    )

    # Inline listener rule: the listener is an in-stack resource, not a
    # parameter, so we cannot use build_listener_rule (which does Ref(param)).
    listener_rule = t.add_resource(
        ListenerRule(
            "OtelGrpcListenerRule",
            ListenerArn=Ref(otel_listener),
            Priority=300,
            Conditions=[
                AlbCondition(
                    Field="path-pattern",
                    PathPatternConfig=PathPatternConfig(Values=["/v1/*"]),
                )
            ],
            Actions=[
                ListenerRuleAction(
                    Type="forward",
                    TargetGroupArn=Ref(target_group),
                )
            ],
        )
    )

    # ------------------------------------------------------------------
    # Log group (inlined to avoid cardinal_tags / InstallIdShort in tags)
    # ------------------------------------------------------------------
    log_group = t.add_resource(
        LogGroup(
            "OtelGrpcLogGroup",
            LogGroupName=f"/cardinal/{_SERVICE_KEY}",
            RetentionInDays=14,
            Tags=_tags(component="satellite-otel-log"),
        )
    )
    apply_policy(log_group, "log-group")

    # ------------------------------------------------------------------
    # Task definition (inlined — exec/task roles are in-stack resources,
    # so we use GetAtt(..., "Arn") instead of Ref(param)).
    # ------------------------------------------------------------------
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
        Environment(Name="LRDB_S3_BUCKET", Value=Ref("RawBucketName")),
        Environment(Name="LRDB_S3_REGION", Value=Ref("AWS::Region")),
        Environment(Name="ORG", Value=default_org),
        Environment(Name="COLLECTOR", Value=default_collector),
    ]

    # Add service-specific env vars from defaults (e.g. OTEL_RESOURCE_ATTRIBUTES)
    svc_env = otel_cfg.get("environment") or {}
    env += [Environment(Name=k, Value=str(v)) for k, v in svc_env.items()]

    secrets = [
        Secret(Name="LICENSE_DATA", ValueFrom=Ref("LicenseSecretArn")),
    ]

    command = otel_cfg.get("command")
    container_kwargs = dict(
        Name=_SERVICE_KEY,
        Image=image_ref,
        Essential=True,
        Environment=env,
        Secrets=secrets,
        PortMappings=[
            PortMapping(ContainerPort=_OTLP_HTTP_PORT, Protocol="tcp")
        ],
        LogConfiguration=LogConfiguration(
            LogDriver="awslogs",
            Options={
                "awslogs-group": Ref(log_group),
                "awslogs-region": Ref("AWS::Region"),
                "awslogs-stream-prefix": _SERVICE_KEY,
            },
        ),
    )
    if command:
        container_kwargs["Command"] = command

    task_def = t.add_resource(
        TaskDefinition(
            "OtelGrpcTaskDef",
            RequiresCompatibilities=["FARGATE"],
            NetworkMode="awsvpc",
            RuntimePlatform=RuntimePlatform(
                CpuArchitecture="ARM64",
                OperatingSystemFamily="LINUX",
            ),
            Cpu=Ref("OtelCpu"),
            Memory=Ref("OtelMemory"),
            ExecutionRoleArn=GetAtt(exec_role, "Arn"),
            TaskRoleArn=GetAtt(task_role, "Arn"),
            ContainerDefinitions=[ContainerDefinition(**container_kwargs)],
            Tags=_tags(component="satellite-otel-task"),
        )
    )

    # ------------------------------------------------------------------
    # ECS service (no Cloud Map, no ServiceRegistries)
    # ------------------------------------------------------------------
    service = t.add_resource(
        Service(
            "CollectorService",
            Cluster=Ref("EcsClusterArn"),
            CapacityProviderStrategy=[
                CapacityProviderStrategyItem(CapacityProvider="FARGATE_SPOT", Weight=1),
            ],
            DesiredCount=Ref("OtelReplicas"),
            TaskDefinition=Ref(task_def),
            NetworkConfiguration=NetworkConfiguration(
                AwsvpcConfiguration=AwsvpcConfiguration(
                    Subnets=Split(",", Ref("TaskSubnetsCsv")),
                    SecurityGroups=[Ref(task_sg)],
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
            DependsOn=[listener_rule.title],
            Tags=_tags(component="satellite-otel-svc"),
        )
    )

    # ------------------------------------------------------------------
    # Outputs
    # ------------------------------------------------------------------
    t.add_output(
        Output(
            "CollectorAlbDnsName",
            Description="DNS name of the satellite collector ALB.",
            Value=GetAtt(alb, "DNSName"),
        )
    )
    t.add_output(
        Output(
            "CollectorEndpoint",
            Description="OTLP/HTTP endpoint for the satellite collector.",
            Value=Sub(
                "http://${AlbDns}:4318",
                AlbDns=GetAtt(alb, "DNSName"),
            ),
        )
    )
    t.add_output(
        Output(
            "CollectorServiceName",
            Description="ECS service name of the collector.",
            Value=GetAtt(service, "Name"),
        )
    )
    t.add_output(
        Output(
            "CollectorTaskRoleArn",
            Description="ARN of the collector task role.",
            Value=GetAtt(task_role, "Arn"),
        )
    )

    return t


if __name__ == "__main__":
    print(build().to_yaml(), end="")
