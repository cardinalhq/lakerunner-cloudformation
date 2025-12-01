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
from troposphere.iam import Role, Policy
from troposphere.elasticloadbalancingv2 import (
    LoadBalancer, TargetGroup, TargetGroupAttribute, Listener, Matcher,
    Action as AlbAction, ListenerRule, Condition as AlbCondition
)
from troposphere.logs import LogGroup
from troposphere.ec2 import SecurityGroup, SecurityGroupRule
# EFS no longer needed for simplified config approach

def load_otel_config(config_file="otel-stack-defaults.yaml"):
    """Load OTEL collector configuration from YAML file"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(script_dir, "..", config_file)

    with open(config_path, 'r') as f:
        return yaml.safe_load(f)

def load_default_otel_yaml():
    """Load the default OTEL configuration YAML as a string"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(script_dir, "..", "otel-collector-config.yaml")

    with open(config_path, 'r') as f:
        return f.read().strip()

def load_lakerunner_config():
    """Load lakerunner stack defaults to extract organization_id and collector_name"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(script_dir, "..", "lakerunner-stack-defaults.yaml")
    
    with open(config_path, 'r') as f:
        config = yaml.safe_load(f)
    
    # Extract organization_id from api_keys section
    organization_id = config['api_keys'][0]['organization_id']
    
    # Extract collector_name from storage_profiles section  
    collector_name = config['storage_profiles'][0]['collector_name']
    
    return organization_id, collector_name

def create_otel_collector_template():
    """Create CloudFormation template for OTEL collector stack"""

    t = Template()
    t.set_description("Lakerunner OTEL Collector: ECS service with ALB for telemetry ingestion")

    # Load configuration
    config = load_otel_config()
    otel_services = config.get('otel_services', {})
    images = config.get('images', {})
    
    # Load organization and collector info from lakerunner stack defaults
    organization_id, collector_name = load_lakerunner_config()

    # -----------------------
    # Parameters
    # -----------------------
    CommonInfraStackName = t.add_parameter(Parameter(
        "CommonInfraStackName", Type="String",
        Description="REQUIRED: Name of the CommonInfra stack to import infrastructure values from."
    ))

    LoadBalancerType = t.add_parameter(Parameter(
        "LoadBalancerType", Type="String",
        Default="internal",
        AllowedValues=["internal", "internet-facing"],
        Description="Whether to create an internal or external ALB for the OTEL collector."
    ))

    # Container image override for air-gapped deployments
    OtelCollectorImage = t.add_parameter(Parameter(
        "OtelCollectorImage", Type="String",
        Default=images.get('otel_collector', 'public.ecr.aws/cardinalhq.io/cardinalhq-otel-collector:latest'),
        Description="Container image for OTEL collector service"
    ))

    # OTEL Configuration (optional override)
    OtelConfigYaml = t.add_parameter(Parameter(
        "OtelConfigYaml", Type="String",
        Default="",
        Description="OPTIONAL: Custom OTEL collector configuration in YAML format. Leave blank to use default configuration."
    ))



    # Parameter groups for console
    t.set_metadata({
        "AWS::CloudFormation::Interface": {
            "ParameterGroups": [
                {
                    "Label": {"default": "Infrastructure"},
                    "Parameters": ["CommonInfraStackName", "LoadBalancerType"]
                },
                {
                    "Label": {"default": "Container Images"},
                    "Parameters": ["OtelCollectorImage"]
                },
                {
                    "Label": {"default": "Configuration"},
                    "Parameters": ["OtelConfigYaml"]
                }
            ],
            "ParameterLabels": {
                "CommonInfraStackName": {"default": "Common Infra Stack Name"},
                "LoadBalancerType": {"default": "Load Balancer Type"},
                "OtelCollectorImage": {"default": "OTEL Collector Image"},
                "OtelConfigYaml": {"default": "Custom OTEL Configuration (YAML)"}
            }
        }
    })

    # Helper function for imports
    def ci_export(suffix):
        return Sub("${CommonInfraStackName}-%s" % suffix, CommonInfraStackName=Ref(CommonInfraStackName))

    # Resolved values (import from CommonInfra)
    ClusterArnValue = ImportValue(ci_export("ClusterArn"))
    VpcIdValue = ImportValue(ci_export("VpcId"))
    PrivateSubnetsValue = Split(",", ImportValue(ci_export("PrivateSubnets")))
    PublicSubnetsValue = Split(",", ImportValue(ci_export("PublicSubnets")))
    BucketNameValue = ImportValue(ci_export("BucketName"))
    EfsIdValue = ImportValue(ci_export("EfsId"))

    # Conditions
    t.add_condition("IsInternal", Equals(Ref(LoadBalancerType), "internal"))
    t.add_condition("HasCustomConfig", Not(Equals(Ref(OtelConfigYaml), "")))

    # -----------------------
    # Security Groups
    # -----------------------
    # ALB Security Group
    AlbSecurityGroup = t.add_resource(SecurityGroup(
        "AlbSecurityGroup",
        GroupDescription="Security group for OTEL collector ALB",
        VpcId=VpcIdValue,
        SecurityGroupIngress=[
            SecurityGroupRule(
                IpProtocol="tcp",
                FromPort=4317,
                ToPort=4317,
                CidrIp="0.0.0.0/0",
                Description="OTEL gRPC receiver"
            ),
            SecurityGroupRule(
                IpProtocol="tcp",
                FromPort=4318,
                ToPort=4318,
                CidrIp="0.0.0.0/0",
                Description="OTEL HTTP receiver"
            )
        ],
        Tags=Tags(Name=Sub("${AWS::StackName}-alb-sg"))
    ))

    # Task Security Group
    TaskSecurityGroup = t.add_resource(SecurityGroup(
        "TaskSecurityGroup",
        GroupDescription="Security group for OTEL collector tasks",
        VpcId=VpcIdValue,
        SecurityGroupIngress=[
            SecurityGroupRule(
                IpProtocol="tcp",
                FromPort=4317,
                ToPort=4317,
                SourceSecurityGroupId=Ref(AlbSecurityGroup),
                Description="OTEL gRPC from ALB"
            ),
            SecurityGroupRule(
                IpProtocol="tcp",
                FromPort=4318,
                ToPort=4318,
                SourceSecurityGroupId=Ref(AlbSecurityGroup),
                Description="OTEL HTTP from ALB"
            ),
            SecurityGroupRule(
                IpProtocol="tcp",
                FromPort=13133,
                ToPort=13133,
                SourceSecurityGroupId=Ref(AlbSecurityGroup),
                Description="OTEL health check from ALB"
            )
        ],
        Tags=Tags(Name=Sub("${AWS::StackName}-task-sg"))
    ))

    # -----------------------
    # Load Balancer
    # -----------------------
    ApplicationLoadBalancer = t.add_resource(LoadBalancer(
        "ApplicationLoadBalancer",
        Name=Sub("${AWS::StackName}-alb"),
        Scheme=If("IsInternal", "internal", "internet-facing"),
        Type="application",
        IpAddressType="ipv4",
        Subnets=If("IsInternal", PrivateSubnetsValue, PublicSubnetsValue),
        SecurityGroups=[Ref(AlbSecurityGroup)],
        Tags=Tags(Name=Sub("${AWS::StackName}-alb"))
    ))

    # Target Groups
    OtelGrpcTargetGroup = t.add_resource(TargetGroup(
        "OtelGrpcTargetGroup",
        Name=Sub("${AWS::StackName}-otel-grpc"),
        Port=4317,
        Protocol="HTTP",
        VpcId=VpcIdValue,
        TargetType="ip",
        HealthCheckPath="/healthz",
        HealthCheckProtocol="HTTP",
        HealthCheckPort="13133",
        HealthCheckIntervalSeconds=5,
        HealthCheckTimeoutSeconds=2,
        HealthyThresholdCount=2,
        UnhealthyThresholdCount=2,
        Matcher=Matcher(HttpCode="200"),
        TargetGroupAttributes=[
            TargetGroupAttribute(Key="deregistration_delay.timeout_seconds", Value="5")
        ],
        Tags=Tags(Name=Sub("${AWS::StackName}-otel-grpc-tg"))
    ))

    OtelHttpTargetGroup = t.add_resource(TargetGroup(
        "OtelHttpTargetGroup",
        Name=Sub("${AWS::StackName}-otel-http"),
        Port=4318,
        Protocol="HTTP",
        VpcId=VpcIdValue,
        TargetType="ip",
        HealthCheckPath="/healthz",
        HealthCheckProtocol="HTTP",
        HealthCheckPort="13133",
        HealthCheckIntervalSeconds=5,
        HealthCheckTimeoutSeconds=2,
        HealthyThresholdCount=2,
        UnhealthyThresholdCount=2,
        Matcher=Matcher(HttpCode="200"),
        TargetGroupAttributes=[
            TargetGroupAttribute(Key="deregistration_delay.timeout_seconds", Value="5")
        ],
        Tags=Tags(Name=Sub("${AWS::StackName}-otel-http-tg"))
    ))

    # Listeners
    GrpcListener = t.add_resource(Listener(
        "GrpcListener",
        LoadBalancerArn=Ref(ApplicationLoadBalancer),
        Port=4317,
        Protocol="HTTP",
        DefaultActions=[AlbAction(
            Type="forward",
            TargetGroupArn=Ref(OtelGrpcTargetGroup)
        )]
    ))

    HttpListener = t.add_resource(Listener(
        "HttpListener",
        LoadBalancerArn=Ref(ApplicationLoadBalancer),
        Port=4318,
        Protocol="HTTP",
        DefaultActions=[AlbAction(
            Type="forward",
            TargetGroupArn=Ref(OtelHttpTargetGroup)
        )]
    ))

    # -----------------------
    # EFS Access Point no longer needed - using environment variable config

    # -----------------------
    # IAM Roles
    # -----------------------
    # Task Execution Role
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
                                Sub("arn:aws:ssm:${AWS::Region}:${AWS::AccountId}:parameter/otel/*")
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
                        }
                    ]
                }
            )
        ]
    ))

    # Task Role
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
                PolicyName="S3Access",
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
                                Sub("arn:aws:s3:::${BucketName}", BucketName=BucketNameValue),
                                Sub("arn:aws:s3:::${BucketName}/*", BucketName=BucketNameValue)
                            ]
                        }
                    ]
                }
            )
        ]
    ))


    # -----------------------
    # ECS Service and Task Definition
    # -----------------------
    service_config = otel_services.get('otel-gateway', {})

    # Log Group
    OtelLogGroup = t.add_resource(LogGroup(
        "LogGroupOtelGateway",
        LogGroupName=Sub("/ecs/otel-gateway"),
        RetentionInDays=14
    ))

    # Environment variables
    environment = [
        Environment(Name="OTEL_SERVICE_NAME", Value="otel-gateway"),
        Environment(Name="AWS_S3_BUCKET", Value=BucketNameValue),
        Environment(Name="AWS_REGION", Value=Ref("AWS::Region")),
        Environment(Name="ORG", Value=organization_id),
        Environment(Name="COLLECTOR", Value=collector_name),
        Environment(
            Name="CHQ_COLLECTOR_CONFIG_YAML",
            Value=If(
                "HasCustomConfig",
                Ref(OtelConfigYaml),
                load_default_otel_yaml()
            )
        )
    ]

    # Add service-specific environment variables  
    service_env = service_config.get('environment', {})
    for key, value in service_env.items():
        environment.append(Environment(Name=key, Value=value))

    # Health check - temporarily disabled for debugging
    # health_check = HealthCheck(
    #     Command=["CMD-SHELL", "curl -f http://localhost:13133/healthz || exit 1"],
    #     Interval=30,
    #     Timeout=5,
    #     Retries=3,
    #     StartPeriod=60
    # )
    health_check = None

    # Port mappings
    port_mappings = [
        PortMapping(ContainerPort=4317, Protocol="tcp"),
        PortMapping(ContainerPort=4318, Protocol="tcp"),
        PortMapping(ContainerPort=13133, Protocol="tcp")
    ]

    # Mount points (only scratch directory needed)
    mount_points = [
        MountPoint(
            ContainerPath="/scratch",
            SourceVolume="scratch",
            ReadOnly=False
        )
    ]

    # Container definition
    container_args = {
        "Name": "OtelCollector",
        "Image": Ref(OtelCollectorImage),
        "Command": ["/app/bin/run-with-env-config"],
        "Environment": environment,
        "MountPoints": mount_points,
        "PortMappings": port_mappings,
        "User": "0",
        "LogConfiguration": LogConfiguration(
            LogDriver="awslogs",
            Options={
                "awslogs-group": Ref(OtelLogGroup),
                "awslogs-region": Ref("AWS::Region"),
                "awslogs-stream-prefix": "otel-gateway"
            }
        )
    }
    
    # Only add HealthCheck if it's defined
    if health_check is not None:
        container_args["HealthCheck"] = health_check
    
    container = ContainerDefinition(**container_args)

    # Volumes (only scratch directory needed)
    volumes = [
        Volume(Name="scratch")
    ]

    # Task definition
    OtelTaskDefinition = t.add_resource(TaskDefinition(
        "TaskDefOtelGateway",
        Family="otel-gateway-task",
        Cpu=str(service_config.get('cpu', 1024)),
        Memory=str(service_config.get('memory_mib', 2048)),
        NetworkMode="awsvpc",
        RequiresCompatibilities=["FARGATE"],
        ExecutionRoleArn=GetAtt(ExecutionRole, "Arn"),
        TaskRoleArn=GetAtt(TaskRole, "Arn"),
        ContainerDefinitions=[container],
        Volumes=volumes,
        RuntimePlatform=RuntimePlatform(
            CpuArchitecture="ARM64",
            OperatingSystemFamily="LINUX"
        )
    ))

    # ECS Service
    EcsService = t.add_resource(Service(
        "ServiceOtelGateway",
        ServiceName="otel-gateway",
        Cluster=ClusterArnValue,
        TaskDefinition=Ref(OtelTaskDefinition),
        LaunchType="FARGATE",
        DesiredCount=str(service_config.get('replicas', 1)),
        NetworkConfiguration=NetworkConfiguration(
            AwsvpcConfiguration=AwsvpcConfiguration(
                Subnets=PrivateSubnetsValue,
                SecurityGroups=[Ref(TaskSecurityGroup)],
                AssignPublicIp="DISABLED"
            )
        ),
        LoadBalancers=[
            EcsLoadBalancer(
                ContainerName="OtelCollector",
                ContainerPort=4317,
                TargetGroupArn=Ref(OtelGrpcTargetGroup)
            ),
            EcsLoadBalancer(
                ContainerName="OtelCollector",
                ContainerPort=4318,
                TargetGroupArn=Ref(OtelHttpTargetGroup)
            )
        ],
        EnableExecuteCommand=True,
        EnableECSManagedTags=True,
        PropagateTags="SERVICE",
        DependsOn=[GrpcListener, HttpListener],
        Tags=Tags(
            Name=Sub("${AWS::StackName}-otel-gateway"),
            ManagedBy="Lakerunner",
            Environment=Ref("AWS::StackName"),
            Component="Service"
        )
    ))

    # -----------------------
    # Outputs
    # -----------------------
    t.add_output(Output(
        "OtelGrpcEndpoint",
        Description="OTEL gRPC endpoint",
        Value=Sub("http://${LoadBalancerDNS}:4317", LoadBalancerDNS=GetAtt(ApplicationLoadBalancer, "DNSName")),
        Export=Export(Sub("${AWS::StackName}-GrpcEndpoint"))
    ))

    t.add_output(Output(
        "OtelHttpEndpoint",
        Description="OTEL HTTP endpoint",
        Value=Sub("http://${LoadBalancerDNS}:4318", LoadBalancerDNS=GetAtt(ApplicationLoadBalancer, "DNSName")),
        Export=Export(Sub("${AWS::StackName}-HttpEndpoint"))
    ))

    t.add_output(Output(
        "ServiceArn",
        Description="ARN of the OTEL gateway service",
        Value=Ref(EcsService),
        Export=Export(Sub("${AWS::StackName}-ServiceArn"))
    ))

    # EFS access point output no longer needed - using environment variable config

    return t

if __name__ == "__main__":
    template = create_otel_collector_template()
    print(template.to_yaml())