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
    ImportValue, Split, Tags
)
from troposphere.ecs import (
    Service, TaskDefinition, ContainerDefinition, Environment,
    LogConfiguration, Secret as EcsSecret, Volume, MountPoint,
    HealthCheck, PortMapping, RuntimePlatform, NetworkConfiguration, AwsvpcConfiguration,
    LoadBalancer as EcsLoadBalancer
)
from troposphere.iam import Role, Policy
from troposphere.elasticloadbalancingv2 import LoadBalancer, TargetGroup, TargetGroupAttribute, Listener, Matcher
from troposphere.elasticloadbalancingv2 import Action as AlbAction
from troposphere.ssm import Parameter as SSMParameter
from troposphere.ec2 import SecurityGroup, SecurityGroupIngress
from troposphere.logs import LogGroup
from troposphere.secretsmanager import Secret, GenerateSecretString

def load_grafana_config(config_file="lakerunner-grafana-defaults.yaml"):
    """Load Grafana configuration from YAML file"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(script_dir, "..", config_file)

    with open(config_path, 'r') as f:
        return yaml.safe_load(f)

def create_grafana_template():
    """Create CloudFormation template for Grafana stack"""

    t = Template()
    t.set_description("Lakerunner Grafana Service: Grafana service with ALB, PostgreSQL storage, and datasource configuration")

    # Load Grafana configuration
    config = load_grafana_config()
    grafana_config = config.get('grafana', {})
    images = config.get('images', {})
    api_keys = config.get('api_keys', [])

    # -----------------------
    # Parameters
    # -----------------------
    CommonInfraStackName = t.add_parameter(Parameter(
        "CommonInfraStackName", Type="String",
        Description="REQUIRED: Name of the CommonInfra stack to import infrastructure values from."
    ))

    QueryApiUrl = t.add_parameter(Parameter(
        "QueryApiUrl", Type="String",
        Description="REQUIRED: Full URL to the Query API endpoint (e.g., http://alb-dns-name.region.elb.amazonaws.com:7101)"
    ))


    # Container image overrides for air-gapped deployments
    GrafanaImage = t.add_parameter(Parameter(
        "GrafanaImage", Type="String",
        Default=images.get('grafana', 'grafana/grafana:latest'),
        Description="Container image for Grafana service"
    ))

    GrafanaInitImage = t.add_parameter(Parameter(
        "GrafanaInitImage", Type="String",
        Default=images.get('grafana_init', 'lakerunner-grafana-init:latest'),
        Description="Container image for Grafana init container (datasource provisioning and database setup)"
    ))

    # Grafana Reset Token Configuration
    GrafanaResetToken = t.add_parameter(Parameter(
        "GrafanaResetToken", Type="String",
        Default="change-to-reset-grafana",
        Description="Reset token for Grafana data wipe. Changing this value will wipe all Grafana data on next deployment."
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
                    "Parameters": ["CommonInfraStackName", "QueryApiUrl", "AlbScheme"]
                },
                {
                    "Label": {"default": "Container Images"},
                    "Parameters": ["GrafanaImage", "GrafanaInitImage", "GrafanaResetToken"]
                }
            ],
            "ParameterLabels": {
                "CommonInfraStackName": {"default": "Common Infra Stack Name"},
                "QueryApiUrl": {"default": "Query API URL"},
                "AlbScheme": {"default": "ALB Scheme"},
                "GrafanaImage": {"default": "Grafana Image"},
                "GrafanaInitImage": {"default": "Grafana Init Image"},
                "GrafanaResetToken": {"default": "Grafana Reset Token"},
            }
        }
    })

    # Helper function for CommonInfra imports
    def ci_export(suffix):
        return Sub("${CommonInfraStackName}-%s" % suffix, CommonInfraStackName=Ref(CommonInfraStackName))

    # Import values from other stacks
    ClusterArnValue = ImportValue(ci_export("ClusterArn"))
    TaskSecurityGroupIdValue = ImportValue(ci_export("TaskSGId"))
    VpcIdValue = ImportValue(ci_export("VpcId"))
    PrivateSubnetsValue = Split(",", ImportValue(ci_export("PrivateSubnets")))

    # Import PublicSubnets - CommonInfra always exports this, but may be empty string if not provided
    PublicSubnetsImport = ImportValue(ci_export("PublicSubnets"))
    PublicSubnetsValue = Split(",", PublicSubnetsImport)

    # Conditions
    t.add_condition("IsInternetFacing", Equals(Ref(AlbScheme), "internet-facing"))

    # -----------------------
    # ALB Security Group
    # -----------------------
    AlbSG = t.add_resource(SecurityGroup(
        "GrafanaAlbSecurityGroup",
        GroupDescription="Security group for Grafana ALB",
        VpcId=VpcIdValue,
        SecurityGroupEgress=[{
            "IpProtocol": "-1",
            "CidrIp": "0.0.0.0/0",
            "Description": "Allow all outbound"
        }]
    ))

    t.add_resource(SecurityGroupIngress(
        "GrafanaAlb3000Open",
        GroupId=Ref(AlbSG),
        IpProtocol="tcp",
        FromPort=3000, ToPort=3000,
        CidrIp="0.0.0.0/0",
        Description="HTTP 3000 for Grafana",
    ))

    # Add ingress rule to task security group to allow ALB traffic
    t.add_resource(SecurityGroupIngress(
        "TaskFromGrafanaAlb3000",
        GroupId=TaskSecurityGroupIdValue,
        IpProtocol="tcp",
        FromPort=3000, ToPort=3000,
        SourceSecurityGroupId=Ref(AlbSG),
        Description="Grafana ALB to tasks 3000",
    ))

    # -----------------------
    # Grafana ALB + listeners + target groups
    # -----------------------
    GrafanaAlb = t.add_resource(LoadBalancer(
        "GrafanaAlb",
        Scheme=Ref(AlbScheme),
        SecurityGroups=[Ref(AlbSG)],
        Subnets=If(
            "IsInternetFacing",
            PublicSubnetsValue,
            PrivateSubnetsValue
        ),
        Type="application",
    ))

    GrafanaTg = t.add_resource(TargetGroup(
        "GrafanaTg",
        Name=If("IsInternetFacing", Sub("${AWS::StackName}-ext"), Sub("${AWS::StackName}-int")),
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
        "GrafanaListener",
        LoadBalancerArn=Ref(GrafanaAlb),
        Port="3000",
        Protocol="HTTP",
        DefaultActions=[AlbAction(Type="forward", TargetGroupArn=Ref(GrafanaTg))]
    ))

    # Create Grafana datasource configuration with Query API ALB DNS
    # Get the first API key from the config for the datasource
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
                    "customPath": "${QUERY_API_URL}"
                },
                "secureJsonData": {
                    "apiKey": default_api_key
                }
            }
        ]
    }

    # Create SSM Parameter with Query API URL substitution
    grafana_datasource_param = t.add_resource(SSMParameter(
        "GrafanaDatasourceConfig",
        Name=Sub("${AWS::StackName}-grafana-datasource-config"),
        Type="String",
        Value=Sub(yaml.dump(grafana_datasource_config), QUERY_API_URL=Ref(QueryApiUrl)),
        Description="Grafana datasource configuration for Cardinal plugin"
    ))

    # -----------------------
    # Task Execution Role (for Grafana)
    # -----------------------
    ExecutionRole = t.add_resource(Role(
        "GrafanaExecRole",
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
                                Sub("arn:aws:secretsmanager:${AWS::Region}:${AWS::AccountId}:secret:${AWS::StackName}-*"),
                                Sub("${DbSecretArn}*",
                                    DbSecretArn=ImportValue(Sub("${CommonInfraStackName}-DbSecretArn",
                                                               CommonInfraStackName=Ref(CommonInfraStackName))))
                            ]
                        }
                    ]
                }
            )
        ]
    ))

    # -----------------------
    # Task Role (for Grafana)
    # -----------------------
    TaskRole = t.add_resource(Role(
        "GrafanaTaskRole",
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
                PolicyName="LogAccess",
                PolicyDocument={
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": [
                                "logs:CreateLogStream",
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
    # Grafana admin password secret
    # -----------------------
    grafana_secret = t.add_resource(Secret(
        "GrafanaSecret",
        Name=Sub("${AWS::StackName}-grafana-admin"),
        Description="Grafana admin password",
        GenerateSecretString=GenerateSecretString(
            SecretStringTemplate='{"username": "lakerunner"}',
            GenerateStringKey='password',
            ExcludeCharacters=' !"#$%&\'()*+,./:;<=>?@[\\]^`{|}~',
            PasswordLength=32
        )
    ))

    # -----------------------
    # Grafana database user secret
    # -----------------------
    grafana_db_secret = t.add_resource(Secret(
        "GrafanaDbSecret",
        Name=Sub("${AWS::StackName}-grafana-db"),
        Description="Grafana database user password",
        GenerateSecretString=GenerateSecretString(
            SecretStringTemplate='{"username": "grafana"}',
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

    # -----------------------
    # PostgreSQL Database Configuration
    # -----------------------
    # Database connection is handled via environment variables and secrets
    # No additional resources needed here as database is managed by setup stack

    # -----------------------
    # Grafana Service
    # -----------------------
    # Create log group
    log_group = t.add_resource(LogGroup(
        "GrafanaLogGroup",
        LogGroupName=Sub("/ecs/grafana"),
        RetentionInDays=14
    ))

    # Build volumes list
    volumes = [
        Volume(Name="scratch")
    ]

    # Build environment variables
    base_env = [
        Environment(Name="BUMP_REVISION", Value="1"),
        Environment(Name="OTEL_SERVICE_NAME", Value="grafana"),
        Environment(Name="TMPDIR", Value="/scratch"),
        Environment(Name="HOME", Value="/scratch")
    ]

    # Add database connection environment variables
    base_env.extend([
        Environment(Name="GF_DATABASE_HOST", Value=ImportValue(Sub("${CommonInfraStackName}-DbEndpoint", CommonInfraStackName=Ref(CommonInfraStackName)))),
        Environment(Name="GF_DATABASE_PORT", Value=ImportValue(Sub("${CommonInfraStackName}-DbPort", CommonInfraStackName=Ref(CommonInfraStackName)))),
        Environment(Name="GF_DATABASE_NAME", Value="grafana"),
        Environment(Name="GF_DATABASE_USER", Value="grafana"),
    ])

    # Add Grafana-specific environment variables (excluding sensitive ones)
    env_config = grafana_config.get('environment', {})
    sensitive_keys = {'GF_SECURITY_ADMIN_PASSWORD', 'GF_DATABASE_USER', 'GF_DATABASE_PASSWORD'}
    for key, value in env_config.items():
        if key not in sensitive_keys:
            base_env.append(Environment(Name=key, Value=value))

    # Build secrets
    secrets = [
        EcsSecret(
            Name="GF_SECURITY_ADMIN_PASSWORD",
            ValueFrom=Sub("${SecretArn}:password::", SecretArn=Ref(grafana_secret))
        ),
        EcsSecret(
            Name="GF_DATABASE_PASSWORD",
            ValueFrom=Sub("${SecretArn}:password::", SecretArn=Ref(grafana_db_secret))
        ),
        EcsSecret(
            Name="GRAFANA_DATASOURCE_CONFIG",
            ValueFrom=Ref(grafana_datasource_param)
        )
    ]

    # Build mount points
    mount_points = [
        MountPoint(
            ContainerPath="/scratch",
            SourceVolume="scratch",
            ReadOnly=False
        )
    ]

    # Build health check
    health_check = HealthCheck(
        Command=["CMD-SHELL", "curl -f http://localhost:3000/api/health"],
        Interval=30,
        Timeout=5,
        Retries=3,
        StartPeriod=60
    )

    # Port mappings
    port_mappings = [PortMapping(ContainerPort=3000, Protocol="tcp")]

    # Build container definitions - init container + main container
    container_definitions = []

    # Init container for database setup
    init_container = ContainerDefinition(
        Name="GrafanaInit",
        Image=Ref(GrafanaInitImage),
        Essential=False,
        Environment=[
            Environment(Name="GRAFANA_DB_NAME", Value="grafana"),
            Environment(Name="GRAFANA_DB_USER", Value="grafana"),
            Environment(Name="PGHOST", Value=ImportValue(Sub("${CommonInfraStackName}-DbEndpoint", CommonInfraStackName=Ref(CommonInfraStackName)))),
            Environment(Name="PGPORT", Value=ImportValue(Sub("${CommonInfraStackName}-DbPort", CommonInfraStackName=Ref(CommonInfraStackName)))),
            Environment(Name="PGDATABASE", Value="postgres"),  # Connect to default postgres db first
            Environment(Name="PGSSLMODE", Value="require"),
            Environment(Name="RESET_TOKEN", Value=Ref(GrafanaResetToken)),
            Environment(Name="GF_SECURITY_ADMIN_USER", Value="lakerunner")  # Needed for password reset
        ],
        Secrets=[
            EcsSecret(
                Name="PGUSER",
                ValueFrom=Sub("${DbSecretArn}:username::",
                             DbSecretArn=ImportValue(Sub("${CommonInfraStackName}-DbSecretArn",
                                                        CommonInfraStackName=Ref(CommonInfraStackName))))
            ),
            EcsSecret(
                Name="PGPASSWORD",
                ValueFrom=Sub("${DbSecretArn}:password::",
                             DbSecretArn=ImportValue(Sub("${CommonInfraStackName}-DbSecretArn",
                                                        CommonInfraStackName=Ref(CommonInfraStackName))))
            ),
            EcsSecret(
                Name="GRAFANA_DB_PASSWORD",
                ValueFrom=Sub("${SecretArn}:password::",
                             SecretArn=Ref(grafana_db_secret))
            ),
            EcsSecret(
                Name="GF_SECURITY_ADMIN_PASSWORD",
                ValueFrom=Sub("${SecretArn}:password::",
                             SecretArn=Ref(grafana_secret))
            )
        ],
        LogConfiguration=LogConfiguration(
            LogDriver="awslogs",
            Options={
                "awslogs-group": Ref(log_group),
                "awslogs-region": Ref("AWS::Region"),
                "awslogs-stream-prefix": "grafana-init"
            }
        )
    )
    container_definitions.append(init_container)

    # Main Grafana container
    grafana_container = ContainerDefinition(
        Name="GrafanaContainer",
        Image=Ref(GrafanaImage),
        Environment=base_env,
        Secrets=secrets,
        MountPoints=mount_points,
        PortMappings=port_mappings,
        HealthCheck=health_check,
        User="0",
        DependsOn=[{"ContainerName": "GrafanaInit", "Condition": "SUCCESS"}],
        LogConfiguration=LogConfiguration(
            LogDriver="awslogs",
            Options={
                "awslogs-group": Ref(log_group),
                "awslogs-region": Ref("AWS::Region"),
                "awslogs-stream-prefix": "grafana"
            }
        )
    )
    container_definitions.append(grafana_container)

    # Create task definition
    task_def = t.add_resource(TaskDefinition(
        "GrafanaTaskDef",
        Family="grafana-task",
        Cpu=str(grafana_config.get('cpu', 512)),
        Memory=str(grafana_config.get('memory_mib', 1024)),
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
    desired_count = str(grafana_config.get('replicas', 1))

    grafana_service = t.add_resource(Service(
        "GrafanaService",
        ServiceName="grafana",
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
        LoadBalancers=[EcsLoadBalancer(
            ContainerName="GrafanaContainer",
            ContainerPort=3000,
            TargetGroupArn=Ref(GrafanaTg)
        )],
        DependsOn=["GrafanaListener"],
        EnableExecuteCommand=True,
        EnableECSManagedTags=True,
        PropagateTags="SERVICE",
        Tags=Tags(
            Name=Sub("${AWS::StackName}-grafana"),
            ManagedBy="Lakerunner",
            Environment=Ref("AWS::StackName"),
            Component="Service"
        )
    ))

    # -----------------------
    # Outputs
    # -----------------------
    t.add_output(Output(
        "GrafanaAlbDNS",
        Value=GetAtt(GrafanaAlb, "DNSName"),
        Export=Export(name=Sub("${AWS::StackName}-AlbDNS"))
    ))
    t.add_output(Output(
        "GrafanaAlbArn",
        Value=Ref(GrafanaAlb),
        Export=Export(name=Sub("${AWS::StackName}-AlbArn"))
    ))
    t.add_output(Output(
        "GrafanaServiceArn",
        Value=Ref(grafana_service),
        Export=Export(name=Sub("${AWS::StackName}-ServiceArn"))
    ))
    t.add_output(Output(
        "GrafanaUrl",
        Description="URL to access Grafana dashboard",
        Value=Sub("http://${GrafanaAlbDns}:3000", GrafanaAlbDns=GetAtt(GrafanaAlb, "DNSName"))
    ))

    return t

if __name__ == "__main__":
    template = create_grafana_template()
    print(template.to_yaml())
