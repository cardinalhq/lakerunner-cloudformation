#!/usr/bin/env python3
"""MSK stack for Lakerunner: Amazon MSK (Kafka) cluster."""

from troposphere import (
    Template, Parameter, Ref, Sub, If, Equals, Not, Export, Output, GetAtt, Select, Split
)
from troposphere.msk import Cluster, BrokerNodeGroupInfo, EBSStorageInfo, StorageInfo, ClientAuthentication, Sasl, Scram, EncryptionInfo, EncryptionInTransit
from troposphere.ec2 import SecurityGroup
from troposphere.iam import PolicyType, Role, Policy
from troposphere.secretsmanager import Secret, GenerateSecretString
from troposphere.kms import Key, Alias
from troposphere.awslambda import Function, Code
from troposphere.cloudformation import CustomResource


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
# Note: This security group initially has no ingress rules for better security.
# ECS/EKS clusters should reference this security group ID and add specific
# ingress rules only for the ports they need (typically 9094 for TLS).
MskSecurityGroup = t.add_resource(SecurityGroup(
    "MSKSecurityGroup",
    GroupDescription="Security group for MSK cluster - grant access by referencing this SG ID",
    VpcId=Ref(VpcId),
    SecurityGroupIngress=[
        # Commented out broad CIDR rules - use security group references instead
        # SecurityGroupRule(
        #     IpProtocol="tcp",
        #     FromPort=9094,
        #     ToPort=9094,
        #     CidrIp="10.0.0.0/8",  # Too broad - use SourceSecurityGroupId instead
        #     Description="Kafka TLS"
        # )
    ],
    Tags=[
        {"Key": "Name", "Value": Sub("${AWS::StackName}-msk-sg")},
        {"Key": "ManagedBy", "Value": "Lakerunner"}
    ]
))

# -----------------------
# KMS Key for MSK SCRAM Secrets
# -----------------------
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
        {"Key": "Component", "Value": "MSK"}
    ]
))

MSKSecretsKeyAlias = t.add_resource(Alias(
    "MSKSecretsKeyAlias",
    AliasName=Sub("alias/${AWS::StackName}-msk-secrets"),
    TargetKeyId=Ref(MSKSecretsKey)
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
# Custom Lambda Function for SCRAM Association
# -----------------------
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

t.add_output(Output(
    "MSKSecurityGroupId",
    Description="MSK security group ID for granting access from ECS/EKS",
    Value=Ref(MskSecurityGroup),
    Export=Export(name=Sub("${AWS::StackName}-MSKSecurityGroupId"))
))

t.add_output(Output(
    "MSKSecretsKeyArn",
    Description="KMS key ARN for MSK secrets encryption",
    Value=GetAtt(MSKSecretsKey, "Arn"),
    Export=Export(name=Sub("${AWS::StackName}-MSKSecretsKeyArn"))
))

# Note: Bootstrap servers are not available as CloudFormation attributes
# They need to be retrieved using AWS CLI or SDK after cluster creation

# SASL/SCRAM authentication is automatically configured:
# - Secret contains username "lakerunner" and auto-generated password
# - Secret is automatically associated with the MSK cluster
# - Use GetBootstrapBrokers API to get connection endpoints

print(t.to_yaml())
