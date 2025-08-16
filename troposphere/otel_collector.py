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
from troposphere.efs import AccessPoint, PosixUser, RootDirectory, CreationInfo
from troposphere.awslambda import Function, Code, VPCConfig, FileSystemConfig
from troposphere.cloudformation import CustomResource

def load_otel_config(config_file="defaults.yaml"):
    """Load OTEL collector configuration from YAML file"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(script_dir, config_file)

    with open(config_path, 'r') as f:
        return yaml.safe_load(f)

def create_otel_collector_template():
    """Create CloudFormation template for OTEL collector stack"""

    t = Template()
    t.set_description("Lakerunner OTEL Collector: ECS service with ALB for telemetry ingestion")

    # Load configuration
    config = load_otel_config()
    otel_services = config.get('otel_services', {})
    images = config.get('images', {})

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

    # Customer configuration
    OrganizationId = t.add_parameter(Parameter(
        "OrganizationId", Type="String",
        Default="12340000-0000-4000-8000-000000000000",
        Description="Organization ID for OTEL data routing"
    ))

    CollectorName = t.add_parameter(Parameter(
        "CollectorName", Type="String",
        Default="lakerunner",
        Description="Collector name for OTEL data routing"
    ))

    ForceReplaceConfig = t.add_parameter(Parameter(
        "ForceReplaceConfig", Type="String",
        Default="false",
        AllowedValues=["true", "false"],
        Description="Whether to force replace the OTEL config file during stack creation (default: false)"
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
                    "Label": {"default": "Customer Configuration"},
                    "Parameters": ["OrganizationId", "CollectorName", "ForceReplaceConfig"]
                }
            ],
            "ParameterLabels": {
                "CommonInfraStackName": {"default": "Common Infra Stack Name"},
                "LoadBalancerType": {"default": "Load Balancer Type"},
                "OtelCollectorImage": {"default": "OTEL Collector Image"},
                "OrganizationId": {"default": "Organization ID"},
                "CollectorName": {"default": "Collector Name"},
                "ForceReplaceConfig": {"default": "Force Replace Config"}
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
        HealthCheckPath="/",
        HealthCheckProtocol="HTTP",
        HealthCheckPort="13133",
        HealthCheckIntervalSeconds=30,
        HealthCheckTimeoutSeconds=5,
        HealthyThresholdCount=2,
        UnhealthyThresholdCount=3,
        Matcher=Matcher(HttpCode="200"),
        TargetGroupAttributes=[
            TargetGroupAttribute(Key="deregistration_delay.timeout_seconds", Value="30")
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
        HealthCheckPath="/",
        HealthCheckProtocol="HTTP",
        HealthCheckPort="13133",
        HealthCheckIntervalSeconds=30,
        HealthCheckTimeoutSeconds=5,
        HealthyThresholdCount=2,
        UnhealthyThresholdCount=3,
        Matcher=Matcher(HttpCode="200"),
        TargetGroupAttributes=[
            TargetGroupAttribute(Key="deregistration_delay.timeout_seconds", Value="30")
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
    # EFS Access Point for OTEL Config
    # -----------------------
    OtelConfigAccessPoint = t.add_resource(AccessPoint(
        "OtelConfigAccessPoint",
        FileSystemId=EfsIdValue,
        PosixUser=PosixUser(
            Gid="0",
            Uid="0"
        ),
        RootDirectory=RootDirectory(
            Path="/otel-config",
            CreationInfo=CreationInfo(
                OwnerGid="0",
                OwnerUid="0",
                Permissions="755"
            )
        ),
        AccessPointTags=Tags(Name=Sub("${AWS::StackName}-otel-config"))
    ))

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
                        }
                    ]
                }
            )
        ]
    ))

    # -----------------------
    # Lambda Function for Config Upload
    # -----------------------
    # Lambda execution role
    LambdaExecutionRole = t.add_resource(Role(
        "LambdaExecutionRole",
        RoleName=Sub("${AWS::StackName}-lambda-role"),
        AssumeRolePolicyDocument={
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Principal": {"Service": "lambda.amazonaws.com"},
                "Action": "sts:AssumeRole"
            }]
        },
        ManagedPolicyArns=[
            "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
        ],
        Policies=[
            Policy(
                PolicyName="EFSAccess",
                PolicyDocument={
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": [
                                "elasticfilesystem:ClientMount",
                                "elasticfilesystem:ClientWrite",
                                "elasticfilesystem:DescribeFileSystems",
                                "elasticfilesystem:DescribeMountTargets",
                                "elasticfilesystem:DescribeAccessPoints"
                            ],
                            "Resource": "*"
                        }
                    ]
                }
            )
        ]
    ))

    # Read the OTEL config file content
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_file_path = os.path.join(script_dir, "otel-config.yaml")
    with open(config_file_path, 'r') as f:
        otel_config_content = f.read()

    # Create a base64-encoded version of the config to avoid string escaping issues
    import base64
    config_b64 = base64.b64encode(otel_config_content.encode()).decode()
    
    lambda_code = f'''
import json
import boto3
import os
import urllib3
import base64

def send_response(event, context, response_status, response_data=None, physical_resource_id=None):
    if response_data is None:
        response_data = {{}}
    
    response_url = event['ResponseURL']
    response_body = {{
        'Status': response_status,
        'Reason': f'See CloudWatch Log Stream: {{context.log_stream_name}}',
        'PhysicalResourceId': physical_resource_id or context.log_stream_name,
        'StackId': event['StackId'],
        'RequestId': event['RequestId'],
        'LogicalResourceId': event['LogicalResourceId'],
        'Data': response_data
    }}
    
    json_response = json.dumps(response_body)
    headers = {{'Content-Type': 'application/json'}}
    
    http = urllib3.PoolManager()
    response = http.request('PUT', response_url, body=json_response, headers=headers)
    print(f"Response status: {{response.status}}")

def lambda_handler(event, context):
    print(f"Event: {{json.dumps(event)}}")
    
    try:
        request_type = event.get('RequestType')
        if request_type == 'Delete':
            send_response(event, context, 'SUCCESS')
            return
        
        force_replace = event['ResourceProperties'].get('ForceReplace', 'false').lower() == 'true'
        
        # Decode the base64 config content
        config_content = base64.b64decode('{config_b64}').decode()
        
        # For Lambda in VPC with EFS, the file system is mounted at /mnt/efs
        # This requires the Lambda to have EFS file system configured
        config_file_path = '/mnt/efs/config.yaml'
        
        # Check if file exists and force_replace setting
        file_exists = os.path.exists(config_file_path)
        should_write = force_replace or not file_exists
        
        if should_write:
            # Ensure directory exists
            os.makedirs(os.path.dirname(config_file_path), exist_ok=True)
            
            # Write the config file
            with open(config_file_path, 'w') as f:
                f.write(config_content)
            
            print(f"Config file written to {{config_file_path}} (force_replace={{force_replace}}, existed={{file_exists}})")
            
            response_data = {{
                'ConfigPath': config_file_path,
                'Action': 'replaced' if file_exists else 'created',
                'ForceReplace': force_replace
            }}
        else:
            print(f"Config file already exists at {{config_file_path}} and force_replace is false")
            response_data = {{
                'ConfigPath': config_file_path,
                'Action': 'skipped',
                'ForceReplace': force_replace
            }}
        
        send_response(event, context, 'SUCCESS', response_data)
        
    except Exception as e:
        print(f"Error: {{str(e)}}")
        import traceback
        traceback.print_exc()
        send_response(event, context, 'FAILED')
        raise
'''

    ConfigUploaderFunction = t.add_resource(Function(
        "ConfigUploaderFunction",
        FunctionName=Sub("${AWS::StackName}-config-uploader"),
        Runtime="python3.9",
        Handler="index.lambda_handler",
        Role=GetAtt(LambdaExecutionRole, "Arn"),
        Code=Code(ZipFile=lambda_code),
        Timeout=300,
        VpcConfig=VPCConfig(
            SecurityGroupIds=[Ref(TaskSecurityGroup)],
            SubnetIds=PrivateSubnetsValue
        ),
        FileSystemConfigs=[
            FileSystemConfig(
                Arn=GetAtt(OtelConfigAccessPoint, "Arn"),
                LocalMountPath="/mnt/efs"
            )
        ]
    ))

    # Custom resource to trigger the Lambda
    ConfigUploader = t.add_resource(CustomResource(
        "ConfigUploader",
        ServiceToken=GetAtt(ConfigUploaderFunction, "Arn"),
        EfsId=EfsIdValue,
        AccessPointId=Ref(OtelConfigAccessPoint),
        ForceReplace=Ref(ForceReplaceConfig)
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
        Environment(Name="ORGANIZATION_ID", Value=Ref(OrganizationId)),
        Environment(Name="COLLECTOR_NAME", Value=Ref(CollectorName))
    ]

    # Add service-specific environment variables
    service_env = service_config.get('environment', {})
    for key, value in service_env.items():
        environment.append(Environment(Name=key, Value=value))

    # Health check
    health_check = HealthCheck(
        Command=["CMD-SHELL", "curl -f http://localhost:13133/ || exit 1"],
        Interval=30,
        Timeout=5,
        Retries=3,
        StartPeriod=60
    )

    # Port mappings
    port_mappings = [
        PortMapping(ContainerPort=4317, Protocol="tcp"),
        PortMapping(ContainerPort=4318, Protocol="tcp"),
        PortMapping(ContainerPort=13133, Protocol="tcp")
    ]

    # Mount points
    mount_points = [
        MountPoint(
            ContainerPath="/scratch",
            SourceVolume="scratch",
            ReadOnly=False
        ),
        MountPoint(
            ContainerPath="/etc/otel",
            SourceVolume="otel-config",
            ReadOnly=True
        )
    ]

    # Container definition
    container = ContainerDefinition(
        Name="OtelCollector",
        Image=Ref(OtelCollectorImage),
        Command=service_config.get('command', []),
        Environment=environment,
        MountPoints=mount_points,
        PortMappings=port_mappings,
        HealthCheck=health_check,
        User="0",
        LogConfiguration=LogConfiguration(
            LogDriver="awslogs",
            Options={
                "awslogs-group": Ref(OtelLogGroup),
                "awslogs-region": Ref("AWS::Region"),
                "awslogs-stream-prefix": "otel-gateway"
            }
        )
    )

    # Volumes
    volumes = [
        Volume(Name="scratch"),
        Volume(
            Name="otel-config",
            EFSVolumeConfiguration=EFSVolumeConfiguration(
                FilesystemId=EfsIdValue,
                TransitEncryption="ENABLED",
                AuthorizationConfig=AuthorizationConfig(
                    AccessPointId=Ref(OtelConfigAccessPoint),
                    IAM="ENABLED"
                )
            )
        )
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
        EnableExecuteCommand=True
    ))

    # -----------------------
    # Outputs
    # -----------------------
    t.add_output(Output(
        "LoadBalancerDNS",
        Description="DNS name of the OTEL collector load balancer",
        Value=GetAtt(ApplicationLoadBalancer, "DNSName"),
        Export=Export(Sub("${AWS::StackName}-LoadBalancerDNS"))
    ))

    t.add_output(Output(
        "GrpcEndpoint",
        Description="OTEL gRPC endpoint URL",
        Value=Sub("http://${LoadBalancerDNS}:4317", LoadBalancerDNS=GetAtt(ApplicationLoadBalancer, "DNSName")),
        Export=Export(Sub("${AWS::StackName}-GrpcEndpoint"))
    ))

    t.add_output(Output(
        "HttpEndpoint",
        Description="OTEL HTTP endpoint URL",
        Value=Sub("http://${LoadBalancerDNS}:4318", LoadBalancerDNS=GetAtt(ApplicationLoadBalancer, "DNSName")),
        Export=Export(Sub("${AWS::StackName}-HttpEndpoint"))
    ))

    t.add_output(Output(
        "ServiceArn",
        Description="ARN of the OTEL gateway service",
        Value=Ref(EcsService),
        Export=Export(Sub("${AWS::StackName}-ServiceArn"))
    ))

    t.add_output(Output(
        "OtelConfigAccessPointId",
        Description="EFS Access Point ID for OTEL configuration. Upload config.yaml to this location.",
        Value=Ref(OtelConfigAccessPoint),
        Export=Export(Sub("${AWS::StackName}-OtelConfigAccessPointId"))
    ))

    return t

if __name__ == "__main__":
    template = create_otel_collector_template()
    print(template.to_yaml())