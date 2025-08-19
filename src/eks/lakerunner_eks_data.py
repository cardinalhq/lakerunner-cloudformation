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
    Template, Parameter, Ref, Sub, GetAtt, If, Equals, Output,
    Not, Tags
)
from troposphere.s3 import (
    Bucket, LifecycleRule, LifecycleConfiguration,
    NotificationConfiguration, QueueConfigurations,
    S3Key, Filter, Rules
)
from troposphere.sqs import Queue, QueuePolicy
from troposphere.secretsmanager import Secret, GenerateSecretString
from troposphere.rds import DBInstance, DBSubnetGroup
from troposphere.ssm import Parameter as SsmParameter

def load_defaults():
    """Load default configuration from lakerunner-stack-defaults.yaml"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(script_dir, "..", "..", "lakerunner-stack-defaults.yaml")

    with open(config_path, 'r') as f:
        return yaml.safe_load(f)

def create_data_template():
    """Create CloudFormation template for data layer infrastructure"""

    t = Template()
    t.set_description("EKS Data Layer: RDS PostgreSQL, S3 bucket, SQS queue, and Secrets Manager")

    # -----------------------
    # Parameters
    # -----------------------
    VpcId = t.add_parameter(Parameter(
        "VpcId",
        Type="String",
        Description="VPC ID from VPC stack"
    ))

    PrivateSubnet1Id = t.add_parameter(Parameter(
        "PrivateSubnet1Id",
        Type="String",
        Description="Private Subnet 1 ID from VPC stack"
    ))

    PrivateSubnet2Id = t.add_parameter(Parameter(
        "PrivateSubnet2Id",
        Type="String",
        Description="Private Subnet 2 ID from VPC stack"
    ))

    NodeGroupSecurityGroupId = t.add_parameter(Parameter(
        "NodeGroupSecurityGroupId",
        Type="String",
        Description="EKS Node Group Security Group ID from VPC stack"
    ))

    # Configuration overrides (optional multi-line parameters)
    ApiKeysOverride = t.add_parameter(Parameter(
        "ApiKeysOverride",
        Type="String",
        Default="",
        Description="OPTIONAL: Custom API keys configuration in YAML format. Leave blank to use defaults from defaults.yaml."
    ))

    StorageProfilesOverride = t.add_parameter(Parameter(
        "StorageProfilesOverride",
        Type="String",
        Default="",
        Description="OPTIONAL: Custom storage profiles configuration in YAML format. Leave blank to use defaults from defaults.yaml."
    ))

    # Parameter groups for console
    t.set_metadata({
        "AWS::CloudFormation::Interface": {
            "ParameterGroups": [
                {
                    "Label": {"default": "Network Configuration"},
                    "Parameters": ["VpcId", "PrivateSubnet1Id", "PrivateSubnet2Id", "NodeGroupSecurityGroupId"]
                },
                {
                    "Label": {"default": "Configuration Overrides (Advanced)"},
                    "Parameters": ["ApiKeysOverride", "StorageProfilesOverride"]
                }
            ],
            "ParameterLabels": {
                "VpcId": {"default": "VPC ID"},
                "PrivateSubnet1Id": {"default": "Private Subnet 1 ID"},
                "PrivateSubnet2Id": {"default": "Private Subnet 2 ID"},
                "NodeGroupSecurityGroupId": {"default": "Node Group Security Group ID"},
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

    # -----------------------
    # SQS + S3 (with lifecycle + notifications)
    # -----------------------
    queue_res = t.add_resource(Queue(
        "IngestQueue",
        QueueName="lakerunner-ingest-queue",
        MessageRetentionPeriod=60 * 60 * 24 * 4,  # 4 days in seconds
        Tags=Tags(
            Name=Sub("${AWS::StackName}-ingest-queue")
        )
    ))

    bucket_res = t.add_resource(Bucket(
        "IngestBucket",
        DeletionPolicy="Delete",
        LifecycleConfiguration=LifecycleConfiguration(
            Rules=[LifecycleRule(Prefix="otel-raw/", Status="Enabled", ExpirationInDays=10)]
        ),
        NotificationConfiguration=NotificationConfiguration(
            QueueConfigurations=[
                QueueConfigurations(
                    Event="s3:ObjectCreated:*",
                    Queue=GetAtt(queue_res, "Arn"),
                    Filter=Filter(
                        S3Key=S3Key(
                            Rules=[Rules(Name="prefix", Value=p)]
                        )
                    )
                ) for p in ["otel-raw/", "logs-raw/", "metrics-raw/"]
            ]
        ),
        Tags=Tags(
            Name=Sub("${AWS::StackName}-ingest-bucket")
        )
    ))

    t.add_resource(QueuePolicy(
        "IngestQueuePolicy",
        Queues=[Ref(queue_res)],
        PolicyDocument={
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Principal": {"Service": "s3.amazonaws.com"},
                "Action": ["sqs:GetQueueAttributes", "sqs:GetQueueUrl", "sqs:SendMessage"],
                "Resource": GetAtt(queue_res, "Arn"),
                "Condition": {
                    "StringEquals": {"aws:SourceAccount": Ref("AWS::AccountId")}
                }
            }]
        }
    ))

    # -----------------------
    # Secrets for DB (always create; random name)
    # -----------------------
    db_secret = t.add_resource(Secret(
        "DbSecret",
        GenerateSecretString=GenerateSecretString(
            SecretStringTemplate='{"username":"lakerunner"}',
            GenerateStringKey="password",
            ExcludePunctuation=True,
        ),
        Tags=Tags(
            Name=Sub("${AWS::StackName}-db-secret")
        )
    ))

    # -----------------------
    # RDS Postgres (always create)
    # -----------------------
    db_subnets = t.add_resource(DBSubnetGroup(
        "DbSubnetGroup",
        DBSubnetGroupDescription="DB subnets for EKS",
        SubnetIds=[Ref(PrivateSubnet1Id), Ref(PrivateSubnet2Id)],
        Tags=Tags(
            Name=Sub("${AWS::StackName}-db-subnet-group")
        )
    ))

    db_res = t.add_resource(DBInstance(
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
        VPCSecurityGroups=[Ref(NodeGroupSecurityGroupId)],
        DBSubnetGroupName=Ref(db_subnets),
        MasterUsername=Sub("{{resolve:secretsmanager:${S}:SecretString:username}}", S=Ref(db_secret)),
        MasterUserPassword=Sub("{{resolve:secretsmanager:${S}:SecretString:password}}", S=Ref(db_secret)),
        DeletionProtection=False,
        Tags=Tags(
            Name=Sub("${AWS::StackName}-db")
        )
    ))

    db_endpoint = GetAtt(db_res, "Endpoint.Address")
    db_port = GetAtt(db_res, "Endpoint.Port")

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
        Description="API keys configuration"
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
            Sub(storage_profiles_default_cf, Bucket=Ref(bucket_res))
        ),
        Description="Storage profiles configuration"
    ))

    # -----------------------
    # Outputs
    # -----------------------
    t.add_output(Output(
        "DbEndpoint",
        Value=db_endpoint,
        Description="Database endpoint"
    ))

    t.add_output(Output(
        "DbPort",
        Value=db_port,
        Description="Database port"
    ))

    t.add_output(Output(
        "DbSecretArn",
        Value=Ref(db_secret),
        Description="Database credentials secret ARN"
    ))

    t.add_output(Output(
        "BucketName",
        Value=Ref(bucket_res),
        Description="S3 ingest bucket name"
    ))

    t.add_output(Output(
        "BucketArn",
        Value=GetAtt(bucket_res, "Arn"),
        Description="S3 ingest bucket ARN"
    ))

    t.add_output(Output(
        "QueueName",
        Value=GetAtt(queue_res, "QueueName"),
        Description="SQS ingest queue name"
    ))

    t.add_output(Output(
        "QueueArn",
        Value=GetAtt(queue_res, "Arn"),
        Description="SQS ingest queue ARN"
    ))

    t.add_output(Output(
        "QueueUrl",
        Value=Ref(queue_res),
        Description="SQS ingest queue URL"
    ))

    return t

# Generate template
if __name__ == "__main__":
    template = create_data_template()
    print(template.to_yaml())