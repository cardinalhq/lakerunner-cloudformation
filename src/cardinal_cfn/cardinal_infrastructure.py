"""cardinal-infrastructure: standalone data-plane template.

Customer-deployable prerequisite for ``cardinal-lakerunner``. Creates the
resources that ``cardinal-lakerunner`` needs as inputs but does not manage
itself:

- RDS PostgreSQL instance + DB subnet group + master secret
- S3 ingest bucket + lifecycle policy + S3 -> SQS notification
- SQS ingest queue + queue policy
- License / admin-key secrets
- /cardinal/storage-profiles and /cardinal/api-keys SSM parameters

The opinionated config (engine version, sizing, lifecycle days,
password shape, secret JSON layout) is the single supported infra
path -- the older shell driver was removed.

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
db-master secret) are CFN-named and collide-free on
retry.
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
from troposphere.ec2 import SecurityGroup
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
    """Standard Cardinal tag set for infra-stack resources."""

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

    vpc_id = t.add_parameter(
        Parameter(
            "VpcId",
            Type="AWS::EC2::VPC::Id",
            Description=(
                "VPC the RDS instance and its security group live in. "
                "Same VPC the lakerunner stack is deployed into."
            ),
        )
    )
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
    organization_id = t.add_parameter(
        Parameter(
            "OrganizationId",
            Type="String",
            Default="12340000-0000-4000-8000-000000000000",
            Description=(
                "Canonical single-install organization UUID. Used in both the "
                "storage-profiles and api-keys SSM seeds; thread the same value "
                "into the lakerunner stack's OrganizationId parameter."
            ),
            AllowedPattern=(
                r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
            ),
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
    # api-keys is plaintext YAML by design (an SSM parameter cannot be a
    # SecureString in CloudFormation), so this key lands in plaintext SSM
    # regardless; NoEcho only keeps it out of the console echo.
    initial_ingest_api_key = add_no_echo_parameter(
        t,
        "InitialIngestApiKey",
        default="",
        description=(
            "Ingest API key seeded into the api-keys SSM parameter for "
            "OrganizationId. Leave blank to seed an empty list (no key)."
        ),
    )

    add_parameter_group_metadata(
        t,
        groups=[
            {
                "label": "Networking",
                "parameters": ["VpcId", "PrivateSubnets"],
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
                "label": "Organization",
                "parameters": ["OrganizationId", "InitialIngestApiKey"],
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
        ],
        labels={
            "VpcId": "VPC for the RDS instance",
            "PrivateSubnets": "Private subnets (2+ in distinct AZs)",
            "DBEngineVersion": "PostgreSQL engine version",
            "DBInstanceClass": "RDS instance class",
            "DBAllocatedStorage": "Storage (GiB)",
            "IngestBucketName": "Ingest bucket name (blank = default)",
            "IngestBucketLifecycleDays": "Ingest object retention (days)",
            "LicenseData": "License token (z64:...)",
            "OrganizationId": "Organization UUID",
            "InitialIngestApiKey": "Initial ingest API key (blank = none)",
        },
    )

    # -----------------------------------------------------------------------
    # Conditions
    # -----------------------------------------------------------------------

    t.add_condition(
        "UseDefaultBucketName",
        Equals(Ref(ingest_bucket_name), ""),
    )

    t.add_condition(
        "HasInitialIngestApiKey",
        Not(Equals(Ref(initial_ingest_api_key), "")),
    )

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
                Tags=_tags(component="ingest-queue"),
            )
        )
    )

    t.add_resource(
        QueuePolicy(
            "IngestQueuePolicy",
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
                # S3 validates the SQS notification destination when the
                # bucket's notification config is applied and fails if the
                # queue policy is not yet in place, so the bucket must be
                # created after IngestQueuePolicy.
                DependsOn="IngestQueuePolicy",
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
            )
        )
    )

    # -----------------------------------------------------------------------
    # RDS subnet group + master secret + DB instance + target attachment
    # -----------------------------------------------------------------------

    db_security_group = t.add_resource(
        _retain(
            SecurityGroup(
                "RdsSecurityGroup",
                GroupDescription=(
                    "Cardinal RDS. Ingress rules are added by the "
                    "lakerunner stack (one per task tier that needs DB "
                    "access)."
                ),
                VpcId=Ref(vpc_id),
                SecurityGroupEgress=[
                    {
                        "IpProtocol": "-1",
                        "CidrIp": "0.0.0.0/0",
                        "Description": "All egress (RDS does not initiate connections; default kept for AWS::EC2::SecurityGroup parity).",
                    }
                ],
                Tags=_tags(component="rds-sg"),
            )
        )
    )

    db_subnet_group = t.add_resource(
        _retain(
            DBSubnetGroup(
                "DBSubnetGroup",
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
                VPCSecurityGroups=[Ref(db_security_group)],
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
            SecretId=Ref(db_master_secret),
            TargetId=Ref(db_instance),
            TargetType="AWS::RDS::DBInstance",
        )
    )

    # -----------------------------------------------------------------------
    # Application secrets (license, admin-key)
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

    # -----------------------------------------------------------------------
    # SSM parameters (operator-managed YAML). The migrator's initializer
    # (lakerunner migrate, since #109/#110) imports these into configdb and
    # expects YAML *lists* -- a top-level map (e.g. "{}") fails to unmarshal
    # into []initialize.StorageProfile and aborts the migration. Mirror
    # Seed storage-profiles with an install-specific profile
    # (bucket + region substituted) and api-keys with an empty list.
    # -----------------------------------------------------------------------

    storage_profiles_param = t.add_resource(
        _retain(
            SSMParameter(
                "StorageProfilesParam",
                Name=Ref(storage_profiles_param_name),
                Type="String",
                Value=Sub(
                    "- organization_id: ${OrgId}\n"
                    "  instance_num: 1\n"
                    "  collector_name: lakerunner\n"
                    "  cloud_provider: aws\n"
                    "  region: ${AWS::Region}\n"
                    "  bucket: ${BucketName}\n"
                    "  insecure_tls: false\n"
                    "  use_path_style: true\n",
                    OrgId=Ref(organization_id),
                    BucketName=bucket_name_value,
                ),
                Description="Cardinal storage profiles (YAML; operator-managed).",
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
                Value=If(
                    "HasInitialIngestApiKey",
                    Sub(
                        "- organization_id: ${OrgId}\n"
                        "  keys:\n"
                        "    - ${Key}\n",
                        OrgId=Ref(organization_id),
                        Key=Ref(initial_ingest_api_key),
                    ),
                    "[]",
                ),
                Description="Cardinal external API keys (YAML; operator-managed).",
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
    # Outputs (consumed as parameters by the lakerunner stack).
    # -----------------------------------------------------------------------

    def _emit_output(name: str, description: str, value) -> None:
        t.add_output(
            Output(
                name,
                Description=description,
                Value=value,
            )
        )

    _emit_output("RdsSecurityGroupId",
                 "Security group ID attached to the RDS instance. The lakerunner "
                 "stack adds tier-specific ingress rules to this group.",
                 Ref(db_security_group))
    _emit_output("DbEndpoint", "RDS endpoint hostname.",
                 GetAtt(db_instance, "Endpoint.Address"))
    _emit_output("DbPort", "RDS endpoint port.",
                 GetAtt(db_instance, "Endpoint.Port"))
    _emit_output("DbName", "RDS database name.", "lakerunner")
    _emit_output("DbMasterSecretArn", "ARN of the RDS master-credentials secret.",
                 Ref(db_master_secret))
    _emit_output("IngestBucketName", "Name of the S3 ingest bucket.",
                 Ref(ingest_bucket))
    _emit_output("IngestQueueUrl", "URL of the SQS ingest queue.",
                 Ref(ingest_queue))
    _emit_output("IngestQueueArn", "ARN of the SQS ingest queue.",
                 GetAtt(ingest_queue, "Arn"))
    _emit_output("LicenseSecretArn", "ARN of the license secret.",
                 Ref(license_secret))
    _emit_output("AdminKeySecretArn", "ARN of the first-boot admin key secret.",
                 Ref(admin_key_secret))
    _emit_output("StorageProfilesParamName", "Name of the storage-profiles SSM parameter.",
                 Ref(storage_profiles_param))
    _emit_output("ApiKeysParamName", "Name of the external API-keys SSM parameter.",
                 Ref(api_keys_param))

    return t


if __name__ == "__main__":
    print(build().to_yaml(), end="")
