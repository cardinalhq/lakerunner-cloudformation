"""cardinal-satellite-cwmetrics: CloudWatch metrics into an existing satellite.

Producer-only stack, a sibling of cardinal-satellite-services rather than a
second satellite: it creates no bucket, no queue and no cross-account role.
It streams the account's CloudWatch metrics through Firehose into the raw
bucket already owned by cardinal-satellite-infra-base, under cwmetrics-raw/.

That prefix is one of the bucket's notification prefixes, and the
cardinal-satellite-access role already grants the poller read/delete on the
whole bucket, so the objects reach Lakerunner over the existing queue and
the existing trust relationship -- no extra SQS traffic, no second role.

  CloudWatch metrics -> MetricStream -> Firehose -> s3://<raw>/cwmetrics-raw/

Pull model is preserved: nothing here pushes to the Lakerunner account.

Deploy order: cardinal-satellite-infra-base first (this stack takes its
RawBucketName output as a parameter).
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
from troposphere.cloudwatch import MetricStream, MetricStreamFilter
from troposphere.firehose import (
    BufferingHints,
    CloudWatchLoggingOptions,
    DeliveryStream,
    ExtendedS3DestinationConfiguration,
)
from troposphere.iam import Policy, Role
from troposphere.logs import LogGroup

from cardinal_cfn.naming import cardinal_tags
from cardinal_cfn.parameters import add_parameter_group_metadata
from cardinal_cfn.policies import apply_policy
from cardinal_cfn.satellite_infra_base import INGEST_PREFIXES

MANAGED_BY = "cardinal-cfn-satellite"

# Must be one of the raw bucket's notified prefixes or the delivered objects
# never reach the poller.
_RAW_PREFIX = "cwmetrics-raw/"
assert _RAW_PREFIX in INGEST_PREFIXES

# Failed-record payloads.  Deliberately OUTSIDE the notified prefixes: they
# are not ingestible telemetry and would otherwise be handed to the poller,
# fail to parse, and churn against the queue's redrive count.
_ERROR_PREFIX = "cwmetrics-errors/"

# Lakerunner ingests the plain CloudWatch metric-stream JSON format.  Not
# opentelemetry1.0: that is a double conversion which renders the metrics in
# OTLP summary form and loses the fidelity Lakerunner wants.  Deliberately
# not a parameter -- the other formats do not work with the ingest path.
_OUTPUT_FORMAT = "json"

_LOG_RETENTION_DAYS = 7


def _tags(*, component: str) -> Tags:
    return cardinal_tags(component=component, managed_by=MANAGED_BY)


def build() -> Template:
    t = Template()
    t.set_description(
        "Cardinal satellite CloudWatch metrics: a metric stream and Firehose "
        "delivering into the existing satellite raw bucket under "
        "cwmetrics-raw/. Creates no bucket, queue or access role -- it reuses "
        "the ones from cardinal-satellite-infra-base. Pull model; nothing "
        "pushes to the Lakerunner account."
    )

    # ------------------------------------------------------------------
    # Parameters
    # ------------------------------------------------------------------
    t.add_parameter(
        Parameter(
            "RawBucketName",
            Type="String",
            Description=(
                "Name of the raw ingest bucket (RawBucketName output of "
                "cardinal-satellite-infra-base)."
            ),
            AllowedPattern=r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$",
        )
    )
    t.add_parameter(
        Parameter(
            "OrganizationId",
            Type="String",
            AllowedPattern=(
                r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
            ),
            Description=(
                "UUID of the organization these metrics are attributed to. "
                "Use the same value as the satellite-services stack in this "
                "account -- it becomes the first path segment under "
                "cwmetrics-raw/, which is how Lakerunner resolves the org."
            ),
        )
    )
    t.add_parameter(
        Parameter(
            "ExcludeNamespace",
            Type="String",
            Default="AWS/Usage",
            Description=(
                "A single CloudWatch namespace to exclude from the stream; "
                "every other namespace is streamed. Blank streams all "
                "namespaces. CloudFormation cannot build a variable-length "
                "filter list from a parameter, so this is one namespace, not "
                "a list -- excluding more means editing the generator."
            ),
        )
    )
    t.add_parameter(
        Parameter(
            "FirehoseBufferSeconds",
            Type="Number",
            Default=300,
            MinValue=60,
            MaxValue=900,
            Description=(
                "Seconds Firehose buffers before writing an object. Higher "
                "means fewer, larger objects (fewer SQS messages and fewer "
                "ingest tasks) at the cost of delivery latency."
            ),
        )
    )
    t.add_parameter(
        Parameter(
            "FirehoseBufferSizeMB",
            Type="Number",
            Default=64,
            MinValue=1,
            MaxValue=128,
            Description="Buffer size in MB before Firehose writes an object.",
        )
    )
    t.add_parameter(
        Parameter(
            "NameSuffix",
            Type="String",
            Default="",
            Description=(
                "Optional suffix appended to this stack's fixed physical "
                "names (the Firehose log group) so a second cwmetrics stack "
                "can coexist in one account. Blank keeps the original names, "
                "so existing stacks are unaffected."
            ),
            AllowedPattern=r"^$|^[a-z0-9]([a-z0-9-]{0,14}[a-z0-9])?$",
        )
    )

    add_parameter_group_metadata(
        t,
        groups=[
            {
                "label": "Inputs",
                "parameters": ["RawBucketName", "OrganizationId"],
            },
            {
                "label": "Stream",
                "parameters": [
                    "ExcludeNamespace",
                    "FirehoseBufferSeconds",
                    "FirehoseBufferSizeMB",
                ],
            },
            {"label": "Naming", "parameters": ["NameSuffix"]},
        ],
    )

    t.add_condition("HasNameSuffix", Not(Equals(Ref("NameSuffix"), "")))
    t.add_condition("HasExcludeNamespace", Not(Equals(Ref("ExcludeNamespace"), "")))

    bucket_arn = Sub("arn:${AWS::Partition}:s3:::${RawBucketName}")

    # ------------------------------------------------------------------
    # Firehose delivery log group + role
    # ------------------------------------------------------------------
    log_group = t.add_resource(
        LogGroup(
            "FirehoseLogGroup",
            LogGroupName=If(
                "HasNameSuffix",
                Sub("/cardinal/cwmetrics-firehose-${NameSuffix}"),
                "/cardinal/cwmetrics-firehose",
            ),
            RetentionInDays=_LOG_RETENTION_DAYS,
            Tags=_tags(component="cwmetrics-firehose-log"),
        )
    )
    apply_policy(log_group, "log-group")

    delivery_role = t.add_resource(
        Role(
            "FirehoseDeliveryRole",
            # Unnamed (CloudFormation-generated): unlike the SaaS-side
            # terraform, no cross-account bucket policy has to grant this
            # role by name -- the bucket is in this same account.
            AssumeRolePolicyDocument={
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {"Service": "firehose.amazonaws.com"},
                        "Action": "sts:AssumeRole",
                        "Condition": {
                            "StringEquals": {
                                "sts:ExternalId": Ref("AWS::AccountId")
                            }
                        },
                    }
                ],
            },
            Policies=[
                Policy(
                    PolicyName="cardinal-cwmetrics-delivery",
                    PolicyDocument={
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                # Bucket-level actions cannot be prefix-scoped.
                                "Sid": "RawBucketList",
                                "Effect": "Allow",
                                "Action": [
                                    "s3:GetBucketLocation",
                                    "s3:ListBucket",
                                    "s3:ListBucketMultipartUploads",
                                ],
                                "Resource": bucket_arn,
                            },
                            {
                                # Object writes are scoped to this producer's
                                # two prefixes: Firehose has no business
                                # touching otel-raw/.
                                "Sid": "CwmetricsPrefixWrite",
                                "Effect": "Allow",
                                "Action": [
                                    "s3:AbortMultipartUpload",
                                    "s3:GetObject",
                                    "s3:PutObject",
                                ],
                                "Resource": [
                                    Sub(
                                        "arn:${AWS::Partition}:s3:::"
                                        "${RawBucketName}/" + _RAW_PREFIX + "*"
                                    ),
                                    Sub(
                                        "arn:${AWS::Partition}:s3:::"
                                        "${RawBucketName}/" + _ERROR_PREFIX + "*"
                                    ),
                                ],
                            },
                            {
                                "Sid": "FirehoseErrorLogging",
                                "Effect": "Allow",
                                "Action": ["logs:PutLogEvents"],
                                "Resource": Sub(
                                    "arn:${AWS::Partition}:logs:${AWS::Region}:"
                                    "${AWS::AccountId}:log-group:"
                                    "${LogGroupName}:*",
                                    LogGroupName=Ref(log_group),
                                ),
                            },
                        ],
                    },
                )
            ],
            Tags=_tags(component="cwmetrics-firehose-role"),
        )
    )

    # ------------------------------------------------------------------
    # Delivery stream
    # ------------------------------------------------------------------
    # Explicit !{timestamp:...} namespaces rather than relying on Firehose's
    # implicit date partitioning, so the layout is the same whether or not a
    # custom prefix is in play.  Org first: Lakerunner resolves the org from
    # the first path segment, matching otel-raw/${ORG}/... .
    delivery_stream = t.add_resource(
        DeliveryStream(
            "CwmetricsDeliveryStream",
            DeliveryStreamType="DirectPut",
            ExtendedS3DestinationConfiguration=(
                ExtendedS3DestinationConfiguration(
                    BucketARN=bucket_arn,
                    RoleARN=GetAtt(delivery_role, "Arn"),
                    Prefix=Sub(
                        _RAW_PREFIX
                        + "${OrganizationId}/${AWS::AccountId}/"
                        + "!{timestamp:yyyy/MM/dd/HH}/"
                    ),
                    ErrorOutputPrefix=(
                        _ERROR_PREFIX
                        + "!{firehose:error-output-type}/"
                        + "!{timestamp:yyyy/MM/dd/HH}/"
                    ),
                    CompressionFormat="GZIP",
                    BufferingHints=BufferingHints(
                        IntervalInSeconds=Ref("FirehoseBufferSeconds"),
                        SizeInMBs=Ref("FirehoseBufferSizeMB"),
                    ),
                    # On by default: without it a delivery failure is
                    # invisible and the metrics simply stop arriving.
                    CloudWatchLoggingOptions=CloudWatchLoggingOptions(
                        Enabled=True,
                        LogGroupName=Ref(log_group),
                        LogStreamName="s3-delivery",
                    ),
                )
            ),
            Tags=_tags(component="cwmetrics-firehose"),
        )
    )

    # ------------------------------------------------------------------
    # Metric stream
    # ------------------------------------------------------------------
    stream_role = t.add_resource(
        Role(
            "MetricStreamRole",
            AssumeRolePolicyDocument={
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {
                            "Service": "streams.metrics.cloudwatch.amazonaws.com"
                        },
                        "Action": "sts:AssumeRole",
                        "Condition": {
                            "StringEquals": {
                                "aws:SourceAccount": Ref("AWS::AccountId")
                            }
                        },
                    }
                ],
            },
            Policies=[
                Policy(
                    PolicyName="cardinal-cwmetrics-put",
                    PolicyDocument={
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Sid": "FirehosePut",
                                "Effect": "Allow",
                                "Action": [
                                    "firehose:PutRecord",
                                    "firehose:PutRecordBatch",
                                ],
                                "Resource": GetAtt(delivery_stream, "Arn"),
                            }
                        ],
                    },
                )
            ],
            Tags=_tags(component="cwmetrics-stream-role"),
        )
    )

    t.add_resource(
        MetricStream(
            "CwmetricsStream",
            FirehoseArn=GetAtt(delivery_stream, "Arn"),
            RoleArn=GetAtt(stream_role, "Arn"),
            OutputFormat=_OUTPUT_FORMAT,
            ExcludeFilters=If(
                "HasExcludeNamespace",
                [MetricStreamFilter(Namespace=Ref("ExcludeNamespace"))],
                Ref("AWS::NoValue"),
            ),
            Tags=_tags(component="cwmetrics-stream"),
        )
    )

    # ------------------------------------------------------------------
    # Outputs
    # ------------------------------------------------------------------
    t.add_output(
        Output(
            "DeliveryStreamArn",
            Description="Firehose delivery stream ARN.",
            Value=GetAtt(delivery_stream, "Arn"),
        )
    )
    t.add_output(
        Output(
            "RawPrefix",
            Description="Key prefix these metrics are delivered under.",
            Value=Sub(_RAW_PREFIX + "${OrganizationId}/${AWS::AccountId}/"),
        )
    )
    t.add_output(
        Output(
            "ErrorPrefix",
            Description=(
                "Key prefix for Firehose failed-record output. Not notified "
                "to the ingest queue; inspect manually if metrics go missing."
            ),
            Value=_ERROR_PREFIX,
        )
    )
    t.add_output(
        Output(
            "FirehoseLogGroupName",
            Description="CloudWatch log group carrying Firehose delivery errors.",
            Value=Ref(log_group),
        )
    )

    return t


if __name__ == "__main__":
    print(build().to_yaml(), end="")
