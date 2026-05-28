#!/usr/bin/env bash
# Deploy the cardinal-lakerunner application stack. Reads the
# cardinal-infrastructure stack's outputs at runtime and wires them into the
# lakerunner parameters; the operator only supplies the customer-side
# identifiers (ECS cluster, VPC, subnets, cert, DEX admin login).

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$SCRIPT_DIR/lib.sh"
LOG_TAG="cardinal-lakerunner"

REGION="${REGION:-us-east-1}"
VERSION="${VERSION:?VERSION is required (e.g. v0.0.80)}"
STACK_NAME="${STACK_NAME:-cardinal-lakerunner}"
INFRA_STACK_NAME="${INFRA_STACK_NAME:-cardinal-infrastructure}"
TEMPLATE_BUCKET="${TEMPLATE_BUCKET:-cardinal-cfn-${REGION}}"

# Required customer-supplied identifiers.
require_env VPC_ID
require_env PRIVATE_SUBNETS
require_env CLUSTER_NAME
require_env CLUSTER_ARN

# DEX admin login.
require_env DEX_ADMIN_EMAIL
require_env DEX_ADMIN_PASSWORD_HASH
OIDC_SUPERADMIN_EMAILS="${OIDC_SUPERADMIN_EMAILS:-$DEX_ADMIN_EMAIL}"

# TLS for the ALB 443 listener. Provide ONE of:
#   - CERTIFICATE_ARN (existing ACM or IAM-server-cert ARN), OR
#   - CERTIFICATE_BODY + CERTIFICATE_PRIVATE_KEY (PEM material -- the cert
#     child stack builds an AWS::IAM::ServerCertificate from them).
CERTIFICATE_ARN="${CERTIFICATE_ARN:-}"
CERTIFICATE_BODY="${CERTIFICATE_BODY:-}"
CERTIFICATE_PRIVATE_KEY="${CERTIFICATE_PRIVATE_KEY:-}"
CERTIFICATE_CHAIN="${CERTIFICATE_CHAIN:-}"

if [ -z "$CERTIFICATE_ARN" ] && { [ -z "$CERTIFICATE_BODY" ] || [ -z "$CERTIFICATE_PRIVATE_KEY" ]; }; then
  die "either CERTIFICATE_ARN, or both CERTIFICATE_BODY and CERTIFICATE_PRIVATE_KEY, must be set"
fi

# Optional knobs.
SERVICE_NAMESPACE_NAME="${SERVICE_NAMESPACE_NAME:-cardinal.local}"
ORGANIZATION_ID="${ORGANIZATION_ID:-12340000-0000-4000-8000-000000000000}"

TEMPLATE_URL="https://${TEMPLATE_BUCKET}.s3.${REGION}.amazonaws.com/lakerunner/${VERSION}/cardinal-lakerunner.yaml"
TEMPLATE_BASE_URL="https://${TEMPLATE_BUCKET}.s3.${REGION}.amazonaws.com/lakerunner/${VERSION}/cardinal-lakerunner/"

preflight_aws "$REGION"
verify_template_published "$TEMPLATE_URL"

# ---------------------------------------------------------------------------
# Read infra-stack outputs (the lakerunner parameters that "really live" on
# cardinal-infrastructure).
# ---------------------------------------------------------------------------
log "reading $INFRA_STACK_NAME outputs from $REGION"
status="$(describe_stack_status "$REGION" "$INFRA_STACK_NAME")"
case "$status" in
  *_COMPLETE) ;;
  *) die "$INFRA_STACK_NAME is $status; must be *_COMPLETE before deploying $STACK_NAME" ;;
esac

infra_out() {
  aws cloudformation describe-stacks --region "$REGION" --stack-name "$INFRA_STACK_NAME" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue | [0]" --output text
}

DB_ENDPOINT="$(infra_out DbEndpoint)"
DB_MASTER_SECRET_ARN="$(infra_out DbMasterSecretArn)"
INGEST_BUCKET_NAME="$(infra_out IngestBucketName)"
INGEST_QUEUE_URL="$(infra_out IngestQueueUrl)"
INGEST_QUEUE_ARN="$(infra_out IngestQueueArn)"
LICENSE_SECRET_ARN="$(infra_out LicenseSecretArn)"
ADMIN_KEY_SECRET_ARN="$(infra_out AdminKeySecretArn)"
STORAGE_PROFILES_PARAM_NAME="$(infra_out StorageProfilesParamName)"
API_KEYS_PARAM_NAME="$(infra_out ApiKeysParamName)"
RDS_SECURITY_GROUP_ID="$(infra_out RdsSecurityGroupId)"

for v in DB_ENDPOINT DB_MASTER_SECRET_ARN INGEST_BUCKET_NAME INGEST_QUEUE_URL \
         INGEST_QUEUE_ARN LICENSE_SECRET_ARN ADMIN_KEY_SECRET_ARN \
         STORAGE_PROFILES_PARAM_NAME API_KEYS_PARAM_NAME RDS_SECURITY_GROUP_ID; do
  if [ -z "${!v}" ] || [ "${!v}" = "None" ]; then
    die "$INFRA_STACK_NAME output for $v is empty"
  fi
done

# ---------------------------------------------------------------------------
# Build params file. PEM material is multi-line; use Python for safe JSON.
# ---------------------------------------------------------------------------
PARAMS_FILE="$(mktemp "${TMPDIR:-/tmp}/cardinal-lakerunner-params.XXXXXX.json")"
trap 'rm -f "$PARAMS_FILE"' EXIT

CERT_BODY="$CERTIFICATE_BODY" \
CERT_KEY="$CERTIFICATE_PRIVATE_KEY" \
CERT_CHAIN="$CERTIFICATE_CHAIN" \
python3 - "$PARAMS_FILE" \
  "$DB_ENDPOINT" "$DB_MASTER_SECRET_ARN" \
  "$INGEST_BUCKET_NAME" "$INGEST_QUEUE_URL" "$INGEST_QUEUE_ARN" \
  "$LICENSE_SECRET_ARN" "$ADMIN_KEY_SECRET_ARN" \
  "$STORAGE_PROFILES_PARAM_NAME" "$API_KEYS_PARAM_NAME" "$RDS_SECURITY_GROUP_ID" \
  "$VPC_ID" "$PRIVATE_SUBNETS" \
  "$CLUSTER_NAME" "$CLUSTER_ARN" \
  "$CERTIFICATE_ARN" \
  "$DEX_ADMIN_EMAIL" "$DEX_ADMIN_PASSWORD_HASH" "$OIDC_SUPERADMIN_EMAILS" \
  "$TEMPLATE_BASE_URL" "$ORGANIZATION_ID" "$SERVICE_NAMESPACE_NAME" <<'PY'
import json, os, sys

(out_path,
 db_endpoint, db_master_secret_arn,
 ingest_bucket, ingest_queue_url, ingest_queue_arn,
 license_secret_arn, admin_key_secret_arn,
 storage_profiles_param, api_keys_param, rds_sg_id,
 vpc_id, private_subnets,
 cluster_name, cluster_arn,
 cert_arn,
 dex_email, dex_hash, oidc_admins,
 tmpl_base, org_id, ns_name) = sys.argv[1:]

cert_body  = os.environ.get("CERT_BODY", "")
cert_key   = os.environ.get("CERT_KEY", "")
cert_chain = os.environ.get("CERT_CHAIN", "")

params = [
    {"ParameterKey": "DbEndpoint",                "ParameterValue": db_endpoint},
    {"ParameterKey": "DbMasterSecretArn",         "ParameterValue": db_master_secret_arn},
    {"ParameterKey": "IngestBucketName",          "ParameterValue": ingest_bucket},
    {"ParameterKey": "IngestQueueUrl",            "ParameterValue": ingest_queue_url},
    {"ParameterKey": "IngestQueueArn",            "ParameterValue": ingest_queue_arn},
    {"ParameterKey": "LicenseSecretArn",          "ParameterValue": license_secret_arn},
    {"ParameterKey": "AdminKeySecretArn",         "ParameterValue": admin_key_secret_arn},
    {"ParameterKey": "StorageProfilesParamName",  "ParameterValue": storage_profiles_param},
    {"ParameterKey": "ApiKeysParamName",          "ParameterValue": api_keys_param},
    {"ParameterKey": "RdsSecurityGroupId",        "ParameterValue": rds_sg_id},
    {"ParameterKey": "VpcId",                     "ParameterValue": vpc_id},
    {"ParameterKey": "PrivateSubnets",            "ParameterValue": private_subnets},
    {"ParameterKey": "ClusterName",               "ParameterValue": cluster_name},
    {"ParameterKey": "ClusterArn",                "ParameterValue": cluster_arn},
    {"ParameterKey": "CertificateArn",            "ParameterValue": cert_arn},
    {"ParameterKey": "CertificateBody",           "ParameterValue": cert_body},
    {"ParameterKey": "CertificatePrivateKey",     "ParameterValue": cert_key},
    {"ParameterKey": "CertificateChain",          "ParameterValue": cert_chain},
    {"ParameterKey": "DexAdminEmail",             "ParameterValue": dex_email},
    {"ParameterKey": "DexAdminPasswordHash",      "ParameterValue": dex_hash},
    {"ParameterKey": "OidcSuperadminEmails",      "ParameterValue": oidc_admins},
    {"ParameterKey": "TemplateBaseUrl",           "ParameterValue": tmpl_base},
    {"ParameterKey": "OrganizationId",            "ParameterValue": org_id},
    {"ParameterKey": "ServiceNamespaceName",      "ParameterValue": ns_name},
    {"ParameterKey": "OtelConfigYaml",            "ParameterValue": ""},
]
with open(out_path, "w") as f:
    json.dump(params, f, indent=2)
PY

log "parameter file ready ($(wc -c <"$PARAMS_FILE") bytes, $(jq '. | length' "$PARAMS_FILE") params)"

deploy_stack "$REGION" "$STACK_NAME" "$TEMPLATE_URL" "$PARAMS_FILE" \
  "CAPABILITY_NAMED_IAM,CAPABILITY_AUTO_EXPAND"

log "outputs:"
dump_outputs_as_env "$REGION" "$STACK_NAME"
