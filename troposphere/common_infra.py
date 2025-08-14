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

from troposphere import (
    Template, Parameter, Ref, Sub, GetAtt, If, Equals, NoValue, Export, Output,
    Select, Not, Tags
)
from troposphere.ec2 import SecurityGroup, SecurityGroupIngress
from troposphere.ecs import Cluster
from troposphere.elasticloadbalancingv2 import LoadBalancer, Listener, TargetGroup
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
    Type="List<AWS::EC2::Subnet::Id>",
    Description="REQUIRED: Public subnet IDs (for ALB). Provide at least two in different AZs."
))

PrivateSubnets = t.add_parameter(Parameter(
    "PrivateSubnets",
    Type="List<AWS::EC2::Subnet::Id>",
    Description="REQUIRED: Private subnet IDs (for RDS/ECS/EFS). Provide at least two in different AZs."
))

CreateAlb = t.add_parameter(Parameter(
    "CreateAlb",
    Type="String",
    AllowedValues=["Yes", "No"],
    Default="Yes",
    Description="Create an internet-facing Application Load Balancer with listeners on 7101 and 3000. Set to 'No' to skip."
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
                "Parameters": ["CreateAlb"]
            }
        ],
        "ParameterLabels": {
            "VpcId": {"default": "VPC Id"},
            "PublicSubnets": {"default": "Public Subnets (for ALB)"},
            "PrivateSubnets": {"default": "Private Subnets (for ECS/RDS/EFS)"},
            "CreateAlb": {"default": "Create Application Load Balancer?"}
        }
    }
})

# -----------------------
# Conditions
# -----------------------
t.add_condition("CreateAlbCond", Equals(Ref(CreateAlb), "Yes"))

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

# ALB SG (only if creating ALB)
AlbSG = t.add_resource(SecurityGroup(
    "AlbSecurityGroup",
    Condition="CreateAlbCond",
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
    Condition="CreateAlbCond",
    GroupId=Ref(AlbSG),
    IpProtocol="tcp",
    FromPort=3000, ToPort=3000,
    CidrIp="0.0.0.0/0",
    Description="HTTP 3000",
))
t.add_resource(SecurityGroupIngress(
    "Alb7101Open",
    Condition="CreateAlbCond",
    GroupId=Ref(AlbSG),
    IpProtocol="tcp",
    FromPort=7101, ToPort=7101,
    CidrIp="0.0.0.0/0",
    Description="HTTP 7101",
))

for port in (3000, 7101):
    t.add_resource(SecurityGroupIngress(
        f"TaskFromAlb{port}",
        Condition="CreateAlbCond",
        GroupId=Ref(TaskSG),
        IpProtocol="tcp",
        FromPort=port, ToPort=port,
        SourceSecurityGroupId=Ref(AlbSG),
        Description=f"ALB to tasks {port}",
    ))

# -----------------------
# Optional ALB + listeners + target groups
# -----------------------
Alb = t.add_resource(LoadBalancer(
    "Alb",
    Condition="CreateAlbCond",
    Name=Sub("${AWS::StackName}-alb"),
    Scheme="internet-facing",
    SecurityGroups=[Ref(AlbSG)],
    Subnets=Ref(PublicSubnets),
    Type="application",
))

Tg7101 = t.add_resource(TargetGroup(
    "Tg7101",
    Condition="CreateAlbCond",
    Port=7101, Protocol="HTTP",
    VpcId=Ref(VpcId),
    TargetType="ip",
    TargetGroupAttributes=[TargetGroupAttribute(Key="stickiness.enabled", Value="false")]
))
Tg3000 = t.add_resource(TargetGroup(
    "Tg3000",
    Condition="CreateAlbCond",
    Port=3000, Protocol="HTTP",
    VpcId=Ref(VpcId),
    TargetType="ip",
    TargetGroupAttributes=[TargetGroupAttribute(Key="stickiness.enabled", Value="false")]
))

t.add_resource(Listener(
    "Listener7101",
    Condition="CreateAlbCond",
    LoadBalancerArn=Ref(Alb),
    Port="7101",
    Protocol="HTTP",
    DefaultActions=[AlbAction(Type="forward", TargetGroupArn=Ref(Tg7101))]
))
t.add_resource(Listener(
    "Listener3000",
    Condition="CreateAlbCond",
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
DbPort = GetAtt(DbRes, "Endpoint.Port")  # Postgres default is 5432; we don't make it configurable

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

# -----------------------
# SSM params (examples)
# -----------------------
t.add_resource(SsmParameter(
    "StorageProfilesParam",
    Name="/lakerunner/storage_profiles",
    Type="String",
    Value=Sub(
        "- bucket: ${Bucket}\n  cloud_provider: aws\n  collector_name: lakerunner\n  insecure_tls: false\n  instance_num: 1\n  organization_id: 12340000-0000-4000-8000-000000000000\n  region: ${AWS::Region}\n  use_path_style: true",
        Bucket=Ref(BucketRes)
    ),
    Description="Storage profiles config",
))
t.add_resource(SsmParameter(
    "ApiKeysParam",
    Name="/lakerunner/api_keys",
    Type="String",
    Value="- organization_id: 12340000-0000-4000-8000-000000000000\n  keys:\n    - f70603aa00e6f67999cc66e336134887",
    Description="API keys",
))

# Optional outputs for ALB
t.add_output(Output(
    "AlbDNS",
    Condition="CreateAlbCond",
    Value=GetAtt(Alb, "DNSName"),
    Export=Export(name=Sub("${AWS::StackName}-AlbDNS"))
))
t.add_output(Output(
    "Tg7101Arn",
    Condition="CreateAlbCond",
    Value=Ref(Tg7101),
    Export=Export(name=Sub("${AWS::StackName}-Tg7101Arn"))
))
t.add_output(Output(
    "Tg3000Arn",
    Condition="CreateAlbCond",
    Value=Ref(Tg3000),
    Export=Export(name=Sub("${AWS::StackName}-Tg3000Arn"))
))
t.add_output(Output(
    "TaskSecurityGroupId",
    Value=Ref(TaskSG),
    Export=Export(name=Sub("${AWS::StackName}-TaskSGId"))
))

print(t.to_yaml())
