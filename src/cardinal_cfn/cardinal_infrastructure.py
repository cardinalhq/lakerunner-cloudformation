"""cardinal-infrastructure: standalone data-plane template.

Customer-deployable peer to ``cardinal-vpc``. Creates the resources that
``cardinal-lakerunner`` needs as inputs but does not manage itself:

- RDS PostgreSQL instance + DB subnet group + master secret
- S3 ingest bucket + lifecycle policy + S3 -> SQS notification
- SQS ingest queue + queue policy
- License / internal-keys / admin-key / maestro-db secrets
- /cardinal/storage-profiles and /cardinal/api-keys SSM parameters

Conceptually a CloudFormation port of ``scripts/data-setup.sh``; the
opinionated config (engine version, sizing, lifecycle days, password
shape, secret JSON layout) mirrors the script 1:1.

Outputs match the keys the script emits, so the lakerunner stack can
consume either path identically.

Importing existing resources
----------------------------

To bring an existing data-setup.sh-created install under stack
management:

1. Run a ``create-change-set --change-set-type IMPORT`` with
   ``ImportMode=Yes`` and the matching ``*Name`` overrides set to the
   live physical names. The CFN-only resources (``IngestQueuePolicy``
   and ``DBMasterSecretAttachment``) are skipped in this mode -- CFN
   import does not support resources that lack a real AWS-side
   physical ID.
2. After import succeeds, run a normal stack update with
   ``ImportMode=No``. CFN creates the two skipped resources via their
   underlying API calls (``set-queue-attributes`` and
   ``put-secret-value``). Both are no-ops when the live config already
   matches the template, which it will for any install bootstrapped
   by ``scripts/data-setup.sh``.

Recovery from a failed first create
-----------------------------------

Every resource has ``DeletionPolicy: Retain`` (RDS uses ``Snapshot``).
A rollback after partial create therefore orphans whatever was already
created. Resources with explicit physical names (the S3 ingest bucket,
the two SSM parameters, the license + admin-key secrets) will then
collide on retry. To recover:

1. Delete the failed stack -- orphaned resources stay put.
2. Either: (a) pass alternate values for ``IngestBucketName`` /
   ``LicenseSecretName`` / ``AdminKeySecretName`` /
   ``StorageProfilesParamName`` / ``ApiKeysParamName`` on retry, or
   (b) manually delete the orphans via the console (note: Secrets
   Manager imposes a 7-day minimum recovery window unless you call
   ``delete-secret --force-delete-without-recovery``), then redeploy.

Resources without explicit names (RDS, SQS, DB subnet group, the
db-master / internal-keys / maestro-db secrets) are CFN-named and
collide-free on retry.
"""

from troposphere import (
    AWS_NO_VALUE,
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
from troposphere.rds import DBInstance, DBSubnetGroup
from troposphere.s3 import (
    AbortIncompleteMultipartUpload,
    Bucket,
    LifecycleConfiguration,
    LifecycleRule,
    NotificationConfiguration,
    QueueConfigurations,
)
from troposphere.secretsmanager import (
    GenerateSecretString,
    Secret,
    SecretTargetAttachment,
)
from troposphere.sqs import Queue, QueuePolicy
from troposphere.ssm import Parameter as SSMParameter

from cardinal_cfn.parameters import add_no_echo_parameter, add_parameter_group_metadata


MANAGED_BY = "cardinal-cfn-infrastructure"
PROJECT = "cardinal"
APPLICATION = "cardinal-lakerunner"


def _tags(*, component: str) -> Tags:
    """Tag set matching scripts/data-setup.sh's ``tags_json_array``."""

    return Tags(
        Application=APPLICATION,
        Project=PROJECT,
        ManagedBy=MANAGED_BY,
        Component=component,
        Name=f"cardinal-{component}",
    )


def _retain(resource):
    resource.DeletionPolicy = "Retain"
    resource.UpdateReplacePolicy = "Retain"
    return resource


def _snapshot(resource):
    resource.DeletionPolicy = "Snapshot"
    resource.UpdateReplacePolicy = "Snapshot"
    return resource


def build() -> Template:
    t = Template()
    t.set_description(
        "Cardinal infrastructure: RDS, S3 ingest bucket, SQS ingest queue, "
        "secrets, and SSM parameters consumed by the cardinal-lakerunner stack. "
        "All resources retain on stack delete (RDS snapshots) so customer data "
        "survives rollback or stack tear-down."
    )

    # -----------------------------------------------------------------------
    # Parameters
    # -----------------------------------------------------------------------

    private_subnets = t.add_parameter(
        Parameter(
            "PrivateSubnets",
            Type="List<AWS::EC2::Subnet::Id>",
            Description=(
                "Two or more private subnets in distinct AZs for the RDS "
                "subnet group."
            ),
        )
    )
    db_sg_id = t.add_parameter(
        Parameter(
            "DBSecurityGroupId",
            Type="AWS::EC2::SecurityGroup::Id",
            Description=(
                "Security group attached to the RDS instance. The customer "
                "creates this and grants the lakerunner ECS tasks ingress."
            ),
        )
    )
    db_engine_version = t.add_parameter(
        Parameter(
            "DBEngineVersion",
            Type="String",
            Default="18.3",
            Description="PostgreSQL engine version for the RDS instance.",
        )
    )
    db_instance_class = t.add_parameter(
        Parameter(
            "DBInstanceClass",
            Type="String",
            Default="db.t3.medium",
            Description="RDS instance class.",
        )
    )
    db_allocated_storage = t.add_parameter(
        Parameter(
            "DBAllocatedStorage",
            Type="Number",
            Default=100,
            MinValue=20,
            Description="Allocated storage for the RDS instance, in GiB.",
        )
    )
    bucket_lifecycle_days = t.add_parameter(
        Parameter(
            "IngestBucketLifecycleDays",
            Type="Number",
            Default=7,
            MinValue=1,
            Description=(
                "Days after which objects in the ingest bucket expire. "
                "Lakerunner consumes objects within minutes; this is the "
                "garbage-collection backstop."
            ),
        )
    )
    ingest_bucket_name = t.add_parameter(
        Parameter(
            "IngestBucketName",
            Type="String",
            Default="",
            Description=(
                "Name for the ingest bucket. Leave blank to use the default "
                "cardinal-ingest-<account>-<region>. Override on a redeploy "
                "if recovering from a failed create that orphaned the bucket."
            ),
            AllowedPattern=r"^$|^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$",
        )
    )
    license_secret_name = t.add_parameter(
        Parameter(
            "LicenseSecretName",
            Type="String",
            Default="cardinal-license",
            Description=(
                "Secrets Manager name for the license secret. Override on "
                "redeploy if a previous create orphaned the secret."
            ),
            MinLength=1,
        )
    )
    admin_key_secret_name = t.add_parameter(
        Parameter(
            "AdminKeySecretName",
            Type="String",
            Default="cardinal-admin-key",
            Description=(
                "Secrets Manager name for the first-boot admin key secret. "
                "Override on redeploy if a previous create orphaned it."
            ),
            MinLength=1,
        )
    )
    storage_profiles_param_name = t.add_parameter(
        Parameter(
            "StorageProfilesParamName",
            Type="String",
            Default="/cardinal/storage-profiles",
            Description=(
                "SSM parameter name for the operator-managed storage-profiles "
                "JSON. Override on redeploy if a previous create orphaned it."
            ),
            AllowedPattern=r"^/[A-Za-z0-9._/-]{1,1011}$",
        )
    )
    api_keys_param_name = t.add_parameter(
        Parameter(
            "ApiKeysParamName",
            Type="String",
            Default="/cardinal/api-keys",
            Description=(
                "SSM parameter name for the operator-managed external "
                "API keys JSON. Override on redeploy if orphaned."
            ),
            AllowedPattern=r"^/[A-Za-z0-9._/-]{1,1011}$",
        )
    )
    license_data = add_no_echo_parameter(
        t,
        "LicenseData",
        description=(
            "Cardinal license token (z64:...). Stored verbatim as the "
            "string body of the license secret."
        ),
    )

    # -----------------------------------------------------------------------
    # Optional explicit-name parameters (used when importing an existing
    # data-setup.sh-created install into the stack). Blank => CFN-auto-named.
    # -----------------------------------------------------------------------

    db_instance_identifier = t.add_parameter(
        Parameter(
            "DBInstanceIdentifier",
            Type="String",
            Default="",
            Description=(
                "Optional explicit DB instance identifier. Set to the "
                "existing identifier (e.g. 'cardinal-db') when importing; "
                "leave blank for fresh installs."
            ),
        )
    )
    db_subnet_group_name = t.add_parameter(
        Parameter(
            "DBSubnetGroupName",
            Type="String",
            Default="",
            Description=(
                "Optional explicit DB subnet group name. Set when "
                "importing an existing subnet group; blank otherwise."
            ),
        )
    )
    ingest_queue_name = t.add_parameter(
        Parameter(
            "IngestQueueName",
            Type="String",
            Default="",
            Description=(
                "Optional explicit SQS queue name. Set when importing "
                "an existing queue (e.g. 'cardinal-ingest'); blank "
                "otherwise."
            ),
        )
    )
    db_master_secret_name = t.add_parameter(
        Parameter(
            "DBMasterSecretName",
            Type="String",
            Default="",
            Description=(
                "Optional explicit name for the DB master secret. Set "
                "to the existing name (e.g. 'cardinal-db-master') when "
                "importing; blank otherwise."
            ),
        )
    )
    internal_keys_secret_name = t.add_parameter(
        Parameter(
            "InternalKeysSecretName",
            Type="String",
            Default="",
            Description=(
                "Optional explicit name for the internal-keys secret. "
                "Set to the existing name (e.g. 'cardinal-internal-keys') "
                "when importing; blank otherwise."
            ),
        )
    )
    maestro_db_secret_name = t.add_parameter(
        Parameter(
            "MaestroDBSecretName",
            Type="String",
            Default="",
            Description=(
                "Optional explicit name for the maestro-db secret. Set "
                "to the existing name (e.g. 'cardinal-maestro-db') when "
                "importing; blank otherwise."
            ),
        )
    )
    import_mode = t.add_parameter(
        Parameter(
            "ImportMode",
            Type="String",
            Default="No",
            AllowedValues=["Yes", "No"],
            Description=(
                "Set to 'Yes' when running this template as a "
                "create-change-set --change-set-type IMPORT. The "
                "CFN-only resources (DBMasterSecretAttachment and the "
                "IngestQueuePolicy) are skipped in this mode and must "
                "be added by a follow-up stack update with "
                "ImportMode=No."
            ),
        )
    )

    add_parameter_group_metadata(
        t,
        groups=[
            {
                "label": "Networking",
                "parameters": ["PrivateSubnets", "DBSecurityGroupId"],
            },
            {
                "label": "Database sizing",
                "parameters": [
                    "DBEngineVersion",
                    "DBInstanceClass",
                    "DBAllocatedStorage",
                ],
            },
            {
                "label": "Ingest",
                "parameters": [
                    "IngestBucketName",
                    "IngestBucketLifecycleDays",
                ],
            },
            {
                "label": "License",
                "parameters": ["LicenseData"],
            },
            {
                "label": "Recovery overrides (advanced)",
                "parameters": [
                    "LicenseSecretName",
                    "AdminKeySecretName",
                    "StorageProfilesParamName",
                    "ApiKeysParamName",
                ],
            },
            {
                "label": "Import overrides (set only when importing existing resources)",
                "parameters": [
                    "DBInstanceIdentifier",
                    "DBSubnetGroupName",
                    "IngestQueueName",
                    "DBMasterSecretName",
                    "InternalKeysSecretName",
                    "MaestroDBSecretName",
                ],
            },
        ],
        labels={
            "PrivateSubnets": "Private subnets (2+ in distinct AZs)",
            "DBSecurityGroupId": "Security group for the RDS instance",
            "DBEngineVersion": "PostgreSQL engine version",
            "DBInstanceClass": "RDS instance class",
            "DBAllocatedStorage": "Storage (GiB)",
            "IngestBucketName": "Ingest bucket name (blank = default)",
            "IngestBucketLifecycleDays": "Ingest object retention (days)",
            "LicenseData": "License token (z64:...)",
        },
    )

    # -----------------------------------------------------------------------
    # Conditions
    # -----------------------------------------------------------------------

    t.add_condition(
        "UseDefaultBucketName",
        Equals(Ref(ingest_bucket_name), ""),
    )

    # CFN has no "not-equals". Each ``AutoNameX`` is True iff the
    # corresponding override parameter is blank. Use site idiom:
    # ``If("AutoNameX", AWS_NO_VALUE, Ref(X))``.
    t.add_condition(
        "AutoNameDBInstance", Equals(Ref(db_instance_identifier), "")
    )
    t.add_condition(
        "AutoNameDBSubnetGroup", Equals(Ref(db_subnet_group_name), "")
    )
    t.add_condition("AutoNameIngestQueue", Equals(Ref(ingest_queue_name), ""))
    t.add_condition(
        "AutoNameDBMasterSecret", Equals(Ref(db_master_secret_name), "")
    )
    t.add_condition(
        "AutoNameInternalKeysSecret",
        Equals(Ref(internal_keys_secret_name), ""),
    )
    t.add_condition(
        "AutoNameMaestroDBSecret", Equals(Ref(maestro_db_secret_name), "")
    )
    t.add_condition("CreateCfnOnlyResources", Equals(Ref(import_mode), "No"))

    bucket_name_value = If(
        "UseDefaultBucketName",
        Sub("cardinal-ingest-${AWS::AccountId}-${AWS::Region}"),
        Ref(ingest_bucket_name),
    )

    # -----------------------------------------------------------------------
    # SQS ingest queue + S3 source policy
    # -----------------------------------------------------------------------

    ingest_queue = t.add_resource(
        _retain(
            Queue(
                "IngestQueue",
                QueueName=If(
                    "AutoNameIngestQueue", Ref(AWS_NO_VALUE), Ref(ingest_queue_name)
                ),
                Tags=_tags(component="ingest-queue"),
            )
        )
    )

    ingest_queue_policy = t.add_resource(
        QueuePolicy(
            "IngestQueuePolicy",
            Condition="CreateCfnOnlyResources",
            Queues=[Ref(ingest_queue)],
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
                        "Resource": GetAtt(ingest_queue, "Arn"),
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

    # -----------------------------------------------------------------------
    # S3 ingest bucket
    # -----------------------------------------------------------------------

    ingest_bucket = t.add_resource(
        _retain(
            Bucket(
                "IngestBucket",
                BucketName=bucket_name_value,
                LifecycleConfiguration=LifecycleConfiguration(
                    Rules=[
                        LifecycleRule(
                            Id="cardinal-ingest-expire",
                            Status="Enabled",
                            Prefix="",
                            ExpirationInDays=Ref(bucket_lifecycle_days),
                            AbortIncompleteMultipartUpload=AbortIncompleteMultipartUpload(
                                DaysAfterInitiation=1
                            ),
                        )
                    ]
                ),
                NotificationConfiguration=NotificationConfiguration(
                    QueueConfigurations=[
                        QueueConfigurations(
                            Event="s3:ObjectCreated:*",
                            Queue=GetAtt(ingest_queue, "Arn"),
                        )
                    ]
                ),
                Tags=_tags(component="ingest-bucket"),
                DependsOn=ingest_queue_policy.title,
            )
        )
    )

    # -----------------------------------------------------------------------
    # RDS subnet group + master secret + DB instance + target attachment
    # -----------------------------------------------------------------------

    db_subnet_group = t.add_resource(
        _retain(
            DBSubnetGroup(
                "DBSubnetGroup",
                DBSubnetGroupName=If(
                    "AutoNameDBSubnetGroup",
                    Ref(AWS_NO_VALUE),
                    Ref(db_subnet_group_name),
                ),
                DBSubnetGroupDescription="Cardinal lakerunner DB subnet group",
                SubnetIds=Ref(private_subnets),
                Tags=_tags(component="db-subnet-group"),
            )
        )
    )

    db_master_secret = t.add_resource(
        _retain(
            Secret(
                "DBMasterSecret",
                Name=If(
                    "AutoNameDBMasterSecret",
                    Ref(AWS_NO_VALUE),
                    Ref(db_master_secret_name),
                ),
                Description=(
                    "Cardinal RDS master credentials. Connection JSON "
                    "(host/port/engine/dbname) is filled in by the "
                    "associated SecretTargetAttachment."
                ),
                GenerateSecretString=GenerateSecretString(
                    SecretStringTemplate='{"username":"lakerunner"}',
                    GenerateStringKey="password",
                    PasswordLength=40,
                    ExcludePunctuation=True,
                ),
                Tags=_tags(component="db-master"),
            )
        )
    )

    db_instance = t.add_resource(
        _snapshot(
            DBInstance(
                "DBInstance",
                DBInstanceIdentifier=If(
                    "AutoNameDBInstance",
                    Ref(AWS_NO_VALUE),
                    Ref(db_instance_identifier),
                ),
                Engine="postgres",
                EngineVersion=Ref(db_engine_version),
                DBInstanceClass=Ref(db_instance_class),
                AllocatedStorage=Ref(db_allocated_storage),
                StorageType="gp3",
                StorageEncrypted=True,
                DBName="lakerunner",
                Port=5432,
                MasterUsername="lakerunner",
                MasterUserPassword=Sub(
                    "{{resolve:secretsmanager:${SecretArn}::password}}",
                    SecretArn=Ref(db_master_secret),
                ),
                DBSubnetGroupName=Ref(db_subnet_group),
                VPCSecurityGroups=[Ref(db_sg_id)],
                PubliclyAccessible=False,
                MultiAZ=False,
                BackupRetentionPeriod=7,
                DeletionProtection=True,
                Tags=_tags(component="db"),
            )
        )
    )

    # SecretTargetAttachment rewrites the secret to embed
    # {engine, host, port, dbname} alongside username/password -- matching
    # the connection JSON the lakerunner task containers consume.
    t.add_resource(
        SecretTargetAttachment(
            "DBMasterSecretAttachment",
            Condition="CreateCfnOnlyResources",
            SecretId=Ref(db_master_secret),
            TargetId=Ref(db_instance),
            TargetType="AWS::RDS::DBInstance",
        )
    )

    # -----------------------------------------------------------------------
    # Application secrets (license, internal-keys, admin-key, maestro-db)
    # -----------------------------------------------------------------------

    license_secret = t.add_resource(
        _retain(
            Secret(
                "LicenseSecret",
                Name=Ref(license_secret_name),
                Description="Cardinal lakerunner license token (z64:...).",
                SecretString=Ref(license_data),
                Tags=_tags(component="license"),
            )
        )
    )

    internal_keys_secret = t.add_resource(
        _retain(
            Secret(
                "InternalKeysSecret",
                Name=If(
                    "AutoNameInternalKeysSecret",
                    Ref(AWS_NO_VALUE),
                    Ref(internal_keys_secret_name),
                ),
                Description=(
                    "Internal service keys: opaque high-entropy string "
                    "shared between lakerunner services."
                ),
                GenerateSecretString=GenerateSecretString(
                    PasswordLength=64,
                    ExcludePunctuation=True,
                ),
                Tags=_tags(component="internal-keys"),
            )
        )
    )

    admin_key_secret = t.add_resource(
        _retain(
            Secret(
                "AdminKeySecret",
                Name=Ref(admin_key_secret_name),
                Description=(
                    "First-boot admin API key. JSON shape "
                    '{"key": "<random>"} so the ECS secret pointer '
                    '":key::" resolves at task launch.'
                ),
                GenerateSecretString=GenerateSecretString(
                    SecretStringTemplate="{}",
                    GenerateStringKey="key",
                    PasswordLength=64,
                    ExcludePunctuation=True,
                ),
                Tags=_tags(component="admin-key"),
            )
        )
    )

    maestro_db_secret = t.add_resource(
        _retain(
            Secret(
                "MaestroDBSecret",
                Name=If(
                    "AutoNameMaestroDBSecret",
                    Ref(AWS_NO_VALUE),
                    Ref(maestro_db_secret_name),
                ),
                Description=(
                    'Maestro app DB credential. JSON shape '
                    '{"username":"maestro","password":"<random>"}.'
                ),
                GenerateSecretString=GenerateSecretString(
                    SecretStringTemplate='{"username":"maestro"}',
                    GenerateStringKey="password",
                    PasswordLength=40,
                    ExcludePunctuation=True,
                ),
                Tags=_tags(component="maestro-db"),
            )
        )
    )

    # -----------------------------------------------------------------------
    # SSM parameters (operator-managed JSON, default {})
    # -----------------------------------------------------------------------

    storage_profiles_param = t.add_resource(
        _retain(
            SSMParameter(
                "StorageProfilesParam",
                Name=Ref(storage_profiles_param_name),
                Type="String",
                Value="{}",
                Description="Cardinal storage profiles (operator-managed JSON).",
                Tags={
                    "Application": APPLICATION,
                    "Project": PROJECT,
                    "ManagedBy": MANAGED_BY,
                    "Component": "ssm-storage-profiles",
                    "Name": "cardinal-ssm-storage-profiles",
                },
            )
        )
    )

    api_keys_param = t.add_resource(
        _retain(
            SSMParameter(
                "ApiKeysParam",
                Name=Ref(api_keys_param_name),
                Type="String",
                Value="{}",
                Description="Cardinal external API keys (operator-managed JSON).",
                Tags={
                    "Application": APPLICATION,
                    "Project": PROJECT,
                    "ManagedBy": MANAGED_BY,
                    "Component": "ssm-api-keys",
                    "Name": "cardinal-ssm-api-keys",
                },
            )
        )
    )

    # -----------------------------------------------------------------------
    # Outputs (1:1 with the JSON keys that scripts/data-setup.sh emits)
    # -----------------------------------------------------------------------

    t.add_output(
        Output(
            "DbEndpoint",
            Description="RDS endpoint hostname.",
            Value=GetAtt(db_instance, "Endpoint.Address"),
        )
    )
    t.add_output(
        Output(
            "DbPort",
            Description="RDS endpoint port.",
            Value=GetAtt(db_instance, "Endpoint.Port"),
        )
    )
    t.add_output(
        Output(
            "DbName",
            Description="RDS database name.",
            Value="lakerunner",
        )
    )
    t.add_output(
        Output(
            "DbMasterSecretArn",
            Description="ARN of the RDS master-credentials secret.",
            Value=Ref(db_master_secret),
        )
    )
    t.add_output(
        Output(
            "MaestroDbSecretArn",
            Description="ARN of the Maestro app DB credentials secret.",
            Value=Ref(maestro_db_secret),
        )
    )
    t.add_output(
        Output(
            "IngestBucketName",
            Description="Name of the S3 ingest bucket.",
            Value=Ref(ingest_bucket),
        )
    )
    t.add_output(
        Output(
            "IngestQueueUrl",
            Description="URL of the SQS ingest queue.",
            Value=Ref(ingest_queue),
        )
    )
    t.add_output(
        Output(
            "IngestQueueArn",
            Description="ARN of the SQS ingest queue.",
            Value=GetAtt(ingest_queue, "Arn"),
        )
    )
    t.add_output(
        Output(
            "LicenseSecretArn",
            Description="ARN of the license secret.",
            Value=Ref(license_secret),
        )
    )
    t.add_output(
        Output(
            "InternalKeysSecretArn",
            Description="ARN of the internal service keys secret.",
            Value=Ref(internal_keys_secret),
        )
    )
    t.add_output(
        Output(
            "AdminKeySecretArn",
            Description="ARN of the first-boot admin key secret.",
            Value=Ref(admin_key_secret),
        )
    )
    t.add_output(
        Output(
            "StorageProfilesParamName",
            Description="Name of the storage-profiles SSM parameter.",
            Value=Ref(storage_profiles_param),
        )
    )
    t.add_output(
        Output(
            "ApiKeysParamName",
            Description="Name of the external API-keys SSM parameter.",
            Value=Ref(api_keys_param),
        )
    )

    return t


if __name__ == "__main__":
    print(build().to_yaml(), end="")
