#!/bin/sh
# Cardinal lakerunner infra-setup driver, in raw AWS CLI.
#
# Creates the data resources the lakerunner application stack needs
# but does not manage itself: RDS, S3 ingest, SQS, the five `cardinal-*`
# Secrets Manager secrets, and the two SSM parameters.
#
# Caller-supplied compute-plane identifiers (ECS cluster, Cloud Map
# namespace) are accepted as env vars and passed through to the JSON
# output unchanged -- the script does not create or validate them.
#
# Idempotent. Each step does describe-then-act on the deterministic
# names below; re-running after a partial failure converges. The script
# can be re-run safely against a partially-created install.
#
# Output: a JSON document on stdout whose keys map 1:1 to the
# cardinal-lakerunner stack parameters that name infra resources.
#
# Dependencies: POSIX shell, AWS CLI v2, jq, openssl.

set -eu

# ============================================================================
#  PARAMETERS  --  edit these (or set as env vars before invocation)
# ============================================================================

# Required.
: "${REGION:=us-east-2}"
: "${PRIVATE_SUBNETS:=subnet-aaaaaaaa,subnet-bbbbbbbb}"   # CSV, >=2 subnets in distinct AZs
: "${DB_SG_ID:=sg-xxxxxxxx}"                              # cardinal-db-sg

# Caller-supplied compute-plane identifiers (passed through to the JSON
# output verbatim; not created or validated here).
: "${CLUSTER_NAME:=}"
: "${CLUSTER_ARN:=}"
: "${SERVICE_NAMESPACE_ID:=}"
: "${SERVICE_NAMESPACE_NAME:=}"

# License token (z64:...). Provide exactly one of:
#   LICENSE_DATA       -- the token itself (single-line string)
#   LICENSE_DATA_FILE  -- path to a file whose contents are the token
# LICENSE_DATA wins if both are set.
: "${LICENSE_DATA:=}"
: "${LICENSE_DATA_FILE:=}"

# Optional sizing (defaults match the Lambda).
: "${DB_INSTANCE_CLASS:=db.t3.medium}"
: "${DB_ALLOCATED_STORAGE:=100}"
: "${BUCKET_LIFECYCLE_DAYS:=7}"

# ============================================================================
#  CONSTANTS  --  these names are the contract with the lakerunner stack
#                  (mirrors handler.py constants; do not change)
# ============================================================================

DB_IDENTIFIER="cardinal-db"
DB_SUBNET_GROUP_NAME="cardinal-db-subnet-group"
DB_NAME="lakerunner"
DB_USERNAME="lakerunner"
DB_PORT=5432

SQS_QUEUE_NAME="cardinal-ingest"

SECRET_DB_MASTER="cardinal-db-master"
SECRET_LICENSE="cardinal-license"
SECRET_INTERNAL_KEYS="cardinal-internal-keys"
SECRET_ADMIN_KEY="cardinal-admin-key"
SECRET_MAESTRO_DB="cardinal-maestro-db"

SSM_STORAGE_PROFILES="/cardinal/storage-profiles"
SSM_API_KEYS="/cardinal/api-keys"

PROJECT="cardinal"
APPLICATION="cardinal-lakerunner"
MANAGED_BY="cardinal-data-setup-script"

# ============================================================================
#  HELPERS
# ============================================================================

log()  { printf '[data-setup] %s\n' "$*" >&2; }
fail() { printf '[data-setup] ERROR: %s\n' "$*" >&2; exit 1; }

for tool in aws jq openssl; do
    command -v "$tool" >/dev/null 2>&1 || fail "$tool is required on PATH"
done

if [ -n "${LICENSE_DATA}" ]; then
    license_data="${LICENSE_DATA}"
elif [ -n "${LICENSE_DATA_FILE}" ]; then
    [ -r "${LICENSE_DATA_FILE}" ] || fail "license file not readable: ${LICENSE_DATA_FILE}"
    license_data=$(cat "${LICENSE_DATA_FILE}")
else
    fail "set LICENSE_DATA (the z64:... token) or LICENSE_DATA_FILE (path to a file containing it)"
fi

for var in CLUSTER_NAME CLUSTER_ARN SERVICE_NAMESPACE_ID SERVICE_NAMESPACE_NAME; do
    eval "value=\${$var}"
    [ -n "$value" ] || fail "$var must be set (caller pre-creates ECS cluster + Cloud Map namespace)"
done

aws_cli() { aws --region "$REGION" "$@"; }

ACCOUNT_ID=$(aws_cli sts get-caller-identity --query Account --output text)
BUCKET="cardinal-ingest-${ACCOUNT_ID}-${REGION}"
QUEUE_ARN="arn:aws:sqs:${REGION}:${ACCOUNT_ID}:${SQS_QUEUE_NAME}"

# 40-char password using the alphabet the Lambda uses (no shell-troublesome
# punctuation; openssl rand for cryptographic strength).
gen_password() {
    openssl rand -base64 64 \
        | tr -dc 'A-HJ-NP-Za-km-z2-9' \
        | head -c 40
}

# 32-byte hex (64 chars) -- matches handler._random_hex(32).
gen_hex32() { openssl rand -hex 32; }

# Returns a JSON array of {Key,Value} suitable for the --tags option of
# RDS / Secrets Manager / SSM (all of which accept JSON shorthand for --tags).
tags_json_array() {
    component="$1"
    jq -nc \
        --arg app "$APPLICATION" --arg proj "$PROJECT" \
        --arg mgd "$MANAGED_BY" --arg comp "$component" \
        '[
            {Key:"Application",Value:$app},
            {Key:"Project",Value:$proj},
            {Key:"ManagedBy",Value:$mgd},
            {Key:"Component",Value:$comp},
            {Key:"Name",Value:("cardinal-"+$comp)}
         ]'
}

# Returns "Key1=Value1,Key2=Value2" suitable for the --tags option of
# SQS create-queue (which uses a flat map shorthand, not the JSON array).
tags_sqs_map() {
    component="$1"
    printf 'Application=%s,Project=%s,ManagedBy=%s,Component=%s,Name=cardinal-%s' \
        "$APPLICATION" "$PROJECT" "$MANAGED_BY" "$component" "$component"
}

# Returns the JSON {"TagSet": [...]} envelope S3 put-bucket-tagging wants.
tags_s3_envelope() {
    component="$1"
    jq -nc --argjson set "$(tags_json_array "$component")" '{TagSet:$set}'
}

# ============================================================================
#  STORAGE  --  SQS queue, S3 bucket + lifecycle + BPA, queue policy, S3 -> SQS
# ============================================================================

ensure_sqs_queue() {
    log "ensuring SQS queue $SQS_QUEUE_NAME"
    if url=$(aws_cli sqs get-queue-url --queue-name "$SQS_QUEUE_NAME" \
                --query QueueUrl --output text 2>/dev/null); then
        printf '%s' "$url"
        return
    fi
    aws_cli sqs create-queue \
        --queue-name "$SQS_QUEUE_NAME" \
        --tags "$(tags_sqs_map ingest-queue)" \
        --query QueueUrl --output text
}

ensure_s3_bucket() {
    log "ensuring S3 bucket $BUCKET"
    if aws_cli s3api head-bucket --bucket "$BUCKET" >/dev/null 2>&1; then
        return
    fi
    if [ "$REGION" = "us-east-1" ]; then
        aws_cli s3api create-bucket --bucket "$BUCKET" >/dev/null
    else
        aws_cli s3api create-bucket \
            --bucket "$BUCKET" \
            --create-bucket-configuration "LocationConstraint=$REGION" >/dev/null
    fi
    aws_cli s3api put-bucket-tagging \
        --bucket "$BUCKET" \
        --tagging "$(tags_s3_envelope ingest-bucket)" >/dev/null
}

ensure_s3_lifecycle() {
    log "applying lifecycle (expire after $BUCKET_LIFECYCLE_DAYS days) to $BUCKET"
    cfg=$(jq -nc --argjson d "$BUCKET_LIFECYCLE_DAYS" '{
        Rules: [{
            ID: "cardinal-ingest-expire",
            Filter: {Prefix: ""},
            Status: "Enabled",
            Expiration: {Days: $d},
            AbortIncompleteMultipartUpload: {DaysAfterInitiation: 1}
        }]
    }')
    aws_cli s3api put-bucket-lifecycle-configuration \
        --bucket "$BUCKET" --lifecycle-configuration "$cfg"
}

ensure_sqs_policy() {
    queue_url="$1"
    log "applying SQS policy on $queue_url (S3 sourced from $BUCKET)"
    policy=$(jq -nc \
        --arg arn "$QUEUE_ARN" \
        --arg account "$ACCOUNT_ID" \
        --arg bucket "$BUCKET" \
        '{
            Version: "2012-10-17",
            Statement: [{
                Effect: "Allow",
                Principal: {Service: "s3.amazonaws.com"},
                Action: ["sqs:SendMessage","sqs:GetQueueAttributes","sqs:GetQueueUrl"],
                Resource: $arn,
                Condition: {
                    StringEquals: {"aws:SourceAccount": $account},
                    ArnLike: {"aws:SourceArn": ("arn:aws:s3:::"+$bucket)}
                }
            }]
        }')
    aws_cli sqs set-queue-attributes \
        --queue-url "$queue_url" \
        --attributes "$(jq -nc --arg p "$policy" '{Policy:$p}')"
}

ensure_s3_notification() {
    log "applying S3 -> SQS notification on $BUCKET"
    cfg=$(jq -nc --arg arn "$QUEUE_ARN" '{
        QueueConfigurations: [{
            Id: "cardinal-ingest-to-sqs",
            QueueArn: $arn,
            Events: ["s3:ObjectCreated:*"]
        }]
    }')
    aws_cli s3api put-bucket-notification-configuration \
        --bucket "$BUCKET" --notification-configuration "$cfg"
}

# ============================================================================
#  DATABASE  --  subnet group, master secret, RDS instance, conn-JSON rewrite
# ============================================================================

ensure_db_subnet_group() {
    log "ensuring DB subnet group $DB_SUBNET_GROUP_NAME"
    if aws_cli rds describe-db-subnet-groups \
            --db-subnet-group-name "$DB_SUBNET_GROUP_NAME" >/dev/null 2>&1; then
        return
    fi
    # SubnetIds expects multiple values; expand the CSV.
    # shellcheck disable=SC2086
    aws_cli rds create-db-subnet-group \
        --db-subnet-group-name "$DB_SUBNET_GROUP_NAME" \
        --db-subnet-group-description "Cardinal lakerunner DB subnet group" \
        --subnet-ids $(printf '%s' "$PRIVATE_SUBNETS" | tr ',' ' ') \
        --tags "$(tags_json_array db-subnet-group)" >/dev/null
}

# Returns "ARN<TAB>password" on stdout. Reuses the existing secret's password
# on a partial re-run so the RDS create call can match what the secret holds.
ensure_db_master_secret() {
    log "ensuring secret $SECRET_DB_MASTER"
    if existing_arn=$(aws_cli secretsmanager describe-secret \
            --secret-id "$SECRET_DB_MASTER" --query ARN --output text 2>/dev/null); then
        raw=$(aws_cli secretsmanager get-secret-value \
                --secret-id "$existing_arn" --query SecretString --output text)
        existing_password=$(printf '%s' "$raw" | jq -r '.password // empty')
        [ -n "$existing_password" ] || \
            fail "secret $SECRET_DB_MASTER exists but has no password field"
        printf '%s\t%s' "$existing_arn" "$existing_password"
        return
    fi
    password=$(gen_password)
    placeholder=$(jq -nc --arg u "$DB_USERNAME" --arg p "$password" \
                    '{username:$u, password:$p}')
    arn=$(aws_cli secretsmanager create-secret \
            --name "$SECRET_DB_MASTER" \
            --description "Cardinal RDS master credentials (placeholder; rewritten with full conn JSON post-DB-create)" \
            --secret-string "$placeholder" \
            --tags "$(tags_json_array db-master)" \
            --query ARN --output text)
    printf '%s\t%s' "$arn" "$password"
}

ensure_db_instance() {
    password="$1"
    if aws_cli rds describe-db-instances \
            --db-instance-identifier "$DB_IDENTIFIER" >/dev/null 2>&1; then
        log "RDS instance $DB_IDENTIFIER already exists; skipping create"
        return
    fi
    log "creating RDS instance $DB_IDENTIFIER (this can take 10+ minutes)"
    aws_cli rds create-db-instance \
        --db-instance-identifier "$DB_IDENTIFIER" \
        --db-instance-class "$DB_INSTANCE_CLASS" \
        --engine postgres \
        --master-username "$DB_USERNAME" \
        --master-user-password "$password" \
        --allocated-storage "$DB_ALLOCATED_STORAGE" \
        --storage-type gp3 \
        --storage-encrypted \
        --db-name "$DB_NAME" \
        --port "$DB_PORT" \
        --db-subnet-group-name "$DB_SUBNET_GROUP_NAME" \
        --vpc-security-group-ids "$DB_SG_ID" \
        --no-publicly-accessible \
        --backup-retention-period 7 \
        --deletion-protection \
        --tags "$(tags_json_array db)" >/dev/null
}

wait_db_available() {
    log "waiting for $DB_IDENTIFIER to become available"
    aws_cli rds wait db-instance-available --db-instance-identifier "$DB_IDENTIFIER"
}

# Rewrites the master secret with full connection JSON (what the lakerunner
# task containers parse). Idempotent: skip if host/port/dbname already present.
update_db_master_secret() {
    arn="$1"; password="$2"; endpoint="$3"; port="$4"
    raw=$(aws_cli secretsmanager get-secret-value \
            --secret-id "$arn" --query SecretString --output text)
    if printf '%s' "$raw" | jq -e 'has("host") and has("port") and has("dbname")' \
            >/dev/null 2>&1; then
        log "secret $SECRET_DB_MASTER already has connection JSON; skipping rewrite"
        return
    fi
    log "writing connection JSON into $SECRET_DB_MASTER"
    new=$(jq -nc \
        --arg u "$DB_USERNAME" --arg p "$password" \
        --arg h "$endpoint" --argjson port "$port" \
        --arg db "$DB_NAME" \
        '{username:$u, password:$p, engine:"postgres", host:$h, port:$port, dbname:$db}')
    aws_cli secretsmanager put-secret-value \
        --secret-id "$arn" --secret-string "$new" >/dev/null
}

# ============================================================================
#  SECRETS  --  license, internal-keys, admin-key, maestro-db
#                Each is create-only: never overwrite on re-run.
# ============================================================================

ensure_secret_with_value() {
    name="$1"; value="$2"; description="$3"; component="$4"
    if existing=$(aws_cli secretsmanager describe-secret \
            --secret-id "$name" --query ARN --output text 2>/dev/null); then
        log "secret $name exists; reusing"
        printf '%s' "$existing"
        return
    fi
    log "creating secret $name"
    aws_cli secretsmanager create-secret \
        --name "$name" \
        --description "$description" \
        --secret-string "$value" \
        --tags "$(tags_json_array "$component")" \
        --query ARN --output text
}

# ============================================================================
#  SSM  --  storage-profiles, api-keys (operator-managed JSON, default {})
# ============================================================================

ensure_ssm_parameter() {
    name="$1"; value="$2"; description="$3"
    if aws_cli ssm get-parameter --name "$name" >/dev/null 2>&1; then
        log "ssm parameter $name exists"
        return
    fi
    log "creating ssm parameter $name"
    aws_cli ssm put-parameter \
        --name "$name" --type String --value "$value" \
        --description "$description" \
        --tags "$(tags_json_array ssm-parameter)" >/dev/null
}

# ============================================================================
#  ORCHESTRATION
# ============================================================================

main() {
    log "account=$ACCOUNT_ID region=$REGION"
    log "subnets=$PRIVATE_SUBNETS  db-sg=$DB_SG_ID"
    log "cluster=$CLUSTER_NAME  namespace=$SERVICE_NAMESPACE_NAME"

    # ---- storage ----------------------------------------------------------
    queue_url=$(ensure_sqs_queue)
    ensure_s3_bucket
    ensure_s3_lifecycle
    ensure_sqs_policy "$queue_url"
    ensure_s3_notification

    # ---- database ---------------------------------------------------------
    ensure_db_subnet_group
    master=$(ensure_db_master_secret)
    db_master_secret_arn=$(printf '%s' "$master" | cut -f1)
    db_master_password=$(printf '%s' "$master" | cut -f2)
    ensure_db_instance "$db_master_password"
    wait_db_available
    db_endpoint=$(aws_cli rds describe-db-instances \
        --db-instance-identifier "$DB_IDENTIFIER" \
        --query 'DBInstances[0].Endpoint.Address' --output text)
    db_port=$(aws_cli rds describe-db-instances \
        --db-instance-identifier "$DB_IDENTIFIER" \
        --query 'DBInstances[0].Endpoint.Port' --output text)
    update_db_master_secret \
        "$db_master_secret_arn" "$db_master_password" "$db_endpoint" "$db_port"

    # ---- secrets ----------------------------------------------------------
    license_arn=$(ensure_secret_with_value "$SECRET_LICENSE" \
        "$license_data" \
        "Cardinal lakerunner license" \
        license)
    internal_keys_arn=$(ensure_secret_with_value "$SECRET_INTERNAL_KEYS" \
        "$(gen_hex32)" \
        "Internal service keys (random 32-byte hex)" \
        internal-keys)
    # admin-key is JSON {"key": "..."} so the ECS secret pointer ":key::"
    # resolves at task launch.
    admin_key_arn=$(ensure_secret_with_value "$SECRET_ADMIN_KEY" \
        "$(jq -nc --arg k "$(gen_hex32)" '{key:$k}')" \
        "First-boot admin API key (rotated by admin-api)." \
        admin-key)
    maestro_db_arn=$(ensure_secret_with_value "$SECRET_MAESTRO_DB" \
        "$(jq -nc --arg p "$(gen_password)" '{username:"maestro", password:$p}')" \
        "Maestro app DB credential (username/password JSON)." \
        maestro-db)

    # ---- SSM --------------------------------------------------------------
    ensure_ssm_parameter "$SSM_STORAGE_PROFILES" "{}" \
        "Cardinal storage profiles (operator-managed JSON)"
    ensure_ssm_parameter "$SSM_API_KEYS" "{}" \
        "Cardinal external API keys (operator-managed JSON)"

    log "done; emitting outputs JSON to stdout"

    # ---- output -----------------------------------------------------------
    # 1:1 with the cardinal-lakerunner stack parameters that name infra
    # resources the customer (or this script) provisioned out-of-band.
    jq -nc \
        --arg DbEndpoint "$db_endpoint" \
        --arg DbPort "$db_port" \
        --arg DbName "$DB_NAME" \
        --arg DbMasterSecretArn "$db_master_secret_arn" \
        --arg MaestroDbSecretArn "$maestro_db_arn" \
        --arg IngestBucketName "$BUCKET" \
        --arg IngestQueueUrl "$queue_url" \
        --arg IngestQueueArn "$QUEUE_ARN" \
        --arg LicenseSecretArn "$license_arn" \
        --arg InternalKeysSecretArn "$internal_keys_arn" \
        --arg AdminKeySecretArn "$admin_key_arn" \
        --arg StorageProfilesParamName "$SSM_STORAGE_PROFILES" \
        --arg ApiKeysParamName "$SSM_API_KEYS" \
        --arg ClusterName "$CLUSTER_NAME" \
        --arg ClusterArn "$CLUSTER_ARN" \
        --arg ServiceNamespaceId "$SERVICE_NAMESPACE_ID" \
        --arg ServiceNamespaceName "$SERVICE_NAMESPACE_NAME" \
        '{
            DbEndpoint:$DbEndpoint, DbPort:$DbPort, DbName:$DbName,
            DbMasterSecretArn:$DbMasterSecretArn,
            MaestroDbSecretArn:$MaestroDbSecretArn,
            IngestBucketName:$IngestBucketName,
            IngestQueueUrl:$IngestQueueUrl, IngestQueueArn:$IngestQueueArn,
            LicenseSecretArn:$LicenseSecretArn,
            InternalKeysSecretArn:$InternalKeysSecretArn,
            AdminKeySecretArn:$AdminKeySecretArn,
            StorageProfilesParamName:$StorageProfilesParamName,
            ApiKeysParamName:$ApiKeysParamName,
            ClusterName:$ClusterName, ClusterArn:$ClusterArn,
            ServiceNamespaceId:$ServiceNamespaceId,
            ServiceNamespaceName:$ServiceNamespaceName
        }'
}

main "$@"
