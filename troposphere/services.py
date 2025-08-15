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
from troposphere.elasticloadbalancingv2 import TargetGroup, TargetGroupAttribute, Listener, Matcher
from troposphere.elasticloadbalancingv2 import Action as AlbAction
from troposphere.efs import AccessPoint, PosixUser, RootDirectory, CreationInfo
from troposphere.logs import LogGroup
from troposphere.secretsmanager import Secret, GenerateSecretString

def load_service_config(config_file="defaults.yaml"):
    """Load service configuration from YAML file"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(script_dir, config_file)

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

    # Parameter groups for console
    t.set_metadata({
        "AWS::CloudFormation::Interface": {
            "ParameterGroups": [
                {
                    "Label": {"default": "Infrastructure"},
                    "Parameters": ["CommonInfraStackName"]
                },
                {
                    "Label": {"default": "Container Images"},
                    "Parameters": ["GoServicesImage", "QueryApiImage", "QueryWorkerImage", "GrafanaImage"]
                }
            ],
            "ParameterLabels": {
                "CommonInfraStackName": {"default": "Common Infra Stack Name"},
                "GoServicesImage": {"default": "Go Services Image"},
                "QueryApiImage": {"default": "Query API Image"},
                "QueryWorkerImage": {"default": "Query Worker Image"},
                "GrafanaImage": {"default": "Grafana Image"}
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
                                Sub("arn:aws:ssm:${AWS::Region}:${AWS::AccountId}:parameter/lakerunner/*")
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
            SecretStringTemplate='{"username": "admin"}',
            GenerateStringKey='password',
            ExcludeCharacters=' "\\@/',
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
        access_points[ap_name] = t.add_resource(AccessPoint(
            f"EfsAccessPoint{ap_name.title()}",
            FileSystemId=EfsIdValue,
            PosixUser=PosixUser(
                Gid="0",
                Uid="0"
            ),
            RootDirectory=RootDirectory(
                Path=mount_config['efs_path'],
                CreationInfo=CreationInfo(
                    OwnerGid="0",
                    OwnerUid="0",
                    Permissions="750"
                )
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

        # Add service-specific environment variables (excluding sensitive ones)
        service_env = service_config.get('environment', {})
        sensitive_keys = {'TOKEN_HMAC256_KEY', 'GF_SECURITY_ADMIN_PASSWORD'}
        for key, value in service_env.items():
            if key not in sensitive_keys:
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

        container = ContainerDefinition(
            Name="AppContainer",
            Image=container_image,
            Command=service_config.get('command', []),
            Environment=base_env,
            Secrets=secrets,
            MountPoints=mount_points,
            PortMappings=port_mappings,
            HealthCheck=health_check,
            User="0",
            LogConfiguration=LogConfiguration(
                LogDriver="awslogs",
                Options={
                    "awslogs-group": Ref(log_group),
                    "awslogs-region": Ref("AWS::Region"),
                    "awslogs-stream-prefix": service_name
                }
            )
        )

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
            ContainerDefinitions=[container],
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

        # Store target group mapping for ALB services (use common stack target groups)
        if ingress and ingress.get('attach_alb'):
            port = ingress['port']

            # Map to the appropriate target group created in CommonInfra stack
            if port == 7101:
                target_group_arn = ImportValue(ci_export("Tg7101Arn"))
            elif port == 3000:
                target_group_arn = ImportValue(ci_export("Tg3000Arn"))
            else:
                # For other ports, we'd need to add them to the common stack
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

    # -----------------------
    # ALB integration - use target groups created in common stack
    # -----------------------
    # Note: Listeners and target groups are created in the CommonInfra stack
    # We just need to attach our services to the existing target groups

    # -----------------------
    # Outputs
    # -----------------------
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
