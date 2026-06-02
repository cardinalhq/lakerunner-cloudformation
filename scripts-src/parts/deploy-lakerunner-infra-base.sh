#!/bin/sh
# Jenkins job 1: deploy the cardinal-lakerunner-infra-base stack.
#
# This is the head of the chain -- it has no upstream stacks.  It owns the IAM
# roles, security groups, cooked bucket, license/admin secrets, and SSM params
# that every downstream stack consumes.
#
# Self-contained single-file driver: this front-half composes the published
# template URL from TEMPLATE_BASE_URL + VERSION and builds the PARAMS block, then
# falls through into the engine embedded below (scripts-src/build.sh stitches the
# two; do not edit the generated copy).  Pure environment-variable interface (no
# flags).

set -eu

DEFAULT_TEMPLATE_BASE_URL="https://cardinal-cfn-us-east-1.s3.us-east-1.amazonaws.com/lakerunner"
TEMPLATE_KEY="cardinal-lakerunner-infra-base.yaml"

usage() {
    cat <<EOF
deploy-lakerunner-infra-base.sh -- deploy the cardinal-lakerunner-infra-base stack.

All inputs come from environment variables (no flags).

Required:
  STACK_NAME           Stack to create/update.
  REGION               AWS region (never defaulted; must be set explicitly).
  VERSION              Published template tag, e.g. v0.0.70.
  VPC_ID               VPC for the security groups.
  CLUSTER_ARN          Customer-supplied ECS cluster ARN.
  LICENSE_DATA_FILE    Path to license JSON (seeds the license secret).

Optional (template defaults preserved when unset):
  ALB_SCHEME                   internet-facing | internal (template default: internal).
  ALB_ALLOWED_CIDR1            ALB ingress CIDR allowlist (template default 10.0.0.0/8).
  ALB_ALLOWED_CIDR2            (template default 172.16.0.0/12).
  ALB_ALLOWED_CIDR3            (template default 192.168.0.0/16).
  ORGANIZATION_ID              Canonical org id seeded into config.
  INITIAL_INGEST_API_KEY       Bootstrap ingest API key.
  COOKED_BUCKET_NAME           Explicit cooked bucket name.
  LICENSE_SECRET_NAME          (template default cardinal-license).
  ADMIN_KEY_SECRET_NAME        (template default cardinal-admin-key).
  API_KEYS_PARAM_NAME          (template default /cardinal/api-keys).
  STORAGE_PROFILES_PARAM_NAME  (template default /cardinal/storage-profiles).
  TEMPLATE_BASE_URL            Default: $DEFAULT_TEMPLATE_BASE_URL
  DEPLOYER_ROLE_ARN            Passed to create-change-set.
  NO_EXECUTE                   Non-empty: change-set only, do not execute.
EOF
}

case "${1:-}" in
    -h|--help) usage; exit 0 ;;
    "") : ;;
    *) echo "[deploy-lakerunner-infra-base] ERROR: this script takes no arguments; configure it via environment variables" >&2; usage >&2; exit 2 ;;
esac

# --- Required-input check (collect all missing, fail once). -------------------
missing=""
[ -z "${STACK_NAME:-}" ] && missing="$missing STACK_NAME"
[ -z "${REGION:-}" ] && missing="$missing REGION"
[ -z "${VERSION:-}" ] && missing="$missing VERSION"
[ -z "${VPC_ID:-}" ] && missing="$missing VPC_ID"
[ -z "${CLUSTER_ARN:-}" ] && missing="$missing CLUSTER_ARN"
[ -z "${LICENSE_DATA_FILE:-}" ] && missing="$missing LICENSE_DATA_FILE"
if [ -n "$missing" ]; then
    usage >&2
    echo "[deploy-lakerunner-infra-base] ERROR: missing required: $(echo "$missing" | sed 's/^ //; s/ /, /g')" >&2
    exit 2
fi

if [ ! -r "$LICENSE_DATA_FILE" ]; then
    echo "[deploy-lakerunner-infra-base] ERROR: cannot read LICENSE_DATA_FILE: $LICENSE_DATA_FILE" >&2
    exit 2
fi
license_data=$(cat "$LICENSE_DATA_FILE")

template_base_url="${TEMPLATE_BASE_URL:-$DEFAULT_TEMPLATE_BASE_URL}"

# --- Compose the deploy-stack.sh environment. --------------------------------
TEMPLATE_URL="$template_base_url/$VERSION/$TEMPLATE_KEY"

# Build PARAMS (newline-separated Key=Value).  Required values always present;
# optional ones added only when set so the template default applies otherwise.
params="VpcId=$VPC_ID
ClusterArn=$CLUSTER_ARN
LicenseData=$license_data"
[ -n "${ALB_SCHEME:-}" ] && params="$params
AlbScheme=$ALB_SCHEME"
[ -n "${ALB_ALLOWED_CIDR1:-}" ] && params="$params
AlbAllowedCidr1=$ALB_ALLOWED_CIDR1"
[ -n "${ALB_ALLOWED_CIDR2:-}" ] && params="$params
AlbAllowedCidr2=$ALB_ALLOWED_CIDR2"
[ -n "${ALB_ALLOWED_CIDR3:-}" ] && params="$params
AlbAllowedCidr3=$ALB_ALLOWED_CIDR3"
[ -n "${ORGANIZATION_ID:-}" ] && params="$params
OrganizationId=$ORGANIZATION_ID"
[ -n "${INITIAL_INGEST_API_KEY:-}" ] && params="$params
InitialIngestApiKey=$INITIAL_INGEST_API_KEY"
[ -n "${COOKED_BUCKET_NAME:-}" ] && params="$params
CookedBucketName=$COOKED_BUCKET_NAME"
[ -n "${LICENSE_SECRET_NAME:-}" ] && params="$params
LicenseSecretName=$LICENSE_SECRET_NAME"
[ -n "${ADMIN_KEY_SECRET_NAME:-}" ] && params="$params
AdminKeySecretName=$ADMIN_KEY_SECRET_NAME"
[ -n "${API_KEYS_PARAM_NAME:-}" ] && params="$params
ApiKeysParamName=$API_KEYS_PARAM_NAME"
[ -n "${STORAGE_PROFILES_PARAM_NAME:-}" ] && params="$params
StorageProfilesParamName=$STORAGE_PROFILES_PARAM_NAME"

PARAMS="$params"
FROM_STACKS=""
MAPS=""

export TEMPLATE_URL PARAMS FROM_STACKS MAPS
