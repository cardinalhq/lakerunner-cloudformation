#!/usr/bin/env python3
"""MSK stack for Lakerunner: Amazon MSK (Kafka) cluster."""

import yaml
import os
from troposphere import (
    Template, Parameter, Ref, Sub, If, Equals, Not, Export, Output, GetAtt, Select, Split
)
from troposphere.msk import Cluster, BrokerNodeGroupInfo, EBSStorageInfo, StorageInfo, ClientAuthentication, Tls, Sasl, Scram, EncryptionInfo, EncryptionAtRest, EncryptionInTransit, BatchScramSecret
from troposphere.ec2 import SecurityGroup, SecurityGroupRule
from troposphere.iam import PolicyType, Role, Policy
from troposphere.secretsmanager import Secret, GenerateSecretString


t = Template()
t.set_description("Amazon MSK (Kafka) cluster for Lakerunner.")

# -----------------------
# Parameters
# -----------------------
VpcId = t.add_parameter(Parameter(
    "VpcId",
    Type="AWS::EC2::VPC::Id",
    Description="REQUIRED: VPC ID where MSK cluster will be deployed.",
))

PrivateSubnets = t.add_parameter(Parameter(
    "PrivateSubnets",
    Type="List<AWS::EC2::Subnet::Id>",
    Description="REQUIRED: Private subnet IDs for the MSK cluster (minimum 2, maximum 3).",
))

ExistingTaskRoleArn = t.add_parameter(Parameter(
    "ExistingTaskRoleArn",
    Type="String",
    Default="",
    Description="OPTIONAL: Existing task role ARN to attach MSK permissions to. Leave blank to create a new role.",
))

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
    Description="MSK broker instance type.",
))

MSKBrokerNodes = t.add_parameter(Parameter(
    "MSKBrokerNodes",
    Type="Number",
    Default=2,
    MinValue=2,
    MaxValue=15,
    Description="Number of MSK broker nodes. Must be between 2 and 15.",
))

# -----------------------
# Conditions
# -----------------------
t.add_condition("CreateTaskRole", Equals(Ref(ExistingTaskRoleArn), ""))
t.add_condition("UseExistingTaskRole", Not(Equals(Ref(ExistingTaskRoleArn), "")))

# -----------------------
# Task Role for MSK Access (conditional)
# -----------------------
MSKTaskRole = t.add_resource(Role(
    "MSKTaskRole",
    Condition="CreateTaskRole",
    RoleName=Sub("${AWS::StackName}-msk-task-role"),
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
            PolicyName="BaseECSTaskPolicy",
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
                    }
                ]
            }
        )
    ]
))

# -----------------------
# Security Group for MSK
# -----------------------
MskSecurityGroup = t.add_resource(SecurityGroup(
    "MSKSecurityGroup",
    GroupDescription="Security group for MSK cluster",
    VpcId=Ref(VpcId),
    SecurityGroupIngress=[
        SecurityGroupRule(
            IpProtocol="tcp",
            FromPort=9092,
            ToPort=9092,
            CidrIp="10.0.0.0/8",  # Allow from private networks
            Description="Kafka plaintext"
        ),
        SecurityGroupRule(
            IpProtocol="tcp", 
            FromPort=9094,
            ToPort=9094,
            CidrIp="10.0.0.0/8",  # Allow from private networks
            Description="Kafka TLS"
        ),
        SecurityGroupRule(
            IpProtocol="tcp",
            FromPort=2181,
            ToPort=2181,
            CidrIp="10.0.0.0/8",  # Allow from private networks
            Description="ZooKeeper"
        )
    ],
    Tags=[
        {"Key": "Name", "Value": Sub("${AWS::StackName}-msk-sg")},
        {"Key": "ManagedBy", "Value": "Lakerunner"}
    ]
))

# -----------------------
# MSK Cluster
# -----------------------
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
                VolumeSize=100  # 100 GB per broker
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
            ClientBroker="TLS",  # TLS encryption for client connections
            InCluster=True
        )
    ),
    Tags={
        "Name": Sub("${AWS::StackName}-msk-cluster"),
        "ManagedBy": "Lakerunner",
        "Environment": Ref("AWS::StackName")
    }
))

# -----------------------
# MSK SASL/SCRAM Credentials Secret
# -----------------------
MSKCredentials = t.add_resource(Secret(
    "MSKCredentials",
    Name=Sub("AmazonMSK_${AWS::StackName}"),
    Description="MSK SASL/SCRAM credentials for Kafka authentication",
    GenerateSecretString=GenerateSecretString(
        SecretStringTemplate='{"username": "lakerunner"}',
        GenerateStringKey="password",
        PasswordLength=32,
        ExcludeCharacters='"@/\\'
    ),
    Tags=[
        {"Key": "Name", "Value": Sub("${AWS::StackName}-msk-credentials")},
        {"Key": "ManagedBy", "Value": "Lakerunner"},
        {"Key": "Component", "Value": "MSK"}
    ]
))

# -----------------------
# MSK Service Role for Secrets Manager Access
# -----------------------
MSKServiceRole = t.add_resource(Role(
    "MSKServiceRole",
    RoleName=Sub("${AWS::StackName}-msk-service-role"),
    AssumeRolePolicyDocument={
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "kafka.amazonaws.com"},
            "Action": "sts:AssumeRole"
        }]
    },
    Policies=[
        Policy(
            PolicyName="MSKSecretsManagerAccess",
            PolicyDocument={
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": [
                            "secretsmanager:GetSecretValue",
                            "secretsmanager:DescribeSecret"
                        ],
                        "Resource": Ref(MSKCredentials)
                    }
                ]
            }
        )
    ]
))

# -----------------------
# Associate SCRAM Secret with MSK Cluster
# -----------------------
MSKScramSecretAssociation = t.add_resource(BatchScramSecret(
    "MSKScramSecretAssociation",
    ClusterArn=GetAtt(MSKCluster, "Arn"),
    SecretArnList=[Ref(MSKCredentials)]
))

# -----------------------
# IAM Policy for Task Role (MSK permissions)
# -----------------------
t.add_resource(PolicyType(
    "MSKTaskPolicy",
    PolicyName="MSKAccess",
    Roles=[If(
        "UseExistingTaskRole",
        Select(1, Split("/", Ref(ExistingTaskRoleArn))),  # Extract role name from existing ARN
        Ref(MSKTaskRole)  # Use created role name directly
    )],
    PolicyDocument={
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "kafka:DescribeCluster",
                    "kafka:DescribeClusterV2",
                    "kafka:GetBootstrapBrokers",
                    "kafka-cluster:Connect",
                    "kafka-cluster:AlterCluster",
                    "kafka-cluster:DescribeCluster"
                ],
                "Resource": GetAtt(MSKCluster, "Arn")
            },
            {
                "Effect": "Allow",
                "Action": [
                    "kafka-cluster:*Topic*",
                    "kafka-cluster:WriteData",
                    "kafka-cluster:ReadData"
                ],
                "Resource": Sub("${MSKClusterArn}/*", MSKClusterArn=GetAtt(MSKCluster, "Arn"))
            },
            {
                "Effect": "Allow",
                "Action": [
                    "kafka-cluster:AlterGroup",
                    "kafka-cluster:DescribeGroup"
                ],
                "Resource": Sub("${MSKClusterArn}/*", MSKClusterArn=GetAtt(MSKCluster, "Arn"))
            },
            {
                "Effect": "Allow",
                "Action": [
                    "secretsmanager:GetSecretValue",
                    "secretsmanager:DescribeSecret"
                ],
                "Resource": Ref(MSKCredentials)
            }
        ]
    }
))

# -----------------------
# Outputs
# -----------------------
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
    "TaskRoleArn",
    Description="Task role ARN for MSK access (created or existing)",
    Value=If(
        "UseExistingTaskRole",
        Ref(ExistingTaskRoleArn),
        GetAtt(MSKTaskRole, "Arn")
    ),
    Export=Export(name=Sub("${AWS::StackName}-TaskRoleArn"))
))

t.add_output(Output(
    "MSKCredentialsArn",
    Description="MSK SASL/SCRAM credentials secret ARN",
    Value=Ref(MSKCredentials),
    Export=Export(name=Sub("${AWS::StackName}-MSKCredentialsArn"))
))

# Note: Bootstrap servers are not available as CloudFormation attributes
# They need to be retrieved using AWS CLI or SDK after cluster creation

# SASL/SCRAM authentication is automatically configured:
# - Secret contains username "lakerunner" and auto-generated password
# - Secret is automatically associated with the MSK cluster
# - Use GetBootstrapBrokers API to get connection endpoints

print(t.to_yaml())