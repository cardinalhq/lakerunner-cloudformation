"""Cardinal data-setup Lambda.

Run by a privileged execution role to create the Cardinal data-bearing
infrastructure: RDS Postgres, S3 ingest bucket (with lifecycle and
S3->SQS notification), SQS ingest queue, license / internal-keys /
admin-key / maestro-db secrets, and the two SSM parameters.

Idempotent: every step does describe-then-act on a deterministic name,
so re-invocations after partial failure converge. The execution role
has full update/delete on every resource the Lambda manages, so
recovery from a partially-created install does not require any
out-of-band IT involvement -- the Lambda fixes its own messes.

Two invocation paths:

1. CFN custom resource. The wrapper template
   ``cardinal-data-setup.yaml`` deploys this Lambda and a
   ``Custom::CardinalDataSetup`` resource that triggers Create / Update
   / Delete events. The handler conforms to the cfn-response protocol:
   it sends ``SUCCESS`` with the Data dict on completion or ``FAILED``
   with the error message on exception.

2. Direct invoke. ``aws lambda invoke --function-name
   cardinal-data-setup ...`` with an event body that omits
   ``RequestType``. The handler short-circuits the cfn-response branch
   and just runs the ensure_* sequence. Returns the same Data dict.

Event schema (Properties for CFN; top-level keys for direct invoke):

    Region                       AWS region (defaults to AWS_REGION env)
    VpcId                        VPC ID for DB subnet group lookup
    PrivateSubnets               comma-separated subnet IDs (or list)
    DbSgId                       SG ID applied to the RDS instance
    LicenseData                  license JSON, raw string (NoEcho upstream)
    DexAdminEmail                string
    DexAdminPasswordHash         bcrypt hash, raw string (NoEcho upstream)
    OidcSuperadminEmails         comma-separated email allowlist
    DbInstanceClass              default "db.t3.medium"
    DbAllocatedStorage           default 100 (GiB)
    BucketLifecycleDays          default 7

Output dict (returned + sent as cfn-response Data):

    DbEndpoint, DbPort, DbName,
    DbMasterSecretArn, MaestroDbSecretArn,
    IngestBucketName, IngestQueueUrl, IngestQueueArn,
    LicenseSecretArn, InternalKeysSecretArn, AdminKeySecretArn,
    StorageProfilesParamName, ApiKeysParamName.

Naming contract:

    ECS cluster                     cardinal
    S3 ingest bucket                cardinal-ingest-<account>-<region>
    SQS ingest queue                cardinal-ingest
    Migration ECS task family       cardinal-migrator
    Per-service log groups          /cardinal/<service>
    SSM params                      /cardinal/storage-profiles, /cardinal/api-keys
    Secrets                         cardinal-{db-master,license,internal-keys,
                                      admin-key,maestro-db}

These names are referenced by both the lakerunner stack and the
customer-supplied IAM policies. Drift here breaks every consumer.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import secrets
import time
import urllib.parse
from typing import Any

import boto3
from botocore.exceptions import ClientError


logger = logging.getLogger()
logger.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Constants -- the naming contract. Do not change without updating both the
# lakerunner template parameter wiring and the customer IAM cookbook.
# ---------------------------------------------------------------------------
PROJECT = "cardinal"
APPLICATION = "cardinal-lakerunner"
MANAGED_BY = "cardinal-data-setup-lambda"

DB_IDENTIFIER = "cardinal-db"
DB_SUBNET_GROUP_NAME = "cardinal-db-subnet-group"
DB_NAME = "lakerunner"
DB_USERNAME = "lakerunner"
DB_PORT = 5432

SQS_QUEUE_NAME = "cardinal-ingest"

SECRET_NAMES = {
    "db_master": "cardinal-db-master",
    "license": "cardinal-license",
    "internal_keys": "cardinal-internal-keys",
    "admin_key": "cardinal-admin-key",
    "maestro_db": "cardinal-maestro-db",
}

SSM_PARAM_NAMES = {
    "storage_profiles": "/cardinal/storage-profiles",
    "api_keys": "/cardinal/api-keys",
}


def _common_tags(component: str) -> list[dict[str, str]]:
    return [
        {"Key": "Application", "Value": APPLICATION},
        {"Key": "Project", "Value": PROJECT},
        {"Key": "ManagedBy", "Value": MANAGED_BY},
        {"Key": "Component", "Value": component},
        {"Key": "Name", "Value": f"cardinal-{component}"},
    ]


def _bucket_name(account_id: str, region: str) -> str:
    return f"cardinal-ingest-{account_id}-{region}"


def _queue_arn(account_id: str, region: str) -> str:
    return f"arn:aws:sqs:{region}:{account_id}:{SQS_QUEUE_NAME}"


# ---------------------------------------------------------------------------
# CFN custom-resource response transport
# ---------------------------------------------------------------------------
def _send_cfn_response(event: dict, status: str, data: dict, reason: str = "") -> None:
    response_url = event.get("ResponseURL")
    if not response_url:
        return
    body = {
        "Status": status,
        "Reason": reason or f"See CloudWatch logs: {os.environ.get('AWS_LAMBDA_LOG_STREAM_NAME', '?')}",
        "PhysicalResourceId": event.get("PhysicalResourceId") or "cardinal-data-setup",
        "StackId": event["StackId"],
        "RequestId": event["RequestId"],
        "LogicalResourceId": event["LogicalResourceId"],
        "Data": data,
    }
    encoded = json.dumps(body).encode("utf-8")
    parsed = urllib.parse.urlparse(response_url)
    conn = http.client.HTTPSConnection(parsed.netloc)
    try:
        conn.request(
            "PUT",
            parsed.path + ("?" + parsed.query if parsed.query else ""),
            body=encoded,
            headers={"Content-Type": "", "Content-Length": str(len(encoded))},
        )
        resp = conn.getresponse()
        resp.read()
        if resp.status >= 300:
            logger.error("cfn-response %s: %s", resp.status, resp.reason)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Idempotency helpers
# ---------------------------------------------------------------------------
def _props(event: dict) -> dict:
    """Return the resource properties (CFN custom resource) or the event itself."""
    return event.get("ResourceProperties") or event


def _str(props: dict, key: str, default: str | None = None) -> str:
    val = props.get(key, default)
    if val is None:
        raise ValueError(f"missing required property: {key}")
    return str(val).strip()


def _int(props: dict, key: str, default: int | None = None) -> int:
    val = props.get(key, default)
    if val is None:
        raise ValueError(f"missing required property: {key}")
    return int(val)


def _list(props: dict, key: str) -> list[str]:
    val = props.get(key)
    if val is None:
        raise ValueError(f"missing required property: {key}")
    if isinstance(val, list):
        return [str(x).strip() for x in val if str(x).strip()]
    return [s.strip() for s in str(val).split(",") if s.strip()]


def ensure_db_subnet_group(rds, name: str, subnets: list[str]) -> None:
    try:
        rds.describe_db_subnet_groups(DBSubnetGroupName=name)
        logger.info("db subnet group %s exists", name)
        return
    except ClientError as e:
        if e.response["Error"]["Code"] != "DBSubnetGroupNotFoundFault":
            raise
    logger.info("creating db subnet group %s", name)
    rds.create_db_subnet_group(
        DBSubnetGroupName=name,
        DBSubnetGroupDescription="Cardinal lakerunner DB subnet group",
        SubnetIds=subnets,
        Tags=_common_tags("db-subnet-group"),
    )


def ensure_db_instance(rds, *, db_id: str, sg_id: str, instance_class: str, allocated_storage: int, master_password: str) -> None:
    try:
        rds.describe_db_instances(DBInstanceIdentifier=db_id)
        logger.info("db instance %s exists", db_id)
        return
    except ClientError as e:
        if e.response["Error"]["Code"] != "DBInstanceNotFound":
            raise
    logger.info("creating db instance %s (this can take 10+ minutes)", db_id)
    rds.create_db_instance(
        DBInstanceIdentifier=db_id,
        DBInstanceClass=instance_class,
        Engine="postgres",
        MasterUsername=DB_USERNAME,
        MasterUserPassword=master_password,
        AllocatedStorage=allocated_storage,
        StorageType="gp3",
        StorageEncrypted=True,
        DBName=DB_NAME,
        Port=DB_PORT,
        DBSubnetGroupName=DB_SUBNET_GROUP_NAME,
        VpcSecurityGroupIds=[sg_id],
        PubliclyAccessible=False,
        BackupRetentionPeriod=7,
        DeletionProtection=True,
        Tags=_common_tags("db"),
    )


def wait_db_available(rds, db_id: str, timeout_seconds: int = 840) -> dict:
    """Poll until the DB instance is available or the timeout expires.

    Default timeout 840s (14 minutes) is intentionally below the Lambda's
    900s execution-time cap so the handler can still send a FAILED
    response on timeout. RDS creates routinely fit in this window for
    `db.t3.medium` / `db.t3.large` with default storage; very large
    instance classes or busy regions may take longer, in which case the
    Lambda exits with a TimeoutError, the customer re-invokes, and the
    second call sees the DB is now available and skips the create step.
    """
    deadline = time.time() + timeout_seconds
    while True:
        resp = rds.describe_db_instances(DBInstanceIdentifier=db_id)
        instance = resp["DBInstances"][0]
        status = instance["DBInstanceStatus"]
        if status == "available":
            return instance
        if time.time() > deadline:
            raise TimeoutError(
                f"db {db_id} did not become available within {timeout_seconds}s "
                f"(current: {status}). Re-invoke the Lambda to continue waiting; "
                f"ensure_* helpers will skip steps that have already completed."
            )
        logger.info("db %s status: %s; sleeping", db_id, status)
        time.sleep(30)


def ensure_db_master_secret(secretsmanager, *, name: str, username: str) -> tuple[str, str]:
    """Idempotently materialize the DB master secret. Returns (arn, password).

    Convergence rule: the password is the contract between the DB instance
    and this secret. If the secret already exists, we return its current
    password so the caller passes the SAME password to ``create_db_instance``
    on a partial re-run (DB create may have failed previously, but the
    secret was created). If the secret does not exist, we generate a fresh
    one and write it before any DB-create attempt.
    """
    try:
        secretsmanager.describe_secret(SecretId=name)
        raw = secretsmanager.get_secret_value(SecretId=name)["SecretString"]
        existing = json.loads(raw)
        if "password" not in existing:
            raise ValueError(f"secret {name} exists but is missing 'password' key")
        logger.info("secret %s exists; reusing existing password", name)
        return secretsmanager.describe_secret(SecretId=name)["ARN"], existing["password"]
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise

    password = _generate_password()
    placeholder = json.dumps({"username": username, "password": password})
    logger.info("creating secret %s with fresh password", name)
    resp = secretsmanager.create_secret(
        Name=name,
        Description="Cardinal RDS master credentials (initial placeholder; updated post-DB-create with full connection JSON)",
        SecretString=placeholder,
        Tags=_common_tags(name),
    )
    return resp["ARN"], password


def update_db_master_secret_value(secretsmanager, *, name: str, db_instance: dict) -> None:
    """Write the full connection JSON into the existing secret."""
    raw = secretsmanager.get_secret_value(SecretId=name)["SecretString"]
    if raw.startswith("{") and '"host"' in raw:
        logger.info("secret %s already contains connection JSON", name)
        return
    parsed = json.loads(raw)
    new_value = {
        "username": parsed["username"],
        "password": parsed["password"],
        "engine": "postgres",
        "host": db_instance["Endpoint"]["Address"],
        "port": db_instance["Endpoint"]["Port"],
        "dbname": DB_NAME,
    }
    logger.info("writing connection JSON into secret %s", name)
    secretsmanager.put_secret_value(SecretId=name, SecretString=json.dumps(new_value))


def ensure_secret_with_value(secretsmanager, *, name: str, value: str, description: str) -> str:
    try:
        resp = secretsmanager.describe_secret(SecretId=name)
        logger.info("secret %s exists; not overwriting", name)
        return resp["ARN"]
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
    logger.info("creating secret %s", name)
    resp = secretsmanager.create_secret(
        Name=name,
        Description=description,
        SecretString=value,
        Tags=_common_tags(name),
    )
    return resp["ARN"]


def ensure_s3_bucket(s3, *, bucket: str, region: str) -> None:
    try:
        s3.head_bucket(Bucket=bucket)
        logger.info("bucket %s exists", bucket)
        return
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code not in ("404", "NoSuchBucket", "NotFound"):
            raise
    logger.info("creating bucket %s", bucket)
    if region == "us-east-1":
        s3.create_bucket(Bucket=bucket)
    else:
        s3.create_bucket(
            Bucket=bucket,
            CreateBucketConfiguration={"LocationConstraint": region},
        )
    s3.put_bucket_tagging(
        Bucket=bucket,
        Tagging={"TagSet": _common_tags("ingest-bucket")},
    )


def ensure_s3_block_public_access(s3, bucket: str) -> None:
    s3.put_public_access_block(
        Bucket=bucket,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        },
    )
    logger.info("applied block-public-access to %s", bucket)


def ensure_s3_lifecycle(s3, *, bucket: str, expiration_days: int) -> None:
    s3.put_bucket_lifecycle_configuration(
        Bucket=bucket,
        LifecycleConfiguration={
            "Rules": [
                {
                    "ID": "cardinal-ingest-expire",
                    "Filter": {"Prefix": ""},
                    "Status": "Enabled",
                    "Expiration": {"Days": expiration_days},
                    "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1},
                }
            ]
        },
    )
    logger.info("applied lifecycle (expire after %d days) to %s", expiration_days, bucket)


def ensure_sqs_queue(sqs, queue_name: str) -> str:
    try:
        return sqs.get_queue_url(QueueName=queue_name)["QueueUrl"]
    except ClientError as e:
        if e.response["Error"]["Code"] != "AWS.SimpleQueueService.NonExistentQueue":
            raise
    logger.info("creating queue %s", queue_name)
    resp = sqs.create_queue(
        QueueName=queue_name,
        tags={
            "Application": APPLICATION,
            "Project": PROJECT,
            "ManagedBy": MANAGED_BY,
            "Component": "ingest-queue",
            "Name": queue_name,
        },
    )
    return resp["QueueUrl"]


def ensure_sqs_policy(sqs, *, queue_url: str, queue_arn: str, bucket: str, account_id: str) -> None:
    policy_doc = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "s3.amazonaws.com"},
                "Action": ["sqs:SendMessage", "sqs:GetQueueAttributes", "sqs:GetQueueUrl"],
                "Resource": queue_arn,
                "Condition": {
                    "StringEquals": {"aws:SourceAccount": account_id},
                    "ArnLike": {"aws:SourceArn": f"arn:aws:s3:::{bucket}"},
                },
            }
        ],
    }
    sqs.set_queue_attributes(QueueUrl=queue_url, Attributes={"Policy": json.dumps(policy_doc)})
    logger.info("applied queue policy to %s", queue_url)


def ensure_s3_notification(s3, *, bucket: str, queue_arn: str) -> None:
    s3.put_bucket_notification_configuration(
        Bucket=bucket,
        NotificationConfiguration={
            "QueueConfigurations": [
                {
                    "Id": "cardinal-ingest-to-sqs",
                    "QueueArn": queue_arn,
                    "Events": ["s3:ObjectCreated:*"],
                }
            ]
        },
    )
    logger.info("applied S3->SQS notification config to %s", bucket)


def ensure_ssm_parameter(ssm, *, name: str, value: str, description: str) -> None:
    try:
        ssm.get_parameter(Name=name)
        logger.info("ssm parameter %s exists", name)
        return
    except ClientError as e:
        if e.response["Error"]["Code"] != "ParameterNotFound":
            raise
    logger.info("creating ssm parameter %s", name)
    ssm.put_parameter(
        Name=name,
        Type="String",
        Value=value,
        Description=description,
        Tags=_common_tags("ssm-parameter"),
    )


# ---------------------------------------------------------------------------
# Orchestration: the actual create/update sequence
# ---------------------------------------------------------------------------
def run(props: dict, region: str | None = None) -> dict:
    region = region or props.get("Region") or os.environ.get("AWS_REGION")
    if not region:
        raise ValueError("region is required (Region property or AWS_REGION env)")

    sts = boto3.client("sts", region_name=region)
    rds = boto3.client("rds", region_name=region)
    s3 = boto3.client("s3", region_name=region)
    sqs = boto3.client("sqs", region_name=region)
    secretsmanager = boto3.client("secretsmanager", region_name=region)
    ssm = boto3.client("ssm", region_name=region)

    account_id = sts.get_caller_identity()["Account"]

    subnets = _list(props, "PrivateSubnets")
    db_sg_id = _str(props, "DbSgId")
    db_instance_class = _str(props, "DbInstanceClass", "db.t3.medium")
    db_allocated_storage = _int(props, "DbAllocatedStorage", 100)
    bucket_lifecycle_days = _int(props, "BucketLifecycleDays", 7)
    license_data = _str(props, "LicenseData")
    dex_admin_email = _str(props, "DexAdminEmail")
    dex_admin_password_hash = _str(props, "DexAdminPasswordHash")
    oidc_superadmin_emails = _str(props, "OidcSuperadminEmails", "")

    bucket_name = _bucket_name(account_id, region)
    queue_arn = _queue_arn(account_id, region)

    # ------------------------------------------------------------------ storage
    queue_url = ensure_sqs_queue(sqs, SQS_QUEUE_NAME)
    ensure_s3_bucket(s3, bucket=bucket_name, region=region)
    ensure_s3_block_public_access(s3, bucket_name)
    ensure_s3_lifecycle(s3, bucket=bucket_name, expiration_days=bucket_lifecycle_days)
    ensure_sqs_policy(sqs, queue_url=queue_url, queue_arn=queue_arn, bucket=bucket_name, account_id=account_id)
    ensure_s3_notification(s3, bucket=bucket_name, queue_arn=queue_arn)

    # ----------------------------------------------------------------- database
    ensure_db_subnet_group(rds, DB_SUBNET_GROUP_NAME, subnets)
    # The secret carries the source-of-truth password. ensure_db_master_secret
    # generates one on first create and reuses the existing one on re-run, so
    # ensure_db_instance always sees a password that matches the secret.
    db_master_secret_arn, master_password = ensure_db_master_secret(
        secretsmanager,
        name=SECRET_NAMES["db_master"],
        username=DB_USERNAME,
    )
    ensure_db_instance(
        rds,
        db_id=DB_IDENTIFIER,
        sg_id=db_sg_id,
        instance_class=db_instance_class,
        allocated_storage=db_allocated_storage,
        master_password=master_password,
    )
    db_instance = wait_db_available(rds, DB_IDENTIFIER)
    update_db_master_secret_value(secretsmanager, name=SECRET_NAMES["db_master"], db_instance=db_instance)

    # ------------------------------------------------------------------ secrets
    license_secret_arn = ensure_secret_with_value(
        secretsmanager,
        name=SECRET_NAMES["license"],
        value=license_data,
        description="Cardinal lakerunner license JSON",
    )
    internal_keys_secret_arn = ensure_secret_with_value(
        secretsmanager,
        name=SECRET_NAMES["internal_keys"],
        value=_random_hex(32),
        description="Internal service keys (random 32-byte hex)",
    )
    admin_key_secret_arn = ensure_secret_with_value(
        secretsmanager,
        name=SECRET_NAMES["admin_key"],
        value=_random_hex(32),
        description="First-boot admin API key (rotated by admin-api)",
    )
    maestro_db_secret_arn = ensure_secret_with_value(
        secretsmanager,
        name=SECRET_NAMES["maestro_db"],
        value=json.dumps({
            "dex_admin_email": dex_admin_email,
            "dex_admin_password_hash": dex_admin_password_hash,
            "oidc_superadmin_emails": oidc_superadmin_emails,
        }),
        description="Maestro/DEX OIDC config",
    )

    # --------------------------------------------------------------------- SSM
    ensure_ssm_parameter(
        ssm,
        name=SSM_PARAM_NAMES["storage_profiles"],
        value="{}",
        description="Cardinal storage profiles (operator-managed JSON)",
    )
    ensure_ssm_parameter(
        ssm,
        name=SSM_PARAM_NAMES["api_keys"],
        value="{}",
        description="Cardinal external API keys (operator-managed JSON)",
    )

    return {
        "DbEndpoint": db_instance["Endpoint"]["Address"],
        "DbPort": str(db_instance["Endpoint"]["Port"]),
        "DbName": DB_NAME,
        "DbMasterSecretArn": db_master_secret_arn,
        "MaestroDbSecretArn": maestro_db_secret_arn,
        "IngestBucketName": bucket_name,
        "IngestQueueUrl": queue_url,
        "IngestQueueArn": queue_arn,
        "LicenseSecretArn": license_secret_arn,
        "InternalKeysSecretArn": internal_keys_secret_arn,
        "AdminKeySecretArn": admin_key_secret_arn,
        "StorageProfilesParamName": SSM_PARAM_NAMES["storage_profiles"],
        "ApiKeysParamName": SSM_PARAM_NAMES["api_keys"],
    }


def _generate_password() -> str:
    """40-char URL-safe password without punctuation that breaks shells."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(40))


def _random_hex(byte_length: int) -> str:
    return secrets.token_hex(byte_length)


# ---------------------------------------------------------------------------
# Lambda entrypoint
# ---------------------------------------------------------------------------
def handler(event: dict, context: Any) -> dict:
    logger.info("event: %s", json.dumps({k: v for k, v in event.items() if k != "ResourceProperties"}))
    request_type = event.get("RequestType")

    if request_type == "Delete":
        # Default no-op on Delete: data resources are intentionally retained.
        # The Lambda role has the permission to actually delete; flipping the
        # default is a future config flag (DeletePolicy property).
        logger.info("Delete event: no-op (data resources retained by policy)")
        if event.get("ResponseURL"):
            _send_cfn_response(event, "SUCCESS", {})
        return {"status": "noop-on-delete"}

    try:
        data = run(_props(event))
    except Exception as exc:  # noqa: BLE001 -- we want to translate every failure to cfn-response
        logger.exception("data-setup failed")
        if event.get("ResponseURL"):
            _send_cfn_response(event, "FAILED", {}, reason=str(exc)[:1024])
        raise

    if event.get("ResponseURL"):
        _send_cfn_response(event, "SUCCESS", data)
    return data
