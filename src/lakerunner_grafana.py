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

def load_grafana_config(config_file="lakerunner-grafana-defaults.yaml"):
    """Load Grafana configuration from YAML file"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(script_dir, "..", config_file)

    with open(config_path, 'r') as f:
        return yaml.safe_load(f)

def create_grafana_template():
    """Create CloudFormation template for Grafana stack"""

    t = Template()
    t.set_description("Lakerunner Grafana: Grafana service with ALB, EFS storage, and datasource configuration")

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

    ServicesStackName = t.add_parameter(Parameter(
        "ServicesStackName", Type="String",
        Description="REQUIRED: Name of the Services stack to import Query API ALB DNS and port from."
    ))

    # Container image override for air-gapped deployments
    GrafanaImage = t.add_parameter(Parameter(
        "GrafanaImage", Type="String",
        Default=images.get('grafana', 'grafana/grafana:latest'),
        Description="Container image for Grafana service"
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
                    "Parameters": ["CommonInfraStackName", "ServicesStackName", "AlbScheme"]
                },
                {
                    "Label": {"default": "Container Images"},
                    "Parameters": ["GrafanaImage"]
                },
                {
                    "Label": {"default": "Grafana Configuration"},
                    "Parameters": ["GrafanaResetToken"]
                }
            ],
            "ParameterLabels": {
                "CommonInfraStackName": {"default": "Common Infra Stack Name"},
                "ServicesStackName": {"default": "Services Stack Name"},
                "AlbScheme": {"default": "ALB Scheme"},
                "GrafanaImage": {"default": "Grafana Image"},
                "GrafanaResetToken": {"default": "Grafana Reset Token"}
            }
        }
    })

    # Helper function for CommonInfra imports
    def ci_export(suffix):
        return Sub("${CommonInfraStackName}-%s" % suffix, CommonInfraStackName=Ref(CommonInfraStackName))

    # Helper function for Services imports
    def svc_export(suffix):
        return Sub("${ServicesStackName}-%s" % suffix, ServicesStackName=Ref(ServicesStackName))

    # Import values from other stacks
    ClusterArnValue = ImportValue(ci_export("ClusterArn"))
    EfsIdValue = ImportValue(ci_export("EfsId"))
    TaskSecurityGroupIdValue = ImportValue(ci_export("TaskSGId"))
    VpcIdValue = ImportValue(ci_export("VpcId"))
    PrivateSubnetsValue = Split(",", ImportValue(ci_export("PrivateSubnets")))
    
    # Import PublicSubnets - CommonInfra always exports this, but may be empty string if not provided
    PublicSubnetsImport = ImportValue(ci_export("PublicSubnets"))
    PublicSubnetsValue = Split(",", PublicSubnetsImport)

    # Import Query API ALB DNS from Services stack
    QueryApiAlbDns = ImportValue(svc_export("AlbDNS"))

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
                    "customPath": "http://${QUERY_API_ALB_DNS}:7101"
                },
                "secureJsonData": {
                    "apiKey": default_api_key
                }
            }
        ]
    }

    # Create SSM Parameter with Query API ALB DNS substitution
    grafana_datasource_param = t.add_resource(SSMParameter(
        "GrafanaDatasourceConfig",
        Name=Sub("${AWS::StackName}-grafana-datasource-config"),
        Type="String",
        Value=Sub(yaml.dump(grafana_datasource_config), QUERY_API_ALB_DNS=QueryApiAlbDns),
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
                                Sub("arn:aws:secretsmanager:${AWS::Region}:${AWS::AccountId}:secret:${AWS::StackName}-*")
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
                PolicyName="EFSAccess",
                PolicyDocument={
                    "Version": "2012-10-17",
                    "Statement": [
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
    # Grafana admin password secret
    # -----------------------
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

    # -----------------------
    # EFS Access Point for Grafana
    # -----------------------
    grafana_access_point = t.add_resource(AccessPoint(
        "GrafanaEfsAccessPoint",
        FileSystemId=EfsIdValue,
        PosixUser=PosixUser(Gid="0", Uid="0"),  # Use root for access point
        RootDirectory=RootDirectory(
            Path="/grafana",
            CreationInfo=CreationInfo(
                OwnerGid="0",     # root group owns the directory
                OwnerUid="0",     # root user owns the directory  
                Permissions="755"  # owner rwx, group rx, others rx - allows access to multiple users
            )
        ),
        AccessPointTags=Tags(Name=Sub("${AWS::StackName}-grafana"))
    ))

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
        Volume(Name="scratch"),
        Volume(
            Name="efs-grafana",
            EFSVolumeConfiguration=EFSVolumeConfiguration(
                FilesystemId=EfsIdValue,
                TransitEncryption="ENABLED",
                AuthorizationConfig=AuthorizationConfig(
                    AccessPointId=Ref(grafana_access_point),
                    IAM="ENABLED"
                )
            )
        )
    ]

    # Build environment variables
    base_env = [
        Environment(Name="BUMP_REVISION", Value="1"),
        Environment(Name="OTEL_SERVICE_NAME", Value="grafana"),
        Environment(Name="TMPDIR", Value="/scratch"),
        Environment(Name="HOME", Value="/scratch")
    ]

    # Add Grafana-specific environment variables (excluding sensitive ones)
    env_config = grafana_config.get('environment', {})
    sensitive_keys = {'GF_SECURITY_ADMIN_PASSWORD'}
    for key, value in env_config.items():
        if key not in sensitive_keys:
            # Special handling for GF_RESET_TOKEN to use parameter instead of defaults
            if key == 'GF_RESET_TOKEN':
                base_env.append(Environment(Name=key, Value=Ref(GrafanaResetToken)))
            else:
                base_env.append(Environment(Name=key, Value=value))

    # Build secrets
    secrets = [
        EcsSecret(
            Name="GF_SECURITY_ADMIN_PASSWORD",
            ValueFrom=Sub("${SecretArn}:password::", SecretArn=Ref(grafana_secret))
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
        ),
        MountPoint(
            ContainerPath="/var/lib/grafana",
            SourceVolume="efs-grafana",
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

    # Create init container for Grafana setup (datasource provisioning + reset logic)
    init_container = ContainerDefinition(
        Name="GrafanaInit",
        Image="public.ecr.aws/docker/library/alpine:latest",
        Essential=False,
        Command=["/bin/sh", "-c"],
        Environment=[
            Environment(Name="PROVISIONING_DIR", Value="${GF_PATHS_PROVISIONING:-/etc/grafana/provisioning}"),
            Environment(Name="RESET_TOKEN", Value=Ref(GrafanaResetToken)),
            Environment(Name="QUERY_API_URL", Value=Sub("http://${QueryApiAlbDns}", QueryApiAlbDns=QueryApiAlbDns))
        ],
        Secrets=[
            EcsSecret(
                Name="GRAFANA_DATASOURCE_CONFIG",
                ValueFrom=Sub("${AWS::StackName}-grafana-datasource-config")
            )
        ],
        MountPoints=mount_points,
        User="0",
        LogConfiguration=LogConfiguration(
            LogDriver="awslogs",
            Options={
                "awslogs-group": Ref(log_group),
                "awslogs-region": Ref("AWS::Region"),
                "awslogs-stream-prefix": "grafana-init"
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

# All containers run as root, so no ownership changes needed

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
        EnableExecuteCommand=True
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