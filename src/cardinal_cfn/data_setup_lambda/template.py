"""Generates ``cardinal-data-setup.yaml`` -- the CFN wrapper that deploys
the data-setup Lambda and invokes it via a custom resource.

Customer flow with this template:

1. Customer's IT pre-creates ``DataSetupLambdaRoleArn`` (a Lambda
   execution role with full create/update/delete on RDS, S3, SQS,
   Secrets Manager, SSM, plus VPC config + logs).
2. Customer creates this CFN stack with the role ARN and the data
   inputs (VPC, subnets, DB SG ID, license, etc.). The stack:
   - Deploys ``cardinal-data-setup`` Lambda (code from S3).
   - Triggers it once via a ``Custom::CardinalDataSetup`` custom
     resource on stack-create.
   - Exposes the Lambda's outputs (DB endpoint, secret ARNs, bucket
     name, queue URL/ARN, SSM names) as stack outputs the customer
     copies into the lakerunner stack's parameters.
3. Stack-update re-invokes the Lambda; ensure_* helpers no-op on
   already-correct state.
4. Stack-delete: Lambda's Delete handler is a no-op by default
   (data resources retained). Customer can re-invoke the Lambda
   manually with a future DeletePolicy flag if they want teardown.

Alternative: customer skips this template entirely, deploys the
Lambda by hand, and runs ``aws lambda invoke``. The Lambda handler
supports both flows.
"""

from __future__ import annotations

import os

from troposphere import (
    GetAtt,
    Join,
    Output,
    Parameter,
    Ref,
    Select,
    Split,
    Tags,
    Template,
)
from troposphere.cloudformation import CustomResource
from troposphere.awslambda import Code, Function


VERSION = os.environ.get("CARDINAL_VERSION", "dev")
# us-east-1 is the publishing source-of-truth; cardinal-cfn-us-east-2 is
# populated via S3 bucket replication. Override CARDINAL_BUCKET_NAME for
# air-gapped builds that target a customer-owned mirror.
DEFAULT_BUCKET = os.environ.get("CARDINAL_BUCKET_NAME", "cardinal-cfn-us-east-1")
DEFAULT_LAMBDA_URL = (
    f"s3://{DEFAULT_BUCKET}/lakerunner/{VERSION}/cardinal-data-setup-lambda.zip"
)


def build_template() -> Template:
    t = Template()
    t.set_description(
        f"Cardinal lakerunner -- data-setup Lambda wrapper. version={VERSION}. "
        "Deploys the Python Lambda that creates RDS, S3 ingest, SQS, secrets, "
        "and SSM parameters, then invokes it once. Outputs become parameters "
        "to the lakerunner stack."
    )
    t.set_metadata({"cardinal:version": VERSION})

    # ---- parameters --------------------------------------------------------
    t.add_parameter(Parameter(
        "DataSetupLambdaRoleArn",
        Type="String",
        Description=(
            "ARN of the Lambda execution role the customer's IT pre-created. "
            "Must allow create+update+delete on RDS, S3, SQS, Secrets Manager, "
            "SSM, plus VPC config + logs. See docs/operations/required-roles.md."
        ),
    ))
    t.add_parameter(Parameter(
        "VpcId",
        Type="AWS::EC2::VPC::Id",
        Description="VPC ID for the DB subnet group.",
    ))
    t.add_parameter(Parameter(
        "PrivateSubnets",
        Type="List<AWS::EC2::Subnet::Id>",
        Description="Private subnets for the RDS subnet group.",
    ))
    t.add_parameter(Parameter(
        "DbSgId",
        Type="AWS::EC2::SecurityGroup::Id",
        Description="DB security group ID (customer-supplied).",
    ))
    t.add_parameter(Parameter(
        "LicenseData",
        Type="String",
        NoEcho=True,
        Description="Cardinal lakerunner license JSON (raw).",
    ))
    t.add_parameter(Parameter(
        "DbInstanceClass",
        Type="String",
        Default="db.t3.medium",
        Description="RDS instance class.",
    ))
    t.add_parameter(Parameter(
        "DbAllocatedStorage",
        Type="Number",
        Default=100,
        MinValue=20,
        Description="RDS allocated storage in GiB.",
    ))
    t.add_parameter(Parameter(
        "BucketLifecycleDays",
        Type="Number",
        Default=7,
        MinValue=1,
        Description="S3 ingest bucket object expiration in days.",
    ))
    t.add_parameter(Parameter(
        "LambdaCodeS3Url",
        Type="String",
        Default=DEFAULT_LAMBDA_URL,
        AllowedPattern=r"^s3://[^/]+/[^/]+/[^/]+/[^/]+\.zip$",
        ConstraintDescription=(
            "LambdaCodeS3Url must be an s3://<bucket>/<prefix>/<version>/<file>.zip URL "
            "(exactly three slash-separated path segments). The bucket must be in the same "
            "region as this stack."
        ),
        Description=(
            "Full s3:// URL of the data-setup Lambda zip. The bucket must be in the same "
            "region as this stack. Default targets the published bucket in us-east-2; "
            "override for other regions or air-gapped mirrors. Example: "
            "s3://cardinal-cfn-us-east-1/lakerunner/v0.0.42/cardinal-data-setup-lambda.zip."
        ),
    ))

    # ---- Lambda function ---------------------------------------------------
    fn = t.add_resource(Function(
        "DataSetupFunction",
        FunctionName="cardinal-data-setup",
        Runtime="python3.11",
        Handler="handler.handler",
        Role=Ref("DataSetupLambdaRoleArn"),
        # 900 = AWS hard cap. wait_db_available()'s timeout (840s) is set
        # to leave ~60s for the handler to send the cfn-response on slow-DB
        # timeout, after which the operator re-invokes; ensure_* helpers
        # skip already-completed steps.
        Timeout=900,
        MemorySize=512,
        # Parse LambdaCodeS3Url (s3://bucket/prefix/version/file.zip) into the
        # S3Bucket + S3Key pair the Lambda CFN resource requires. AllowedPattern
        # on the parameter enforces the three-segment shape this parsing assumes.
        Code=Code(
            S3Bucket=Select(2, Split("/", Ref("LambdaCodeS3Url"))),
            S3Key=Join("/", [
                Select(3, Split("/", Ref("LambdaCodeS3Url"))),
                Select(4, Split("/", Ref("LambdaCodeS3Url"))),
                Select(5, Split("/", Ref("LambdaCodeS3Url"))),
            ]),
        ),
        Description="Cardinal lakerunner data-setup: idempotent ensure_* over RDS, S3, SQS, Secrets, SSM.",
        Tags=Tags(
            Application="cardinal-lakerunner",
            Project="cardinal",
            ManagedBy="cardinal-data-setup-stack",
            Component="data-setup-lambda",
            Name="cardinal-data-setup",
        ),
    ))

    # ---- Custom resource: invoke the Lambda once on Create -----------------
    cr = t.add_resource(CustomResource(
        "DataSetup",
        ServiceToken=GetAtt(fn, "Arn"),
        # The next properties are passed through to the Lambda as
        # event["ResourceProperties"]; the handler reads them by key.
        Region=Ref("AWS::Region"),
        VpcId=Ref("VpcId"),
        PrivateSubnets=Ref("PrivateSubnets"),
        DbSgId=Ref("DbSgId"),
        LicenseData=Ref("LicenseData"),
        DbInstanceClass=Ref("DbInstanceClass"),
        DbAllocatedStorage=Ref("DbAllocatedStorage"),
        BucketLifecycleDays=Ref("BucketLifecycleDays"),
    ))

    # ---- Outputs (one per Lambda response key) -----------------------------
    for key in [
        "DbEndpoint",
        "DbPort",
        "DbName",
        "DbMasterSecretArn",
        "MaestroDbSecretArn",
        "IngestBucketName",
        "IngestQueueUrl",
        "IngestQueueArn",
        "LicenseSecretArn",
        "InternalKeysSecretArn",
        "AdminKeySecretArn",
        "StorageProfilesParamName",
        "ApiKeysParamName",
    ]:
        t.add_output(Output(
            key,
            Value=GetAtt(cr, key),
            Description=f"Forwarded from cardinal-data-setup Lambda response.",
        ))

    return t


if __name__ == "__main__":
    print(build_template().to_yaml())
