"""cardinal-satellite-infra-base: per-source-account ingest primitive.

Standalone stack a source ("satellite") account deploys to expose its raw
OTEL telemetry to a Lakerunner install in another account, using a pull
model: an in-account/in-region raw bucket + SQS queue + S3->SQS
notification, plus a cross-account IAM role the Lakerunner poller assumes
to read/delete the raw objects and consume the queue.

Nothing here pushes to the Lakerunner account; the only cross-account
relationship is the role's trust policy naming the Lakerunner principal.
"""

from troposphere import (
    Equals,
    GetAtt,
    If,
    Not,
    Output,
    Parameter,
    Ref,
    Sub,
    Tags,
    Template,
)
from troposphere.iam import Policy, Role
from troposphere.s3 import (
    AbortIncompleteMultipartUpload,
    Bucket,
    BucketEncryption,
    LifecycleConfiguration,
    LifecycleRule,
    NotificationConfiguration,
    PublicAccessBlockConfiguration,
    QueueConfigurations,
    ServerSideEncryptionByDefault,
    ServerSideEncryptionRule,
)
from troposphere.sqs import Queue, QueuePolicy

APPLICATION = "cardinal-lakerunner"
PROJECT = "cardinal"
MANAGED_BY = "cardinal-cfn-satellite"


def _tags(*, component: str) -> Tags:
    return Tags(
        Application=APPLICATION,
        Project=PROJECT,
        ManagedBy=MANAGED_BY,
        Component=component,
        Name=f"cardinal-{component}",
    )


def _delete(resource):
    resource.DeletionPolicy = "Delete"
    resource.UpdateReplacePolicy = "Delete"
    return resource


def build() -> Template:
    t = Template()
    t.set_description(
        "Cardinal satellite infra base: per-source-account raw ingest bucket, "
        "SQS queue, S3->SQS notification, and the cross-account role the "
        "Lakerunner poller assumes. Pull model; nothing pushes to Lakerunner."
    )

    t.add_parameter(
        Parameter(
            "LakerunnerPrincipal",
            Type="String",
            Description=(
                "ARN of the Lakerunner principal allowed to assume the access "
                "role (the poller role ARN, or the Lakerunner account root ARN "
                "arn:aws:iam::<acct>:root)."
            ),
            AllowedPattern=r"^arn:aws[a-zA-Z-]*:iam::\d{12}:(root|role/.+)$",
        )
    )

    t.add_parameter(
        Parameter(
            "ExternalId",
            Type="String",
            Default="",
            Description=(
                "Optional sts:ExternalId required on AssumeRole "
                "(confused-deputy mitigation). Blank disables the check."
            ),
        )
    )

    t.add_parameter(
        Parameter(
            "RawBucketName",
            Type="String",
            Default="",
            Description=(
                "Name for the raw ingest bucket. Blank uses the default "
                "cardinal-otel-raw-<account>-<region>."
            ),
            AllowedPattern=r"^$|^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$",
        )
    )

    t.add_parameter(
        Parameter(
            "RawBucketLifecycleDays",
            Type="Number",
            Default=7,
            MinValue=1,
            Description=(
                "Days after which raw objects expire. Raw is ephemeral "
                "(Lakerunner deletes after processing); this bounds orphans."
            ),
        )
    )

    t.add_condition("UseDefaultBucketName", Equals(Ref("RawBucketName"), ""))
    t.add_condition("HasExternalId", Not(Equals(Ref("ExternalId"), "")))

    bucket_name_value = If(
        "UseDefaultBucketName",
        Sub("cardinal-otel-raw-${AWS::AccountId}-${AWS::Region}"),
        Ref("RawBucketName"),
    )

    queue = t.add_resource(
        _delete(Queue("RawIngestQueue", Tags=_tags(component="otel-raw-queue")))
    )

    t.add_resource(
        QueuePolicy(
            "RawIngestQueuePolicy",
            Queues=[Ref(queue)],
            PolicyDocument={
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {"Service": "s3.amazonaws.com"},
                        "Action": [
                            "sqs:SendMessage",
                            "sqs:GetQueueAttributes",
                            "sqs:GetQueueUrl",
                        ],
                        "Resource": GetAtt(queue, "Arn"),
                        "Condition": {
                            "StringEquals": {
                                "aws:SourceAccount": Ref("AWS::AccountId")
                            },
                            "ArnLike": {
                                "aws:SourceArn": Sub(
                                    "arn:${AWS::Partition}:s3:::${BucketName}",
                                    BucketName=bucket_name_value,
                                )
                            },
                        },
                    }
                ],
            },
        )
    )

    t.add_resource(
        _delete(
            Bucket(
                "RawIngestBucket",
                # S3 validates the SQS notification target when the bucket's
                # notification config is applied and fails if the queue policy
                # is not yet in place, so the bucket is created after it.
                DependsOn="RawIngestQueuePolicy",
                BucketName=bucket_name_value,
                PublicAccessBlockConfiguration=PublicAccessBlockConfiguration(
                    BlockPublicAcls=True,
                    BlockPublicPolicy=True,
                    IgnorePublicAcls=True,
                    RestrictPublicBuckets=True,
                ),
                BucketEncryption=BucketEncryption(
                    ServerSideEncryptionConfiguration=[
                        ServerSideEncryptionRule(
                            ServerSideEncryptionByDefault=(
                                ServerSideEncryptionByDefault(
                                    SSEAlgorithm="AES256"
                                )
                            )
                        )
                    ]
                ),
                LifecycleConfiguration=LifecycleConfiguration(
                    Rules=[
                        LifecycleRule(
                            Id="cardinal-otel-raw-expire",
                            Status="Enabled",
                            Prefix="",
                            ExpirationInDays=Ref("RawBucketLifecycleDays"),
                            AbortIncompleteMultipartUpload=(
                                AbortIncompleteMultipartUpload(
                                    DaysAfterInitiation=1
                                )
                            ),
                        )
                    ]
                ),
                NotificationConfiguration=NotificationConfiguration(
                    QueueConfigurations=[
                        QueueConfigurations(
                            Event="s3:ObjectCreated:*",
                            Queue=GetAtt(queue, "Arn"),
                        )
                    ]
                ),
                Tags=_tags(component="otel-raw-bucket"),
            )
        )
    )

    t.add_resource(
        Role(
            "LakerunnerAccessRole",
            AssumeRolePolicyDocument={
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {"AWS": Ref("LakerunnerPrincipal")},
                        "Action": "sts:AssumeRole",
                        "Condition": If(
                            "HasExternalId",
                            {
                                "StringEquals": {
                                    "sts:ExternalId": Ref("ExternalId")
                                }
                            },
                            Ref("AWS::NoValue"),
                        ),
                    }
                ],
            },
            Policies=[
                Policy(
                    PolicyName="cardinal-satellite-access",
                    PolicyDocument={
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Sid": "RawBucketReadDelete",
                                "Effect": "Allow",
                                "Action": [
                                    "s3:GetObject",
                                    "s3:DeleteObject",
                                    "s3:ListBucket",
                                    "s3:GetBucketLocation",
                                ],
                                "Resource": [
                                    Sub(
                                        "arn:${AWS::Partition}:s3:::"
                                        "${BucketName}",
                                        BucketName=bucket_name_value,
                                    ),
                                    Sub(
                                        "arn:${AWS::Partition}:s3:::"
                                        "${BucketName}/*",
                                        BucketName=bucket_name_value,
                                    ),
                                ],
                            },
                            {
                                "Sid": "RawQueueConsume",
                                "Effect": "Allow",
                                "Action": [
                                    "sqs:ReceiveMessage",
                                    "sqs:DeleteMessage",
                                    "sqs:GetQueueAttributes",
                                    "sqs:GetQueueUrl",
                                    "sqs:ChangeMessageVisibility",
                                ],
                                "Resource": GetAtt(queue, "Arn"),
                            },
                        ],
                    },
                )
            ],
            Tags=_tags(component="satellite-access-role"),
        )
    )

    t.add_output(
        Output(
            "RawBucketName",
            Description="Raw ingest bucket name.",
            Value=Ref("RawIngestBucket"),
        )
    )
    t.add_output(
        Output(
            "RawQueueUrl",
            Description="Raw ingest SQS queue URL.",
            Value=Ref(queue),
        )
    )
    t.add_output(
        Output(
            "RawQueueArn",
            Description="Raw ingest SQS queue ARN.",
            Value=GetAtt(queue, "Arn"),
        )
    )
    t.add_output(
        Output(
            "LakerunnerAccessRoleArn",
            Description="ARN of the role the Lakerunner poller assumes.",
            Value=GetAtt("LakerunnerAccessRole", "Arn"),
        )
    )
    t.add_output(
        Output(
            "Region",
            Description="Region of this satellite's bucket/queue.",
            Value=Ref("AWS::Region"),
        )
    )

    return t


if __name__ == "__main__":
    print(build().to_yaml(), end="")
