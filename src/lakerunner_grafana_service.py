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
    """Create CloudFormation template for Grafana + AI stack"""

    t = Template()
    t.set_description(
        "Lakerunner Grafana + AI: Grafana with MCP Gateway, Conductor Server,"
        " and Maestro sidecars, ALB, PostgreSQL storage, and pre-configured plugins"
    )

    # Load configuration
    config = load_grafana_config()
    grafana_config = config.get('grafana', {})
    mcp_gw_config = config.get('mcp_gateway', {})
    conductor_config = config.get('conductor_server', {})
    maestro_config = config.get('maestro_server', {})
    task_config = config.get('task', {})
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

    grafana_image = images.get('grafana', 'grafana/grafana:latest')
    grafana_init_image = images.get('grafana_init', 'lakerunner-grafana-init:latest')
    mcp_gateway_image = images.get('mcp_gateway', 'public.ecr.aws/cardinalhq.io/mcp-gateway:v0.2.0')
    conductor_server_image = images.get('conductor_server', 'public.ecr.aws/cardinalhq.io/conductor-server:latest')
    maestro_server_image = images.get('maestro_server', 'public.ecr.aws/cardinalhq.io/maestro:latest')

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

    # AI Configuration parameters
    BedrockModel = t.add_parameter(Parameter(
        "BedrockModel", Type="String",
        Default="us.anthropic.claude-sonnet-4-6",
        AllowedValues=[
            "us.anthropic.claude-sonnet-4-6",
            "eu.anthropic.claude-sonnet-4-6",
            "au.anthropic.claude-sonnet-4-6",
            "jp.anthropic.claude-sonnet-4-6",
            "global.anthropic.claude-sonnet-4-6",
        ],
        Description="Bedrock model inference profile for AI features. Choose a geographic profile to control where requests are routed."
    ))

    LakerunnerApiKey = t.add_parameter(Parameter(
        "LakerunnerApiKey", Type="String",
        Default=api_keys[0]['keys'][0] if api_keys and api_keys[0].get('keys') else "",
        Description="API key for Lakerunner services. Defaults to the standard key."
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
                    "Label": {"default": "AI Configuration"},
                    "Parameters": ["BedrockModel", "LakerunnerApiKey"]
                },
                {
                    "Label": {"default": "Grafana Maintenance"},
                    "Parameters": ["GrafanaResetToken"]
                }
            ],
            "ParameterLabels": {
                "CommonInfraStackName": {"default": "Common Infra Stack Name"},
                "QueryApiUrl": {"default": "Query API URL"},
                "AlbScheme": {"default": "ALB Scheme"},
                "BedrockModel": {"default": "Bedrock Model"},
                "LakerunnerApiKey": {"default": "Lakerunner API Key"},
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
    # Secrets
    # -----------------------
    # Grafana admin password
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

    # Grafana database user password
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

    # Internal API key shared between AI services (auto-generated)
    ai_internal_secret = t.add_resource(Secret(
        "AiInternalSecret",
        Name=Sub("${AWS::StackName}-ai-internal"),
        Description="Internal API key for communication between AI services",
        GenerateSecretString=GenerateSecretString(
            SecretStringTemplate='{}',
            GenerateStringKey='api_key',
            ExcludePunctuation=True,
            PasswordLength=48
        )
    ))

    # Lakerunner API key (stored in Secrets Manager for ECS secret injection)
    lakerunner_api_key_secret = t.add_resource(Secret(
        "LakerunnerApiKeySecret",
        Name=Sub("${AWS::StackName}-lakerunner-api-key"),
        Description="Lakerunner API key for AI services",
        SecretString=Ref(LakerunnerApiKey)
    ))

    t.add_output(Output(
        "GrafanaAdminSecretArn",
        Description="ARN of the Grafana admin password secret. Use AWS CLI to retrieve: aws secretsmanager get-secret-value --secret-id <ARN>",
        Value=Ref(grafana_secret),
        Export=Export(Sub("${AWS::StackName}-GrafanaAdminSecretArn"))
    ))

    # -----------------------
    # Task Execution Role
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
    # Task Role (Grafana + AI services)
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
            ),
            Policy(
                PolicyName="BedrockAccess",
                PolicyDocument={
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": [
                                "bedrock:InvokeModel",
                                "bedrock:InvokeModelWithResponseStream"
                            ],
                            "Resource": "*"
                        }
                    ]
                }
            )
        ]
    ))

    # -----------------------
    # Log Groups
    # -----------------------
    grafana_log_group = t.add_resource(LogGroup(
        "GrafanaLogGroup",
        LogGroupName=Sub("/ecs/${AWS::StackName}/grafana"),
        RetentionInDays=14
    ))

    mcp_gw_log_group = t.add_resource(LogGroup(
        "McpGatewayLogGroup",
        LogGroupName=Sub("/ecs/${AWS::StackName}/mcp-gateway"),
        RetentionInDays=14
    ))

    conductor_log_group = t.add_resource(LogGroup(
        "ConductorServerLogGroup",
        LogGroupName=Sub("/ecs/${AWS::StackName}/conductor-server"),
        RetentionInDays=14
    ))

    maestro_log_group = t.add_resource(LogGroup(
        "MaestroServerLogGroup",
        LogGroupName=Sub("/ecs/${AWS::StackName}/maestro-server"),
        RetentionInDays=14
    ))

    # -----------------------
    # Volumes
    # -----------------------
    volumes = [
        Volume(Name="scratch")
    ]

    # -----------------------
    # Container Definitions
    # -----------------------
    container_definitions = []

    # -- Init container for database setup --
    init_container = ContainerDefinition(
        Name="GrafanaInit",
        Image=grafana_init_image,
        Essential=False,
        Environment=[
            Environment(Name="GRAFANA_DB_NAME", Value="grafana"),
            Environment(Name="GRAFANA_DB_USER", Value="grafana"),
            Environment(Name="PGHOST", Value=ImportValue(Sub("${CommonInfraStackName}-DbEndpoint", CommonInfraStackName=Ref(CommonInfraStackName)))),
            Environment(Name="PGPORT", Value=ImportValue(Sub("${CommonInfraStackName}-DbPort", CommonInfraStackName=Ref(CommonInfraStackName)))),
            Environment(Name="PGDATABASE", Value="postgres"),
            Environment(Name="PGSSLMODE", Value="require"),
            Environment(Name="RESET_TOKEN", Value=Ref(GrafanaResetToken)),
            Environment(Name="GF_SECURITY_ADMIN_USER", Value="lakerunner")
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
                "awslogs-group": Ref(grafana_log_group),
                "awslogs-region": Ref("AWS::Region"),
                "awslogs-stream-prefix": "grafana-init"
            }
        )
    )
    container_definitions.append(init_container)

    # -- MCP Gateway sidecar --
    mcp_gw_port = mcp_gw_config.get('port', 8080)
    mcp_gw_env = [
        Environment(Name="LAKERUNNER_API_URL", Value=Ref(QueryApiUrl)),
    ]
    for key, value in mcp_gw_config.get('environment', {}).items():
        mcp_gw_env.append(Environment(Name=key, Value=str(value)))

    mcp_gw_secrets = [
        EcsSecret(
            Name="LAKERUNNER_API_KEY",
            ValueFrom=Ref(lakerunner_api_key_secret)
        ),
        EcsSecret(
            Name="CARDINALHQ_API_KEY",
            ValueFrom=Ref(lakerunner_api_key_secret)
        ),
        EcsSecret(
            Name="MCP_API_KEY",
            ValueFrom=Sub("${SecretArn}:api_key::", SecretArn=Ref(ai_internal_secret))
        ),
    ]

    mcp_gateway_container = ContainerDefinition(
        Name="McpGateway",
        Image=mcp_gateway_image,
        Essential=True,
        User="65532",
        PortMappings=[PortMapping(ContainerPort=mcp_gw_port, Protocol="tcp")],
        Environment=mcp_gw_env,
        Secrets=mcp_gw_secrets,
        HealthCheck=HealthCheck(
            Command=["CMD-SHELL", "wget --no-verbose --tries=1 --spider http://localhost:%d/healthz || exit 1" % mcp_gw_port],
            Interval=30,
            Timeout=5,
            Retries=3,
            StartPeriod=30
        ),
        DependsOn=[{"ContainerName": "GrafanaInit", "Condition": "SUCCESS"}],
        LogConfiguration=LogConfiguration(
            LogDriver="awslogs",
            Options={
                "awslogs-group": Ref(mcp_gw_log_group),
                "awslogs-region": Ref("AWS::Region"),
                "awslogs-stream-prefix": "mcp-gateway"
            }
        )
    )
    container_definitions.append(mcp_gateway_container)

    # -- Conductor Server sidecar --
    conductor_port = conductor_config.get('port', 4100)
    conductor_env = [
        Environment(Name="AWS_BEDROCK_MODEL", Value=Ref(BedrockModel)),
        Environment(Name="WORKFLOW_BEDROCK_INFERENCE_MODEL", Value=Ref(BedrockModel)),
        Environment(Name="AWS_REGION", Value=Ref("AWS::Region")),
    ]
    for key, value in conductor_config.get('environment', {}).items():
        conductor_env.append(Environment(Name=key, Value=str(value)))

    conductor_secrets = [
        EcsSecret(
            Name="LAKERUNNER_API_KEY",
            ValueFrom=Ref(lakerunner_api_key_secret)
        ),
        EcsSecret(
            Name="CARDINALHQ_API_KEY",
            ValueFrom=Ref(lakerunner_api_key_secret)
        ),
        EcsSecret(
            Name="MCP_API_KEY",
            ValueFrom=Sub("${SecretArn}:api_key::", SecretArn=Ref(ai_internal_secret))
        ),
    ]

    conductor_container = ContainerDefinition(
        Name="ConductorServer",
        Image=conductor_server_image,
        Essential=True,
        User="65532",
        PortMappings=[PortMapping(ContainerPort=conductor_port, Protocol="tcp")],
        Environment=conductor_env,
        Secrets=conductor_secrets,
        HealthCheck=HealthCheck(
            Command=["CMD-SHELL", "wget --no-verbose --tries=1 --spider http://localhost:%d/api/health || exit 1" % conductor_port],
            Interval=30,
            Timeout=10,
            Retries=3,
            StartPeriod=30
        ),
        DependsOn=[
            {"ContainerName": "GrafanaInit", "Condition": "SUCCESS"},
            {"ContainerName": "McpGateway", "Condition": "HEALTHY"}
        ],
        LogConfiguration=LogConfiguration(
            LogDriver="awslogs",
            Options={
                "awslogs-group": Ref(conductor_log_group),
                "awslogs-region": Ref("AWS::Region"),
                "awslogs-stream-prefix": "conductor-server"
            }
        )
    )
    container_definitions.append(conductor_container)

    # -- Maestro Server sidecar --
    maestro_port = maestro_config.get('port', 3100)
    maestro_env = [
        Environment(Name="AWS_BEDROCK_MODEL", Value=Ref(BedrockModel)),
        Environment(Name="WORKFLOW_BEDROCK_INFERENCE_MODEL", Value=Ref(BedrockModel)),
        Environment(Name="AWS_REGION", Value=Ref("AWS::Region")),
    ]
    for key, value in maestro_config.get('environment', {}).items():
        maestro_env.append(Environment(Name=key, Value=str(value)))

    maestro_secrets = [
        EcsSecret(
            Name="LAKERUNNER_API_KEY",
            ValueFrom=Ref(lakerunner_api_key_secret)
        ),
        EcsSecret(
            Name="CARDINALHQ_API_KEY",
            ValueFrom=Ref(lakerunner_api_key_secret)
        ),
        EcsSecret(
            Name="MCP_API_KEY",
            ValueFrom=Sub("${SecretArn}:api_key::", SecretArn=Ref(ai_internal_secret))
        ),
    ]

    maestro_container = ContainerDefinition(
        Name="MaestroServer",
        Image=maestro_server_image,
        Essential=True,
        User="65532",
        PortMappings=[PortMapping(ContainerPort=maestro_port, Protocol="tcp")],
        Environment=maestro_env,
        Secrets=maestro_secrets,
        HealthCheck=HealthCheck(
            Command=["CMD-SHELL", "wget --no-verbose --tries=1 --spider http://localhost:%d/health || exit 1" % maestro_port],
            Interval=30,
            Timeout=10,
            Retries=3,
            StartPeriod=30
        ),
        DependsOn=[
            {"ContainerName": "GrafanaInit", "Condition": "SUCCESS"},
            {"ContainerName": "McpGateway", "Condition": "HEALTHY"}
        ],
        LogConfiguration=LogConfiguration(
            LogDriver="awslogs",
            Options={
                "awslogs-group": Ref(maestro_log_group),
                "awslogs-region": Ref("AWS::Region"),
                "awslogs-stream-prefix": "maestro-server"
            }
        )
    )
    container_definitions.append(maestro_container)

    # -- Main Grafana container --
    base_env = [
        Environment(Name="BUMP_REVISION", Value="1"),
        Environment(Name="OTEL_SERVICE_NAME", Value="grafana"),
        Environment(Name="TMPDIR", Value="/scratch"),
        Environment(Name="HOME", Value="/scratch"),
        Environment(Name="GF_DATABASE_HOST", Value=ImportValue(Sub("${CommonInfraStackName}-DbEndpoint", CommonInfraStackName=Ref(CommonInfraStackName)))),
        Environment(Name="GF_DATABASE_PORT", Value=ImportValue(Sub("${CommonInfraStackName}-DbPort", CommonInfraStackName=Ref(CommonInfraStackName)))),
        Environment(Name="GF_DATABASE_NAME", Value="grafana"),
        Environment(Name="GF_DATABASE_USER", Value="grafana"),
    ]

    # Add Grafana-specific environment variables (excluding sensitive ones)
    env_config = grafana_config.get('environment', {})
    sensitive_keys = {'GF_SECURITY_ADMIN_PASSWORD', 'GF_DATABASE_USER', 'GF_DATABASE_PASSWORD'}
    for key, value in env_config.items():
        if key not in sensitive_keys:
            base_env.append(Environment(Name=key, Value=value))

    grafana_secrets = [
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

    grafana_container = ContainerDefinition(
        Name="GrafanaContainer",
        Image=grafana_image,
        Essential=True,
        Environment=base_env,
        Secrets=grafana_secrets,
        MountPoints=[
            MountPoint(
                ContainerPath="/scratch",
                SourceVolume="scratch",
                ReadOnly=False
            )
        ],
        PortMappings=[PortMapping(ContainerPort=3000, Protocol="tcp")],
        HealthCheck=HealthCheck(
            Command=["CMD-SHELL", "curl -f http://localhost:3000/api/health"],
            Interval=30,
            Timeout=5,
            Retries=3,
            StartPeriod=60
        ),
        User="0",
        DependsOn=[{"ContainerName": "GrafanaInit", "Condition": "SUCCESS"}],
        LogConfiguration=LogConfiguration(
            LogDriver="awslogs",
            Options={
                "awslogs-group": Ref(grafana_log_group),
                "awslogs-region": Ref("AWS::Region"),
                "awslogs-stream-prefix": "grafana"
            }
        )
    )
    container_definitions.append(grafana_container)

    # -----------------------
    # Task Definition
    # -----------------------
    task_def = t.add_resource(TaskDefinition(
        "GrafanaTaskDef",
        Family=Sub("${AWS::StackName}-grafana-ai"),
        Cpu=str(task_config.get('cpu', 2048)),
        Memory=str(task_config.get('memory_mib', 4096)),
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

    # -----------------------
    # ECS Service
    # -----------------------
    desired_count = str(grafana_config.get('replicas', 1))

    grafana_service = t.add_resource(Service(
        "GrafanaService",
        ServiceName=Sub("${AWS::StackName}-grafana"),
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
