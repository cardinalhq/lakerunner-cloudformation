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
    Template, Parameter, Ref, Sub, GetAtt, If, Equals, NoValue, Export, Output,
    Select, Not, Tags
)
from troposphere.ec2 import SecurityGroup, SecurityGroupIngress
from troposphere.ecs import Cluster
from troposphere.elasticloadbalancingv2 import LoadBalancer, Listener, TargetGroup, Matcher
from troposphere.elasticloadbalancingv2 import Action as AlbAction
from troposphere.elasticloadbalancingv2 import TargetGroupAttribute
from troposphere.s3 import (
    Bucket, LifecycleRule, LifecycleConfiguration,
    NotificationConfiguration, QueueConfigurations,
    S3Key, Filter, Rules
)
from troposphere.sqs import Queue, QueuePolicy
from troposphere.secretsmanager import Secret, GenerateSecretString
from troposphere.efs import FileSystem, MountTarget
from troposphere.rds import DBInstance, DBSubnetGroup
from troposphere.ssm import Parameter as SsmParameter

t = Template()
t.set_description("CommonInfra stack for Lakerunner.")

# -----------------------
# Parameters (with helpful descriptions)
# -----------------------
VpcId = t.add_parameter(Parameter(
    "VpcId",
    Type="AWS::EC2::VPC::Id",
    Description="REQUIRED: VPC where resources will be created."
))

PublicSubnets = t.add_parameter(Parameter(
    "PublicSubnets",
    Type="CommaDelimitedList",
    Default="",
    Description="Public subnet IDs (for internet-facing ALB). Required when AlbScheme=internet-facing. Provide at least two in different AZs."
))

PrivateSubnets = t.add_parameter(Parameter(
    "PrivateSubnets",
    Type="List<AWS::EC2::Subnet::Id>",
    Description="REQUIRED: Private subnet IDs (for RDS/ECS/EFS). Provide at least two in different AZs."
))

AlbScheme = t.add_parameter(Parameter(
    "AlbScheme",
    Type="String",
    AllowedValues=["internet-facing", "internal"],
    Default="internal",
    Description="Load balancer scheme: 'internet-facing' for external access or 'internal' for internal access only."
))

# Configuration overrides (optional multi-line parameters)
ApiKeysOverride = t.add_parameter(Parameter(
    "ApiKeysOverride",
    Type="String",
    Default="",
    Description="OPTIONAL: Custom API keys configuration in YAML format. Leave blank to use defaults from defaults.yaml. Example: - organization_id: xxx\\n  keys:\\n    - keyvalue"
))

StorageProfilesOverride = t.add_parameter(Parameter(
    "StorageProfilesOverride",
    Type="String",
    Default="",
    Description="OPTIONAL: Custom storage profiles configuration in YAML format. Leave blank to use defaults from defaults.yaml. Bucket name and region will be auto-filled."
))

# -----------------------
# UI Hints in Console (Parameter Groups & Labels)
# -----------------------
t.set_metadata({
    "AWS::CloudFormation::Interface": {
        "ParameterGroups": [
            {
                "Label": {"default": "Networking"},
                "Parameters": ["VpcId", "PublicSubnets", "PrivateSubnets"]
            },
            {
                "Label": {"default": "Load Balancer"},
                "Parameters": ["AlbScheme"]
            },
            {
                "Label": {"default": "Configuration Overrides (Advanced)"},
                "Parameters": ["ApiKeysOverride", "StorageProfilesOverride"]
            }
        ],
        "ParameterLabels": {
            "VpcId": {"default": "VPC Id"},
            "PublicSubnets": {"default": "Public Subnets (required for internet-facing ALB)"},
            "PrivateSubnets": {"default": "Private Subnets (for ECS/RDS/EFS)"},
            "AlbScheme": {"default": "Load Balancer Scheme"},
            "ApiKeysOverride": {"default": "Custom API Keys (YAML)"},
            "StorageProfilesOverride": {"default": "Custom Storage Profiles (YAML)"}
        }
    }
})

# -----------------------
# Conditions
# -----------------------
t.add_condition("IsInternetFacing", Equals(Ref(AlbScheme), "internet-facing"))
t.add_condition("HasApiKeysOverride", Not(Equals(Ref(ApiKeysOverride), "")))
t.add_condition("HasStorageProfilesOverride", Not(Equals(Ref(StorageProfilesOverride), "")))

# Helper function to load defaults
def load_defaults():
    """Load default configuration from defaults.yaml"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(script_dir, "..", "defaults.yaml")

    with open(config_path, 'r') as f:
        return yaml.safe_load(f)

# -----------------------
# Security Groups
# -----------------------
TaskSG = t.add_resource(SecurityGroup(
    "TaskSG",
    GroupDescription="Security group for ECS tasks",
    VpcId=Ref(VpcId),
    SecurityGroupEgress=[{
        "IpProtocol": "-1",
        "CidrIp": "0.0.0.0/0",
        "Description": "Allow all outbound"
    }]
))

# task-to-task 7101 (adjust/remove as needed)
t.add_resource(SecurityGroupIngress(
    "TaskSG7101Self",
    GroupId=Ref(TaskSG),
    IpProtocol="tcp",
    FromPort=7101,
    ToPort=7101,
    SourceSecurityGroupId=Ref(TaskSG),
    Description="task-to-task 7101",
))

# Allow tasks to connect to PostgreSQL database
t.add_resource(SecurityGroupIngress(
    "TaskSGDbSelf",
    GroupId=Ref(TaskSG),
    IpProtocol="tcp",
    FromPort=5432,
    ToPort=5432,
    SourceSecurityGroupId=Ref(TaskSG),
    Description="task-to-database PostgreSQL",
))

# Allow tasks to connect to EFS (NFS port 2049)
t.add_resource(SecurityGroupIngress(
    "TaskSGEfsSelf",
    GroupId=Ref(TaskSG),
    IpProtocol="tcp",
    FromPort=2049,
    ToPort=2049,
    SourceSecurityGroupId=Ref(TaskSG),
    Description="task-to-EFS NFS",
))

# ALB SG
AlbSG = t.add_resource(SecurityGroup(
    "AlbSecurityGroup",
    GroupDescription="Security group for ALB",
    VpcId=Ref(VpcId),
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

for port in (3000, 7101):
    t.add_resource(SecurityGroupIngress(
        f"TaskFromAlb{port}",
        GroupId=Ref(TaskSG),
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
    Name=Sub("${AWS::StackName}-alb"),
    Scheme=Ref(AlbScheme),
    SecurityGroups=[Ref(AlbSG)],
    Subnets=If(
        "IsInternetFacing",
        Ref(PublicSubnets),
        Ref(PrivateSubnets)
    ),
    Type="application",
))

Tg7101 = t.add_resource(TargetGroup(
    "Tg7101",
    Port=7101, Protocol="HTTP",
    VpcId=Ref(VpcId),
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
    VpcId=Ref(VpcId),
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

# -----------------------
# ECS cluster (always create)
# -----------------------
ClusterRes = t.add_resource(Cluster(
    "Cluster",
    ClusterName=Sub("${AWS::StackName}-cluster"),
))
t.add_output(Output(
    "ClusterArn",
    Value=GetAtt(ClusterRes, "Arn"),
    Export=Export(name=Sub("${AWS::StackName}-ClusterArn"))
))

# -----------------------
# SQS + S3 (with lifecycle + notifications)
# -----------------------
QueueRes = t.add_resource(Queue(
    "IngestQueue",
    QueueName="lakerunner-ingest-queue",
    MessageRetentionPeriod=60 * 60 * 24 * 4,  # seconds
))

BucketRes = t.add_resource(Bucket(
    "IngestBucket",
    DeletionPolicy="Delete",
    LifecycleConfiguration=LifecycleConfiguration(
        Rules=[LifecycleRule(Prefix="otel-raw/", Status="Enabled", ExpirationInDays=10)]
    ),
    NotificationConfiguration=NotificationConfiguration(
        QueueConfigurations=[
            QueueConfigurations(
                Event="s3:ObjectCreated:*",
                Queue=GetAtt(QueueRes, "Arn"),
                Filter=Filter(
                    S3Key=S3Key(
                        Rules=[Rules(Name="prefix", Value=p)]
                    )
                )
            ) for p in ["otel-raw/", "logs-raw/", "metrics-raw/"]
        ]
    ),
))

t.add_resource(QueuePolicy(
    "IngestQueuePolicy",
    Queues=[Ref(QueueRes)],
    PolicyDocument={
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "s3.amazonaws.com"},
            "Action": ["sqs:GetQueueAttributes", "sqs:GetQueueUrl", "sqs:SendMessage"],
            "Resource": GetAtt(QueueRes, "Arn"),
            "Condition": {
                "StringEquals": {"aws:SourceAccount": Ref("AWS::AccountId")}
            }
        }]
    }
))

# -----------------------
# Secrets for DB (always create; random name)
# -----------------------
DbSecret = t.add_resource(Secret(
    "DbSecret",
    GenerateSecretString=GenerateSecretString(
        SecretStringTemplate='{"username":"lakerunner"}',
        GenerateStringKey="password",
        ExcludePunctuation=True,
    ),
))
DbSecretArnValue = Ref(DbSecret)

# -----------------------
# RDS Postgres (always create)
# -----------------------
DbSubnets = t.add_resource(DBSubnetGroup(
    "DbSubnetGroup",
    DBSubnetGroupDescription="DB subnets",
    SubnetIds=Ref(PrivateSubnets)
))
DbRes = t.add_resource(DBInstance(
    "LakerunnerDb",
    Engine="postgres",
    EngineVersion="17",
    DBName="lakerunner",
    DBInstanceClass="db.t3.medium",
    PubliclyAccessible=False,
    MultiAZ=False,
    CopyTagsToSnapshot=True,
    StorageType="gp3",
    AllocatedStorage="100",
    VPCSecurityGroups=[Ref(TaskSG)],  # for tighter control, add a dedicated DB SG and allow from TaskSG
    DBSubnetGroupName=Ref(DbSubnets),
    MasterUsername=Sub("{{resolve:secretsmanager:${S}:SecretString:username}}", S=DbSecretArnValue),
    MasterUserPassword=Sub("{{resolve:secretsmanager:${S}:SecretString:password}}", S=DbSecretArnValue),
    DeletionProtection=False
))

DbEndpoint = GetAtt(DbRes, "Endpoint.Address")
DbPort = GetAtt(DbRes, "Endpoint.Port")

t.add_output(Output("DbEndpoint", Value=DbEndpoint, Export=Export(name=Sub("${AWS::StackName}-DbEndpoint"))))
t.add_output(Output("DbPort", Value=DbPort, Export=Export(name=Sub("${AWS::StackName}-DbPort"))))
t.add_output(Output("DbSecretArnOut", Value=DbSecretArnValue, Export=Export(name=Sub("${AWS::StackName}-DbSecretArn"))))

# -----------------------
# EFS (always create)
# -----------------------
Fs = t.add_resource(FileSystem(
    "Efs",
    Encrypted=True,
    FileSystemTags=Tags(Name=Sub("${AWS::StackName}-efs")),
))
t.add_resource(MountTarget(
    "EfsMt1",
    FileSystemId=Ref(Fs),
    SubnetId=Select(0, Ref(PrivateSubnets)),
    SecurityGroups=[Ref(TaskSG)]
))
t.add_resource(MountTarget(
    "EfsMt2",
    FileSystemId=Ref(Fs),
    SubnetId=Select(1, Ref(PrivateSubnets)),
    SecurityGroups=[Ref(TaskSG)]
))

t.add_output(Output("EfsId", Value=Ref(Fs), Export=Export(name=Sub("${AWS::StackName}-EfsId"))))

# Export S3 bucket name for IAM policies in other stacks
t.add_output(Output("BucketName", Value=Ref(BucketRes), Export=Export(name=Sub("${AWS::StackName}-BucketName"))))
t.add_output(Output("BucketArn", Value=GetAtt(BucketRes, "Arn"), Export=Export(name=Sub("${AWS::StackName}-BucketArn"))))

# Load defaults for SSM parameters
defaults = load_defaults()

# -----------------------
# SSM params with defaults and overrides
# -----------------------
# API Keys parameter - use override if provided, otherwise use defaults
api_keys_yaml = yaml.dump(defaults['api_keys'], default_flow_style=False)
t.add_resource(SsmParameter(
    "ApiKeysParam",
    Name="/lakerunner/api_keys",
    Type="String",
    Value=If(
        "HasApiKeysOverride",
        Ref(ApiKeysOverride),
        api_keys_yaml
    ),
    Description="API keys configuration",
))

# Storage Profiles parameter - use override if provided, otherwise use defaults with substitutions
storage_profiles_default = yaml.dump(defaults['storage_profiles'], default_flow_style=False)
# Replace placeholders with CloudFormation substitutions
storage_profiles_default_cf = storage_profiles_default.replace("${BUCKET_NAME}", "${Bucket}").replace("${AWS_REGION}", "${AWS::Region}")

t.add_resource(SsmParameter(
    "StorageProfilesParam",
    Name="/lakerunner/storage_profiles",
    Type="String",
    Value=If(
        "HasStorageProfilesOverride",
        Ref(StorageProfilesOverride),
        Sub(storage_profiles_default_cf, Bucket=Ref(BucketRes))
    ),
    Description="Storage profiles configuration",
))


# -----------------------
# Outputs (for access in other stacks)
# -----------------------
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
t.add_output(Output(
    "TaskSecurityGroupId",
    Value=Ref(TaskSG),
    Export=Export(name=Sub("${AWS::StackName}-TaskSGId"))
))
t.add_output(Output(
    "PrivateSubnetsOut",
    Value=Sub("${Subnet1},${Subnet2}", Subnet1=Select(0, Ref(PrivateSubnets)), Subnet2=Select(1, Ref(PrivateSubnets))),
    Export=Export(name=Sub("${AWS::StackName}-PrivateSubnets"))
))
t.add_output(Output(
    "VpcIdOut",
    Value=Ref(VpcId),
    Export=Export(name=Sub("${AWS::StackName}-VpcId"))
))

# Export ALB scheme so Services template can use appropriate subnets
t.add_output(Output(
    "AlbSchemeValue",
    Value=Ref(AlbScheme),
    Export=Export(name=Sub("${AWS::StackName}-AlbScheme"))
))

print(t.to_yaml())
