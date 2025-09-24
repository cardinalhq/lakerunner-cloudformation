#!/usr/bin/env python3
"""Lakerunner root CloudFormation template.

This template provides a single-stack deployment experience with modular
create-or-bring-your-own options for each major component.
"""

import yaml
import os
from troposphere import (
    Template, Parameter, Ref, Equals, Sub, GetAtt, If, Not, And,
    Output, Export, Tags, Select, Split
)
from troposphere.cloudformation import Stack


def load_defaults():
    """Load default configuration from YAML file."""
    config_path = os.path.join(os.path.dirname(__file__), '..', 'lakerunner-stack-defaults.yaml')
    with open(config_path, 'r') as f:
        return yaml.safe_load(f)


def create_byo_template():
    """Create a version of the template optimized for BYO VPC with dropdown lists."""
    # For BYO-focused template, use dropdown parameter types
    # This would be a separate template file: lakerunner-root-byo.py
    pass


# Initialize template
TEMPLATE_DESCRIPTION = (
    "Lakerunner infrastructure deployment with modular create-or-bring-your-own options"
)

t = Template()
t.set_description(TEMPLATE_DESCRIPTION)

# Load defaults
defaults = load_defaults()

# Template base URL parameter (required for nested stacks)
base_url = t.add_parameter(
    Parameter(
        "TemplateBaseUrl",
        Type="String",
        Description="Base URL where nested templates are stored (e.g., https://s3.amazonaws.com/bucket/templates/)",
    )
)

# =============================================================================
# Infrastructure Selection
# The root template orchestrates deployment using existing infrastructure
# =============================================================================

# VPC Selection Parameters (always required)
vpc_id = t.add_parameter(
    Parameter(
        "VPCId",
        Type="AWS::EC2::VPC::Id",
        Description="VPC ID to use for deployment (from VPC stack or existing VPC).",
    )
)

private_subnet1 = t.add_parameter(
    Parameter(
        "PrivateSubnet1Id",
        Type="AWS::EC2::Subnet::Id",
        Description="First private subnet ID (from VPC stack or existing subnet).",
    )
)

private_subnet2 = t.add_parameter(
    Parameter(
        "PrivateSubnet2Id",
        Type="AWS::EC2::Subnet::Id",
        Description="Second private subnet ID (from VPC stack or existing subnet).",
    )
)

public_subnet1 = t.add_parameter(
    Parameter(
        "PublicSubnet1Id",
        Type="String",
        Default="",
        Description="First public subnet ID (from VPC stack or existing subnet). Optional.",
    )
)

public_subnet2 = t.add_parameter(
    Parameter(
        "PublicSubnet2Id",
        Type="String",
        Default="",
        Description="Second public subnet ID (from VPC stack or existing subnet). Optional.",
    )
)

# =============================================================================
# Infrastructure Creation Options
# =============================================================================

create_s3 = t.add_parameter(
    Parameter(
        "CreateS3Storage",
        Type="String",
        Default="Yes",
        AllowedValues=["Yes", "No"],
        Description="Create S3 bucket and SQS queue for data ingestion?",
    )
)

create_rds = t.add_parameter(
    Parameter(
        "CreateRDS",
        Type="String",
        Default="Yes",
        AllowedValues=["Yes", "No"],
        Description="Create Aurora PostgreSQL database?",
    )
)

create_ecs_infra = t.add_parameter(
    Parameter(
        "CreateECSInfrastructure",
        Type="String",
        Default="Yes",
        AllowedValues=["Yes", "No"],
        Description="Create ECS cluster and infrastructure?",
    )
)

create_ecs_services = t.add_parameter(
    Parameter(
        "CreateECSServices",
        Type="String",
        Default="Yes",
        AllowedValues=["Yes", "No"],
        Description="Deploy ECS services for Lakerunner?",
    )
)

create_ecs_collector = t.add_parameter(
    Parameter(
        "CreateECSCollector",
        Type="String",
        Default="Yes",
        AllowedValues=["Yes", "No"],
        Description="Deploy OTEL Collector service?",
    )
)

create_ecs_grafana = t.add_parameter(
    Parameter(
        "CreateECSGrafana",
        Type="String",
        Default="No",
        AllowedValues=["Yes", "No"],
        Description="Deploy Grafana dashboard?",
    )
)

create_msk = t.add_parameter(
    Parameter(
        "CreateMSK",
        Type="String",
        Default="Yes",
        AllowedValues=["Yes", "No"],
        Description="Create Amazon MSK (Kafka) cluster?",
    )
)

# BYO Resource Parameters (when Create=No)
existing_bucket_arn = t.add_parameter(
    Parameter(
        "ExistingBucketArn",
        Type="String",
        Default="",
        Description="Existing S3 bucket ARN. Required when CreateS3Storage=No.",
    )
)

existing_db_endpoint = t.add_parameter(
    Parameter(
        "ExistingDatabaseEndpoint",
        Type="String",
        Default="",
        Description="Existing database endpoint. Required when CreateRDS=No.",
    )
)

existing_db_secret_arn = t.add_parameter(
    Parameter(
        "ExistingDatabaseSecretArn",
        Type="String",
        Default="",
        Description="Existing database secret ARN. Required when CreateRDS=No.",
    )
)

# BYO Task Role (when any component is BYO)
existing_task_role_arn = t.add_parameter(
    Parameter(
        "ExistingTaskRoleArn",
        Type="String",
        Default="",
        Description="Existing task role ARN with permissions for BYO resources. Required when using any existing resources.",
    )
)

existing_msk_cluster_arn = t.add_parameter(
    Parameter(
        "ExistingMSKClusterArn",
        Type="String",
        Default="",
        Description="Existing MSK cluster ARN. Required when CreateMSK=No.",
    )
)

# BYO ECS Resources
existing_cluster_arn = t.add_parameter(
    Parameter(
        "ExistingClusterArn",
        Type="String",
        Default="",
        Description="Existing ECS cluster ARN. Required when CreateECSInfrastructure=No and CreateECSServices=Yes.",
    )
)

existing_security_group_id = t.add_parameter(
    Parameter(
        "ExistingSecurityGroupId",
        Type="String",
        Default="",
        Description="Existing security group ID for ECS tasks. Required when CreateECSInfrastructure=No and CreateECSServices=Yes.",
    )
)


alb_scheme = t.add_parameter(
    Parameter(
        "AlbScheme",
        Type="String",
        Default="internal",
        AllowedValues=["internet-facing", "internal"],
        Description="Load balancer scheme: 'internet-facing' for external access or 'internal' for internal access only.",
    )
)

msk_instance_type = t.add_parameter(
    Parameter(
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
    )
)

msk_broker_nodes = t.add_parameter(
    Parameter(
        "MSKBrokerNodes",
        Type="Number",
        Default=2,
        MinValue=2,
        MaxValue=15,
        Description="Number of MSK broker nodes. Must be between 2 and 15.",
    )
)

db_instance_class = t.add_parameter(
    Parameter(
        "DbInstanceClass",
        Type="String",
        Default="db.r6g.large",
        AllowedValues=[
            "db.r6g.large", "db.r6g.xlarge", "db.r6g.2xlarge", "db.r6g.4xlarge",
            "db.r6g.8xlarge", "db.r6g.12xlarge", "db.r6g.16xlarge"
        ],
        Description="RDS instance class.",
    )
)

# =============================================================================
# Conditions
# =============================================================================

t.add_condition("CreateS3StorageCondition", Equals(Ref(create_s3), "Yes"))
t.add_condition("CreateRDSCondition", Equals(Ref(create_rds), "Yes"))
t.add_condition("CreateECSInfraCondition", Equals(Ref(create_ecs_infra), "Yes"))

# ECS-dependent services require both ECS infrastructure AND the service to be enabled
t.add_condition("CreateECSServicesCondition", And(
    Equals(Ref(create_ecs_infra), "Yes"),
    Equals(Ref(create_ecs_services), "Yes")
))
t.add_condition("CreateECSCollectorCondition", And(
    Equals(Ref(create_ecs_infra), "Yes"),
    Equals(Ref(create_ecs_collector), "Yes")
))
t.add_condition("CreateECSGrafanaCondition", And(
    Equals(Ref(create_ecs_infra), "Yes"),
    Equals(Ref(create_ecs_grafana), "Yes")
))
t.add_condition("CreateMSKCondition", Equals(Ref(create_msk), "Yes"))
t.add_condition("HasPublicSubnetsCondition", And(
    Not(Equals(Ref(public_subnet1), "")),
    Not(Equals(Ref(public_subnet2), ""))
))

# =============================================================================
# Parameter Groups (for CloudFormation console organization)
# =============================================================================

t.set_metadata({
    "AWS::CloudFormation::Interface": {
        "ParameterGroups": [
            {
                "Label": {"default": "Template Configuration"},
                "Parameters": ["TemplateBaseUrl"]
            },
            {
                "Label": {"default": "Infrastructure Selection"},
                "Parameters": [
                    "VPCId",
                    "PrivateSubnet1Id",
                    "PrivateSubnet2Id",
                    "PublicSubnet1Id",
                    "PublicSubnet2Id"
                ]
            },
            {
                "Label": {"default": "Infrastructure Creation Options"},
                "Parameters": [
                    "CreateS3Storage",
                    "CreateRDS",
                    "DbInstanceClass",
                    "CreateMSK",
                    "MSKInstanceType",
                    "MSKBrokerNodes"
                ]
            },
            {
                "Label": {"default": "Service Deployment Options"},
                "Parameters": [
                    "CreateECSInfrastructure",
                    "CreateECSServices",
                    "CreateECSCollector",
                    "CreateECSGrafana"
                    # TODO: Add "CreateEKS" when EKS template is created
                ]
            },
            {
                "Label": {"default": "Existing Resources (BYO)"},
                "Parameters": [
                    "ExistingBucketArn",
                    "ExistingDatabaseEndpoint",
                    "ExistingDatabaseSecretArn",
                    "ExistingTaskRoleArn",
                    "ExistingMSKClusterArn",
                    "ExistingClusterArn",
                    "ExistingSecurityGroupId",
                    "AlbScheme"
                ]
            }
        ],
        "ParameterLabels": {
            "VPCId": {"default": "VPC ID"},
            "PrivateSubnet1Id": {"default": "Private Subnet 1 ID"},
            "PrivateSubnet2Id": {"default": "Private Subnet 2 ID"},
            "PublicSubnet1Id": {"default": "Public Subnet 1 ID (optional)"},
            "PublicSubnet2Id": {"default": "Public Subnet 2 ID (optional)"},
            "CreateS3Storage": {"default": "Create S3 Storage?"},
            "CreateRDS": {"default": "Create RDS Database?"},
            "CreateECSInfrastructure": {"default": "Create ECS Infrastructure?"},
            "CreateECSServices": {"default": "Deploy ECS Services?"},
            "CreateECSCollector": {"default": "Deploy OTEL Collector?"},
            "CreateECSGrafana": {"default": "Deploy Grafana Dashboard?"},
            "CreateMSK": {"default": "Create MSK Kafka?"},
            "MSKInstanceType": {"default": "MSK Instance Type"},
            "MSKBrokerNodes": {"default": "MSK Broker Nodes"},
            "DbInstanceClass": {"default": "RDS Instance Class"},
            # "CreateEKS": {"default": "Deploy EKS Services?"},
            "ExistingBucketArn": {"default": "Existing Bucket ARN"},
            "ExistingDatabaseEndpoint": {"default": "Existing DB Endpoint"},
            "ExistingDatabaseSecretArn": {"default": "Existing DB Secret ARN"},
            "ExistingTaskRoleArn": {"default": "Existing Task Role ARN"},
            "ExistingMSKClusterArn": {"default": "Existing MSK Cluster ARN"},
            "ExistingClusterArn": {"default": "Existing ECS Cluster ARN"},
            "ExistingSecurityGroupId": {"default": "Existing Security Group ID"},
            "AlbScheme": {"default": "ALB Scheme"}
        }
    }
})

# =============================================================================
# Nested Stacks
# =============================================================================

# ECS Infrastructure Stack (conditional)
ecs_stack = t.add_resource(
    Stack(
        "ECSStack",
        Condition="CreateECSInfraCondition",
        TemplateURL=Sub("${TemplateBaseUrl}/lakerunner-ecs.yaml"),
        Parameters={
            "VpcId": Ref(vpc_id),
        },
        Tags=Tags(
            Component="ECS",
            ManagedBy="Lakerunner",
            Environment=Ref("AWS::StackName")
        )
    )
)

# S3 + SQS Storage Stack (conditional)
storage_stack = t.add_resource(
    Stack(
        "StorageStack",
        Condition="CreateS3StorageCondition",
        TemplateURL=Sub("${TemplateBaseUrl}/lakerunner-s3.yaml"),
        Parameters={
            "ExistingTaskRoleArn": Ref(existing_task_role_arn)
        },
        Tags=Tags(
            Component="Storage",
            ManagedBy="Lakerunner",
            Environment=Ref("AWS::StackName")
        )
    )
)

# RDS Stack (conditional) - now standalone
rds_stack = t.add_resource(
    Stack(
        "RDSStack",
        Condition="CreateRDSCondition",
        TemplateURL=Sub("${TemplateBaseUrl}/lakerunner-rds.yaml"),
        Parameters={
            "VpcId": Ref(vpc_id),
            "PrivateSubnets": Sub("${PrivateSubnet1Id},${PrivateSubnet2Id}"),
            "ExistingTaskRoleArn": Ref(existing_task_role_arn),
            "DbInstanceClass": Ref(db_instance_class)
        },
        Tags=Tags(
            Component="Database",
            ManagedBy="Lakerunner",
            Environment=Ref("AWS::StackName")
        )
    )
)

# MSK Stack (conditional)
msk_stack = t.add_resource(
    Stack(
        "MSKStack",
        Condition="CreateMSKCondition",
        TemplateURL=Sub("${TemplateBaseUrl}/lakerunner-msk.yaml"),
        Parameters={
            "VpcId": Ref(vpc_id),
            "PrivateSubnets": Sub("${PrivateSubnet1Id},${PrivateSubnet2Id}"),
            "ExistingTaskRoleArn": Ref(existing_task_role_arn),
            "MSKInstanceType": Ref(msk_instance_type),
            "MSKBrokerNodes": Ref(msk_broker_nodes)
        },
        Tags=Tags(
            Component="MSK",
            ManagedBy="Lakerunner",
            Environment=Ref("AWS::StackName")
        )
    )
)

# ECS Setup Stack (conditional) - Runs database and Kafka setup
# Must run after RDS and MSK are created but before Services are deployed
ecs_setup_stack = t.add_resource(
    Stack(
        "EcsSetupStack",
        Condition="CreateRDSCondition",  # Only run migration if we're creating RDS
        TemplateURL=Sub("${TemplateBaseUrl}/lakerunner-ecs-setup.yaml"),
        Parameters={
            # Pass the CommonInfra stack name for the migration to import values
            "CommonInfraStackName": Ref("AWS::StackName")
        },
        Tags=Tags(
            Component="EcsSetup",
            ManagedBy="Lakerunner",
            Environment=Ref("AWS::StackName")
        )
        # Dependencies are handled implicitly through parameter references in the migration template
    )
)

# ECS Services Stack (conditional) - Part 3a: ECS deployment
# Note: Services must wait for database setup and other dependencies to be ready
ecs_services_stack = t.add_resource(
    Stack(
        "EcsServicesStack",
        Condition="CreateECSServicesCondition",
        TemplateURL=Sub("${TemplateBaseUrl}/lakerunner-ecs-services.yaml"),
        Parameters={
            # Infrastructure parameters - from ECS stack or existing
            "ClusterArn": If("CreateECSInfraCondition",
                            GetAtt(ecs_stack, "Outputs.ClusterArn"),
                            Ref(existing_cluster_arn)),
            "TaskSecurityGroupId": If("CreateECSInfraCondition",
                                     GetAtt(ecs_stack, "Outputs.TaskSGId"),
                                     Ref(existing_security_group_id)),
            "VpcId": Ref(vpc_id),
            "PrivateSubnets": Sub("${PrivateSubnet1Id},${PrivateSubnet2Id}"),
            "PublicSubnets": If("HasPublicSubnetsCondition",
                               Sub("${PublicSubnet1Id},${PublicSubnet2Id}"),
                               ""),

            # Database parameters - from RDS stack or existing
            "DbSecretArn": If("CreateRDSCondition",
                            GetAtt(rds_stack, "Outputs.DbSecretArn"),
                            Ref(existing_db_secret_arn)),
            "DbHost": If("CreateRDSCondition",
                       GetAtt(rds_stack, "Outputs.DbEndpoint"),
                       Ref(existing_db_endpoint)),
            "DbPort": If("CreateRDSCondition",
                       GetAtt(rds_stack, "Outputs.DbPort"),
                       "5432"),

            # Storage parameters - from S3 stack or existing
            "BucketArn": If("CreateS3StorageCondition",
                          GetAtt(storage_stack, "Outputs.BucketArn"),
                          Ref(existing_bucket_arn)),
            "StorageStackName": If("CreateS3StorageCondition", Ref(storage_stack), ""),

            # MSK parameters - from MSK stack or existing
            "MSKClusterArn": If("CreateMSKCondition",
                              GetAtt(msk_stack, "Outputs.MSKClusterArn"),
                              Ref(existing_msk_cluster_arn)),
            "MSKCredentialsArn": If("CreateMSKCondition",
                                  GetAtt(msk_stack, "Outputs.MSKCredentialsArn"),
                                  ""),

            # Optional parameters
            "AlbScheme": Ref(alb_scheme)
        },
        Tags=Tags(
            Component="Services",
            ManagedBy="Lakerunner",
            Environment=Ref("AWS::StackName")
        )
    )
)

# ECS Collector Stack (optional)
ecs_collector_stack = t.add_resource(
    Stack(
        "EcsCollectorStack",
        Condition="CreateECSCollectorCondition",
        TemplateURL=Sub("${TemplateBaseUrl}/lakerunner-ecs-collector.yaml"),
        Parameters={
            "CommonInfraStackName": Ref("AWS::StackName"),
            "ClusterArn": If("CreateECSInfraCondition",
                           GetAtt(ecs_stack, "Outputs.ClusterArn"),
                           Ref(existing_cluster_arn)),
            "VpcId": Ref(vpc_id),
            "PrivateSubnets": Sub("${PrivateSubnet1Id},${PrivateSubnet2Id}"),
            "PublicSubnets": If("HasPublicSubnetsCondition",
                              Sub("${PublicSubnet1Id},${PublicSubnet2Id}"),
                              ""),
            "BucketName": If("CreateS3StorageCondition",
                           GetAtt(storage_stack, "Outputs.BucketName"),
                           Select(5, Split("/", Ref(existing_bucket_arn)))),
            "LoadBalancerType": Ref(alb_scheme)  # Use the same ALB scheme parameter
        },
        Tags=Tags(
            Component="Collector",
            Environment=Ref("AWS::StackName"),
            ManagedBy="Lakerunner"
        )
    )
)

# ECS Grafana Stack (optional)
ecs_grafana_stack = t.add_resource(
    Stack(
        "EcsGrafanaStack",
        Condition="CreateECSGrafanaCondition",
        TemplateURL=Sub("${TemplateBaseUrl}/lakerunner-ecs-grafana.yaml"),
        Parameters={
            # Infrastructure parameters
            "ClusterArn": If("CreateECSInfraCondition",
                            GetAtt(ecs_stack, "Outputs.ClusterArn"),
                            Ref(existing_cluster_arn)),
            "VpcId": Ref(vpc_id),
            "PrivateSubnets": Sub("${PrivateSubnet1Id},${PrivateSubnet2Id}"),
            "PublicSubnets": If("HasPublicSubnetsCondition",
                               Sub("${PublicSubnet1Id},${PublicSubnet2Id}"),
                               ""),
            "TaskSecurityGroupId": If("CreateECSInfraCondition",
                                     GetAtt(ecs_stack, "Outputs.TaskSGId"),
                                     Ref(existing_security_group_id)),
            # Database parameters
            "DbEndpoint": If("CreateRDSCondition",
                           GetAtt(rds_stack, "Outputs.DatabaseEndpoint"),
                           ""),
            "DbPort": If("CreateRDSCondition",
                        GetAtt(rds_stack, "Outputs.DatabasePort"),
                        ""),
            "DbSecretArn": If("CreateRDSCondition",
                             GetAtt(rds_stack, "Outputs.DBMasterSecretArn"),
                             ""),
            # Services integration
            "QueryApiAlbDns": If("CreateECSServicesCondition",
                                GetAtt(ecs_services_stack, "Outputs.AlbDNS"),
                                ""),
            "AlbScheme": Ref(alb_scheme)
        },
        Tags=Tags(
            Component="Grafana",
            Environment=Ref("AWS::StackName"),
            ManagedBy="Lakerunner"
        )
    )
)

# =============================================================================
# Outputs (consolidated values from created or BYO resources)
# =============================================================================

# VPC ID - from user selection
t.add_output(
    Output(
        "VPCId",
        Description="Selected VPC ID",
        Value=Ref(vpc_id),
        Export=Export(Sub("${AWS::StackName}-VPCId"))
    )
)

# Private Subnets - from user selection
t.add_output(
    Output(
        "PrivateSubnets",
        Description="Selected private subnet IDs",
        Value=Sub("${PrivateSubnet1Id},${PrivateSubnet2Id}"),
        Export=Export(Sub("${AWS::StackName}-PrivateSubnets"))
    )
)

# Public Subnets - from user selection (only if provided)
t.add_output(
    Output(
        "PublicSubnets",
        Description="Selected public subnet IDs",
        Condition="HasPublicSubnetsCondition",
        Value=Sub("${PublicSubnet1Id},${PublicSubnet2Id}"),
        Export=Export(Sub("${AWS::StackName}-PublicSubnets"))
    )
)

# Storage Stack Outputs (conditional)
t.add_output(
    Output(
        "BucketName",
        Description="S3 bucket name for ingest (created or existing)",
        Value=If(
            "CreateS3StorageCondition",
            GetAtt(storage_stack, "Outputs.BucketName"),
            Select(5, Split("/", Ref(existing_bucket_arn)))  # Extract bucket name from ARN
        ),
        Export=Export(Sub("${AWS::StackName}-BucketName"))
    )
)

t.add_output(
    Output(
        "BucketArn",
        Description="S3 bucket ARN for ingest (created or existing)",
        Value=If(
            "CreateS3StorageCondition",
            GetAtt(storage_stack, "Outputs.BucketArn"),
            Ref(existing_bucket_arn)
        ),
        Export=Export(Sub("${AWS::StackName}-BucketArn"))
    )
)

# RDS Stack Outputs (conditional)
t.add_output(
    Output(
        "DatabaseEndpoint",
        Description="RDS database endpoint (created or existing)",
        Value=If(
            "CreateRDSCondition",
            GetAtt(rds_stack, "Outputs.DbEndpoint"),
            Ref(existing_db_endpoint)
        ),
        Export=Export(Sub("${AWS::StackName}-DatabaseEndpoint"))
    )
)

t.add_output(
    Output(
        "DatabasePort",
        Description="RDS database port",
        Value=If(
            "CreateRDSCondition",
            GetAtt(rds_stack, "Outputs.DbPort"),
            "5432"  # Default PostgreSQL port for existing databases
        ),
        Export=Export(Sub("${AWS::StackName}-DatabasePort"))
    )
)

t.add_output(
    Output(
        "DatabaseSecretArn",
        Description="RDS database secret ARN (created or existing)",
        Value=If(
            "CreateRDSCondition",
            GetAtt(rds_stack, "Outputs.DbSecretArn"),
            Ref(existing_db_secret_arn)
        ),
        Export=Export(Sub("${AWS::StackName}-DatabaseSecretArn"))
    )
)

# ECS Stack Outputs (conditional)
t.add_output(
    Output(
        "ClusterArn",
        Description="ECS cluster ARN (only available when ECS infrastructure is created)",
        Condition="CreateECSInfraCondition",
        Value=GetAtt(ecs_stack, "Outputs.ClusterArn"),
        Export=Export(Sub("${AWS::StackName}-ClusterArn"))
    )
)

t.add_output(
    Output(
        "TaskSecurityGroupId",
        Description="ECS task security group ID (only available when ECS infrastructure is created)",
        Condition="CreateECSInfraCondition",
        Value=GetAtt(ecs_stack, "Outputs.TaskSGId"),
        Export=Export(Sub("${AWS::StackName}-TaskSGId"))
    )
)

# Note: Task roles are now created individually by each component stack
# This provides better isolation and makes each stack more standalone

# MSK Stack Outputs (conditional)
t.add_output(
    Output(
        "MSKClusterArn",
        Description="MSK cluster ARN (created or existing)",
        Value=If(
            "CreateMSKCondition",
            GetAtt(msk_stack, "Outputs.MSKClusterArn"),
            Ref(existing_msk_cluster_arn)
        ),
        Export=Export(Sub("${AWS::StackName}-MSKClusterArn"))
    )
)


if __name__ == "__main__":
    print(t.to_yaml())
