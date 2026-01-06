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
import json
import os
from troposphere import (
    Template, Parameter, Ref, Sub, GetAtt, If, Equals, Export, Output,
    ImportValue, Split, Tags, Not
)
from troposphere.ecs import (
    Service, TaskDefinition, ContainerDefinition, Environment,
    LogConfiguration, Secret as EcsSecret, Volume,
    HealthCheck, PortMapping, RuntimePlatform, NetworkConfiguration, AwsvpcConfiguration,
    LoadBalancer as EcsLoadBalancer
)
from troposphere.iam import Role, Policy
from troposphere.elasticloadbalancingv2 import LoadBalancer, TargetGroup, TargetGroupAttribute, Listener, Matcher
from troposphere.elasticloadbalancingv2 import Action as AlbAction
from troposphere.ec2 import SecurityGroup, SecurityGroupIngress
from troposphere.logs import LogGroup
from troposphere.secretsmanager import Secret

def load_mcp_config(config_file="lakerunner-mcp-defaults.yaml"):
    """Load MCP configuration from YAML file"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(script_dir, "..", config_file)

    with open(config_path, 'r') as f:
        return yaml.safe_load(f)

def create_mcp_template():
    """Create CloudFormation template for standalone MCP stack"""

    t = Template()
    t.set_description("Lakerunner MCP Service: Standalone MCP server for Claude Code integration")

    # Load MCP configuration
    config = load_mcp_config()
    mcp_config = config.get('mcp', {})
    images = config.get('images', {})
    default_api_key = config.get('default_api_key', '')

    # -----------------------
    # Parameters
    # -----------------------
    CommonInfraStackName = t.add_parameter(Parameter(
        "CommonInfraStackName", Type="String",
        Description="REQUIRED: Name of the CommonInfra stack to import infrastructure values from."
    ))

    LakerunnerEndpoint = t.add_parameter(Parameter(
        "LakerunnerEndpoint", Type="String",
        Description="REQUIRED: Full URL to the Lakerunner endpoint (e.g., https://app.cardinalhq.io or http://alb-dns-name:7101)"
    ))

    LakerunnerApiKey = t.add_parameter(Parameter(
        "LakerunnerApiKey", Type="String",
        Default=default_api_key,
        Description="API key for accessing the Lakerunner API. Defaults to the standard demo key."
    ))

    # Container image override for air-gapped deployments
    McpImage = t.add_parameter(Parameter(
        "McpImage", Type="String",
        Default=images.get('mcp', 'public.ecr.aws/cardinalhq.io/lakerunner/standalone-mcp:latest'),
        Description="Container image for MCP service"
    ))

    # ALB Configuration parameters
    AlbScheme = t.add_parameter(Parameter(
        "AlbScheme",
        Type="String",
        AllowedValues=["internet-facing", "internal"],
        Default="internal",
        Description="Load balancer scheme: 'internet-facing' for external access or 'internal' for internal access only."
    ))

    # Optional MCP API Key for authentication
    McpApiKey = t.add_parameter(Parameter(
        "McpApiKey", Type="String",
        Default="",
        Description="OPTIONAL: API key for MCP endpoint authentication. Leave blank to disable authentication."
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
                    "Label": {"default": "Lakerunner Configuration"},
                    "Parameters": ["LakerunnerEndpoint", "LakerunnerApiKey"]
                },
                {
                    "Label": {"default": "MCP Configuration"},
                    "Parameters": ["McpApiKey"]
                },
                {
                    "Label": {"default": "Container Images"},
                    "Parameters": ["McpImage"]
                }
            ],
            "ParameterLabels": {
                "CommonInfraStackName": {"default": "Common Infra Stack Name"},
                "AlbScheme": {"default": "ALB Scheme"},
                "LakerunnerEndpoint": {"default": "Lakerunner Endpoint URL"},
                "LakerunnerApiKey": {"default": "Lakerunner API Key"},
                "McpApiKey": {"default": "MCP API Key (Optional)"},
                "McpImage": {"default": "MCP Container Image"},
            }
        }
    })

    # Helper function for CommonInfra imports
    def ci_export(suffix):
        return Sub("${CommonInfraStackName}-%s" % suffix, CommonInfraStackName=Ref(CommonInfraStackName))

    # Import values from CommonInfra stack
    ClusterArnValue = ImportValue(ci_export("ClusterArn"))
    TaskSecurityGroupIdValue = ImportValue(ci_export("TaskSGId"))
    VpcIdValue = ImportValue(ci_export("VpcId"))
    PrivateSubnetsValue = Split(",", ImportValue(ci_export("PrivateSubnets")))

    # Import PublicSubnets - CommonInfra always exports this, but may be empty string if not provided
    PublicSubnetsImport = ImportValue(ci_export("PublicSubnets"))
    PublicSubnetsValue = Split(",", PublicSubnetsImport)

    # Conditions
    t.add_condition("IsInternetFacing", Equals(Ref(AlbScheme), "internet-facing"))
    t.add_condition("HasMcpApiKey", Not(Equals(Ref(McpApiKey), "")))

    # -----------------------
    # ALB Security Group
    # -----------------------
    AlbSG = t.add_resource(SecurityGroup(
        "McpAlbSecurityGroup",
        GroupDescription="Security group for MCP ALB",
        VpcId=VpcIdValue,
        SecurityGroupEgress=[{
            "IpProtocol": "-1",
            "CidrIp": "0.0.0.0/0",
            "Description": "Allow all outbound"
        }]
    ))

    # Port configuration
    ports = mcp_config.get('ports', {})
    mcp_port = ports.get('mcp', 8080)
    local_api_port = ports.get('local_api', 20202)

    # Port 8080 for MCP HTTP server
    t.add_resource(SecurityGroupIngress(
        "McpAlb8080Open",
        GroupId=Ref(AlbSG),
        IpProtocol="tcp",
        FromPort=mcp_port, ToPort=mcp_port,
        CidrIp="0.0.0.0/0",
        Description="HTTP 8080 for MCP server",
    ))

    # Port 20202 for local-api (OpenAI-compatible endpoints)
    t.add_resource(SecurityGroupIngress(
        "McpAlb20202Open",
        GroupId=Ref(AlbSG),
        IpProtocol="tcp",
        FromPort=local_api_port, ToPort=local_api_port,
        CidrIp="0.0.0.0/0",
        Description="HTTP 20202 for local-api",
    ))

    # Add ingress rules to task security group to allow ALB traffic
    t.add_resource(SecurityGroupIngress(
        "TaskFromMcpAlb8080",
        GroupId=TaskSecurityGroupIdValue,
        IpProtocol="tcp",
        FromPort=mcp_port, ToPort=mcp_port,
        SourceSecurityGroupId=Ref(AlbSG),
        Description="MCP ALB to tasks 8080",
    ))

    # ALB needs to reach 20202 for health checks
    t.add_resource(SecurityGroupIngress(
        "TaskFromMcpAlb20202",
        GroupId=TaskSecurityGroupIdValue,
        IpProtocol="tcp",
        FromPort=local_api_port, ToPort=local_api_port,
        SourceSecurityGroupId=Ref(AlbSG),
        Description="MCP ALB to tasks 20202 (health check)",
    ))

    # -----------------------
    # ALB + listeners + target groups
    # -----------------------
    McpAlb = t.add_resource(LoadBalancer(
        "McpAlb",
        Scheme=Ref(AlbScheme),
        SecurityGroups=[Ref(AlbSG)],
        Subnets=If(
            "IsInternetFacing",
            PublicSubnetsValue,
            PrivateSubnetsValue
        ),
        Type="application",
    ))

    # Target group for MCP server (8080)
    McpTg = t.add_resource(TargetGroup(
        "McpTg",
        Port=mcp_port, Protocol="HTTP",
        VpcId=VpcIdValue,
        TargetType="ip",
        HealthCheckPath="/health",
        HealthCheckPort=str(local_api_port),
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

    # Target group for local-api (20202) - OpenAI-compatible endpoints
    LocalApiTg = t.add_resource(TargetGroup(
        "LocalApiTg",
        Port=local_api_port, Protocol="HTTP",
        VpcId=VpcIdValue,
        TargetType="ip",
        HealthCheckPath="/health",
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

    # Listener for MCP server
    t.add_resource(Listener(
        "McpListener",
        LoadBalancerArn=Ref(McpAlb),
        Port=str(mcp_port),
        Protocol="HTTP",
        DefaultActions=[AlbAction(Type="forward", TargetGroupArn=Ref(McpTg))]
    ))

    # Listener for local-api (OpenAI-compatible endpoints)
    t.add_resource(Listener(
        "LocalApiListener",
        LoadBalancerArn=Ref(McpAlb),
        Port=str(local_api_port),
        Protocol="HTTP",
        DefaultActions=[AlbAction(Type="forward", TargetGroupArn=Ref(LocalApiTg))]
    ))

    # -----------------------
    # Lakerunner API Key Secret
    # -----------------------
    lakerunner_api_key_secret = t.add_resource(Secret(
        "LakerunnerApiKeySecret",
        Name=Sub("${AWS::StackName}-lakerunner-api-key"),
        Description="Lakerunner API key for MCP service",
        SecretString=Ref(LakerunnerApiKey)
    ))

    # MCP API Key Secret (optional)
    mcp_api_key_secret = t.add_resource(Secret(
        "McpApiKeySecret",
        Condition="HasMcpApiKey",
        Name=Sub("${AWS::StackName}-mcp-api-key"),
        Description="MCP API key for endpoint authentication",
        SecretString=Ref(McpApiKey)
    ))

    # -----------------------
    # Task Execution Role
    # -----------------------
    ExecutionRole = t.add_resource(Role(
        "McpExecRole",
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
                PolicyName="SecretsAccess",
                PolicyDocument={
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": [
                                "secretsmanager:GetSecretValue"
                            ],
                            "Resource": [
                                Sub("arn:aws:secretsmanager:${AWS::Region}:${AWS::AccountId}:secret:${AWS::StackName}-*")
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

    # -----------------------
    # Task Role (with Bedrock permissions)
    # -----------------------
    TaskRole = t.add_resource(Role(
        "McpTaskRole",
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
                            "Resource": [
                                "arn:aws:bedrock:*::foundation-model/*",
                                Sub("arn:aws:bedrock:*:${AWS::AccountId}:inference-profile/*")
                            ]
                        },
                        {
                            "Effect": "Allow",
                            "Action": [
                                "aws-marketplace:ViewSubscriptions",
                                "aws-marketplace:Subscribe"
                            ],
                            "Resource": "*"
                        }
                    ]
                }
            )
        ]
    ))

    # -----------------------
    # MCP Service
    # -----------------------
    # Create log group
    log_group = t.add_resource(LogGroup(
        "McpLogGroup",
        LogGroupName=Sub("/ecs/${AWS::StackName}/mcp"),
        RetentionInDays=14
    ))

    # Build volumes list (scratch for tmpdir)
    volumes = [
        Volume(Name="scratch")
    ]

    # Build environment variables (matches k8s deployment)
    base_env = [
        Environment(Name="LAKERUNNER_API_URL", Value=Ref(LakerunnerEndpoint)),
    ]

    # Add environment variables from config
    env_config = mcp_config.get('environment', {})
    for key, value in env_config.items():
        base_env.append(Environment(Name=key, Value=str(value)))

    # Build secrets
    secrets = [
        EcsSecret(
            Name="LAKERUNNER_API_KEY",
            ValueFrom=Ref(lakerunner_api_key_secret)
        )
    ]

    # Build health check (local-api on 20202 has /health endpoint)
    health_check = HealthCheck(
        Command=["CMD-SHELL", f"wget --no-verbose --tries=1 --spider http://localhost:{local_api_port}/health || exit 1"],
        Interval=30,
        Timeout=5,
        Retries=3,
        StartPeriod=30
    )

    # Port mappings (both ports even though only MCP is exposed via ALB)
    port_mappings = [
        PortMapping(ContainerPort=mcp_port, Protocol="tcp"),
        PortMapping(ContainerPort=local_api_port, Protocol="tcp")
    ]

    # Main container definition
    mcp_container = ContainerDefinition(
        Name="McpContainer",
        Image=Ref(McpImage),
        Environment=base_env,
        Secrets=secrets,
        PortMappings=port_mappings,
        HealthCheck=health_check,
        User="65532",
        LogConfiguration=LogConfiguration(
            LogDriver="awslogs",
            Options={
                "awslogs-group": Ref(log_group),
                "awslogs-region": Ref("AWS::Region"),
                "awslogs-stream-prefix": "mcp"
            }
        )
    )

    # Create task definition
    task_def = t.add_resource(TaskDefinition(
        "McpTaskDef",
        Family=Sub("${AWS::StackName}-mcp-task"),
        Cpu=str(mcp_config.get('cpu', 1024)),
        Memory=str(mcp_config.get('memory_mib', 512)),
        NetworkMode="awsvpc",
        RequiresCompatibilities=["FARGATE"],
        ExecutionRoleArn=GetAtt(ExecutionRole, "Arn"),
        TaskRoleArn=GetAtt(TaskRole, "Arn"),
        ContainerDefinitions=[mcp_container],
        Volumes=volumes,
        RuntimePlatform=RuntimePlatform(
            CpuArchitecture="ARM64",
            OperatingSystemFamily="LINUX"
        )
    ))

    # Create ECS service
    desired_count = str(mcp_config.get('replicas', 1))

    mcp_service = t.add_resource(Service(
        "McpService",
        ServiceName=Sub("${AWS::StackName}-mcp"),
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
        LoadBalancers=[
            EcsLoadBalancer(
                ContainerName="McpContainer",
                ContainerPort=mcp_port,
                TargetGroupArn=Ref(McpTg)
            ),
            EcsLoadBalancer(
                ContainerName="McpContainer",
                ContainerPort=local_api_port,
                TargetGroupArn=Ref(LocalApiTg)
            )
        ],
        DependsOn=["McpListener", "LocalApiListener"],
        EnableExecuteCommand=True,
        EnableECSManagedTags=True,
        PropagateTags="SERVICE",
        Tags=Tags(
            Name=Sub("${AWS::StackName}-mcp"),
            ManagedBy="Lakerunner",
            Environment=Ref("AWS::StackName"),
            Component="Service"
        )
    ))

    # -----------------------
    # Outputs
    # -----------------------
    t.add_output(Output(
        "McpAlbDNS",
        Description="DNS name of the MCP load balancer",
        Value=GetAtt(McpAlb, "DNSName"),
        Export=Export(name=Sub("${AWS::StackName}-AlbDNS"))
    ))
    t.add_output(Output(
        "McpAlbArn",
        Value=Ref(McpAlb),
        Export=Export(name=Sub("${AWS::StackName}-AlbArn"))
    ))
    t.add_output(Output(
        "McpServiceArn",
        Value=Ref(mcp_service),
        Export=Export(name=Sub("${AWS::StackName}-ServiceArn"))
    ))
    t.add_output(Output(
        "McpUrl",
        Description="URL to access MCP server (use this as SSE endpoint in Claude Code)",
        Value=Sub("http://${McpAlbDns}:8080/sse", McpAlbDns=GetAtt(McpAlb, "DNSName"))
    ))
    t.add_output(Output(
        "LocalApiUrl",
        Description="URL for OpenAI-compatible API endpoints (/openai/v1/chat/completions, /openai/v1/models)",
        Value=Sub("http://${McpAlbDns}:20202", McpAlbDns=GetAtt(McpAlb, "DNSName"))
    ))
    t.add_output(Output(
        "TaskRoleArn",
        Description="ARN of the ECS task role with Bedrock permissions",
        Value=GetAtt(TaskRole, "Arn"),
        Export=Export(name=Sub("${AWS::StackName}-TaskRoleArn"))
    ))

    # Output the Bedrock permissions policy for reference
    bedrock_policy_json = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "BedrockFullModelAccess",
                "Effect": "Allow",
                "Action": [
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream"
                ],
                "Resource": [
                    "arn:aws:bedrock:*::foundation-model/*",
                    "arn:aws:bedrock:*:ACCOUNT_ID:inference-profile/*"
                ]
            },
            {
                "Sid": "MarketplaceSubscriptions",
                "Effect": "Allow",
                "Action": [
                    "aws-marketplace:ViewSubscriptions",
                    "aws-marketplace:Subscribe"
                ],
                "Resource": "*"
            }
        ]
    }

    t.add_output(Output(
        "BedrockPermissionsPolicy",
        Description="IAM policy document showing the Bedrock permissions granted to the task role",
        Value=Sub(json.dumps(bedrock_policy_json, indent=2))
    ))

    return t

if __name__ == "__main__":
    template = create_mcp_template()
    print(template.to_yaml())
