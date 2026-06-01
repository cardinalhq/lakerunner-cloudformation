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

    return t


if __name__ == "__main__":
    print(build().to_yaml(), end="")
