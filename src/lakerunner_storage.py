#!/usr/bin/env python3
"""Storage stack for Lakerunner: S3 ingest bucket, SQS queue and configuration."""

import yaml
import os
from troposphere import (
    Template, Parameter, Ref, Sub, If, Equals, Not, Export, Output, GetAtt
)
from troposphere.s3 import (
    Bucket, LifecycleRule, LifecycleConfiguration,
    NotificationConfiguration, QueueConfigurations,
    S3Key, Filter, Rules
)
from troposphere.sqs import Queue, QueuePolicy
from troposphere.ssm import Parameter as SsmParameter


t = Template()
t.set_description("Storage stack for Lakerunner (S3 ingest bucket and SQS queue).")

# -----------------------
# Parameters
# -----------------------
ApiKeysOverride = t.add_parameter(Parameter(
    "ApiKeysOverride",
    Type="String",
    Default="",
    Description="OPTIONAL: Custom API keys configuration in YAML format. Leave blank to use defaults.",
))

StorageProfilesOverride = t.add_parameter(Parameter(
    "StorageProfilesOverride",
    Type="String",
    Default="",
    Description="OPTIONAL: Custom storage profiles configuration in YAML format. Leave blank to use defaults.",
))

t.add_condition("HasApiKeysOverride", Not(Equals(Ref(ApiKeysOverride), "")))
t.add_condition("HasStorageProfilesOverride", Not(Equals(Ref(StorageProfilesOverride), "")))


def load_defaults():
    """Load default configuration from lakerunner-stack-defaults.yaml"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(script_dir, "..", "lakerunner-stack-defaults.yaml")
    with open(config_path, 'r') as f:
        return yaml.safe_load(f)

# -----------------------
# SQS + S3 (with lifecycle + notifications)
# -----------------------
QueueRes = t.add_resource(Queue(
    "IngestQueue",
    QueueName="lakerunner-ingest-queue",
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

api_keys_yaml = yaml.dump(defaults['api_keys'], default_flow_style=False)
t.add_resource(SsmParameter(
    "ApiKeysParam",
    Name="/lakerunner/api_keys",
    Type="String",
    Value=If("HasApiKeysOverride", Ref(ApiKeysOverride), api_keys_yaml),
    Description="API keys configuration",
))

storage_profiles_default = yaml.dump(defaults['storage_profiles'], default_flow_style=False)
storage_profiles_default_cf = storage_profiles_default.replace("${BUCKET_NAME}", "${BucketName}").replace("${AWS_REGION}", "${AWS::Region}")

t.add_resource(SsmParameter(
    "StorageProfilesParam",
    Name="/lakerunner/storage_profiles",
    Type="String",
    Value=If(
        "HasStorageProfilesOverride",
        Ref(StorageProfilesOverride),
        Sub(storage_profiles_default_cf, BucketName=Ref(BucketRes))
    ),
    Description="Storage profiles configuration",
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

print(t.to_yaml())
