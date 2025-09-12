#!/usr/bin/env python3
"""Storage stack for Lakerunner: S3 ingest bucket, SQS queue and configuration."""

import yaml
import os
from troposphere import (
    Template, Parameter, Ref, Sub, If, Equals, Not, Export, Output, GetAtt, Select, Split
)
from troposphere.s3 import (
    Bucket, LifecycleRule, LifecycleConfiguration,
    NotificationConfiguration, QueueConfigurations,
    S3Key, Filter, Rules
)
from troposphere.sqs import Queue, QueuePolicy
from troposphere.ssm import Parameter as SsmParameter
from troposphere.iam import PolicyType, Role, Policy


t = Template()
t.set_description("Storage stack for Lakerunner (S3 ingest bucket and SQS queue).")

# -----------------------
# Parameters
# -----------------------
# Note: API keys parameter moved to ECS services stack where it's actually needed

StorageProfilesOverride = t.add_parameter(Parameter(
    "StorageProfilesOverride",
    Type="String",
    Default="",
    Description="OPTIONAL: Custom storage profiles configuration in YAML format. Leave blank to use defaults.",
))

ExistingTaskRoleArn = t.add_parameter(Parameter(
    "ExistingTaskRoleArn",
    Type="String",
    Default="",
    Description="OPTIONAL: Existing task role ARN to attach S3/SQS permissions to. Leave blank to create a new role.",
))

t.add_condition("HasStorageProfilesOverride", Not(Equals(Ref(StorageProfilesOverride), "")))
t.add_condition("CreateTaskRole", Equals(Ref(ExistingTaskRoleArn), ""))
t.add_condition("UseExistingTaskRole", Not(Equals(Ref(ExistingTaskRoleArn), "")))


def load_defaults():
    """Load default configuration from lakerunner-stack-defaults.yaml"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(script_dir, "..", "lakerunner-stack-defaults.yaml")
    with open(config_path, 'r') as f:
        return yaml.safe_load(f)

# -----------------------
# Task Role for Storage Access (conditional)
# -----------------------
StorageTaskRole = t.add_resource(Role(
    "StorageTaskRole",
    Condition="CreateTaskRole",
    RoleName=Sub("${AWS::StackName}-storage-task-role"),
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
# SQS + S3 (with lifecycle + notifications)
# -----------------------
QueueRes = t.add_resource(Queue(
    "IngestQueue",
    QueueName=Sub("${AWS::StackName}-ingest-queue"),
    MessageRetentionPeriod=60 * 60 * 24 * 4,
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
# SSM params with defaults and overrides
# -----------------------
defaults = load_defaults()

storage_profiles_default = yaml.dump(defaults['storage_profiles'], default_flow_style=False)
storage_profiles_default_cf = storage_profiles_default.replace("${BUCKET_NAME}", "${BucketName}").replace("${AWS_REGION}", "${AWS::Region}")

t.add_resource(SsmParameter(
    "StorageProfilesParam",
    Name=Sub("/lakerunner/${AWS::StackName}/storage_profiles"),
    Type="String",
    Value=If(
        "HasStorageProfilesOverride",
        Ref(StorageProfilesOverride),
        Sub(storage_profiles_default_cf, BucketName=Ref(BucketRes))
    ),
    Description="Storage profiles configuration",
))

# -----------------------
# IAM Policy for Task Role (must be after S3/SQS resources)
# -----------------------
t.add_resource(PolicyType(
    "S3SQSTaskPolicy",
    PolicyName="S3SQSAccess",
    Roles=[If(
        "UseExistingTaskRole",
        Select(1, Split("/", Ref(ExistingTaskRoleArn))),  # Extract role name from existing ARN
        Ref(StorageTaskRole)  # Use created role name directly
    )],
    PolicyDocument={
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "s3:GetObject",
                    "s3:PutObject", 
                    "s3:DeleteObject",
                    "s3:ListBucket"
                ],
                "Resource": [
                    GetAtt(BucketRes, "Arn"),
                    Sub("${BucketArn}/*", BucketArn=GetAtt(BucketRes, "Arn"))
                ]
            },
            {
                "Effect": "Allow",
                "Action": [
                    "sqs:ReceiveMessage",
                    "sqs:DeleteMessage", 
                    "sqs:GetQueueAttributes"
                ],
                "Resource": GetAtt(QueueRes, "Arn")
            }
        ]
    }
))

# -----------------------
# Outputs
# -----------------------
t.add_output(Output(
    "BucketName",
    Value=Ref(BucketRes),
    Export=Export(name=Sub("${AWS::StackName}-BucketName"))
))

t.add_output(Output(
    "BucketArn",
    Value=GetAtt(BucketRes, "Arn"),
    Export=Export(name=Sub("${AWS::StackName}-BucketArn"))
))

t.add_output(Output(
    "TaskRoleArn",
    Description="Task role ARN for storage access (created or existing)",
    Value=If(
        "UseExistingTaskRole",
        Ref(ExistingTaskRoleArn),
        GetAtt(StorageTaskRole, "Arn")
    ),
    Export=Export(name=Sub("${AWS::StackName}-TaskRoleArn"))
))

print(t.to_yaml())
