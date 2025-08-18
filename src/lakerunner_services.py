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
        Description="Container image for Go services (pubsub, ingest, compact, etc.)"
    ))

    QueryApiImage = t.add_parameter(Parameter(
        "QueryApiImage", Type="String",
        Default=images.get('query_api', 'public.ecr.aws/cardinalhq.io/lakerunner/query-api:latest'),
        Description="Container image for query-api service"
    ))

    QueryWorkerImage = t.add_parameter(Parameter(
        "QueryWorkerImage", Type="String",
        Default=images.get('query_worker', 'public.ecr.aws/cardinalhq.io/lakerunner/query-worker:latest'),
        Description="Container image for query-worker service"
    ))

    GrafanaImage = t.add_parameter(Parameter(
        "GrafanaImage", Type="String",
        Default=images.get('grafana', 'grafana/grafana:latest'),
        Description="Container image for Grafana service"
    ))

    # OTLP Telemetry configuration
    OtelEndpoint = t.add_parameter(Parameter(
        "OtelEndpoint", Type="String",
        Default="",
        Description="OPTIONAL: OTEL collector HTTP endpoint URL (e.g., http://collector-dns:4318). Leave blank to disable OTLP telemetry export."
    ))

    # Grafana Reset Token
    GrafanaResetToken = t.add_parameter(Parameter(
        "GrafanaResetToken", Type="String",
        Default="",
        Description="OPTIONAL: Change this value to reset Grafana data (wipe EFS volume). Leave blank for normal operation. Use any string (e.g., timestamp) to trigger reset."
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
                    "Parameters": ["CommonInfraStackName", "AlbScheme"]
                },
                {
                    "Label": {"default": "Container Images"},
                    "Parameters": ["GoServicesImage", "QueryApiImage", "QueryWorkerImage", "GrafanaImage"]
                },
                {
                    "Label": {"default": "Telemetry"},
                    "Parameters": ["OtelEndpoint"]
                },
                {
                    "Label": {"default": "Grafana Configuration"},
                    "Parameters": ["GrafanaResetToken"]
                }
            ],
            "ParameterLabels": {
                "CommonInfraStackName": {"default": "Common Infra Stack Name"},
                "AlbScheme": {"default": "ALB Scheme"},
                "GoServicesImage": {"default": "Go Services Image"},
                "QueryApiImage": {"default": "Query API Image"},
                "QueryWorkerImage": {"default": "Query Worker Image"},
                "GrafanaImage": {"default": "Grafana Image"},
                "OtelEndpoint": {"default": "OTEL Collector Endpoint"},
                "GrafanaResetToken": {"default": "Grafana Reset Token"}
            }
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
        "Alb3000Open",
        GroupId=Ref(AlbSG),
        IpProtocol="tcp",
        FromPort=3000, ToPort=3000,
        CidrIp="0.0.0.0/0",
        Description="HTTP 3000",
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
    for port in (3000, 7101):
        t.add_resource(SecurityGroupIngress(
            f"TaskFromAlb{port}",
            GroupId=TaskSecurityGroupIdValue,
            IpProtocol="tcp",
            FromPort=port, ToPort=port,
            SourceSecurityGroupId=Ref(AlbSG),
            Description=f"ALB to tasks {port}",
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
        HealthCheckIntervalSeconds=30,
        HealthCheckTimeoutSeconds=5,
        HealthyThresholdCount=2,
        UnhealthyThresholdCount=3,
        Matcher=Matcher(HttpCode="200"),
        TargetGroupAttributes=[
            TargetGroupAttribute(Key="stickiness.enabled", Value="false"),
            TargetGroupAttribute(Key="deregistration_delay.timeout_seconds", Value="30")
        ]
    ))
    Tg3000 = t.add_resource(TargetGroup(
        "Tg3000",
        Port=3000, Protocol="HTTP",
        VpcId=VpcIdValue,
        TargetType="ip",
        HealthCheckPath="/api/health",
        HealthCheckProtocol="HTTP",
        HealthCheckIntervalSeconds=30,
        HealthCheckTimeoutSeconds=5,
        HealthyThresholdCount=2,
        UnhealthyThresholdCount=3,
        Matcher=Matcher(HttpCode="200"),
        TargetGroupAttributes=[
            TargetGroupAttribute(Key="stickiness.enabled", Value="false"),
            TargetGroupAttribute(Key="deregistration_delay.timeout_seconds", Value="30")
        ]
    ))

    t.add_resource(Listener(
        "Listener7101",
        LoadBalancerArn=Ref(Alb),
        Port="7101",
        Protocol="HTTP",
        DefaultActions=[AlbAction(Type="forward", TargetGroupArn=Ref(Tg7101))]
    ))
    t.add_resource(Listener(
        "Listener3000",
        LoadBalancerArn=Ref(Alb),
        Port="3000",
        Protocol="HTTP",
        DefaultActions=[AlbAction(Type="forward", TargetGroupArn=Ref(Tg3000))]
    ))

    # Create Grafana datasource configuration with ALB DNS
    # Get the first API key from the config for the datasource
    config = load_service_config()
    api_keys = config.get('api_keys', [])
    default_api_key = ""
    if api_keys and api_keys[0].get('keys'):
        default_api_key = api_keys[0]['keys'][0]

    grafana_datasource_config = {
        "apiVersion": 1,
        "datasources": [
            {
                "name": "Cardinal",
                "type": "cardinalhq-lakerunner-datasource",
                "access": "proxy",
                "isDefault": True,
                "editable": True,
                "jsonData": {
                    "customPath": "http://${ALB_DNS}:7101"
                },
                "secureJsonData": {
                    "apiKey": default_api_key
                }
            }
        ]
    }

    # Create SSM Parameter with ALB DNS substitution
    grafana_datasource_param = t.add_resource(SSMParameter(
        "GrafanaDatasourceConfig",
        Name=Sub("${AWS::StackName}-grafana-datasource-config"),
        Type="String",
        Value=Sub(yaml.dump(grafana_datasource_config), ALB_DNS=GetAtt(Alb, "DNSName")),
        Description="Grafana datasource configuration for Cardinal plugin"
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
    # Task Role (shared by all services)
    # -----------------------
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
                                "ecs:ListServices",
                                "ecs:DescribeServices",
                                "ecs:UpdateService",
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

    # -----------------------
    # Application Secrets
    # -----------------------
    # TOKEN_HMAC256_KEY secret for query API and worker
    token_secret = t.add_resource(Secret(
        "TokenSecret",
        Name=Sub("${AWS::StackName}-token-key"),
        Description="HMAC256 key for token signing/verification",
        GenerateSecretString=GenerateSecretString(
            SecretStringTemplate='{}',
            GenerateStringKey='token_hmac256_key',
            ExcludeCharacters=' "\\@/',
            PasswordLength=64
        )
    ))

    # Grafana admin password secret
    grafana_secret = t.add_resource(Secret(
        "GrafanaSecret",
        Name=Sub("${AWS::StackName}-grafana"),
        Description="Grafana admin password",
        GenerateSecretString=GenerateSecretString(
            SecretStringTemplate='{"username": "lakerunner"}',
            GenerateStringKey='password',
            ExcludeCharacters=' !"#$%&\'()*+,./:;<=>?@[\\]^`{|}~',
            PasswordLength=32
        )
    ))

    # Output the Grafana admin password secret ARN so users can retrieve it
    t.add_output(Output(
        "GrafanaAdminSecretArn",
        Description="ARN of the Grafana admin password secret. Use AWS CLI to retrieve: aws secretsmanager get-secret-value --secret-id <ARN>",
        Value=Ref(grafana_secret),
        Export=Export(Sub("${AWS::StackName}-GrafanaAdminSecretArn"))
    ))

    # Grafana datasource configuration will be created after ALB is defined

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
        safe_name = service_name.replace('-', '').replace('_', '')
        title_name = ''.join(word.capitalize() for word in service_name.replace('-', '_').split('_'))

        # Create log group
        log_group = t.add_resource(LogGroup(
            f"LogGroup{title_name}",
            LogGroupName=Sub(f"/ecs/{service_name}"),
            RetentionInDays=14
        ))

        # Build volumes list
        volumes = [Volume(Name="scratch")]

        # Add EFS volumes
        efs_mounts = service_config.get('efs_mounts', [])
        for mount in efs_mounts:
            ap_name = mount['access_point_name']
            if ap_name in access_points:
                volumes.append(Volume(
                    Name=f"efs-{ap_name}",
                    EFSVolumeConfiguration=EFSVolumeConfiguration(
                        FilesystemId=EfsIdValue,
                        TransitEncryption="ENABLED",
                        AuthorizationConfig=AuthorizationConfig(
                            AccessPointId=Ref(access_points[ap_name]),
                            IAM="ENABLED"
                        )
                    )
                ))

        # Build environment variables
        base_env = [
            Environment(Name="BUMP_REVISION", Value="1"),
            Environment(Name="OTEL_SERVICE_NAME", Value=service_name),
            Environment(Name="TMPDIR", Value="/scratch"),
            Environment(Name="HOME", Value="/scratch"),
            Environment(Name="STORAGE_PROFILE_FILE", Value="env:STORAGE_PROFILES_ENV"),
            Environment(Name="API_KEYS_FILE", Value="env:API_KEYS_ENV"),
            Environment(Name="SQS_QUEUE_URL", Value=Sub("https://sqs.${AWS::Region}.amazonaws.com/${AWS::AccountId}/lakerunner-ingest-queue")),
            Environment(Name="SQS_REGION", Value=Ref("AWS::Region")),
            Environment(Name="ECS_WORKER_CLUSTER_NAME", Value=Select(1, Split("/", ClusterArnValue))),
            Environment(Name="ECS_WORKER_SERVICE_NAME", Value="lakerunner-query-worker"),
            Environment(Name="LRDB_HOST", Value=DbHostValue),
            Environment(Name="LRDB_PORT", Value=DbPortValue),
            Environment(Name="LRDB_DBNAME", Value="lakerunner"),
            Environment(Name="LRDB_USER", Value="lakerunner"),
            Environment(Name="LRDB_SSLMODE", Value="require")
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

        # Add service-specific environment variables (excluding sensitive ones)
        service_env = service_config.get('environment', {})
        sensitive_keys = {'TOKEN_HMAC256_KEY', 'GF_SECURITY_ADMIN_PASSWORD'}
        for key, value in service_env.items():
            if key not in sensitive_keys:
                # Special handling for GF_RESET_TOKEN to use parameter instead of defaults
                if key == 'GF_RESET_TOKEN':
                    base_env.append(Environment(Name=key, Value=Ref(GrafanaResetToken)))
                else:
                    base_env.append(Environment(Name=key, Value=value))

        # Build secrets
        secrets = [
            EcsSecret(Name="STORAGE_PROFILES_ENV", ValueFrom=Sub("arn:aws:ssm:${AWS::Region}:${AWS::AccountId}:parameter/lakerunner/storage_profiles")),
            EcsSecret(Name="API_KEYS_ENV", ValueFrom=Sub("arn:aws:ssm:${AWS::Region}:${AWS::AccountId}:parameter/lakerunner/api_keys")),
            EcsSecret(Name="LRDB_PASSWORD", ValueFrom=Sub("${SecretArn}:password::", SecretArn=DbSecretArnValue))
        ]

        # Add service-specific secrets for sensitive environment variables
        if 'TOKEN_HMAC256_KEY' in service_env:
            secrets.append(EcsSecret(
                Name="TOKEN_HMAC256_KEY",
                ValueFrom=Sub("${SecretArn}:token_hmac256_key::", SecretArn=Ref(token_secret))
            ))

        if 'GF_SECURITY_ADMIN_PASSWORD' in service_env:
            secrets.append(EcsSecret(
                Name="GF_SECURITY_ADMIN_PASSWORD",
                ValueFrom=Sub("${SecretArn}:password::", SecretArn=Ref(grafana_secret))
            ))

        # Add Grafana datasource configuration for Grafana service
        if service_name == 'grafana':
            secrets.append(EcsSecret(
                Name="GRAFANA_DATASOURCE_CONFIG",
                ValueFrom=Ref(grafana_datasource_param)
            ))

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
        elif service_name == 'grafana':
            container_image = Ref(GrafanaImage)
        else:
            # All other services use Go services image (pubsub, ingest, compact, etc.)
            container_image = Ref(GoServicesImage)

        # Get base container command from service config
        container_command = service_config.get('command', [])

        # Build container definitions - for Grafana we need init container + main container
        container_definitions = []

        if service_name == 'grafana':
            # Create init container for Grafana setup (datasource provisioning + reset logic)
            init_container = ContainerDefinition(
                Name="GrafanaInit",
                Image="public.ecr.aws/docker/library/alpine:latest",
                Essential=False,
                Command=["/bin/sh", "-c"],
                Environment=[
                    Environment(Name="PROVISIONING_DIR", Value="${GF_PATHS_PROVISIONING:-/etc/grafana/provisioning}"),
                    Environment(Name="RESET_TOKEN", Value=Ref("GrafanaResetToken")),
                    Environment(Name="ALB_DNS_URL", Value=Sub("http://${AlbDns}", AlbDns=GetAtt("Alb", "DNSName")))
                ],
                Secrets=[
                    EcsSecret(
                        Name="GRAFANA_DATASOURCE_CONFIG",
                        ValueFrom=Sub("${AWS::StackName}-grafana-datasource-config")
                    )
                ],
                MountPoints=mount_points,  # Same EFS mounts as main container
                User="0",
                LogConfiguration=LogConfiguration(
                    LogDriver="awslogs",
                    Options={
                        "awslogs-group": Ref(log_group),
                        "awslogs-region": Ref("AWS::Region"),
                        "awslogs-stream-prefix": service_name + "-init"
                    }
                )
            )

            # Multi-line shell script for init container
            init_script = '''
# Install curl for health checks and other tools
apk add --no-cache curl

# Set up provisioning directory
PROVISIONING_DIR="${GF_PATHS_PROVISIONING:-/etc/grafana/provisioning}"
DATASOURCES_DIR="$PROVISIONING_DIR/datasources"
RESET_TOKEN_FILE="/var/lib/grafana/.grafana_reset_token"

echo "Provisioning directory: $PROVISIONING_DIR"
echo "Datasources directory: $DATASOURCES_DIR"
echo "Reset token file: $RESET_TOKEN_FILE"

# Create provisioning directories
mkdir -p "$DATASOURCES_DIR"

# Ensure Grafana user (472) can access the data directory
# The init container runs as root, so it can set proper ownership
chown -R 472:472 /var/lib/grafana || true
chmod -R 755 /var/lib/grafana || true

# Handle reset token logic
if [ -n "$RESET_TOKEN" ] && [ "$RESET_TOKEN" != "" ]; then
    echo "Reset token provided: $RESET_TOKEN"

    # Check if token file exists and compare
    if [ -f "$RESET_TOKEN_FILE" ]; then
        STORED_TOKEN=$(cat "$RESET_TOKEN_FILE")
        if [ "$STORED_TOKEN" != "$RESET_TOKEN" ]; then
            echo "Reset token changed from '$STORED_TOKEN' to '$RESET_TOKEN' - wiping Grafana data"
            # Remove all Grafana data
            find /var/lib/grafana -mindepth 1 -delete || true
            # Create new token file
            echo "$RESET_TOKEN" > "$RESET_TOKEN_FILE"
        else
            echo "Reset token unchanged - no reset needed"
        fi
    else
        echo "First time with reset token - storing: $RESET_TOKEN"
        echo "$RESET_TOKEN" > "$RESET_TOKEN_FILE"
    fi
else
    echo "No reset token provided - skipping reset logic"
fi

# Write datasource configuration
echo "Writing Grafana datasource configuration..."
# The datasource config comes from SSM parameter as YAML, just write it directly
cat > "$DATASOURCES_DIR/cardinal.yaml" << DATASOURCE_EOF
$GRAFANA_DATASOURCE_CONFIG
DATASOURCE_EOF

echo "Grafana initialization complete"
'''

            # Set the command arguments with the script
            init_container.Command = ["/bin/sh", "-c", init_script]
            container_definitions.append(init_container)

        # Determine user based on service type
        if service_name == 'grafana':
            # Use Grafana container default user (don't set User field)
            user_setting = None
        elif service_name in ['lakerunner-query-api', 'lakerunner-query-worker']:
            # Use user 2000 for query services
            user_setting = "2000"
        else:
            # Use distroless nonroot userid for Go services
            user_setting = "65532"

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

        # Only add User field if we have a specific user setting
        if user_setting is not None:
            container_kwargs["User"] = user_setting

        # For Grafana, add dependency on init container to ensure proper startup order
        if service_name == 'grafana':
            container_kwargs["DependsOn"] = [{"ContainerName": "GrafanaInit", "Condition": "SUCCESS"}]

        container = ContainerDefinition(**container_kwargs)
        container_definitions.append(container)

        # Create task definition
        task_def = t.add_resource(TaskDefinition(
            f"TaskDef{title_name}",
            Family=service_name + "-task",
            Cpu=str(service_config.get('cpu', 512)),
            Memory=str(service_config.get('memory_mib', 1024)),
            NetworkMode="awsvpc",
            RequiresCompatibilities=["FARGATE"],
            ExecutionRoleArn=GetAtt(ExecutionRole, "Arn"),
            TaskRoleArn=GetAtt(TaskRole, "Arn"),
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
            elif port == 3000:
                target_group_arn = Ref(Tg3000)
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
    t.add_output(Output(
        "Tg3000Arn",
        Value=Ref(Tg3000),
        Export=Export(name=Sub("${AWS::StackName}-Tg3000Arn"))
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
