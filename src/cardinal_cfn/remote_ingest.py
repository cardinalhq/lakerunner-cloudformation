"""cardinal-remote-ingest.yaml: cross-account remote ingest bucket (main account).

Standalone root template, one stack instance per remote bucket/account. Creates
an S3 bucket in the main (lakerunner) account that a remote account's otel
collector writes to (by assuming the WriterRole this stack creates), wires the
bucket's s3:ObjectCreated notifications to the main SQS ingest queue, and emits
a storage-profile snippet for the operator to register with lakerunner.

Design: docs/superpowers/specs/2026-05-29-cross-account-remote-ingest-design.md
"""

from troposphere import (
    Equals,
    GetAtt,
    If,
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
    LifecycleConfiguration,
    LifecycleRule,
    NotificationConfiguration,
    OwnershipControls,
    OwnershipControlsRule,
    PublicAccessBlockConfiguration,
    QueueConfigurations,
)

from cardinal_cfn.policies import apply_policy


def _tags(*, component: str) -> Tags:
    return Tags(
        Name=Sub(f"cardinal-remote-ingest-{component}-${{RemoteAccountId}}"),
        Project="cardinal",
        Application="cardinal-lakerunner",
        Component=component,
        ManagedBy="cardinal-cfn",
    )


def build() -> Template:
    t = Template()
    t.set_description(
        "Cardinal remote ingest: an S3 bucket in the main account that a remote "
        "account's otel collector writes to (via an assumed writer role), wired "
        "to the main lakerunner SQS ingest queue. One stack per remote bucket."
    )

    # RemoteAccountId, OrgId, CollectorName, and RemoteOtelRoleNamePattern are
    # referenced by name inside Sub templates below (e.g. "${OrgId}"), so they
    # are declared without binding a local.
    t.add_parameter(Parameter(
        "RemoteAccountId",
        Type="String",
        AllowedPattern=r"^[0-9]{12}$",
        Description="The second (remote) AWS account ID whose otel collector writes to this bucket.",
    ))
    t.add_parameter(Parameter(
        "OrgId",
        Type="String",
        MinLength=1,
        Description="Lakerunner organization_id this bucket's telemetry is attributed to.",
    ))
    queue_arn = t.add_parameter(Parameter(
        "QueueArn",
        Type="String",
        MinLength=1,
        Description="ARN of the main lakerunner SQS ingest queue (infra IngestQueueArn output).",
    ))
    bucket_name = t.add_parameter(Parameter(
        "BucketName",
        Type="String",
        Default="",
        AllowedPattern=r"^$|^cardinal-remote-ingest-[a-z0-9.-]{1,40}$",
        Description=(
            "Bucket name. Blank = cardinal-remote-ingest-<RemoteAccountId>. Any "
            "override MUST keep the cardinal-remote-ingest- prefix so the infra "
            "queue policy grants the notification."
        ),
    ))
    t.add_parameter(Parameter(
        "CollectorName",
        Type="String",
        Default="lakerunner",
        Description="Collector name for the storage profile and otel s3_prefix.",
    ))
    t.add_parameter(Parameter(
        "RemoteOtelRoleNamePattern",
        Type="String",
        Default="cardinal-remote-otel-*",
        Description="Remote task-role name pattern allowed to assume the writer role.",
    ))
    lifecycle_days = t.add_parameter(Parameter(
        "IngestBucketLifecycleDays",
        Type="Number",
        Default=7,
        MinValue=1,
        Description="Days after which objects in the bucket expire (GC backstop).",
    ))

    t.add_condition("UseDefaultBucketName", Equals(Ref(bucket_name), ""))
    bucket_name_value = If(
        "UseDefaultBucketName",
        Sub("cardinal-remote-ingest-${RemoteAccountId}"),
        Ref(bucket_name),
    )

    writer_role = t.add_resource(Role(
        "WriterRole",
        AssumeRolePolicyDocument={
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Principal": {"AWS": Sub("arn:${AWS::Partition}:iam::${RemoteAccountId}:root")},
                "Action": "sts:AssumeRole",
                "Condition": {
                    "ArnLike": {
                        "aws:PrincipalArn": Sub(
                            "arn:${AWS::Partition}:iam::${RemoteAccountId}:role/${RemoteOtelRoleNamePattern}"
                        )
                    }
                },
            }],
        },
        Policies=[Policy(
            PolicyName="cardinal-remote-writer",
            PolicyDocument={
                "Version": "2012-10-17",
                "Statement": [{
                    "Effect": "Allow",
                    "Action": [
                        "s3:PutObject",
                        "s3:AbortMultipartUpload",
                        "s3:ListMultipartUploadParts",
                    ],
                    "Resource": Sub(
                        "arn:${AWS::Partition}:s3:::${BucketName}/*",
                        BucketName=bucket_name_value,
                    ),
                }],
            },
        )],
        Tags=_tags(component="writer-role"),
    ))

    bucket = t.add_resource(Bucket(
        "RemoteIngestBucket",
        BucketName=bucket_name_value,
        OwnershipControls=OwnershipControls(
            Rules=[OwnershipControlsRule(ObjectOwnership="BucketOwnerEnforced")]
        ),
        PublicAccessBlockConfiguration=PublicAccessBlockConfiguration(
            BlockPublicAcls=True,
            BlockPublicPolicy=True,
            IgnorePublicAcls=True,
            RestrictPublicBuckets=True,
        ),
        LifecycleConfiguration=LifecycleConfiguration(Rules=[
            LifecycleRule(
                Id="cardinal-remote-ingest-expire",
                Status="Enabled",
                Prefix="",
                ExpirationInDays=Ref(lifecycle_days),
                AbortIncompleteMultipartUpload=AbortIncompleteMultipartUpload(
                    DaysAfterInitiation=1
                ),
            )
        ]),
        NotificationConfiguration=NotificationConfiguration(
            QueueConfigurations=[
                QueueConfigurations(Event="s3:ObjectCreated:*", Queue=Ref(queue_arn))
            ]
        ),
        Tags=_tags(component="bucket"),
    ))
    apply_policy(bucket, "s3-ingest-bucket")

    t.add_output(Output("BucketName", Description="Remote ingest bucket name.", Value=bucket_name_value))
    t.add_output(Output("BucketArn", Description="Remote ingest bucket ARN.", Value=GetAtt(bucket, "Arn")))
    t.add_output(Output(
        "BucketRegion",
        Description="Bucket region (the main/lakerunner region). Feed to the remote collector's BucketRegion.",
        Value=Ref("AWS::Region"),
    ))
    t.add_output(Output(
        "WriterRoleArn",
        Description="Role ARN the remote collector assumes to write. Feed to the remote collector's WriterRoleArn.",
        Value=GetAtt(writer_role, "Arn"),
    ))
    t.add_output(Output(
        "StorageProfileSnippet",
        Description="YAML list item to append to the infra stack's AdditionalStorageProfilesYaml, then re-run the migrator.",
        Value=Sub(
            "- organization_id: ${OrgId}\n"
            "  instance_num: 1\n"
            "  collector_name: ${CollectorName}\n"
            "  cloud_provider: aws\n"
            "  region: ${AWS::Region}\n"
            "  bucket: ${BucketName}\n"
            "  insecure_tls: false\n"
            "  use_path_style: true\n",
            BucketName=bucket_name_value,
        ),
    ))

    return t


if __name__ == "__main__":
    print(build().to_yaml(), end="")
