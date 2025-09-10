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
    Select, Not, Tags, Join
)
from troposphere.ec2 import SecurityGroup, SecurityGroupIngress
from troposphere.ecs import Cluster
from troposphere.s3 import (
    Bucket, LifecycleRule, LifecycleConfiguration,
    NotificationConfiguration, QueueConfigurations,
    S3Key, Filter, Rules
)
from troposphere.sqs import Queue, QueuePolicy
from troposphere.secretsmanager import Secret, GenerateSecretString
from troposphere.rds import DBInstance, DBSubnetGroup
from troposphere.ssm import Parameter as SsmParameter
from troposphere.msk import Cluster as MSKCluster, BrokerNodeGroupInfo, EBSStorageInfo, StorageInfo, ClientAuthentication, Tls, Sasl, Scram, EncryptionInfo, EncryptionAtRest, EncryptionInTransit, LoggingInfo, BrokerLogs, CloudWatchLogs
from troposphere.logs import LogGroup

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
    Default="",
    Description="Public subnet IDs (for internet-facing ALB). Required when ALB uses internet-facing scheme. Provide at least two in different AZs."
))

PrivateSubnets = t.add_parameter(Parameter(
    "PrivateSubnets",
    Type="List<AWS::EC2::Subnet::Id>",
    Description="REQUIRED: Private subnet IDs (for RDS/ECS). Provide at least two in different AZs."
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

# MSK Configuration
EnableMSK = t.add_parameter(Parameter(
    "EnableMSK",
    Type="String",
    AllowedValues=["Yes", "No"],
    Default="Yes",
    Description="Enable Amazon MSK cluster for Kafka messaging. Choose 'Yes' to create MSK cluster, 'No' to use SQS only."
))

MSKInstanceType = t.add_parameter(Parameter(
    "MSKInstanceType",
    Type="String",
    Default="kafka.t3.small",
    Description="Instance type for MSK brokers (only used when EnableMSK=Yes)"
))

MSKVolumeSize = t.add_parameter(Parameter(
    "MSKVolumeSize",
    Type="Number",
    Default=100,
    Description="EBS volume size in GB for each MSK broker (only used when EnableMSK=Yes)."
))

MSKBrokerCount = t.add_parameter(Parameter(
    "MSKBrokerCount",
    Type="Number",
    Default=3,
    Description="Number of MSK brokers (must be multiple of AZs). 3=minimum HA, 6=production scale (only used when EnableMSK=Yes)."
))

MSKClientBrokerEncryption = t.add_parameter(Parameter(
    "MSKClientBrokerEncryption",
    Type="String",
    AllowedValues=["TLS", "TLS_PLAINTEXT", "PLAINTEXT"],
    Default="TLS_PLAINTEXT",
    Description="Client-broker encryption: TLS (prod), TLS_PLAINTEXT (dev), PLAINTEXT (dev only) (only used when EnableMSK=Yes)."
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
                "Label": {"default": "Kafka Configuration"},
                "Parameters": ["EnableMSK", "MSKInstanceType", "MSKVolumeSize", "MSKBrokerCount", "MSKClientBrokerEncryption"]
            },
            {
                "Label": {"default": "Configuration Overrides (Advanced)"},
                "Parameters": ["ApiKeysOverride", "StorageProfilesOverride"]
            }
        ],
        "ParameterLabels": {
            "VpcId": {"default": "VPC Id"},
            "PublicSubnets": {"default": "Public Subnets (for ALB internet-facing)"},
            "PrivateSubnets": {"default": "Private Subnets (for ECS/RDS)"},
            "EnableMSK": {"default": "Enable MSK Cluster"},
            "MSKInstanceType": {"default": "MSK Instance Type"},
            "MSKVolumeSize": {"default": "MSK Volume Size (GB)"},
            "MSKBrokerCount": {"default": "Number of MSK Brokers"},
            "MSKClientBrokerEncryption": {"default": "Client-Broker Encryption"},
            "ApiKeysOverride": {"default": "Custom API Keys (YAML)"},
            "StorageProfilesOverride": {"default": "Custom Storage Profiles (YAML)"}
        }
    }
})

# -----------------------
# Conditions
# -----------------------
t.add_condition("HasApiKeysOverride", Not(Equals(Ref(ApiKeysOverride), "")))
t.add_condition("HasStorageProfilesOverride", Not(Equals(Ref(StorageProfilesOverride), "")))
t.add_condition("HasPublicSubnets", Not(Equals(Join(",", Ref(PublicSubnets)), "")))
t.add_condition("CreateMSK", Equals(Ref(EnableMSK), "Yes"))
t.add_condition("UseTLS", Not(Equals(Ref(MSKClientBrokerEncryption), "PLAINTEXT")))

# Helper function to load defaults
def load_defaults():
    """Load default configuration from lakerunner-stack-defaults.yaml"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(script_dir, "..", "lakerunner-stack-defaults.yaml")

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

# MSK Security Group (conditional)
MSKSecurityGroup = t.add_resource(SecurityGroup(
    "MSKSecurityGroup",
    Condition="CreateMSK",
    GroupDescription="Security group for MSK cluster",
    VpcId=Ref(VpcId),
    SecurityGroupIngress=[
        {
            "Description": "Kafka plaintext from ECS tasks",
            "IpProtocol": "tcp",
            "FromPort": 9092,
            "ToPort": 9092,
            "SourceSecurityGroupId": Ref(TaskSG)
        },
        {
            "Description": "Kafka TLS from ECS tasks",
            "IpProtocol": "tcp",
            "FromPort": 9094,
            "ToPort": 9094,
            "SourceSecurityGroupId": Ref(TaskSG)
        },
        {
            "Description": "Kafka SASL/SCRAM from ECS tasks",
            "IpProtocol": "tcp",
            "FromPort": 9096,
            "ToPort": 9096,
            "SourceSecurityGroupId": Ref(TaskSG)
        },
        {
            "Description": "Kafka IAM from ECS tasks",
            "IpProtocol": "tcp",
            "FromPort": 9098,
            "ToPort": 9098,
            "SourceSecurityGroupId": Ref(TaskSG)
        }
    ],
    Tags=Tags(
        Name=Sub("${AWS::StackName}-msk-sg")
    )
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

# Export S3 bucket name for IAM policies in other stacks
t.add_output(Output("BucketName", Value=Ref(BucketRes), Export=Export(name=Sub("${AWS::StackName}-BucketName"))))
t.add_output(Output("BucketArn", Value=GetAtt(BucketRes, "Arn"), Export=Export(name=Sub("${AWS::StackName}-BucketArn"))))

# -----------------------
# MSK Cluster (conditional)
# -----------------------
MSKLogGroup = t.add_resource(LogGroup(
    "MSKLogGroup",
    Condition="CreateMSK",
    LogGroupName=Sub("/aws/msk/${AWS::StackName}"),
    RetentionInDays=7
))

MSKCluster = t.add_resource(MSKCluster(
    "MSKCluster",
    Condition="CreateMSK",
    ClusterName=Sub("${AWS::StackName}-msk"),
    KafkaVersion="3.8.0",
    NumberOfBrokerNodes=Ref(MSKBrokerCount),
    BrokerNodeGroupInfo=BrokerNodeGroupInfo(
        InstanceType=Ref(MSKInstanceType),
        ClientSubnets=Ref(PrivateSubnets),
        SecurityGroups=[Ref(MSKSecurityGroup)],
        StorageInfo=StorageInfo(
            EBSStorageInfo=EBSStorageInfo(
                VolumeSize=Ref(MSKVolumeSize)
            )
        )
    ),
    ClientAuthentication=ClientAuthentication(
        Tls=Tls(Enabled=If("UseTLS", True, False)),
        Sasl=Sasl(
            Scram=Scram(Enabled=True)
        )
    ),
    EncryptionInfo=EncryptionInfo(
        EncryptionAtRest=EncryptionAtRest(
            DataVolumeKMSKeyId="alias/aws/msk"
        ),
        EncryptionInTransit=EncryptionInTransit(
            ClientBroker=Ref(MSKClientBrokerEncryption),
            InCluster=True
        )
    ),
    EnhancedMonitoring="PER_TOPIC_PER_BROKER",
    LoggingInfo=LoggingInfo(
        BrokerLogs=BrokerLogs(
            CloudWatchLogs=CloudWatchLogs(
                Enabled=True,
                LogGroup=Ref(MSKLogGroup)
            )
        )
    ),
    Tags={
        "Name": Sub("${AWS::StackName}-msk"),
        "Environment": "lakerunner"
    }
))

t.add_output(Output(
    "MSKClusterArn",
    Condition="CreateMSK", 
    Description="ARN of the MSK cluster",
    Value=Ref(MSKCluster),
    Export=Export(name=Sub("${AWS::StackName}-MSKClusterArn"))
))

# Note: MSK bootstrap broker strings are not available via CloudFormation
# Only the cluster ARN is available. Bootstrap servers must be retrieved using:
# aws kafka get-bootstrap-brokers --cluster-arn <MSK_CLUSTER_ARN>

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

# Kafka Topics parameter - extract just the topics array that setup.go expects
kafka_topics_array = defaults['kafka_topics']['ensure_topics']
kafka_topics_yaml = yaml.dump(kafka_topics_array, default_flow_style=False)
t.add_resource(SsmParameter(
    "KafkaTopicsParam",
    Name="/lakerunner/kafka_topics",
    Type="String", 
    Value=kafka_topics_yaml,
    Description="Kafka topics configuration array for setup job",
))


# -----------------------
# Outputs (for access in other stacks)
# -----------------------
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
    "PublicSubnetsOut",
    Value=If(
        "HasPublicSubnets",
        Join(",", Ref(PublicSubnets)),
        ""
    ),
    Export=Export(name=Sub("${AWS::StackName}-PublicSubnets"))
))
t.add_output(Output(
    "VpcIdOut",
    Value=Ref(VpcId),
    Export=Export(name=Sub("${AWS::StackName}-VpcId"))
))

# Export whether internet-facing ALB is supported
t.add_output(Output(
    "SupportsInternetFacingAlb",
    Value=If("HasPublicSubnets", "Yes", "No"),
    Export=Export(name=Sub("${AWS::StackName}-SupportsInternetFacingAlb")),
    Description="Whether this CommonInfra stack supports internet-facing ALBs (requires PublicSubnets)"
))

# Export MSK enablement status for other stacks
t.add_output(Output(
    "EnableMSKOut",
    Value=Ref(EnableMSK),
    Export=Export(name=Sub("${AWS::StackName}-EnableMSK")),
    Description="Whether MSK cluster is enabled"
))

print(t.to_yaml())
