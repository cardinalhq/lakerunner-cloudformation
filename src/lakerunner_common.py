#!/usr/bin/env python3
"""Lakerunner Common Infrastructure CloudFormation Template - Part 2

Creates shared infrastructure components needed by both ECS and EKS deployments:
- Uses VPC from Part 1 (Landscape) or existing VPC via dropdown selection  
- RDS database cluster (create or bring-your-own)
- S3 bucket and SQS queue for data processing (create or bring-your-own)
- MSK cluster for streaming (create or bring-your-own)
- Shared security groups and IAM roles
- Application secrets and configuration

Deploy this after Part 1 (Landscape), then choose Part 3a (ECS) or Part 3b (EKS).
"""

import yaml
import os
from troposphere import (
    Template, Parameter, Ref, Equals, Sub, GetAtt, If, Not, And, Or,
    Condition, Output, Export, Tags, Base64, Join
)
from troposphere.rds import (
    DBCluster, DBSubnetGroup, DBClusterParameterGroup
)
from troposphere.s3 import Bucket, BucketPolicy
from troposphere.sqs import Queue, QueuePolicy
from troposphere.ec2 import SecurityGroup, SecurityGroupRule
from troposphere.iam import Role, PolicyType, InstanceProfile
from troposphere.secretsmanager import Secret, GenerateSecretString
from troposphere.ssm import Parameter as SSMParameter


def load_defaults():
    """Load default configuration from YAML file."""
    config_path = os.path.join(os.path.dirname(__file__), '..', 'lakerunner-stack-defaults.yaml')
    with open(config_path, 'r') as f:
        return yaml.safe_load(f)


def standard_tags(resource_name, resource_type):
    """Generate standard tags for all resources."""
    return [
        {"Key": "Name", "Value": Sub(f"${{EnvironmentName}}-{resource_name}")},
        {"Key": "Component", "Value": "CommonInfra"},
        {"Key": "ResourceType", "Value": resource_type},
        {"Key": "ManagedBy", "Value": "Lakerunner"},
        {"Key": "Environment", "Value": Ref("EnvironmentName")},
    ]


# Initialize template
t = Template()
t.set_description("Lakerunner Common Infrastructure: Shared services for ECS and EKS deployments")

# Load defaults
defaults = load_defaults()

# =============================================================================
# VPC Selection Parameters (Dropdowns for better UX)
# =============================================================================

vpc_id = t.add_parameter(
    Parameter(
        "VPCId",
        Type="AWS::EC2::VPC::Id",
        Description="Select VPC for Lakerunner infrastructure (from Landscape or existing)",
    )
)

private_subnet1 = t.add_parameter(
    Parameter(
        "PrivateSubnet1Id",
        Type="AWS::EC2::Subnet::Id",
        Description="Select first private subnet for databases and compute",
    )
)

private_subnet2 = t.add_parameter(
    Parameter(
        "PrivateSubnet2Id",
        Type="AWS::EC2::Subnet::Id",
        Description="Select second private subnet (must be in different AZ)",
    )
)

has_public_subnets = t.add_parameter(
    Parameter(
        "HasPublicSubnets",
        Type="String",
        Default="Yes",
        AllowedValues=["Yes", "No"],
        Description="Will you deploy load balancers that need public subnets?",
    )
)

public_subnet1 = t.add_parameter(
    Parameter(
        "PublicSubnet1Id",
        Type="AWS::EC2::Subnet::Id",
        Description="Select first public subnet (required if HasPublicSubnets=Yes)",
    )
)

public_subnet2 = t.add_parameter(
    Parameter(
        "PublicSubnet2Id",
        Type="AWS::EC2::Subnet::Id", 
        Description="Select second public subnet (required if HasPublicSubnets=Yes)",
    )
)

environment_name = t.add_parameter(
    Parameter(
        "EnvironmentName",
        Type="String",
        Default="lakerunner",
        Description="Environment name for resource naming and tagging",
        AllowedPattern=r"^[a-zA-Z][a-zA-Z0-9-]*$"
    )
)

# =============================================================================
# RDS Configuration
# =============================================================================

create_rds = t.add_parameter(
    Parameter(
        "CreateRDS",
        Type="String",
        Default="Yes",
        AllowedValues=["Yes", "No"],
        Description="Create RDS PostgreSQL cluster or use existing database?",
    )
)

db_instance_class = t.add_parameter(
    Parameter(
        "DBInstanceClass",
        Type="String",
        Default="db.r5.large",
        AllowedValues=["db.t3.medium", "db.r5.large", "db.r5.xlarge", "db.r5.2xlarge"],
        Description="RDS instance class (only used when CreateRDS=Yes)",
    )
)

# BYO RDS parameters
existing_db_endpoint = t.add_parameter(
    Parameter(
        "ExistingDBEndpoint",
        Type="String",
        Default="",
        Description="Existing database endpoint (required when CreateRDS=No)",
    )
)

existing_db_port = t.add_parameter(
    Parameter(
        "ExistingDBPort",
        Type="Number",
        Default=5432,
        Description="Existing database port (required when CreateRDS=No)",
    )
)

existing_db_name = t.add_parameter(
    Parameter(
        "ExistingDBName",
        Type="String",
        Default="lakerunner",
        Description="Existing database name (required when CreateRDS=No)",
    )
)

# =============================================================================
# S3 + SQS Storage Configuration
# =============================================================================

create_storage = t.add_parameter(
    Parameter(
        "CreateStorage",
        Type="String",
        Default="Yes",
        AllowedValues=["Yes", "No"],
        Description="Create S3 bucket and SQS queue or use existing storage?",
    )
)

# BYO Storage parameters
existing_s3_bucket = t.add_parameter(
    Parameter(
        "ExistingS3BucketName",
        Type="String",
        Default="",
        Description="Existing S3 bucket name (required when CreateStorage=No)",
    )
)

existing_sqs_queue = t.add_parameter(
    Parameter(
        "ExistingSQSQueueUrl",
        Type="String",
        Default="",
        Description="Existing SQS queue URL (required when CreateStorage=No)",
    )
)

# =============================================================================
# MSK Configuration  
# =============================================================================

create_msk = t.add_parameter(
    Parameter(
        "CreateMSK",
        Type="String",
        Default="No",
        AllowedValues=["Yes", "No"],
        Description="Create MSK (Managed Kafka) cluster?",
    )
)

# BYO MSK parameters
existing_msk_cluster = t.add_parameter(
    Parameter(
        "ExistingMSKClusterArn",
        Type="String",
        Default="",
        Description="Existing MSK cluster ARN (required when CreateMSK=No but MSK is needed)",
    )
)

# =============================================================================
# Conditions
# =============================================================================

t.add_condition("CreateRDSCondition", Equals(Ref(create_rds), "Yes"))
t.add_condition("CreateStorageCondition", Equals(Ref(create_storage), "Yes"))
t.add_condition("CreateMSKCondition", Equals(Ref(create_msk), "Yes"))
t.add_condition("HasPublicSubnetsCondition", Equals(Ref(has_public_subnets), "Yes"))

# =============================================================================
# Parameter Groups for CloudFormation Console
# =============================================================================

t.set_metadata({
    "AWS::CloudFormation::Interface": {
        "ParameterGroups": [
            {
                "Label": {"default": "Environment Configuration"},
                "Parameters": ["EnvironmentName"]
            },
            {
                "Label": {"default": "VPC Selection (Choose from your existing VPCs/Subnets)"},
                "Parameters": [
                    "VPCId",
                    "PrivateSubnet1Id",
                    "PrivateSubnet2Id",
                    "HasPublicSubnets",
                    "PublicSubnet1Id",
                    "PublicSubnet2Id"
                ]
            },
            {
                "Label": {"default": "Database Configuration"},
                "Parameters": [
                    "CreateRDS",
                    "DBInstanceClass",
                    "ExistingDBEndpoint",
                    "ExistingDBPort",
                    "ExistingDBName"
                ]
            },
            {
                "Label": {"default": "Storage Configuration (S3 + SQS)"},
                "Parameters": [
                    "CreateStorage",
                    "ExistingS3BucketName",
                    "ExistingSQSQueueUrl"
                ]
            },
            {
                "Label": {"default": "Streaming Configuration (MSK)"},
                "Parameters": [
                    "CreateMSK",
                    "ExistingMSKClusterArn"
                ]
            }
        ],
        "ParameterLabels": {
            "VPCId": {"default": "VPC"},
            "PrivateSubnet1Id": {"default": "Private Subnet 1"},
            "PrivateSubnet2Id": {"default": "Private Subnet 2"},
            "HasPublicSubnets": {"default": "Use public subnets for load balancers?"},
            "PublicSubnet1Id": {"default": "Public Subnet 1"},
            "PublicSubnet2Id": {"default": "Public Subnet 2"},
            "CreateRDS": {"default": "Create RDS database?"},
            "DBInstanceClass": {"default": "Database instance size"},
            "CreateStorage": {"default": "Create S3 + SQS storage?"},
            "CreateMSK": {"default": "Create MSK cluster?"}
        }
    }
})

# =============================================================================
# Security Groups
# =============================================================================

# Database Security Group
db_security_group = t.add_resource(SecurityGroup(
    "DatabaseSecurityGroup",
    GroupDescription="Security group for RDS database",
    VpcId=Ref(vpc_id),
    SecurityGroupIngress=[
        SecurityGroupRule(
            IpProtocol="tcp",
            FromPort=5432,
            ToPort=5432,
            SourceSecurityGroupId=Ref("ComputeSecurityGroup"),
            Description="PostgreSQL access from compute resources"
        )
    ],
    Tags=standard_tags("db-sg", "SecurityGroup")
))

# Compute Security Group (for ECS/EKS)
compute_security_group = t.add_resource(SecurityGroup(
    "ComputeSecurityGroup", 
    GroupDescription="Security group for compute resources (ECS tasks, EKS nodes)",
    VpcId=Ref(vpc_id),
    SecurityGroupIngress=[
        SecurityGroupRule(
            IpProtocol="tcp",
            FromPort=80,
            ToPort=80,
            CidrIp="10.0.0.0/8",
            Description="HTTP from private networks"
        ),
        SecurityGroupRule(
            IpProtocol="tcp",
            FromPort=443,
            ToPort=443,
            CidrIp="10.0.0.0/8", 
            Description="HTTPS from private networks"
        )
    ],
    SecurityGroupEgress=[
        SecurityGroupRule(
            IpProtocol="-1",
            CidrIp="0.0.0.0/0",
            Description="All outbound traffic"
        )
    ],
    Tags=standard_tags("compute-sg", "SecurityGroup")
))

# =============================================================================
# RDS Database (Conditional)
# =============================================================================

# DB Subnet Group
db_subnet_group = t.add_resource(DBSubnetGroup(
    "DBSubnetGroup",
    Condition="CreateRDSCondition",
    DBSubnetGroupDescription="Subnet group for RDS database",
    SubnetIds=[Ref(private_subnet1), Ref(private_subnet2)],
    Tags=standard_tags("db-subnet-group", "DBSubnetGroup")
))

# Database master password secret
db_secret = t.add_resource(Secret(
    "DatabaseSecret",
    Condition="CreateRDSCondition",
    Description="RDS master password",
    GenerateSecretString=GenerateSecretString(
        SecretStringTemplate='{"username": "postgres"}',
        GenerateStringKey="password",
        PasswordLength=32,
        ExcludeCharacters='"@/\\'
    )
))

# RDS Cluster (Aurora Serverless v2)
rds_cluster = t.add_resource(DBCluster(
    "RDSCluster",
    Condition="CreateRDSCondition",
    Engine="aurora-postgresql",
    EngineMode="provisioned",
    EngineVersion="15.4",
    DatabaseName="lakerunner",
    MasterUsername=Sub("{{resolve:secretsmanager:${DatabaseSecret}:SecretString:username}}"),
    MasterUserPassword=Sub("{{resolve:secretsmanager:${DatabaseSecret}:SecretString:password}}"),
    DBSubnetGroupName=Ref(db_subnet_group),
    VpcSecurityGroupIds=[Ref(db_security_group)],
    BackupRetentionPeriod=7,
    PreferredBackupWindow="03:00-04:00",
    PreferredMaintenanceWindow="sun:04:00-sun:05:00",
    StorageEncrypted=True
))

# =============================================================================
# S3 + SQS Storage (Conditional)
# =============================================================================

# S3 Bucket for data processing (simplified for now)
s3_bucket = t.add_resource(Bucket(
    "S3Bucket",
    Condition="CreateStorageCondition",
    BucketName=Sub("${EnvironmentName}-lakerunner-${AWS::AccountId}-${AWS::Region}")
))

# SQS Queue for S3 event notifications
sqs_queue = t.add_resource(Queue(
    "SQSQueue",
    Condition="CreateStorageCondition",
    QueueName=Sub("${EnvironmentName}-lakerunner-queue"),
    VisibilityTimeout=300,
    MessageRetentionPeriod=1209600  # 14 days
))

# =============================================================================
# Outputs for Part 3 (ECS/EKS)
# =============================================================================

# VPC Information
t.add_output(Output(
    "VPCId",
    Description="Selected VPC ID",
    Value=Ref(vpc_id),
    Export=Export(Sub("${AWS::StackName}-VPCId"))
))

t.add_output(Output(
    "PrivateSubnets",
    Description="Private subnet IDs",
    Value=Sub("${PrivateSubnet1Id},${PrivateSubnet2Id}"),
    Export=Export(Sub("${AWS::StackName}-PrivateSubnets"))
))

t.add_output(Output(
    "PublicSubnets",
    Condition="HasPublicSubnetsCondition",
    Description="Public subnet IDs",
    Value=Sub("${PublicSubnet1Id},${PublicSubnet2Id}"),
    Export=Export(Sub("${AWS::StackName}-PublicSubnets"))
))

# Security Groups
t.add_output(Output(
    "ComputeSecurityGroupId",
    Description="Security group for compute resources",
    Value=Ref(compute_security_group),
    Export=Export(Sub("${AWS::StackName}-ComputeSecurityGroupId"))
))

t.add_output(Output(
    "DatabaseSecurityGroupId",
    Description="Security group for database access",
    Value=Ref(db_security_group),
    Export=Export(Sub("${AWS::StackName}-DatabaseSecurityGroupId"))
))

# Database Information
t.add_output(Output(
    "DatabaseEndpoint",
    Description="Database endpoint (created or existing)",
    Value=If(
        "CreateRDSCondition",
        GetAtt(rds_cluster, "Endpoint.Address"),
        Ref(existing_db_endpoint)
    ),
    Export=Export(Sub("${AWS::StackName}-DatabaseEndpoint"))
))

t.add_output(Output(
    "DatabasePort",
    Description="Database port",
    Value=If(
        "CreateRDSCondition",
        GetAtt(rds_cluster, "Endpoint.Port"),
        Ref(existing_db_port)
    ),
    Export=Export(Sub("${AWS::StackName}-DatabasePort"))
))

t.add_output(Output(
    "DatabaseName",
    Description="Database name",
    Value=If(
        "CreateRDSCondition",
        "lakerunner",
        Ref(existing_db_name)
    ),
    Export=Export(Sub("${AWS::StackName}-DatabaseName"))
))

t.add_output(Output(
    "DatabaseSecretArn",
    Condition="CreateRDSCondition",
    Description="Database credentials secret ARN",
    Value=Ref(db_secret),
    Export=Export(Sub("${AWS::StackName}-DatabaseSecretArn"))
))

# Storage Information
t.add_output(Output(
    "S3BucketName",
    Description="S3 bucket name (created or existing)",
    Value=If(
        "CreateStorageCondition",
        Ref(s3_bucket),
        Ref(existing_s3_bucket)
    ),
    Export=Export(Sub("${AWS::StackName}-S3BucketName"))
))

t.add_output(Output(
    "S3BucketArn",
    Condition="CreateStorageCondition",
    Description="S3 bucket ARN",
    Value=GetAtt(s3_bucket, "Arn"),
    Export=Export(Sub("${AWS::StackName}-S3BucketArn"))
))

t.add_output(Output(
    "SQSQueueUrl",
    Description="SQS queue URL (created or existing)",
    Value=If(
        "CreateStorageCondition",
        Ref(sqs_queue),
        Ref(existing_sqs_queue)
    ),
    Export=Export(Sub("${AWS::StackName}-SQSQueueUrl"))
))

t.add_output(Output(
    "EnvironmentName",
    Description="Environment name for resource naming",
    Value=Ref(environment_name),
    Export=Export(Sub("${AWS::StackName}-EnvironmentName"))
))


if __name__ == "__main__":
    print(t.to_yaml())