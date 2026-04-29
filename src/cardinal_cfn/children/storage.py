"""storage.yaml nested stack: S3 ingest bucket, SQS queue, notifications."""

from troposphere import (
    Template,
    Ref,
    GetAtt,
    Output,
    Sub,
)
from troposphere.s3 import (
    Bucket,
    LifecycleConfiguration,
    LifecycleRule,
    AbortIncompleteMultipartUpload,
    NotificationConfiguration,
    QueueConfigurations,
    S3Key,
    Filter,
    Rules,
)
from troposphere.sqs import Queue, QueuePolicy

from cardinal_cfn.naming import cardinal_tags
from cardinal_cfn.parameters import add_install_id_parameters
from cardinal_cfn.policies import apply_policy


def build() -> Template:
    t = Template()
    t.set_description("Cardinal storage: ingest S3 bucket, SQS queue, S3->SQS notifications.")

    add_install_id_parameters(t)

    queue = t.add_resource(
        Queue(
            "IngestQueue",
            MessageRetentionPeriod=60 * 60 * 24 * 4,
            Tags=cardinal_tags(component="storage", role="ingest-queue"),
        )
    )
    apply_policy(queue, "sqs-ingest-queue")

    # The queue policy must exist before the bucket so S3 can validate the
    # NotificationConfiguration target permissions at bucket-create time.
    queue_policy = t.add_resource(
        QueuePolicy(
            "IngestQueuePolicy",
            Queues=[Ref(queue)],
            PolicyDocument={
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {"Service": "s3.amazonaws.com"},
                        "Action": ["sqs:GetQueueAttributes", "sqs:GetQueueUrl", "sqs:SendMessage"],
                        "Resource": GetAtt(queue, "Arn"),
                        "Condition": {
                            "StringEquals": {"aws:SourceAccount": Ref("AWS::AccountId")}
                        },
                    }
                ],
            },
        )
    )

    bucket = t.add_resource(
        Bucket(
            "IngestBucket",
            DependsOn=queue_policy.title,
            BucketName=Sub("cardinal-ingest-${AWS::AccountId}-${AWS::Region}-${InstallIdLong}"),
            LifecycleConfiguration=LifecycleConfiguration(
                Rules=[
                    LifecycleRule(Prefix="otel-raw/", Status="Enabled", ExpirationInDays=3),
                    LifecycleRule(
                        Id="CleanupIncompleteMultipartUploads",
                        Status="Enabled",
                        AbortIncompleteMultipartUpload=AbortIncompleteMultipartUpload(
                            DaysAfterInitiation=1
                        ),
                    ),
                ]
            ),
            NotificationConfiguration=NotificationConfiguration(
                QueueConfigurations=[
                    QueueConfigurations(
                        Event="s3:ObjectCreated:*",
                        Queue=GetAtt(queue, "Arn"),
                        Filter=Filter(S3Key=S3Key(Rules=[Rules(Name="prefix", Value=p)])),
                    )
                    for p in ("otel-raw/", "logs-raw/", "metrics-raw/")
                ]
            ),
            Tags=cardinal_tags(component="storage", role="ingest-bucket"),
        )
    )
    apply_policy(bucket, "s3-ingest-bucket")

    t.add_output(Output("BucketName", Value=Ref(bucket)))
    t.add_output(Output("BucketArn", Value=GetAtt(bucket, "Arn")))
    t.add_output(Output("QueueUrl", Value=Ref(queue)))
    t.add_output(Output("QueueArn", Value=GetAtt(queue, "Arn")))

    return t


if __name__ == "__main__":
    print(build().to_yaml())
