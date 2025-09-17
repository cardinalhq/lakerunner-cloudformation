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
from troposphere.ecs import Cluster as ECSCluster
from troposphere.s3 import (
    Bucket, LifecycleRule, LifecycleConfiguration,
    NotificationConfiguration, QueueConfigurations,
    S3Key, Filter, Rules
)
from troposphere.sqs import Queue, QueuePolicy
from troposphere.secretsmanager import Secret, GenerateSecretString
from troposphere.rds import DBInstance, DBSubnetGroup
from troposphere.ssm import Parameter as SsmParameter
from troposphere.msk import Cluster, BrokerNodeGroupInfo, EBSStorageInfo, StorageInfo, ClientAuthentication, Tls, Sasl, Scram, EncryptionInfo, EncryptionAtRest, EncryptionInTransit, BatchScramSecret
from troposphere.iam import PolicyType, Role, Policy
from troposphere.awslambda import Function, Code
from troposphere.cloudformation import CustomResource
from troposphere.kms import Key, Alias

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

# MSK parameters
MSKInstanceType = t.add_parameter(Parameter(
    "MSKInstanceType",
    Type="String",
    Default="kafka.t3.small",
    AllowedValues=[
        "kafka.t3.small",
        "kafka.m5.large", "kafka.m5.xlarge", "kafka.m5.2xlarge", "kafka.m5.4xlarge",
        "kafka.m5.8xlarge", "kafka.m5.12xlarge", "kafka.m5.16xlarge", "kafka.m5.24xlarge",
        "kafka.m7g.large", "kafka.m7g.xlarge", "kafka.m7g.2xlarge", "kafka.m7g.4xlarge",
        "kafka.m7g.8xlarge", "kafka.m7g.12xlarge", "kafka.m7g.16xlarge"
    ],
    Description="MSK broker instance type."
))

MSKBrokerNodes = t.add_parameter(Parameter(
    "MSKBrokerNodes",
    Type="Number",
    Default=2,
    MinValue=2,
    MaxValue=15,
    Description="Number of MSK broker nodes. Must be between 2 and 15."
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
                "Label": {"default": "MSK Configuration"},
                "Parameters": ["MSKInstanceType", "MSKBrokerNodes"]
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
            "MSKInstanceType": {"default": "MSK Instance Type"},
            "MSKBrokerNodes": {"default": "Number of MSK Broker Nodes"},
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
    }],
    Tags=[
        {"Key": "Name", "Value": Sub("${AWS::StackName}-task-sg")},
        {"Key": "ManagedBy", "Value": "Lakerunner"},
        {"Key": "Environment", "Value": Ref("AWS::StackName")},
        {"Key": "Component", "Value": "Compute"}
    ]
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

# Security group for MSK cluster
MskSecurityGroup = t.add_resource(SecurityGroup(
    "MSKSecurityGroup",
    GroupDescription="Security group for MSK cluster - grant access by referencing this SG ID",
    VpcId=Ref(VpcId),
    Tags=[
        {"Key": "Name", "Value": Sub("${AWS::StackName}-msk-sg")},
        {"Key": "ManagedBy", "Value": "Lakerunner"},
        {"Key": "Environment", "Value": Ref("AWS::StackName")},
        {"Key": "Component", "Value": "Messaging"}
    ]
))

# Allow ECS tasks to connect to MSK on port 9094 (TLS)
t.add_resource(SecurityGroupIngress(
    "MSKFromTasksSG",
    GroupId=Ref(MskSecurityGroup),
    IpProtocol="tcp",
    FromPort=9094,
    ToPort=9094,
    SourceSecurityGroupId=Ref(TaskSG),
    Description="Kafka TLS from ECS tasks",
))

# Allow ECS tasks to connect to MSK on port 9096 (SASL_SSL)
t.add_resource(SecurityGroupIngress(
    "MSKFromTasksSGSASL",
    GroupId=Ref(MskSecurityGroup),
    IpProtocol="tcp",
    FromPort=9096,
    ToPort=9096,
    SourceSecurityGroupId=Ref(TaskSG),
    Description="Kafka SASL_SSL from ECS tasks",
))

# -----------------------
# ECS cluster (always create)
# -----------------------
ClusterRes = t.add_resource(ECSCluster(
    "Cluster",
    ClusterName=Sub("${AWS::StackName}-cluster"),
    Tags=Tags(
        Name=Sub("${AWS::StackName}-cluster"),
        ManagedBy="Lakerunner",
        Environment=Ref("AWS::StackName"),
        Component="Compute"
    )
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
    Tags=Tags(
        Name=Sub("${AWS::StackName}-ingest-queue"),
        ManagedBy="Lakerunner",
        Environment=Ref("AWS::StackName"),
        Component="Messaging"
    )
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
    Tags=Tags(
        Name=Sub("${AWS::StackName}-ingest-bucket"),
        ManagedBy="Lakerunner",
        Environment=Ref("AWS::StackName"),
        Component="Storage"
    )
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
    DeletionProtection=False,
    Tags=[
        {"Key": "Name", "Value": Sub("${AWS::StackName}-database")},
        {"Key": "ManagedBy", "Value": "Lakerunner"},
        {"Key": "Environment", "Value": Ref("AWS::StackName")},
        {"Key": "Component", "Value": "Database"}
    ]
))

DbEndpoint = GetAtt(DbRes, "Endpoint.Address")
DbPort = GetAtt(DbRes, "Endpoint.Port")

t.add_output(Output("DbEndpoint", Value=DbEndpoint, Export=Export(name=Sub("${AWS::StackName}-DbEndpoint"))))
t.add_output(Output("DbPort", Value=DbPort, Export=Export(name=Sub("${AWS::StackName}-DbPort"))))
t.add_output(Output("DbSecretArnOut", Value=DbSecretArnValue, Export=Export(name=Sub("${AWS::StackName}-DbSecretArn"))))

# -----------------------
# MSK Cluster and associated resources
# -----------------------

# KMS key for MSK SCRAM secrets (required for MSK secret association)
MSKSecretsKey = t.add_resource(Key(
    "MSKSecretsKey",
    Description="KMS key for MSK SASL/SCRAM secrets",
    KeyPolicy={
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "Enable IAM User Permissions",
                "Effect": "Allow",
                "Principal": {"AWS": Sub("arn:aws:iam::${AWS::AccountId}:root")},
                "Action": "kms:*",
                "Resource": "*"
            },
            {
                "Sid": "Allow MSK Service",
                "Effect": "Allow",
                "Principal": {"Service": "kafka.amazonaws.com"},
                "Action": [
                    "kms:Decrypt",
                    "kms:GenerateDataKey"
                ],
                "Resource": "*"
            },
            {
                "Sid": "Allow Secrets Manager",
                "Effect": "Allow",
                "Principal": {"Service": "secretsmanager.amazonaws.com"},
                "Action": [
                    "kms:Decrypt",
                    "kms:GenerateDataKey",
                    "kms:ReEncrypt*"
                ],
                "Resource": "*"
            }
        ]
    },
    Tags=[
        {"Key": "Name", "Value": Sub("${AWS::StackName}-msk-secrets-key")},
        {"Key": "ManagedBy", "Value": "Lakerunner"},
        {"Key": "Environment", "Value": Ref("AWS::StackName")},
        {"Key": "Component", "Value": "Messaging"}
    ]
))

# KMS key alias for easier identification
MSKSecretsKeyAlias = t.add_resource(Alias(
    "MSKSecretsKeyAlias",
    AliasName=Sub("alias/${AWS::StackName}-msk-secrets"),
    TargetKeyId=Ref(MSKSecretsKey)
))

# MSK SASL/SCRAM Credentials Secret
MSKCredentials = t.add_resource(Secret(
    "MSKCredentials",
    Name=Sub("AmazonMSK_${AWS::StackName}"),
    Description="MSK SASL/SCRAM credentials for Kafka authentication",
    KmsKeyId=Ref(MSKSecretsKey),
    GenerateSecretString=GenerateSecretString(
        SecretStringTemplate='{"username": "lakerunner"}',
        GenerateStringKey="password",
        PasswordLength=32,
        ExcludeCharacters='"@/\\'
    ),
    Tags=[
        {"Key": "Name", "Value": Sub("${AWS::StackName}-msk-credentials")},
        {"Key": "ManagedBy", "Value": "Lakerunner"},
        {"Key": "Environment", "Value": Ref("AWS::StackName")},
        {"Key": "Component", "Value": "Messaging"}
    ]
))

# MSK Cluster
MSKCluster = t.add_resource(Cluster(
    "MSKCluster",
    ClusterName=Sub("${AWS::StackName}-msk-cluster"),
    KafkaVersion="3.9.x",
    NumberOfBrokerNodes=Ref(MSKBrokerNodes),
    BrokerNodeGroupInfo=BrokerNodeGroupInfo(
        InstanceType=Ref(MSKInstanceType),
        ClientSubnets=Ref(PrivateSubnets),
        SecurityGroups=[Ref(MskSecurityGroup)],
        StorageInfo=StorageInfo(
            EBSStorageInfo=EBSStorageInfo(
                VolumeSize=100  # Will make this configurable if needed
            )
        )
    ),
    ClientAuthentication=ClientAuthentication(
        Sasl=Sasl(
            Scram=Scram(
                Enabled=True
            )
        )
    ),
    EncryptionInfo=EncryptionInfo(
        EncryptionInTransit=EncryptionInTransit(
            ClientBroker="TLS",
            InCluster=True
        )
    ),
    Tags={
        "Name": Sub("${AWS::StackName}-msk-cluster"),
        "ManagedBy": "Lakerunner",
        "Environment": Ref("AWS::StackName"),
        "Component": "Messaging"
    }
))

# Custom resource to handle MSK SCRAM secret association timing
MSKScramAssociationFunction = t.add_resource(Function(
    "MSKScramAssociationFunction",
    FunctionName=Sub("${AWS::StackName}-msk-scram-association"),
    Runtime="python3.13",
    Handler="index.handler",
    Role=GetAtt("MSKScramAssociationRole", "Arn"),
    Timeout=300,
    Code=Code(
        ZipFile="""
import json
import boto3
import urllib.request
import time

def send_response(event, context, status, data=None, reason=""):
    response = {
        "Status": status,
        "Reason": f"{reason} See CloudWatch Logs: {context.log_stream_name}",
        "PhysicalResourceId": event.get("PhysicalResourceId") or "MSKScramAssociation",
        "StackId": event["StackId"],
        "RequestId": event["RequestId"],
        "LogicalResourceId": event["LogicalResourceId"],
        "Data": data or {}
    }

    body = json.dumps(response).encode()
    req = urllib.request.Request(event["ResponseURL"], data=body, method="PUT")
    req.add_header("content-type", "")
    req.add_header("content-length", str(len(body)))

    try:
        with urllib.request.urlopen(req) as r:
            r.read()
    except Exception as e:
        print(f"Failed to send response: {e}")

def handler(event, context):
    print(f"Event: {json.dumps(event)}")

    try:
        props = event.get("ResourceProperties", {})
        cluster_arn = props["ClusterArn"]
        secret_arns = props["SecretArnList"]

        kafka = boto3.client("kafka")

        if event["RequestType"] == "Delete":
            # Try to disassociate secrets on delete
            try:
                kafka.batch_disassociate_scram_secret(
                    ClusterArn=cluster_arn,
                    SecretArnList=secret_arns
                )
                print("Successfully disassociated SCRAM secrets")
            except Exception as e:
                print(f"Error disassociating SCRAM secrets (ignoring): {e}")

            send_response(event, context, "SUCCESS", {"Message": "Delete completed"})
            return

        # Wait for MSK cluster to be ACTIVE
        max_wait = 20 * 60  # 20 minutes
        start_time = time.time()

        while time.time() - start_time < max_wait:
            try:
                response = kafka.describe_cluster(ClusterArn=cluster_arn)
                state = response["ClusterInfo"]["State"]
                print(f"MSK cluster state: {state}")

                if state == "ACTIVE":
                    break
                elif state in ["FAILED", "DELETING"]:
                    send_response(event, context, "FAILED",
                                reason=f"MSK cluster in failed state: {state}")
                    return

                time.sleep(30)
            except Exception as e:
                print(f"Error checking cluster state: {e}")
                time.sleep(30)
        else:
            send_response(event, context, "FAILED",
                        reason="Timeout waiting for MSK cluster to become ACTIVE")
            return

        # Validate secret naming convention and content
        secrets = boto3.client("secretsmanager")
        for secret_arn in secret_arns:
            secret_name = secret_arn.split('/')[-1]
            if not secret_name.startswith('AmazonMSK_'):
                print(f"WARNING: Secret {secret_name} does not follow AmazonMSK_ naming convention")

            # Verify secret has username and password
            try:
                secret_value = secrets.get_secret_value(SecretId=secret_arn)
                secret_data = json.loads(secret_value['SecretString'])
                if 'username' not in secret_data or 'password' not in secret_data:
                    print(f"WARNING: Secret {secret_name} missing required username/password fields")
                else:
                    print(f"Secret {secret_name} validation passed")
            except Exception as e:
                print(f"WARNING: Could not validate secret {secret_name}: {e}")

        # Associate SCRAM secrets
        print(f"Attempting to associate SCRAM secrets with cluster: {cluster_arn}")
        print(f"Secret ARNs to associate: {secret_arns}")

        kafka.batch_associate_scram_secret(
            ClusterArn=cluster_arn,
            SecretArnList=secret_arns
        )

        print("Successfully associated SCRAM secrets")
        send_response(event, context, "SUCCESS", {"Message": "SCRAM secrets associated"})

    except Exception as e:
        print(f"Error: {e}")
        send_response(event, context, "FAILED", reason=str(e))
"""
    )
))

# IAM role for the MSK SCRAM association function
MSKScramAssociationRole = t.add_resource(Role(
    "MSKScramAssociationRole",
    AssumeRolePolicyDocument={
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "lambda.amazonaws.com"},
                "Action": "sts:AssumeRole"
            }
        ]
    },
    ManagedPolicyArns=[
        "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
    ],
    Policies=[
        Policy(
            PolicyName="MSKScramAssociationPolicy",
            PolicyDocument={
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": [
                            "kafka:DescribeCluster",
                            "kafka:BatchAssociateScramSecret",
                            "kafka:BatchDisassociateScramSecret"
                        ],
                        "Resource": GetAtt(MSKCluster, "Arn")
                    },
                    {
                        "Effect": "Allow",
                        "Action": [
                            "secretsmanager:GetSecretValue"
                        ],
                        "Resource": Ref(MSKCredentials)
                    },
                    {
                        "Effect": "Allow",
                        "Action": [
                            "kms:Decrypt",
                            "kms:GenerateDataKey"
                        ],
                        "Resource": GetAtt(MSKSecretsKey, "Arn")
                    }
                ]
            }
        )
    ]
))

# Custom resource to associate SCRAM secret with MSK cluster
MSKScramSecretAssociation = t.add_resource(CustomResource(
    "MSKScramSecretAssociation",
    ServiceToken=GetAtt(MSKScramAssociationFunction, "Arn"),
    ClusterArn=GetAtt(MSKCluster, "Arn"),
    SecretArnList=[Ref(MSKCredentials)]
))

# MSK outputs
t.add_output(Output(
    "MSKClusterArn",
    Description="MSK cluster ARN",
    Value=GetAtt(MSKCluster, "Arn"),
    Export=Export(name=Sub("${AWS::StackName}-MSKClusterArn"))
))

t.add_output(Output(
    "MSKClusterName",
    Description="MSK cluster name",
    Value=Ref(MSKCluster),
    Export=Export(name=Sub("${AWS::StackName}-MSKClusterName"))
))

t.add_output(Output(
    "MSKCredentialsArn",
    Description="MSK SASL/SCRAM credentials secret ARN",
    Value=Ref(MSKCredentials),
    Export=Export(name=Sub("${AWS::StackName}-MSKCredentialsArn"))
))

t.add_output(Output(
    "MSKSecretsKeyArn",
    Description="KMS key ARN for MSK secrets encryption",
    Value=GetAtt(MSKSecretsKey, "Arn"),
    Export=Export(name=Sub("${AWS::StackName}-MSKSecretsKeyArn"))
))

t.add_output(Output(
    "MSKSecurityGroupId",
    Description="MSK security group ID for granting access from ECS/EKS",
    Value=Ref(MskSecurityGroup),
    Export=Export(name=Sub("${AWS::StackName}-MSKSecurityGroupId"))
))

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

print(t.to_yaml())
