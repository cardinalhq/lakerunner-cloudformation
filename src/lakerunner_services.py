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
    Select, Not, Tags, Split
)
from troposphere.ecs import (
    Service, TaskDefinition, ContainerDefinition, Environment,
    LogConfiguration, Secret as EcsSecret, Volume, MountPoint,
    HealthCheck, PortMapping, RuntimePlatform, NetworkConfiguration, AwsvpcConfiguration,
    LoadBalancer as EcsLoadBalancer, EFSVolumeConfiguration, AuthorizationConfig
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

    # -----------------------
    # Parameters (infrastructure inputs)
    # -----------------------
    ClusterArn = t.add_parameter(Parameter(
        "ClusterArn", Type="String",
        Description="REQUIRED: ARN of the ECS cluster hosting the services.",
    ))
    DbSecretArn = t.add_parameter(Parameter(
        "DbSecretArn", Type="String",
        Description="REQUIRED: ARN of the database credentials secret.",
    ))
    DbHost = t.add_parameter(Parameter(
        "DbHost", Type="String",
        Description="REQUIRED: Database endpoint hostname.",
    ))
    DbPort = t.add_parameter(Parameter(
        "DbPort", Type="String",
        Description="REQUIRED: Database port number.",
    ))
    TaskSecurityGroupId = t.add_parameter(Parameter(
        "TaskSecurityGroupId", Type="AWS::EC2::SecurityGroup::Id",
        Description="REQUIRED: Security group used by ECS tasks.",
    ))
    VpcId = t.add_parameter(Parameter(
        "VpcId", Type="AWS::EC2::VPC::Id",
        Description="REQUIRED: VPC where services run.",
    ))
    PrivateSubnets = t.add_parameter(Parameter(
        "PrivateSubnets", Type="List<AWS::EC2::Subnet::Id>",
        Description="REQUIRED: Private subnet IDs for tasks.",
    ))
    PublicSubnets = t.add_parameter(Parameter(
        "PublicSubnets", Type="CommaDelimitedList",
        Default="",
        Description="OPTIONAL: Public subnets for ALB (required if AlbScheme is internet-facing).",
    ))
    BucketArn = t.add_parameter(Parameter(
        "BucketArn", Type="String",
        Description="REQUIRED: ARN of the ingest bucket.",
    ))
    EfsId = t.add_parameter(Parameter(
        "EfsId", Type="String", Default="",
        Description="OPTIONAL: EFS file system ID for services requiring EFS.",
    ))

    # Container image overrides for air-gapped deployments
    GoServicesImage = t.add_parameter(Parameter(
        "GoServicesImage", Type="String",
        Default=images.get('go_services', 'public.ecr.aws/cardinalhq.io/lakerunner:latest'),
        Description="Container image for Go services (pubsub, ingest, compact, etc.)",
    ))

    QueryApiImage = t.add_parameter(Parameter(
        "QueryApiImage", Type="String",
        Default=images.get('query_api', 'public.ecr.aws/cardinalhq.io/lakerunner/query-api:latest'),
        Description="Container image for query-api service",
    ))

    QueryWorkerImage = t.add_parameter(Parameter(
        "QueryWorkerImage", Type="String",
        Default=images.get('query_worker', 'public.ecr.aws/cardinalhq.io/lakerunner/query-worker:latest'),
        Description="Container image for query-worker service",
    ))

    # OTLP Telemetry configuration
    OtelEndpoint = t.add_parameter(Parameter(
        "OtelEndpoint", Type="String",
        Default="",
        Description="OPTIONAL: OTEL collector HTTP endpoint URL (e.g., http://collector-dns:4318). Leave blank to disable OTLP telemetry export."
    ))

    # API keys configuration
    ApiKeysOverride = t.add_parameter(Parameter(
        "ApiKeysOverride",
        Type="String",
        Default="",
        Description="OPTIONAL: Custom API keys configuration in YAML format. Leave blank to use defaults."
    ))

    # Storage stack name for parameter references
    StorageStackName = t.add_parameter(Parameter(
        "StorageStackName",
        Type="String",
        Description="REQUIRED: Name of the storage stack for parameter references."
    ))

    # ALB Configuration parameters

    AlbScheme = t.add_parameter(Parameter(
        "AlbScheme",
        Type="String",
        AllowedValues=["internet-facing", "internal"],
        Default="internal",
        Description="Load balancer scheme: 'internet-facing' for external access or 'internal' for internal access only."
    ))

    # Parameter groups for console
    t.set_metadata({
        "AWS::CloudFormation::Interface": {
            "ParameterGroups": [
                {
                    "Label": {"default": "Infrastructure"},
                    "Parameters": ["ClusterArn", "DbSecretArn", "DbHost", "DbPort",
                                   "TaskSecurityGroupId", "VpcId", "PrivateSubnets",
                                   "PublicSubnets", "BucketArn", "EfsId", "AlbScheme", 
                                   "StorageStackName"]
                },
                {
                    "Label": {"default": "Container Images"},
                    "Parameters": ["GoServicesImage", "QueryApiImage", "QueryWorkerImage"]
                },
                {
                    "Label": {"default": "Configuration"},
                    "Parameters": ["OtelEndpoint", "ApiKeysOverride"]
                }
            ],
            "ParameterLabels": {
                "ClusterArn": {"default": "ECS Cluster ARN"},
                "DbSecretArn": {"default": "DB Secret ARN"},
                "DbHost": {"default": "DB Host"},
                "DbPort": {"default": "DB Port"},
                "TaskSecurityGroupId": {"default": "Task Security Group"},
                "VpcId": {"default": "VPC ID"},
                "PrivateSubnets": {"default": "Private Subnets"},
                "PublicSubnets": {"default": "Public Subnets"},
                "BucketArn": {"default": "Bucket ARN"},
                "EfsId": {"default": "EFS File System ID"},
                "AlbScheme": {"default": "ALB Scheme"},
                "GoServicesImage": {"default": "Go Services Image"},
                "QueryApiImage": {"default": "Query API Image"},
                "QueryWorkerImage": {"default": "Query Worker Image"},
                "OtelEndpoint": {"default": "OTEL Collector Endpoint"},
                "ApiKeysOverride": {"default": "API Keys Override"},
                "StorageStackName": {"default": "Storage Stack Name"}
            }
        }
    })

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

    # Resolved values
    ClusterArnValue = Ref(ClusterArn)
    DbSecretArnValue = Ref(DbSecretArn)
    DbHostValue = Ref(DbHost)
    DbPortValue = Ref(DbPort)
    EfsIdValue = Ref(EfsId)
    TaskSecurityGroupIdValue = Ref(TaskSecurityGroupId)
    VpcIdValue = Ref(VpcId)
    PrivateSubnetsValue = Ref(PrivateSubnets)
    BucketArnValue = Ref(BucketArn)
    PublicSubnetsValue = Ref(PublicSubnets)

    # Conditions
    t.add_condition("EnableOtlp", Not(Equals(Ref(OtelEndpoint), "")))
    t.add_condition("IsInternetFacing", Equals(Ref(AlbScheme), "internet-facing"))
    t.add_condition("HasApiKeysOverride", Not(Equals(Ref(ApiKeysOverride), "")))


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
    t.add_resource(SecurityGroupIngress(
        "TaskFromAlb7101",
        GroupId=TaskSecurityGroupIdValue,
        IpProtocol="tcp",
        FromPort=7101, ToPort=7101,
        SourceSecurityGroupId=Ref(AlbSG),
        Description="ALB to tasks 7101",
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
        Port=7101, Protocol="HTTP",
        VpcId=VpcIdValue,
        TargetType="ip",
        HealthCheckPath="/ready",
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
                                Sub("arn:aws:secretsmanager:${AWS::Region}:${AWS::AccountId}:secret:${AWS::StackName}-*")
                            ]
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
                                Sub("arn:aws:secretsmanager:${AWS::Region}:${AWS::AccountId}:secret:${AWS::StackName}-*")
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
                                Sub("arn:aws:secretsmanager:${AWS::Region}:${AWS::AccountId}:secret:${AWS::StackName}-*")
                            ]
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
                                Sub("arn:aws:secretsmanager:${AWS::Region}:${AWS::AccountId}:secret:${AWS::StackName}-*")
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
    # Application Secrets
    # -----------------------
    # API keys configuration parameter
    api_keys_default = yaml.dump(config.get('api_keys', {}), default_flow_style=False)
    t.add_resource(SSMParameter(
        "ApiKeysParam",
        Name=Sub("/lakerunner/${AWS::StackName}/api_keys"),
        Type="String",
        Value=If("HasApiKeysOverride", Ref(ApiKeysOverride), api_keys_default),
        Description="API keys configuration for Lakerunner services",
    ))


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

        # Create log group
        log_group = t.add_resource(LogGroup(
            f"LogGroup{title_name}",
            LogGroupName=Sub(f"/ecs/{service_name}"),
            RetentionInDays=14
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
        ]

        # Add service-specific discovery environment variables
        if service_name == 'lakerunner-query-api':
            # Query API needs to discover query worker instances
            base_env.extend([
                Environment(Name="ECS_WORKER_SERVICE_NAME", Value="lakerunner-query-worker"),
                Environment(Name="QUERY_WORKER_CLUSTER_NAME", Value=Select(1, Split("/", ClusterArnValue)))
            ])
        elif service_name == 'lakerunner-query-worker':
            # Query workers will receive API registrations (no discovery env vars needed)
            pass

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

        # Build secrets
        secrets = [
            EcsSecret(Name="STORAGE_PROFILES_ENV", ValueFrom=Sub("arn:aws:ssm:${AWS::Region}:${AWS::AccountId}:parameter/lakerunner/${StorageStackName}/storage_profiles", StorageStackName=Ref(StorageStackName))),
            EcsSecret(Name="API_KEYS_ENV", ValueFrom=Sub("arn:aws:ssm:${AWS::Region}:${AWS::AccountId}:parameter/lakerunner/${AWS::StackName}/api_keys")),
            EcsSecret(Name="LRDB_PASSWORD", ValueFrom=Sub("${SecretArn}:password::", SecretArn=DbSecretArnValue)),
            EcsSecret(Name="CONFIGDB_PASSWORD", ValueFrom=Sub("${SecretArn}:password::", SecretArn=DbSecretArnValue))
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
            port_mappings.append(PortMapping(
                ContainerPort=ingress['port'],
                Protocol="tcp"
            ))

        # Select container image based on service type
        if service_name == 'lakerunner-query-api':
            container_image = Ref(QueryApiImage)
        elif service_name == 'lakerunner-query-worker':
            container_image = Ref(QueryWorkerImage)
        else:
            # All other services use Go services image (pubsub, ingest, compact, etc.)
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

        # Create task definition
        task_def = t.add_resource(TaskDefinition(
            f"TaskDef{title_name}",
            Family=service_name + "-task",
            Cpu=str(service_config.get('cpu', 512)),
            Memory=str(service_config.get('memory_mib', 1024)),
            NetworkMode="awsvpc",
            RequiresCompatibilities=["FARGATE"],
            ExecutionRoleArn=GetAtt(ExecutionRole, "Arn"),
            TaskRoleArn=task_role_arn,
            ContainerDefinitions=container_definitions,
            Volumes=volumes,
            RuntimePlatform=RuntimePlatform(
                CpuArchitecture="ARM64",
                OperatingSystemFamily="LINUX"
            )
        ))

        # Create ECS service
        desired_count = str(service_config.get('replicas', 1))

        ecs_service = t.add_resource(Service(
            f"Service{title_name}",
            ServiceName=service_name,
            Cluster=ClusterArnValue,
            TaskDefinition=Ref(task_def),
            LaunchType="FARGATE",
            DesiredCount=desired_count,
            NetworkConfiguration=NetworkConfiguration(
                AwsvpcConfiguration=AwsvpcConfiguration(
                    Subnets=PrivateSubnetsValue,
                    SecurityGroups=[TaskSecurityGroupIdValue]
                )
            ),
            EnableExecuteCommand=True
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
            # LoadBalancers property - ALB is always created
            ecs_service.LoadBalancers = [EcsLoadBalancer(
                ContainerName="AppContainer",
                ContainerPort=ingress['port'],
                TargetGroupArn=target_groups[service_name]['target_group_arn']
            )]
            # Add dependency on the corresponding listener to ensure target group is attached to ALB
            port = ingress['port']
            listener_name = f"Listener{port}"
            ecs_service.DependsOn = [listener_name]

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

    # Surface the provided EFS filesystem ID so cfn-lint recognizes it is used
    t.add_output(Output("EfsId", Value=EfsIdValue))

    # Output service ARNs
    for service_name, _ in services.items():
        title_name = ''.join(word.capitalize() for word in service_name.replace('-', '_').split('_'))
        t.add_output(Output(
            f"Service{title_name}Arn",
            Value=Ref(f"Service{title_name}"),
            Export=Export(name=Sub(f"${{AWS::StackName}}-{service_name}-ServiceArn"))
        ))

    return t

if __name__ == "__main__":
    template = create_services_template()
    print(template.to_yaml())
