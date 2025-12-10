#!/usr/bin/env python3
# Copyright (C) 2025 CardinalHQ, Inc
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, version 3.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.

import yaml
import os
from troposphere import (
    Template, Parameter, Ref, Sub, GetAtt, If, Equals, Export, Output,
    Select, Not, Tags, ImportValue, Join, And, Split, Condition
)
from troposphere.ecs import (
    Service, TaskDefinition, ContainerDefinition, Environment,
    LogConfiguration, Secret as EcsSecret, Volume, MountPoint,
    HealthCheck, PortMapping, RuntimePlatform, NetworkConfiguration, AwsvpcConfiguration,
    LoadBalancer as EcsLoadBalancer, EFSVolumeConfiguration, AuthorizationConfig
)
from troposphere.applicationautoscaling import (
    ScalableTarget, ScalingPolicy,
    TargetTrackingScalingPolicyConfiguration, PredefinedMetricSpecification
)
from troposphere.iam import Role, Policy
from troposphere.elasticloadbalancingv2 import LoadBalancer, TargetGroup, TargetGroupAttribute, Listener, Matcher
from troposphere.elasticloadbalancingv2 import Action as AlbAction
from troposphere.ssm import Parameter as SSMParameter
from troposphere.ec2 import SecurityGroup, SecurityGroupIngress
from troposphere.efs import AccessPoint, PosixUser, RootDirectory, CreationInfo
from troposphere.logs import LogGroup
from troposphere.secretsmanager import Secret, GenerateSecretString
from troposphere.ssm import Parameter as SSMParameter

def load_service_config(config_file="lakerunner-stack-defaults.yaml"):
    """Load service configuration from YAML file"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(script_dir, "..", config_file)

    with open(config_path, 'r') as f:
        return yaml.safe_load(f)

def create_services_template():
    """Create CloudFormation template for all services"""

    t = Template()
    t.set_description("Lakerunner Services: ECS services, task definitions, IAM roles, and ALB integration")

    # Load service configurations and image defaults
    config = load_service_config()
    services = config.get('services', {})
    images = config.get('images', {})

    # Define valid Fargate CPU/memory combinations
    # Format: {cpu_units: [valid_memory_values_in_mib]}
    FARGATE_CPU_MEMORY = {
        256: [512, 1024, 2048],
        512: [1024, 2048, 3072, 4096],
        1024: [2048, 3072, 4096, 5120, 6144, 7168, 8192],
        2048: [4096, 5120, 6144, 7168, 8192, 9216, 10240, 11264, 12288, 13312, 14336, 15360, 16384],
        4096: [8192, 9216, 10240, 11264, 12288, 13312, 14336, 15360, 16384, 17408, 18432, 19456, 20480, 21504, 22528, 23552, 24576, 25600, 26624, 27648, 28672, 29696, 30720],
        8192: list(range(16384, 61441, 4096)),
        16384: list(range(32768, 122881, 8192)),
    }

    # Define lakerunner services that should have configurable parameters
    # query-api and query-worker get CPU + memory + replicas params
    LAKERUNNER_QUERY_SERVICES = ['lakerunner-query-api', 'lakerunner-query-worker']

    # Ingest/compact/rollup services get memory + replicas params
    LAKERUNNER_WORKER_SERVICES = [
        'lakerunner-ingest-logs',
        'lakerunner-ingest-metrics',
        'lakerunner-ingest-traces',
        'lakerunner-compact-logs',
        'lakerunner-compact-metrics',
        'lakerunner-compact-traces',
        'lakerunner-rollup-metrics',
    ]

    # Services that use YAML config only (no parameters)
    # sweeper, monitor use YAML for both replicas and memory
    # pubsub, boxer use YAML for memory but get replicas param
    LAKERUNNER_REPLICAS_ONLY_SERVICES = [
        'lakerunner-pubsub-sqs',
        'lakerunner-boxer-common',
    ]

    # All configurable services (for iteration)
    LAKERUNNER_CONFIGURABLE_SERVICES = (
        LAKERUNNER_QUERY_SERVICES +
        LAKERUNNER_WORKER_SERVICES +
        LAKERUNNER_REPLICAS_ONLY_SERVICES
    )

    # Helper to convert service name to parameter-friendly name
    def service_to_param_name(service_name):
        # lakerunner-query-api -> QueryApi
        parts = service_name.replace('lakerunner-', '').split('-')
        return ''.join(word.capitalize() for word in parts)

    # -----------------------
    # Parameters (imports from CommonInfra)
    # -----------------------
    CommonInfraStackName = t.add_parameter(Parameter(
        "CommonInfraStackName", Type="String",
        Description="REQUIRED: Name of the CommonInfra stack to import infrastructure values from."
    ))

    # Container image overrides for air-gapped deployments
    GoServicesImage = t.add_parameter(Parameter(
        "GoServicesImage", Type="String",
        Default=images.get('go_services', 'public.ecr.aws/cardinalhq.io/lakerunner:latest'),
        Description="Container image for all Go services (pubsub, ingest, compact, query-api, query-worker, etc.)"
    ))


    # OTLP Telemetry configuration
    OtelEndpoint = t.add_parameter(Parameter(
        "OtelEndpoint", Type="String",
        Default="",
        Description="OPTIONAL: OTEL collector HTTP endpoint URL (e.g., http://collector-dns:4318). Leave blank to disable OTLP telemetry export."
    ))

    # MSK Configuration
    MSKBrokers = t.add_parameter(Parameter(
        "MSKBrokers", Type="String", Default="",
        Description="REQUIRED: Comma-separated list of MSK broker endpoints (hostname:port)"
    ))

    # Signal Type Configuration
    EnableLogs = t.add_parameter(Parameter(
        "EnableLogs", Type="String",
        AllowedValues=["Yes", "No"],
        Default="Yes",
        Description="Enable log processing services (ingest-logs, compact-logs)"
    ))

    EnableMetrics = t.add_parameter(Parameter(
        "EnableMetrics", Type="String",
        AllowedValues=["Yes", "No"],
        Default="Yes",
        Description="Enable metrics processing services (ingest-metrics, compact-metrics, rollup-metrics)"
    ))

    EnableTraces = t.add_parameter(Parameter(
        "EnableTraces", Type="String",
        AllowedValues=["Yes", "No"],
        Default="No",
        Description="Enable trace processing services (ingest-traces, compact-traces)"
    ))

    # ALB Configuration parameters

    AlbScheme = t.add_parameter(Parameter(
        "AlbScheme",
        Type="String",
        AllowedValues=["internet-facing", "internal"],
        Default="internal",
        Description="Load balancer scheme: 'internet-facing' for external access or 'internal' for internal access only."
    ))

    # -----------------------
    # Service Configuration Parameters
    # -----------------------
    # Store parameter references for use when creating services
    service_params = {}

    for service_name in LAKERUNNER_CONFIGURABLE_SERVICES:
        if service_name not in services:
            continue  # Skip services not defined in defaults

        service_config = services[service_name]
        param_name = service_to_param_name(service_name)
        service_params[service_name] = {}

        # Replicas parameter for configurable services
        default_replicas = service_config.get('replicas', 1)
        replicas_param = t.add_parameter(Parameter(
            f"{param_name}Replicas",
            Type="Number",
            Default=str(default_replicas),
            MinValue=0,
            MaxValue=20,
            Description=f"Number of {service_name} task replicas"
        ))
        service_params[service_name]['replicas'] = replicas_param

        # CPU and Memory for query services
        if service_name in LAKERUNNER_QUERY_SERVICES:
            default_cpu = service_config.get('cpu', 1024)
            cpu_param = t.add_parameter(Parameter(
                f"{param_name}Cpu",
                Type="Number",
                Default=str(default_cpu),
                AllowedValues=[str(c) for c in FARGATE_CPU_MEMORY.keys()],
                Description=f"CPU units for {service_name} (256, 512, 1024, 2048, 4096, 8192, 16384)"
            ))
            service_params[service_name]['cpu'] = cpu_param

            default_memory = service_config.get('memory_mib', 2048)
            # Get all valid memory values across all CPU tiers
            all_memory_values = sorted(set(
                m for memories in FARGATE_CPU_MEMORY.values() for m in memories
            ))
            memory_param = t.add_parameter(Parameter(
                f"{param_name}Memory",
                Type="Number",
                Default=str(default_memory),
                AllowedValues=[str(m) for m in all_memory_values],
                Description=f"Memory (MiB) for {service_name}. Must be valid for the selected CPU."
            ))
            service_params[service_name]['memory'] = memory_param

        # Memory-only for worker services (ingest, compact, rollup)
        elif service_name in LAKERUNNER_WORKER_SERVICES:
            default_memory = service_config.get('memory_mib', 1024)
            default_cpu = service_config.get('cpu', 512)
            # Get valid memory values for the service's CPU tier
            valid_memories = FARGATE_CPU_MEMORY.get(default_cpu, [512, 1024, 2048])
            memory_param = t.add_parameter(Parameter(
                f"{param_name}Memory",
                Type="Number",
                Default=str(default_memory),
                AllowedValues=[str(m) for m in valid_memories],
                Description=f"Memory (MiB) for {service_name}. Valid values for {default_cpu} CPU: {', '.join(str(m) for m in valid_memories)}"
            ))
            service_params[service_name]['memory'] = memory_param

        # Replicas-only services (pubsub, boxer) - no memory parameter

    # -----------------------
    # Auto-Scaling Parameters for Worker Services
    # -----------------------
    # Services that support CPU-based auto-scaling
    AUTOSCALING_SERVICES = LAKERUNNER_WORKER_SERVICES  # ingest, compact, rollup

    EnableAutoScaling = t.add_parameter(Parameter(
        "EnableAutoScaling",
        Type="String",
        AllowedValues=["Yes", "No"],
        Default="Yes",
        Description="Enable CPU-based auto-scaling for ingest, compact, and rollup services"
    ))

    AutoScalingMaxReplicas = t.add_parameter(Parameter(
        "AutoScalingMaxReplicas",
        Type="Number",
        Default="10",
        MinValue=1,
        MaxValue=50,
        Description="Maximum number of tasks when auto-scaling (applies to all scaled services)"
    ))

    AutoScalingCPUTarget = t.add_parameter(Parameter(
        "AutoScalingCPUTarget",
        Type="Number",
        Default="70",
        MinValue=10,
        MaxValue=95,
        Description="Target CPU utilization percentage for scaling (e.g., 70 means scale when avg CPU > 70%)"
    ))

    AutoScalingScaleOutCooldown = t.add_parameter(Parameter(
        "AutoScalingScaleOutCooldown",
        Type="Number",
        Default="60",
        MinValue=0,
        MaxValue=3600,
        Description="Seconds to wait after a scale-out before another scale-out can occur"
    ))

    AutoScalingScaleInCooldown = t.add_parameter(Parameter(
        "AutoScalingScaleInCooldown",
        Type="Number",
        Default="300",
        MinValue=0,
        MaxValue=3600,
        Description="Seconds to wait after a scale-in before another scale-in can occur"
    ))

    # Build parameter groups for console
    # Query services get their own group with CPU, Memory, and Replicas
    query_service_params = []
    for svc in LAKERUNNER_QUERY_SERVICES:
        if svc in service_params:
            param_name = service_to_param_name(svc)
            query_service_params.extend([
                f"{param_name}Replicas",
                f"{param_name}Cpu",
                f"{param_name}Memory"
            ])

    # Worker services get Memory and Replicas
    worker_service_params = []
    for svc in LAKERUNNER_WORKER_SERVICES:
        if svc in service_params:
            param_name = service_to_param_name(svc)
            worker_service_params.extend([
                f"{param_name}Replicas",
                f"{param_name}Memory"
            ])

    # Replicas-only services (pubsub, boxer)
    replicas_only_params = []
    for svc in LAKERUNNER_REPLICAS_ONLY_SERVICES:
        if svc in service_params:
            param_name = service_to_param_name(svc)
            replicas_only_params.append(f"{param_name}Replicas")

    # Auto-scaling parameters list for parameter groups
    autoscaling_params = [
        "EnableAutoScaling",
        "AutoScalingMaxReplicas",
        "AutoScalingCPUTarget",
        "AutoScalingScaleOutCooldown",
        "AutoScalingScaleInCooldown"
    ]

    # Build parameter labels
    param_labels = {
        "CommonInfraStackName": {"default": "Common Infra Stack Name"},
        "AlbScheme": {"default": "ALB Scheme"},
        "EnableLogs": {"default": "Enable Logs"},
        "EnableMetrics": {"default": "Enable Metrics"},
        "EnableTraces": {"default": "Enable Traces"},
        "MSKBrokers": {"default": "MSK Broker Endpoints"},
        "GoServicesImage": {"default": "Go Services Image"},
        "OtelEndpoint": {"default": "OTEL Collector Endpoint"},
        "EnableAutoScaling": {"default": "Enable Auto-Scaling"},
        "AutoScalingMaxReplicas": {"default": "Max Replicas (Auto-Scaling)"},
        "AutoScalingCPUTarget": {"default": "CPU Target % (Auto-Scaling)"},
        "AutoScalingScaleOutCooldown": {"default": "Scale-Out Cooldown (seconds)"},
        "AutoScalingScaleInCooldown": {"default": "Scale-In Cooldown (seconds)"}
    }

    # Add labels for service configuration parameters
    for service_name in LAKERUNNER_CONFIGURABLE_SERVICES:
        if service_name not in service_params:
            continue
        param_name = service_to_param_name(service_name)
        # Create friendly label from service name (e.g., "Query Api" from "lakerunner-query-api")
        friendly_name = service_name.replace('lakerunner-', '').replace('-', ' ').title()
        param_labels[f"{param_name}Replicas"] = {"default": f"{friendly_name} Replicas"}
        if service_name in LAKERUNNER_QUERY_SERVICES or service_name in LAKERUNNER_WORKER_SERVICES:
            param_labels[f"{param_name}Memory"] = {"default": f"{friendly_name} Memory (MiB)"}
        if service_name in LAKERUNNER_QUERY_SERVICES:
            param_labels[f"{param_name}Cpu"] = {"default": f"{friendly_name} CPU"}

    t.set_metadata({
        "AWS::CloudFormation::Interface": {
            "ParameterGroups": [
                {
                    "Label": {"default": "Infrastructure"},
                    "Parameters": ["CommonInfraStackName", "AlbScheme"]
                },
                {
                    "Label": {"default": "Signal Types"},
                    "Parameters": ["EnableLogs", "EnableMetrics", "EnableTraces"]
                },
                {
                    "Label": {"default": "Query Services Configuration"},
                    "Parameters": query_service_params
                },
                {
                    "Label": {"default": "Worker Services Configuration"},
                    "Parameters": worker_service_params
                },
                {
                    "Label": {"default": "Other Services Configuration"},
                    "Parameters": replicas_only_params
                },
                {
                    "Label": {"default": "Auto-Scaling Configuration"},
                    "Parameters": autoscaling_params
                },
                {
                    "Label": {"default": "MSK Configuration"},
                    "Parameters": ["MSKBrokers"]
                },
                {
                    "Label": {"default": "Container Images"},
                    "Parameters": ["GoServicesImage"]
                },
                {
                    "Label": {"default": "Telemetry"},
                    "Parameters": ["OtelEndpoint"]
                }
            ],
            "ParameterLabels": param_labels
        }
    })

    # Helper function for imports
    def ci_export(suffix):
        return Sub("${CommonInfraStackName}-%s" % suffix, CommonInfraStackName=Ref(CommonInfraStackName))

    # Helper function to shorten service names for ALB target groups (32 char limit)
    def short_service_name(service_name):
        # Replace lakerunner with lr, remove hyphens, abbreviate common words
        short = service_name.replace('lakerunner-', 'lr-')
        short = short.replace('query-api', 'qapi')
        short = short.replace('query-worker', 'qwork')
        short = short.replace('ingest-', 'ing-')
        short = short.replace('compact-', 'cmp-')
        short = short.replace('rollup-', 'rol-')
        short = short.replace('metrics', 'met')
        short = short.replace('pubsub', 'pub')
        return short

    # Resolved values (always import from CommonInfra)
    ClusterArnValue = ImportValue(ci_export("ClusterArn"))
    DbSecretArnValue = ImportValue(ci_export("DbSecretArn"))
    DbHostValue = ImportValue(ci_export("DbEndpoint"))
    DbPortValue = ImportValue(ci_export("DbPort"))
    MSKCredentialsArnValue = ImportValue(ci_export("MSKCredentialsArn"))
    MSKSecretsKeyArnValue = ImportValue(ci_export("MSKSecretsKeyArn"))
    EfsIdValue = ImportValue(ci_export("EfsId"))
    TaskSecurityGroupIdValue = ImportValue(ci_export("TaskSGId"))
    VpcIdValue = ImportValue(ci_export("VpcId"))
    PrivateSubnetsValue = Split(",", ImportValue(ci_export("PrivateSubnets")))
    BucketArnValue = ImportValue(ci_export("BucketArn"))

    # Import PublicSubnets - CommonInfra always exports this, but may be empty string if not provided
    PublicSubnetsImport = ImportValue(ci_export("PublicSubnets"))
    PublicSubnetsValue = Split(",", PublicSubnetsImport)

    # Conditions
    t.add_condition("EnableOtlp", Not(Equals(Ref(OtelEndpoint), "")))
    t.add_condition("IsInternetFacing", Equals(Ref(AlbScheme), "internet-facing"))
    t.add_condition("CreateLogsServices", Equals(Ref(EnableLogs), "Yes"))
    t.add_condition("CreateMetricsServices", Equals(Ref(EnableMetrics), "Yes"))
    t.add_condition("CreateTracesServices", Equals(Ref(EnableTraces), "Yes"))
    t.add_condition("AutoScalingEnabled", Equals(Ref(EnableAutoScaling), "Yes"))
    # Combined conditions for auto-scaling per signal type
    t.add_condition("AutoScaleLogsServices", And(
        Condition("AutoScalingEnabled"),
        Condition("CreateLogsServices")
    ))
    t.add_condition("AutoScaleMetricsServices", And(
        Condition("AutoScalingEnabled"),
        Condition("CreateMetricsServices")
    ))
    t.add_condition("AutoScaleTracesServices", And(
        Condition("AutoScalingEnabled"),
        Condition("CreateTracesServices")
    ))


    # -----------------------
    # ALB Security Group
    # -----------------------
    AlbSG = t.add_resource(SecurityGroup(
        "AlbSecurityGroup",
        GroupDescription="Security group for ALB",
        VpcId=VpcIdValue,
        SecurityGroupEgress=[{
            "IpProtocol": "-1",
            "CidrIp": "0.0.0.0/0",
            "Description": "Allow all outbound"
        }]
    ))

    t.add_resource(SecurityGroupIngress(
        "Alb7101Open",
        GroupId=Ref(AlbSG),
        IpProtocol="tcp",
        FromPort=7101, ToPort=7101,
        CidrIp="0.0.0.0/0",
        Description="HTTP 7101",
    ))

    # Add ingress rules to task security group to allow ALB traffic
    # Allow ALB to reach query-api on port 8080 (application port)
    t.add_resource(SecurityGroupIngress(
        "TaskFromAlb8080",
        GroupId=TaskSecurityGroupIdValue,
        IpProtocol="tcp",
        FromPort=8080, ToPort=8080,
        SourceSecurityGroupId=Ref(AlbSG),
        Description="ALB to query-api app port",
    ))

    # Allow ALB to reach query-api on port 8090 (health check port)
    t.add_resource(SecurityGroupIngress(
        "TaskFromAlb8090",
        GroupId=TaskSecurityGroupIdValue,
        IpProtocol="tcp",
        FromPort=8090, ToPort=8090,
        SourceSecurityGroupId=Ref(AlbSG),
        Description="ALB health checks",
    ))

    # -----------------------
    # ALB + listeners + target groups
    # -----------------------
    Alb = t.add_resource(LoadBalancer(
        "Alb",
        Scheme=Ref(AlbScheme),
        SecurityGroups=[Ref(AlbSG)],
        Subnets=If(
            "IsInternetFacing",
            PublicSubnetsValue,
            PrivateSubnetsValue
        ),
        Type="application",
    ))

    Tg7101 = t.add_resource(TargetGroup(
        "Tg7101",
        Port=8080, Protocol="HTTP",  # Changed to container port 8080
        VpcId=VpcIdValue,
        TargetType="ip",
        HealthCheckPath="/healthz",
        HealthCheckPort="8090",
        HealthCheckProtocol="HTTP",
        HealthCheckIntervalSeconds=5,
        HealthCheckTimeoutSeconds=2,
        HealthyThresholdCount=2,
        UnhealthyThresholdCount=2,
        Matcher=Matcher(HttpCode="200"),
        TargetGroupAttributes=[
            TargetGroupAttribute(Key="stickiness.enabled", Value="false"),
            TargetGroupAttribute(Key="deregistration_delay.timeout_seconds", Value="5")
        ]
    ))

    t.add_resource(Listener(
        "Listener7101",
        LoadBalancerArn=Ref(Alb),
        Port="7101",
        Protocol="HTTP",
        DefaultActions=[AlbAction(Type="forward", TargetGroupArn=Ref(Tg7101))]
    ))


    # -----------------------
    # Task Execution Role (shared by all services)
    # -----------------------
    ExecutionRole = t.add_resource(Role(
        "ExecRole",
        RoleName=Sub("${AWS::StackName}-exec-role"),
        AssumeRolePolicyDocument={
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                "Action": "sts:AssumeRole"
            }]
        },
        ManagedPolicyArns=[
            "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
        ],
        Policies=[
            Policy(
                PolicyName="SSMParameterAccess",
                PolicyDocument={
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": [
                                "ssm:GetParameter",
                                "ssm:GetParameters"
                            ],
                            "Resource": [
                                Sub("arn:aws:ssm:${AWS::Region}:${AWS::AccountId}:parameter/lakerunner/*"),
                                Sub("arn:aws:ssm:${AWS::Region}:${AWS::AccountId}:parameter/${AWS::StackName}-*")
                            ]
                        },
                        {
                            "Effect": "Allow",
                            "Action": [
                                "ssmmessages:CreateControlChannel",
                                "ssmmessages:CreateDataChannel",
                                "ssmmessages:OpenControlChannel",
                                "ssmmessages:OpenDataChannel"
                            ],
                            "Resource": "*"
                        },
                        {
                            "Effect": "Allow",
                            "Action": [
                                "secretsmanager:GetSecretValue"
                            ],
                            "Resource": [
                                Sub("${SecretArn}*", SecretArn=DbSecretArnValue),
                                Sub("${SecretArn}*", SecretArn=MSKCredentialsArnValue),
                                Sub("arn:aws:secretsmanager:${AWS::Region}:${AWS::AccountId}:secret:${AWS::StackName}-*")
                            ]
                        },
                        {
                            "Effect": "Allow",
                            "Action": [
                                "kms:Decrypt",
                                "kms:DescribeKey"
                            ],
                            "Resource": MSKSecretsKeyArnValue
                        }
                    ]
                }
            )
        ]
    ))

    # -----------------------
    # Task Roles
    # -----------------------

    # Base task role for most services (without ECS discovery permissions)
    TaskRole = t.add_resource(Role(
        "TaskRole",
        RoleName=Sub("${AWS::StackName}-task-role"),
        AssumeRolePolicyDocument={
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                "Action": "sts:AssumeRole"
            }]
        },
        Policies=[
            Policy(
                PolicyName="S3AndSQSAccess",
                PolicyDocument={
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": [
                                "s3:GetObject",
                                "s3:PutObject",
                                "s3:DeleteObject",
                                "s3:ListBucket"
                            ],
                            "Resource": [
                                BucketArnValue,
                                Sub("${BucketArn}/*", BucketArn=BucketArnValue)
                            ]
                        },
                        {
                            "Effect": "Allow",
                            "Action": [
                                "sqs:ReceiveMessage",
                                "sqs:DeleteMessage",
                                "sqs:GetQueueAttributes"
                            ],
                            "Resource": [
                                Sub("arn:aws:sqs:${AWS::Region}:${AWS::AccountId}:lakerunner-*")
                            ]
                        },
                        {
                            "Effect": "Allow",
                            "Action": [
                                "secretsmanager:GetSecretValue"
                            ],
                            "Resource": [
                                Sub("${SecretArn}*", SecretArn=DbSecretArnValue),
                                Sub("${SecretArn}*", SecretArn=MSKCredentialsArnValue),
                                Sub("arn:aws:secretsmanager:${AWS::Region}:${AWS::AccountId}:secret:${AWS::StackName}-*")
                            ]
                        },
                        {
                            "Effect": "Allow",
                            "Action": [
                                "kms:Decrypt",
                                "kms:DescribeKey"
                            ],
                            "Resource": MSKSecretsKeyArnValue
                        },
                        {
                            "Effect": "Allow",
                            "Action": [
                                "elasticfilesystem:ClientMount",
                                "elasticfilesystem:ClientWrite",
                                "elasticfilesystem:ClientRootAccess",
                                "elasticfilesystem:DescribeFileSystems",
                                "elasticfilesystem:DescribeMountTargets",
                                "elasticfilesystem:DescribeAccessPoints"
                            ],
                            "Resource": "*"
                        },
                        {
                            "Effect": "Allow",
                            "Action": [
                                "ssmmessages:CreateControlChannel",
                                "ssmmessages:CreateDataChannel",
                                "ssmmessages:OpenControlChannel",
                                "ssmmessages:OpenDataChannel"
                            ],
                            "Resource": "*"
                        },
                        {
                            "Effect": "Allow",
                            "Action": [
                                "logs:DescribeLogGroups",
                                "logs:CreateLogStream",
                                "logs:DescribeLogStreams",
                                "logs:PutLogEvents"
                            ],
                            "Resource": "*"
                        }
                    ]
                }
            )
        ]
    ))

    # Task role for Query API (with ECS discovery permissions)
    QueryApiTaskRole = t.add_resource(Role(
        "QueryApiTaskRole",
        RoleName=Sub("${AWS::StackName}-query-api-task-role"),
        AssumeRolePolicyDocument={
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                "Action": "sts:AssumeRole"
            }]
        },
        Policies=[
            Policy(
                PolicyName="QueryApiAccess",
                PolicyDocument={
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": [
                                "s3:GetObject",
                                "s3:PutObject",
                                "s3:DeleteObject",
                                "s3:ListBucket"
                            ],
                            "Resource": [
                                BucketArnValue,
                                Sub("${BucketArn}/*", BucketArn=BucketArnValue)
                            ]
                        },
                        {
                            "Effect": "Allow",
                            "Action": [
                                "secretsmanager:GetSecretValue"
                            ],
                            "Resource": [
                                Sub("${SecretArn}*", SecretArn=DbSecretArnValue),
                                Sub("${SecretArn}*", SecretArn=MSKCredentialsArnValue),
                                Sub("arn:aws:secretsmanager:${AWS::Region}:${AWS::AccountId}:secret:${AWS::StackName}-*")
                            ]
                        },
                        {
                            "Effect": "Allow",
                            "Action": [
                                "kms:Decrypt",
                                "kms:DescribeKey"
                            ],
                            "Resource": MSKSecretsKeyArnValue
                        },
                        {
                            "Effect": "Allow",
                            "Action": [
                                "ecs:ListServices",
                                "ecs:DescribeServices",
                                "ecs:ListTasks",
                                "ecs:DescribeTasks"
                            ],
                            "Resource": "*"
                        },
                        {
                            "Effect": "Allow",
                            "Action": [
                                "elasticfilesystem:ClientMount",
                                "elasticfilesystem:ClientWrite",
                                "elasticfilesystem:ClientRootAccess",
                                "elasticfilesystem:DescribeFileSystems",
                                "elasticfilesystem:DescribeMountTargets",
                                "elasticfilesystem:DescribeAccessPoints"
                            ],
                            "Resource": "*"
                        },
                        {
                            "Effect": "Allow",
                            "Action": [
                                "ssmmessages:CreateControlChannel",
                                "ssmmessages:CreateDataChannel",
                                "ssmmessages:OpenControlChannel",
                                "ssmmessages:OpenDataChannel"
                            ],
                            "Resource": "*"
                        },
                        {
                            "Effect": "Allow",
                            "Action": [
                                "logs:DescribeLogGroups",
                                "logs:CreateLogStream",
                                "logs:DescribeLogStreams",
                                "logs:PutLogEvents"
                            ],
                            "Resource": "*"
                        }
                    ]
                }
            )
        ]
    ))

    # Task role for Query Worker (no ECS discovery permissions needed)
    QueryWorkerTaskRole = t.add_resource(Role(
        "QueryWorkerTaskRole",
        RoleName=Sub("${AWS::StackName}-query-worker-task-role"),
        AssumeRolePolicyDocument={
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                "Action": "sts:AssumeRole"
            }]
        },
        Policies=[
            Policy(
                PolicyName="QueryWorkerAccess",
                PolicyDocument={
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": [
                                "s3:GetObject",
                                "s3:PutObject",
                                "s3:DeleteObject",
                                "s3:ListBucket"
                            ],
                            "Resource": [
                                BucketArnValue,
                                Sub("${BucketArn}/*", BucketArn=BucketArnValue)
                            ]
                        },
                        {
                            "Effect": "Allow",
                            "Action": [
                                "secretsmanager:GetSecretValue"
                            ],
                            "Resource": [
                                Sub("${SecretArn}*", SecretArn=DbSecretArnValue),
                                Sub("${SecretArn}*", SecretArn=MSKCredentialsArnValue),
                                Sub("arn:aws:secretsmanager:${AWS::Region}:${AWS::AccountId}:secret:${AWS::StackName}-*")
                            ]
                        },
                        {
                            "Effect": "Allow",
                            "Action": [
                                "kms:Decrypt",
                                "kms:DescribeKey"
                            ],
                            "Resource": MSKSecretsKeyArnValue
                        },
                        {
                            "Effect": "Allow",
                            "Action": [
                                "elasticfilesystem:ClientMount",
                                "elasticfilesystem:ClientWrite",
                                "elasticfilesystem:ClientRootAccess",
                                "elasticfilesystem:DescribeFileSystems",
                                "elasticfilesystem:DescribeMountTargets",
                                "elasticfilesystem:DescribeAccessPoints"
                            ],
                            "Resource": "*"
                        },
                        {
                            "Effect": "Allow",
                            "Action": [
                                "ssmmessages:CreateControlChannel",
                                "ssmmessages:CreateDataChannel",
                                "ssmmessages:OpenControlChannel",
                                "ssmmessages:OpenDataChannel"
                            ],
                            "Resource": "*"
                        },
                        {
                            "Effect": "Allow",
                            "Action": [
                                "logs:DescribeLogGroups",
                                "logs:CreateLogStream",
                                "logs:DescribeLogStreams",
                                "logs:PutLogEvents"
                            ],
                            "Resource": "*"
                        }
                    ]
                }
            )
        ]
    ))

    # -----------------------
    # Application Secrets
    # -----------------------


    # Collect services that need EFS access points
    services_needing_efs = {}
    for service_name, service_config in services.items():
        efs_mounts = service_config.get('efs_mounts', [])
        for mount in efs_mounts:
            access_point_name = mount['access_point_name']
            services_needing_efs[access_point_name] = mount

    # Create EFS access points
    access_points = {}
    for ap_name, mount_config in services_needing_efs.items():
        # Configure access point based on service type
        if ap_name == 'grafana':
            # Grafana access point: don't enforce POSIX user, let containers use their own users
            # Root-owned directory with group write permissions allows both root (init) and grafana user access
            posix_user = PosixUser(Gid="0", Uid="0")  # Use root for access point
            creation_info = CreationInfo(
                OwnerGid="0",     # root group owns the directory
                OwnerUid="0",     # root user owns the directory
                Permissions="755"  # owner rwx, group rx, others rx - allows access to multiple users
            )
        else:
            # Default for other services (currently none use EFS except Grafana)
            posix_user = PosixUser(Gid="0", Uid="0")
            creation_info = CreationInfo(
                OwnerGid="0",
                OwnerUid="0",
                Permissions="750"
            )

        access_points[ap_name] = t.add_resource(AccessPoint(
            f"EfsAccessPoint{ap_name.title()}",
            FileSystemId=EfsIdValue,
            PosixUser=posix_user,
            RootDirectory=RootDirectory(
                Path=mount_config['efs_path'],
                CreationInfo=creation_info
            ),
            AccessPointTags=Tags(Name=Sub("${AWS::StackName}-" + ap_name))
        ))

    # Keep track of target groups for ALB integration
    target_groups = {}

    # -----------------------
    # Create services
    # -----------------------
    for service_name, service_config in services.items():
        title_name = ''.join(word.capitalize() for word in service_name.replace('-', '_').split('_'))

        # Determine the condition for this service based on signal type
        signal_type = service_config.get('signal_type', 'common')
        condition_name = None
        if signal_type == 'logs':
            condition_name = "CreateLogsServices"
        elif signal_type == 'metrics':
            condition_name = "CreateMetricsServices"
        elif signal_type == 'traces':
            condition_name = "CreateTracesServices"

        # Create log group
        log_group_kwargs = {
            "LogGroupName": Sub(f"/ecs/{service_name}"),
            "RetentionInDays": 14
        }
        if condition_name:
            log_group_kwargs["Condition"] = condition_name
        log_group = t.add_resource(LogGroup(
            f"LogGroup{title_name}",
            **log_group_kwargs
        ))

        # Build volumes list
        volumes = [Volume(Name="scratch")]

        # Build environment variables
        base_env = [
            Environment(Name="OTEL_SERVICE_NAME", Value=service_name),
            Environment(Name="TMPDIR", Value="/scratch"),
            Environment(Name="HOME", Value="/scratch"),
            Environment(Name="STORAGE_PROFILE_FILE", Value="env:STORAGE_PROFILES_ENV"),
            Environment(Name="API_KEYS_FILE", Value="env:API_KEYS_ENV"),
            Environment(Name="SQS_QUEUE_URL", Value=Sub("https://sqs.${AWS::Region}.amazonaws.com/${AWS::AccountId}/lakerunner-ingest-queue")),
            Environment(Name="SQS_REGION", Value=Ref("AWS::Region")),
            Environment(Name="LRDB_HOST", Value=DbHostValue),
            Environment(Name="LRDB_PORT", Value=DbPortValue),
            Environment(Name="LRDB_DBNAME", Value="lakerunner"),
            Environment(Name="LRDB_USER", Value="lakerunner"),
            Environment(Name="LRDB_SSLMODE", Value="require"),
            Environment(Name="CONFIGDB_HOST", Value=DbHostValue),
            Environment(Name="CONFIGDB_PORT", Value=DbPortValue),
            Environment(Name="CONFIGDB_DBNAME", Value="lakerunner"),
            Environment(Name="CONFIGDB_USER", Value="lakerunner"),
            Environment(Name="CONFIGDB_SSLMODE", Value="require"),
            # MSK Kafka Configuration
            Environment(Name="LAKERUNNER_KAFKA_BROKERS", Value=Ref(MSKBrokers)),
            Environment(Name="LAKERUNNER_KAFKA_TLS_ENABLED", Value="true"),
            Environment(Name="LAKERUNNER_KAFKA_SASL_ENABLED", Value="true"),
            Environment(Name="LAKERUNNER_KAFKA_SASL_MECHANISM", Value="SCRAM-SHA-512"),
        ]


        # Add OTLP telemetry environment variables (conditionally)
        # Note: We add these individually with If() conditions since CloudFormation
        # doesn't support conditional lists in environment arrays
        base_env.extend([
            Environment(
                Name="OTEL_EXPORTER_OTLP_ENDPOINT",
                Value=If("EnableOtlp", Ref(OtelEndpoint), Ref("AWS::NoValue"))
            ),
            Environment(
                Name="ENABLE_OTLP_TELEMETRY",
                Value=If("EnableOtlp", "true", Ref("AWS::NoValue"))
            )
        ])

        # Add service-specific environment variables
        service_env = service_config.get('environment', {})
        for key, value in service_env.items():
            base_env.append(Environment(Name=key, Value=value))

        # Add QUERY_WORKER_CLUSTER_NAME for query-api service
        if service_name == "lakerunner-query-api":
            base_env.append(Environment(Name="QUERY_WORKER_CLUSTER_NAME", Value=ImportValue(ci_export("ClusterName"))))

        # Build secrets
        secrets = [
            EcsSecret(Name="STORAGE_PROFILES_ENV", ValueFrom=Sub("arn:aws:ssm:${AWS::Region}:${AWS::AccountId}:parameter/lakerunner/storage_profiles")),
            EcsSecret(Name="API_KEYS_ENV", ValueFrom=Sub("arn:aws:ssm:${AWS::Region}:${AWS::AccountId}:parameter/lakerunner/api_keys")),
            EcsSecret(Name="LRDB_PASSWORD", ValueFrom=Sub("${SecretArn}:password::", SecretArn=DbSecretArnValue)),
            EcsSecret(Name="CONFIGDB_PASSWORD", ValueFrom=Sub("${SecretArn}:password::", SecretArn=DbSecretArnValue)),
            # MSK SASL/SCRAM Credentials
            EcsSecret(Name="LAKERUNNER_KAFKA_SASL_USERNAME", ValueFrom=Sub("${SecretArn}:username::", SecretArn=MSKCredentialsArnValue)),
            EcsSecret(Name="LAKERUNNER_KAFKA_SASL_PASSWORD", ValueFrom=Sub("${SecretArn}:password::", SecretArn=MSKCredentialsArnValue))
        ]


        # Build mount points
        mount_points = [MountPoint(
            ContainerPath="/scratch",
            SourceVolume="scratch",
            ReadOnly=False
        )]

        # Add EFS mount points
        for mount in efs_mounts:
            ap_name = mount['access_point_name']
            mount_points.append(MountPoint(
                ContainerPath=mount['container_path'],
                SourceVolume=f"efs-{ap_name}",
                ReadOnly=False
            ))

        # Add bind mounts
        bind_mounts = service_config.get('bind_mounts', [])
        for mount in bind_mounts:
            mount_points.append(MountPoint(
                ContainerPath=mount['container_path'],
                SourceVolume=mount['source_volume'],
                ReadOnly=mount.get('read_only', False)
            ))

        # Build health check
        health_check_config = service_config.get('health_check', {})
        health_check = None
        if health_check_config:
            health_check = HealthCheck(
                Command=health_check_config.get('command', []),
                Interval=30,
                Timeout=5,
                Retries=3,
                StartPeriod=60
            )

        # Build port mappings
        port_mappings = []
        ingress = service_config.get('ingress')
        if ingress:
            # Use container_port if specified, otherwise use port
            container_port = ingress.get('container_port', ingress['port'])
            port_mappings.append(PortMapping(
                ContainerPort=container_port,
                Protocol="tcp"
            ))

        # All services now use Go services image
        container_image = Ref(GoServicesImage)

        # Get base container command from service config
        container_command = service_config.get('command', [])

        # Build container definitions
        container_definitions = []

        # Run all containers as root for now
        user_setting = "0"

        # Main application container
        container_kwargs = {
            "Name": "AppContainer",
            "Image": container_image,
            "Command": container_command,
            "Environment": base_env,
            "Secrets": secrets,
            "MountPoints": mount_points,
            "PortMappings": port_mappings,
            "HealthCheck": health_check,
            "LogConfiguration": LogConfiguration(
                LogDriver="awslogs",
                Options={
                    "awslogs-group": Ref(log_group),
                    "awslogs-region": Ref("AWS::Region"),
                    "awslogs-stream-prefix": service_name
                }
            )
        }

        # Always set User field to root
        container_kwargs["User"] = user_setting

        container = ContainerDefinition(**container_kwargs)
        container_definitions.append(container)

        # Select appropriate task role based on service type
        if service_name == 'lakerunner-query-api':
            task_role_arn = GetAtt(QueryApiTaskRole, "Arn")
        elif service_name == 'lakerunner-query-worker':
            task_role_arn = GetAtt(QueryWorkerTaskRole, "Arn")
        else:
            task_role_arn = GetAtt(TaskRole, "Arn")

        # Determine CPU and memory values
        # For lakerunner services with parameters, use the parameter; otherwise use YAML defaults
        if service_name in service_params:
            if 'cpu' in service_params[service_name]:
                # Query services have CPU parameter
                cpu_value = Ref(service_params[service_name]['cpu'])
            else:
                # Other services use YAML default for CPU
                cpu_value = str(service_config.get('cpu', 512))
            if 'memory' in service_params[service_name]:
                memory_value = Ref(service_params[service_name]['memory'])
            else:
                # Replicas-only services use YAML default for memory
                memory_value = str(service_config.get('memory_mib', 1024))
        else:
            # Non-configurable services use YAML defaults
            cpu_value = str(service_config.get('cpu', 512))
            memory_value = str(service_config.get('memory_mib', 1024))

        # Create task definition
        task_def_kwargs = {
            "Family": service_name + "-task",
            "Cpu": cpu_value,
            "Memory": memory_value,
            "NetworkMode": "awsvpc",
            "RequiresCompatibilities": ["FARGATE"],
            "ExecutionRoleArn": GetAtt(ExecutionRole, "Arn"),
            "TaskRoleArn": task_role_arn,
            "ContainerDefinitions": container_definitions,
            "Volumes": volumes,
            "RuntimePlatform": RuntimePlatform(
                CpuArchitecture="ARM64",
                OperatingSystemFamily="LINUX"
            )
        }
        if condition_name:
            task_def_kwargs["Condition"] = condition_name
        task_def = t.add_resource(TaskDefinition(
            f"TaskDef{title_name}",
            **task_def_kwargs
        ))

        # Create ECS service
        # For lakerunner services with parameters, use the parameter; otherwise use YAML defaults
        if service_name in service_params and 'replicas' in service_params[service_name]:
            desired_count = Ref(service_params[service_name]['replicas'])
        else:
            desired_count = str(service_config.get('replicas', 1))

        ecs_service_kwargs = {
            "ServiceName": service_name,
            "Cluster": ClusterArnValue,
            "TaskDefinition": Ref(task_def),
            "LaunchType": "FARGATE",
            "DesiredCount": desired_count,
            "NetworkConfiguration": NetworkConfiguration(
                AwsvpcConfiguration=AwsvpcConfiguration(
                    Subnets=PrivateSubnetsValue,
                    SecurityGroups=[TaskSecurityGroupIdValue]
                )
            ),
            "EnableExecuteCommand": True,
            "EnableECSManagedTags": True,
            "PropagateTags": "SERVICE",
            "Tags": Tags(
                Name=Sub(f"${{AWS::StackName}}-{service_name}"),
                ManagedBy="Lakerunner",
                Environment=Ref("AWS::StackName"),
                Component="Service"
            )
        }
        if condition_name:
            ecs_service_kwargs["Condition"] = condition_name
        ecs_service = t.add_resource(Service(
            f"Service{title_name}",
            **ecs_service_kwargs
        ))

        # Store target group mapping for ALB services (use local target groups)
        if ingress and ingress.get('attach_alb'):
            port = ingress['port']

            # Map to the appropriate target group created in this stack
            if port == 7101:
                target_group_arn = Ref(Tg7101)
            else:
                # For other ports, we'd need to add them to this stack
                target_group_arn = None

            if target_group_arn:
                target_groups[service_name] = {
                    'target_group_arn': target_group_arn,
                    'port': port,
                    'service': ecs_service
                }

        # Add LoadBalancer configuration for services with ALB ingress
        if ingress and ingress.get('attach_alb') and service_name in target_groups:
            # Use container_port if specified, otherwise use port
            container_port = ingress.get('container_port', ingress['port'])
            # LoadBalancers property - ALB is always created
            ecs_service.LoadBalancers = [EcsLoadBalancer(
                ContainerName="AppContainer",
                ContainerPort=container_port,
                TargetGroupArn=target_groups[service_name]['target_group_arn']
            )]
            # Add dependency on the corresponding listener to ensure target group is attached to ALB
            port = ingress['port']
            listener_name = f"Listener{port}"
            ecs_service.DependsOn = [listener_name]

        # -----------------------
        # Auto-Scaling for Worker Services (ingest, compact, rollup)
        # -----------------------
        if service_name in AUTOSCALING_SERVICES:
            # Determine the auto-scaling condition based on signal type
            autoscale_condition = None
            if signal_type == 'logs':
                autoscale_condition = "AutoScaleLogsServices"
            elif signal_type == 'metrics':
                autoscale_condition = "AutoScaleMetricsServices"
            elif signal_type == 'traces':
                autoscale_condition = "AutoScaleTracesServices"

            if autoscale_condition:
                # Create ScalableTarget - registers the ECS service with Application Auto Scaling
                scalable_target = t.add_resource(ScalableTarget(
                    f"ScalableTarget{title_name}",
                    Condition=autoscale_condition,
                    MaxCapacity=Ref(AutoScalingMaxReplicas),
                    MinCapacity=Ref(service_params[service_name]['replicas']),
                    ResourceId=Sub(
                        "service/${ClusterName}/" + service_name,
                        ClusterName=ImportValue(ci_export("ClusterName"))
                    ),
                    ScalableDimension="ecs:service:DesiredCount",
                    ServiceNamespace="ecs",
                    DependsOn=[f"Service{title_name}"]
                ))

                # Create CPU-based Target Tracking Scaling Policy
                t.add_resource(ScalingPolicy(
                    f"ScalingPolicy{title_name}",
                    Condition=autoscale_condition,
                    PolicyName=f"{service_name}-cpu-scaling",
                    PolicyType="TargetTrackingScaling",
                    ScalingTargetId=Ref(scalable_target),
                    TargetTrackingScalingPolicyConfiguration=TargetTrackingScalingPolicyConfiguration(
                        TargetValue=Ref(AutoScalingCPUTarget),
                        PredefinedMetricSpecification=PredefinedMetricSpecification(
                            PredefinedMetricType="ECSServiceAverageCPUUtilization"
                        ),
                        ScaleInCooldown=Ref(AutoScalingScaleInCooldown),
                        ScaleOutCooldown=Ref(AutoScalingScaleOutCooldown)
                    )
                ))

    # -----------------------
    # Outputs
    # -----------------------
    # ALB Outputs
    t.add_output(Output(
        "AlbDNS",
        Value=GetAtt(Alb, "DNSName"),
        Export=Export(name=Sub("${AWS::StackName}-AlbDNS"))
    ))
    t.add_output(Output(
        "AlbArn",
        Value=Ref(Alb),
        Export=Export(name=Sub("${AWS::StackName}-AlbArn"))
    ))
    t.add_output(Output(
        "Tg7101Arn",
        Value=Ref(Tg7101),
        Export=Export(name=Sub("${AWS::StackName}-Tg7101Arn"))
    ))

    # Service Outputs
    t.add_output(Output(
        "TaskRoleArn",
        Value=GetAtt(TaskRole, "Arn"),
        Export=Export(name=Sub("${AWS::StackName}-TaskRoleArn"))
    ))

    t.add_output(Output(
        "ExecutionRoleArn",
        Value=GetAtt(ExecutionRole, "Arn"),
        Export=Export(name=Sub("${AWS::StackName}-ExecutionRoleArn"))
    ))

    # Output service ARNs
    for service_name, service_config in services.items():
        title_name = ''.join(word.capitalize() for word in service_name.replace('-', '_').split('_'))
        # Determine the condition for this service
        signal_type = service_config.get('signal_type', 'common')
        condition_name = None
        if signal_type == 'logs':
            condition_name = "CreateLogsServices"
        elif signal_type == 'metrics':
            condition_name = "CreateMetricsServices"
        elif signal_type == 'traces':
            condition_name = "CreateTracesServices"

        output_kwargs = {
            "Value": Ref(f"Service{title_name}"),
            "Export": Export(name=Sub(f"${{AWS::StackName}}-{service_name}-ServiceArn"))
        }
        if condition_name:
            output_kwargs["Condition"] = condition_name
        t.add_output(Output(
            f"Service{title_name}Arn",
            **output_kwargs
        ))

    return t

if __name__ == "__main__":
    template = create_services_template()
    print(template.to_yaml())
