"""Generates ``cardinal-data-setup.sh`` -- POSIX shell, idempotent ensure_* steps.

The script is run once per AWS account+region with a privileged identity to
create the data resources Cardinal needs (RDS, S3 ingest, SQS, secrets,
SSM params). It is structured as a sequence of describe-then-act steps so
a partial run on re-execution converges.

Resources created here are immutable from the deployer principal's
perspective: any change post-install is an IT break-glass operation, not
a re-run of this script.
"""

from __future__ import annotations


_HEADER = """\
#!/bin/sh
# cardinal-data-setup.sh -- create RDS, S3 ingest, SQS, secrets, SSM params.
#
# Run once per AWS account+region with a privileged identity. Idempotent:
# matching resource is a no-op, drifted config exits 2 with a diff.
#
# After successful run, hand the printed Key=Value block (or --output-file
# JSON) to whoever runs the cardinal-infra-app and cardinal-lakerunner
# CFN stacks.

set -eu

PROJECT="cardinal"
APPLICATION="cardinal-lakerunner"
MANAGED_BY="cardinal-data-setup-script"

REGION=""
VPC_ID=""
PRIVATE_SUBNETS=""
DB_SG_ID=""
LICENSE_DATA_FILE=""
DEX_ADMIN_EMAIL=""
DEX_ADMIN_PASSWORD_HASH_FILE=""
OIDC_SUPERADMIN_EMAILS=""
DB_INSTANCE_CLASS="db.t3.medium"
DB_ALLOCATED_STORAGE="100"
BUCKET_LIFECYCLE_DAYS="7"
OUTPUT_FILE=""

usage() {
    cat <<'EOF'
Usage: cardinal-data-setup.sh [options]

Required:
  --region                            AWS region.
  --vpc-id                            VPC ID for DB subnet group lookup.
  --private-subnets                   Comma-separated private subnet IDs.
  --db-sg-id                          DB security group ID (from prereqs).
  --license-data-file                 Path to license JSON.
  --dex-admin-email                   DEX admin login email.
  --dex-admin-password-hash-file      File containing the bcrypt hash.

Optional:
  --oidc-superadmin-emails            Comma-separated maestro superadmin allowlist.
  --db-instance-class                 RDS instance class (default db.t3.medium).
  --db-allocated-storage              RDS allocated storage GiB (default 100).
  --bucket-lifecycle-days             S3 ingest expiration days (default 7).
  --output-file                       Write a JSON object of {ParameterKey: Value} to this path.

Exit codes:
  0  success or no-op
  1  AWS / unexpected failure
  2  drift detected or input/preflight failure
EOF
}

log() { printf '[%s] %s\\n' "cardinal-data-setup" "$*" >&2; }
fail() { code="$1"; shift; printf '[cardinal-data-setup] ERROR: %s\\n' "$*" >&2; exit "$code"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --region) REGION="$2"; shift 2 ;;
        --vpc-id) VPC_ID="$2"; shift 2 ;;
        --private-subnets) PRIVATE_SUBNETS="$2"; shift 2 ;;
        --db-sg-id) DB_SG_ID="$2"; shift 2 ;;
        --license-data-file) LICENSE_DATA_FILE="$2"; shift 2 ;;
        --dex-admin-email) DEX_ADMIN_EMAIL="$2"; shift 2 ;;
        --dex-admin-password-hash-file) DEX_ADMIN_PASSWORD_HASH_FILE="$2"; shift 2 ;;
        --oidc-superadmin-emails) OIDC_SUPERADMIN_EMAILS="$2"; shift 2 ;;
        --db-instance-class) DB_INSTANCE_CLASS="$2"; shift 2 ;;
        --db-allocated-storage) DB_ALLOCATED_STORAGE="$2"; shift 2 ;;
        --bucket-lifecycle-days) BUCKET_LIFECYCLE_DAYS="$2"; shift 2 ;;
        --output-file) OUTPUT_FILE="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; fail 2 "unknown argument: $1" ;;
    esac
done

for v in REGION VPC_ID PRIVATE_SUBNETS DB_SG_ID LICENSE_DATA_FILE \\
         DEX_ADMIN_EMAIL DEX_ADMIN_PASSWORD_HASH_FILE; do
    eval "val=\\${$v}"
    [ -n "$val" ] || { usage >&2; fail 2 "--$(echo "$v" | tr '[:upper:]_' '[:lower:]-') required"; }
done

for f in "$LICENSE_DATA_FILE" "$DEX_ADMIN_PASSWORD_HASH_FILE"; do
    [ -r "$f" ] || fail 2 "cannot read file: $f"
done

for tool in aws jq openssl; do
    command -v "$tool" >/dev/null 2>&1 || fail 2 "required tool not found: $tool"
done

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT INT TERM HUP

BUCKET_NAME="cardinal-ingest-${ACCOUNT_ID}-${REGION}"
QUEUE_NAME="cardinal-ingest"
QUEUE_ARN="arn:aws:sqs:${REGION}:${ACCOUNT_ID}:${QUEUE_NAME}"
DB_IDENTIFIER="cardinal-db"
DB_NAME="lakerunner"
DB_USERNAME="lakerunner"
DB_PORT="5432"

OUTPUT_JSON_TMP="$TMP_DIR/output.json"
echo '{}' >"$OUTPUT_JSON_TMP"

emit_output() {
    key="$1"; value="$2"
    jq --arg k "$key" --arg v "$value" '. + {($k): $v}' \\
        "$OUTPUT_JSON_TMP" >"$OUTPUT_JSON_TMP.new"
    mv "$OUTPUT_JSON_TMP.new" "$OUTPUT_JSON_TMP"
    printf 'Key=%s,Value=%s\\n' "$key" "$value"
}
"""


_HELPERS = """\

# ---------------------------------------------------------------------------
# Per-config-item idempotency helpers
# ---------------------------------------------------------------------------
ensure_db_subnet_group() {
    name="$1"
    existing=$(aws rds describe-db-subnet-groups --db-subnet-group-name "$name" \\
        --query 'DBSubnetGroups[0].DBSubnetGroupName' --output text 2>/dev/null || echo "")
    if [ -z "$existing" ] || [ "$existing" = "None" ]; then
        log "creating DB subnet group $name"
        # shellcheck disable=SC2086
        aws rds create-db-subnet-group \\
            --db-subnet-group-name "$name" \\
            --db-subnet-group-description "Cardinal lakerunner DB subnet group" \\
            --subnet-ids $(echo "$PRIVATE_SUBNETS" | tr ',' ' ') \\
            --tags "Key=Application,Value=$APPLICATION" \\
                   "Key=Project,Value=$PROJECT" \\
                   "Key=ManagedBy,Value=$MANAGED_BY" \\
                   "Key=Component,Value=db-subnet-group" \\
                   "Key=Name,Value=cardinal-db-subnet-group" >/dev/null
    else
        log "DB subnet group $name exists"
    fi
}

ensure_db_instance() {
    db_id="$1"
    existing=$(aws rds describe-db-instances --db-instance-identifier "$db_id" \\
        --query 'DBInstances[0].DBInstanceStatus' --output text 2>/dev/null || echo "")
    if [ -z "$existing" ] || [ "$existing" = "None" ]; then
        log "creating DB instance $db_id (this can take 10+ minutes)"
        master_password=$(aws secretsmanager get-random-password \\
            --password-length 40 --exclude-punctuation --require-each-included-type \\
            --query 'RandomPassword' --output text)
        ensure_secret_with_value cardinal-db-master "$master_password" "RDS master password (placeholder; replaced post-create with full connection JSON)"

        aws rds create-db-instance \\
            --db-instance-identifier "$db_id" \\
            --db-instance-class "$DB_INSTANCE_CLASS" \\
            --engine postgres \\
            --master-username "$DB_USERNAME" \\
            --master-user-password "$master_password" \\
            --allocated-storage "$DB_ALLOCATED_STORAGE" \\
            --storage-type gp3 \\
            --storage-encrypted \\
            --db-name "$DB_NAME" \\
            --port "$DB_PORT" \\
            --db-subnet-group-name cardinal-db-subnet-group \\
            --vpc-security-group-ids "$DB_SG_ID" \\
            --no-publicly-accessible \\
            --backup-retention-period 7 \\
            --deletion-protection \\
            --tags "Key=Application,Value=$APPLICATION" \\
                   "Key=Project,Value=$PROJECT" \\
                   "Key=ManagedBy,Value=$MANAGED_BY" \\
                   "Key=Component,Value=db" \\
                   "Key=Name,Value=cardinal-db" >/dev/null
    else
        log "DB instance $db_id exists (status: $existing)"
    fi
}

wait_db_available() {
    db_id="$1"
    log "waiting for DB instance $db_id to reach available state"
    aws rds wait db-instance-available --db-instance-identifier "$db_id"
}

ensure_db_master_secret_value() {
    secret_name="$1"
    db_id="$2"
    endpoint=$(aws rds describe-db-instances --db-instance-identifier "$db_id" \\
        --query 'DBInstances[0].Endpoint.Address' --output text)
    port=$(aws rds describe-db-instances --db-instance-identifier "$db_id" \\
        --query 'DBInstances[0].Endpoint.Port' --output text)
    current_password=$(aws secretsmanager get-secret-value --secret-id "$secret_name" \\
        --query 'SecretString' --output text)
    case "$current_password" in
        '{'*) log "secret $secret_name already in connection-JSON form" ;;
        *)
            log "writing connection JSON into secret $secret_name"
            new_value=$(jq -n \\
                --arg user "$DB_USERNAME" \\
                --arg pwd "$current_password" \\
                --arg host "$endpoint" \\
                --arg port "$port" \\
                --arg db "$DB_NAME" \\
                '{username:$user, password:$pwd, engine:"postgres", host:$host, port:($port|tonumber), dbname:$db}')
            aws secretsmanager put-secret-value --secret-id "$secret_name" \\
                --secret-string "$new_value" >/dev/null
            ;;
    esac
}

ensure_s3_bucket() {
    bucket="$1"
    if aws s3api head-bucket --bucket "$bucket" 2>/dev/null; then
        log "bucket $bucket exists"
        return 0
    fi
    log "creating bucket $bucket"
    if [ "$REGION" = "us-east-1" ]; then
        aws s3api create-bucket --bucket "$bucket" >/dev/null
    else
        aws s3api create-bucket --bucket "$bucket" \\
            --create-bucket-configuration "LocationConstraint=$REGION" >/dev/null
    fi
    aws s3api put-bucket-tagging --bucket "$bucket" \\
        --tagging "TagSet=[{Key=Application,Value=$APPLICATION},{Key=Project,Value=$PROJECT},{Key=ManagedBy,Value=$MANAGED_BY},{Key=Component,Value=ingest-bucket},{Key=Name,Value=$bucket}]" >/dev/null
}

ensure_s3_lifecycle() {
    bucket="$1"
    days="$2"
    cat >"$TMP_DIR/lifecycle.json" <<JSON
{"Rules":[{"ID":"cardinal-ingest-expire","Filter":{"Prefix":""},"Status":"Enabled","Expiration":{"Days":$days},"AbortIncompleteMultipartUpload":{"DaysAfterInitiation":1}}]}
JSON
    aws s3api put-bucket-lifecycle-configuration --bucket "$bucket" \\
        --lifecycle-configuration "file://$TMP_DIR/lifecycle.json" >/dev/null
    log "applied lifecycle (expire after $days days) to $bucket"
}

ensure_s3_block_public_access() {
    bucket="$1"
    aws s3api put-public-access-block --bucket "$bucket" \\
        --public-access-block-configuration \\
        "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true" >/dev/null
    log "applied block-public-access to $bucket"
}

ensure_sqs_queue() {
    queue_name="$1"
    existing=$(aws sqs get-queue-url --queue-name "$queue_name" --query 'QueueUrl' --output text 2>/dev/null || echo "")
    if [ -n "$existing" ] && [ "$existing" != "None" ]; then
        log "queue $queue_name exists"
        printf '%s\\n' "$existing"
        return 0
    fi
    log "creating queue $queue_name" >&2
    aws sqs create-queue --queue-name "$queue_name" \\
        --tags "Application=$APPLICATION,Project=$PROJECT,ManagedBy=$MANAGED_BY,Component=ingest-queue,Name=$queue_name" \\
        --query 'QueueUrl' --output text
}

ensure_sqs_policy() {
    queue_url="$1"
    bucket="$2"
    cat >"$TMP_DIR/queue-policy.json" <<JSON
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"s3.amazonaws.com"},"Action":["sqs:SendMessage","sqs:GetQueueAttributes","sqs:GetQueueUrl"],"Resource":"$QUEUE_ARN","Condition":{"StringEquals":{"aws:SourceAccount":"$ACCOUNT_ID"},"ArnLike":{"aws:SourceArn":"arn:aws:s3:::$bucket"}}}]}
JSON
    policy=$(jq -c . "$TMP_DIR/queue-policy.json")
    aws sqs set-queue-attributes --queue-url "$queue_url" \\
        --attributes "Policy=$policy" >/dev/null
    log "applied queue policy to $queue_url"
}

ensure_s3_notification() {
    bucket="$1"
    cat >"$TMP_DIR/notification.json" <<JSON
{"QueueConfigurations":[{"Id":"cardinal-ingest-to-sqs","QueueArn":"$QUEUE_ARN","Events":["s3:ObjectCreated:*"]}]}
JSON
    aws s3api put-bucket-notification-configuration --bucket "$bucket" \\
        --notification-configuration "file://$TMP_DIR/notification.json" >/dev/null
    log "applied S3->SQS notification config to $bucket"
}

ensure_secret_with_value() {
    secret_name="$1"
    secret_value="$2"
    description="${3:-Cardinal lakerunner secret}"
    existing_arn=$(aws secretsmanager describe-secret --secret-id "$secret_name" \\
        --query 'ARN' --output text 2>/dev/null || echo "")
    if [ -z "$existing_arn" ] || [ "$existing_arn" = "None" ]; then
        log "creating secret $secret_name"
        aws secretsmanager create-secret --name "$secret_name" \\
            --description "$description" \\
            --secret-string "$secret_value" \\
            --tags "Key=Application,Value=$APPLICATION" \\
                   "Key=Project,Value=$PROJECT" \\
                   "Key=ManagedBy,Value=$MANAGED_BY" \\
                   "Key=Component,Value=$secret_name" \\
                   "Key=Name,Value=$secret_name" >/dev/null
    else
        log "secret $secret_name exists; not overwriting"
    fi
}

ensure_ssm_parameter() {
    name="$1"
    value="$2"
    description="$3"
    if aws ssm get-parameter --name "$name" >/dev/null 2>&1; then
        log "ssm parameter $name exists"
        return 0
    fi
    log "creating ssm parameter $name"
    aws ssm put-parameter --name "$name" \\
        --type String --value "$value" \\
        --description "$description" \\
        --tags "Key=Application,Value=$APPLICATION" \\
               "Key=Project,Value=$PROJECT" \\
               "Key=ManagedBy,Value=$MANAGED_BY" \\
               "Key=Component,Value=ssm-parameter" \\
               "Key=Name,Value=$name" >/dev/null
}
"""


_STORAGE = """\

# ---------------------------------------------------------------------------
# Storage: SQS queue first, queue policy, S3 bucket, lifecycle/block, notif
# ---------------------------------------------------------------------------
QUEUE_URL=$(ensure_sqs_queue "$QUEUE_NAME")

# S3 bucket created BEFORE queue policy referencing it via ArnLike condition
ensure_s3_bucket "$BUCKET_NAME"
ensure_s3_block_public_access "$BUCKET_NAME"
ensure_s3_lifecycle "$BUCKET_NAME" "$BUCKET_LIFECYCLE_DAYS"
ensure_sqs_policy "$QUEUE_URL" "$BUCKET_NAME"
ensure_s3_notification "$BUCKET_NAME"
"""


_DATABASE = """\

# ---------------------------------------------------------------------------
# Database: subnet group, instance, wait, then write connection JSON
# ---------------------------------------------------------------------------
ensure_db_subnet_group cardinal-db-subnet-group
ensure_db_instance cardinal-db
wait_db_available cardinal-db
ensure_db_master_secret_value cardinal-db-master cardinal-db
"""


_SECRETS = """\

# ---------------------------------------------------------------------------
# Application secrets
# ---------------------------------------------------------------------------
license_value=$(cat "$LICENSE_DATA_FILE")
ensure_secret_with_value cardinal-license "$license_value" "Cardinal lakerunner license JSON"

internal_keys=$(openssl rand -hex 32)
ensure_secret_with_value cardinal-internal-keys "$internal_keys" "Internal service keys (random 32-byte hex)"

admin_key=$(openssl rand -hex 32)
ensure_secret_with_value cardinal-admin-key "$admin_key" "First-boot admin API key (rotated by admin-api)"

dex_hash=$(cat "$DEX_ADMIN_PASSWORD_HASH_FILE")
maestro_db_value=$(jq -n \\
    --arg email "$DEX_ADMIN_EMAIL" \\
    --arg hash "$dex_hash" \\
    --arg superadmins "$OIDC_SUPERADMIN_EMAILS" \\
    '{dex_admin_email:$email, dex_admin_password_hash:$hash, oidc_superadmin_emails:$superadmins}')
ensure_secret_with_value cardinal-maestro-db "$maestro_db_value" "Maestro/DEX OIDC config"
"""


_SSM = """\

# ---------------------------------------------------------------------------
# SSM parameters (placeholders; operator overwrites with real config)
# ---------------------------------------------------------------------------
ensure_ssm_parameter /cardinal/storage-profiles '{}' "Cardinal storage profiles (operator-managed JSON)"
ensure_ssm_parameter /cardinal/api-keys '{}' "Cardinal external API keys (operator-managed JSON)"
"""


_OUTPUT = """\

# ---------------------------------------------------------------------------
# Capture all the ARNs/URLs/names the next layers need
# ---------------------------------------------------------------------------
DB_ENDPOINT=$(aws rds describe-db-instances --db-instance-identifier cardinal-db \\
    --query 'DBInstances[0].Endpoint.Address' --output text)
DB_MASTER_SECRET_ARN=$(aws secretsmanager describe-secret --secret-id cardinal-db-master --query 'ARN' --output text)
LICENSE_SECRET_ARN=$(aws secretsmanager describe-secret --secret-id cardinal-license --query 'ARN' --output text)
INTERNAL_KEYS_SECRET_ARN=$(aws secretsmanager describe-secret --secret-id cardinal-internal-keys --query 'ARN' --output text)
ADMIN_KEY_SECRET_ARN=$(aws secretsmanager describe-secret --secret-id cardinal-admin-key --query 'ARN' --output text)
MAESTRO_DB_SECRET_ARN=$(aws secretsmanager describe-secret --secret-id cardinal-maestro-db --query 'ARN' --output text)

emit_output DbEndpoint "$DB_ENDPOINT"
emit_output DbPort "$DB_PORT"
emit_output DbName "$DB_NAME"
emit_output DbMasterSecretArn "$DB_MASTER_SECRET_ARN"
emit_output MaestroDbSecretArn "$MAESTRO_DB_SECRET_ARN"
emit_output IngestBucketName "$BUCKET_NAME"
emit_output IngestQueueUrl "$QUEUE_URL"
emit_output IngestQueueArn "$QUEUE_ARN"
emit_output LicenseSecretArn "$LICENSE_SECRET_ARN"
emit_output InternalKeysSecretArn "$INTERNAL_KEYS_SECRET_ARN"
emit_output AdminKeySecretArn "$ADMIN_KEY_SECRET_ARN"
emit_output StorageProfilesParamName /cardinal/storage-profiles
emit_output ApiKeysParamName /cardinal/api-keys

if [ -n "$OUTPUT_FILE" ]; then
    cp "$OUTPUT_JSON_TMP" "$OUTPUT_FILE"
    log "wrote $OUTPUT_FILE"
fi

log "done"
"""


def render_data_setup_script() -> str:
    return _HEADER + _HELPERS + _STORAGE + _DATABASE + _SECRETS + _SSM + _OUTPUT


if __name__ == "__main__":
    import sys

    sys.stdout.write(render_data_setup_script())
