#!/bin/sh
# Jenkins job 5: deploy the cardinal-lakerunner-services stack (the application
# tier: query, process, control, otel, maestro).
#
# Upstream:
#   - lakerunner-infra-base : roles, security groups, secrets, SSM param names.
#   - lakerunner-infra-rds  : Db{Endpoint,MasterSecretArn,Name,Port}.
# All of those output names match the template's parameter names, so plain
# FROM_STACKS pulls wire them up.
#
# Special case: PubsubSqsEnv is COMPUTED here.  It is not a single upstream
# output -- we read three outputs from the satellite-infra-base stack and
# assemble the env string the pubsub-sqs container expects:
#   SQS_QUEUE_URL=<RawQueueUrl>;SQS_REGION=<Region>;SQS_ROLE_ARN=<LakerunnerAccessRoleArn>
# then pass it via a PARAMS line (highest precedence).
#
# OTEL_REPLICAS defaults to 0 here: in the satellite topology the same-account
# satellite collector performs ingest, so the lakerunner-tier otel collector is
# off by default.
#
# Thin wrapper over deploy-stack.sh.  Pure environment-variable interface.

set -eu

SCRIPT_DIR=$(cd -- "$(dirname -- "$0")" >/dev/null 2>&1 && pwd)
DEFAULT_TEMPLATE_BASE_URL="https://cardinal-cfn.s3.us-east-2.amazonaws.com/lakerunner"
TEMPLATE_KEY="cardinal-lakerunner-services.yaml"

usage() {
    cat <<EOF
deploy-lakerunner-services.sh -- deploy the cardinal-lakerunner-services stack.

All inputs come from environment variables (no flags).

Required:
  STACK_NAME                  Stack to create/update.
  REGION                      AWS region (never defaulted; must be set explicitly).
  VERSION                     Published template tag.
  INFRA_BASE_STACK            Upstream lakerunner-infra-base.
  INFRA_RDS_STACK             Upstream lakerunner-infra-rds.
  SATELLITE_INFRA_BASE_STACK  Source of RawQueueUrl/Region/LakerunnerAccessRoleArn
                              for the computed PubsubSqsEnv.
  CLUSTER_ARN                 ECS cluster ARN.
  CLUSTER_NAME                ECS cluster name (no upstream output for it).
  VPC_ID                      VPC for the services.
  PRIVATE_SUBNETS             Comma-separated private subnet ids.

Optional (template defaults preserved when unset):
  CERTIFICATE_ARN             ACM/IAM cert ARN.  Maestro HTTPS needs either this
                              or the three PEM files below.
  CERTIFICATE_BODY_FILE       PEM cert body (path).
  CERTIFICATE_PRIVATE_KEY_FILE PEM private key (path).
  CERTIFICATE_CHAIN_FILE      PEM chain (path).
  DEX_ADMIN_EMAIL             (template default admin@cardinal.local).
  DEX_ADMIN_PASSWORD_HASH     bcrypt hash; REQUIRED for Maestro UI login even
                              though the template defaults it to ''.
  DEX_CLIENT_ID               (template default maestro-ui).
  OIDC_SUPERADMIN_EMAILS      (template default admin@cardinal.local).
  SERVICE_NAMESPACE_NAME      Cloud Map namespace (template default cardinal.local).
  PUBLIC_SUBNETS              Comma-separated public subnet ids (template default '').
  OTEL_REPLICAS               lakerunner-tier collector replicas (default 0).
  LAKERUNNER_IMAGE, MAESTRO_IMAGE, OTEL_IMAGE, DEX_IMAGE, DEX_INIT_IMAGE,
  DB_INIT_IMAGE               Image overrides (template defaults otherwise).
  TEMPLATE_BASE_URL           Default: $DEFAULT_TEMPLATE_BASE_URL.  Also
                              forwarded as the TemplateBaseUrl param (nested
                              children load from the matching prefix).
  DEPLOYER_ROLE_ARN           Passed to create-change-set.
  NO_EXECUTE                  Non-empty: change-set only, do not execute.
EOF
}

case "${1:-}" in
    -h|--help) usage; exit 0 ;;
    "") : ;;
    *) echo "[deploy-lakerunner-services] ERROR: this script takes no arguments; configure it via environment variables" >&2; usage >&2; exit 2 ;;
esac

read_file_or_die() {
    p="$1"
    if [ ! -r "$p" ]; then
        echo "[deploy-lakerunner-services] ERROR: cannot read file: $p" >&2
        exit 2
    fi
    cat "$p"
}

missing=""
[ -z "${STACK_NAME:-}" ] && missing="$missing STACK_NAME"
[ -z "${REGION:-}" ] && missing="$missing REGION"
[ -z "${VERSION:-}" ] && missing="$missing VERSION"
[ -z "${INFRA_BASE_STACK:-}" ] && missing="$missing INFRA_BASE_STACK"
[ -z "${INFRA_RDS_STACK:-}" ] && missing="$missing INFRA_RDS_STACK"
[ -z "${SATELLITE_INFRA_BASE_STACK:-}" ] && missing="$missing SATELLITE_INFRA_BASE_STACK"
[ -z "${CLUSTER_ARN:-}" ] && missing="$missing CLUSTER_ARN"
[ -z "${CLUSTER_NAME:-}" ] && missing="$missing CLUSTER_NAME"
[ -z "${VPC_ID:-}" ] && missing="$missing VPC_ID"
[ -z "${PRIVATE_SUBNETS:-}" ] && missing="$missing PRIVATE_SUBNETS"
if [ -n "$missing" ]; then
    usage >&2
    echo "[deploy-lakerunner-services] ERROR: missing required: $(echo "$missing" | sed 's/^ //; s/ /, /g')" >&2
    exit 2
fi

if ! command -v aws >/dev/null 2>&1 || ! command -v jq >/dev/null 2>&1; then
    echo "[deploy-lakerunner-services] ERROR: aws and jq are required" >&2
    exit 2
fi

template_base_url="${TEMPLATE_BASE_URL:-$DEFAULT_TEMPLATE_BASE_URL}"
otel_replicas="${OTEL_REPLICAS:-0}"

TEMPLATE_URL="$template_base_url/$VERSION/$TEMPLATE_KEY"

# --- Compute PubsubSqsEnv from the satellite-infra-base stack outputs. -------
sat_outputs=$(aws cloudformation describe-stacks \
    --stack-name "$SATELLITE_INFRA_BASE_STACK" \
    --region "$REGION" \
    --query 'Stacks[0].Outputs' \
    --output json)

queue_url=$(printf '%s' "$sat_outputs" | jq -r '(.[] | select(.OutputKey == "RawQueueUrl") | .OutputValue) // ""')
sqs_region=$(printf '%s' "$sat_outputs" | jq -r '(.[] | select(.OutputKey == "Region") | .OutputValue) // ""')
role_arn=$(printf '%s' "$sat_outputs" | jq -r '(.[] | select(.OutputKey == "LakerunnerAccessRoleArn") | .OutputValue) // ""')

if [ -z "$queue_url" ] || [ -z "$sqs_region" ] || [ -z "$role_arn" ]; then
    echo "[deploy-lakerunner-services] ERROR: satellite-infra-base stack '$SATELLITE_INFRA_BASE_STACK' is missing one of RawQueueUrl/Region/LakerunnerAccessRoleArn outputs" >&2
    exit 2
fi

pubsub_sqs_env="SQS_QUEUE_URL=$queue_url;SQS_REGION=$sqs_region;SQS_ROLE_ARN=$role_arn"

# --- Compose the deploy-stack.sh environment. --------------------------------
FROM_STACKS="$INFRA_BASE_STACK $INFRA_RDS_STACK"
MAPS=""

# PubsubSqsEnv and TemplateBaseUrl are always set.  TemplateBaseUrl must track
# the version we deploy so nested children load from the matching prefix.
params="PubsubSqsEnv=$pubsub_sqs_env
TemplateBaseUrl=$template_base_url/$VERSION/cardinal-lakerunner/
ClusterArn=$CLUSTER_ARN
ClusterName=$CLUSTER_NAME
VpcId=$VPC_ID
PrivateSubnets=$PRIVATE_SUBNETS
OtelReplicas=$otel_replicas"

[ -n "${PUBLIC_SUBNETS:-}" ] && params="$params
PublicSubnets=$PUBLIC_SUBNETS"
[ -n "${SERVICE_NAMESPACE_NAME:-}" ] && params="$params
ServiceNamespaceName=$SERVICE_NAMESPACE_NAME"

[ -n "${LAKERUNNER_IMAGE:-}" ] && params="$params
LakerunnerImage=$LAKERUNNER_IMAGE"
[ -n "${MAESTRO_IMAGE:-}" ] && params="$params
MaestroImage=$MAESTRO_IMAGE"
[ -n "${OTEL_IMAGE:-}" ] && params="$params
OtelImage=$OTEL_IMAGE"
[ -n "${DEX_IMAGE:-}" ] && params="$params
DexImage=$DEX_IMAGE"
[ -n "${DEX_INIT_IMAGE:-}" ] && params="$params
DexInitImage=$DEX_INIT_IMAGE"
[ -n "${DB_INIT_IMAGE:-}" ] && params="$params
DbInitImage=$DB_INIT_IMAGE"

[ -n "${CERTIFICATE_ARN:-}" ] && params="$params
CertificateArn=$CERTIFICATE_ARN"
[ -n "${CERTIFICATE_BODY_FILE:-}" ] && params="$params
CertificateBody=$(read_file_or_die "$CERTIFICATE_BODY_FILE")"
[ -n "${CERTIFICATE_PRIVATE_KEY_FILE:-}" ] && params="$params
CertificatePrivateKey=$(read_file_or_die "$CERTIFICATE_PRIVATE_KEY_FILE")"
[ -n "${CERTIFICATE_CHAIN_FILE:-}" ] && params="$params
CertificateChain=$(read_file_or_die "$CERTIFICATE_CHAIN_FILE")"

[ -n "${DEX_ADMIN_EMAIL:-}" ] && params="$params
DexAdminEmail=$DEX_ADMIN_EMAIL"
[ -n "${DEX_ADMIN_PASSWORD_HASH:-}" ] && params="$params
DexAdminPasswordHash=$DEX_ADMIN_PASSWORD_HASH"
[ -n "${DEX_CLIENT_ID:-}" ] && params="$params
DexClientId=$DEX_CLIENT_ID"
[ -n "${OIDC_SUPERADMIN_EMAILS:-}" ] && params="$params
OidcSuperadminEmails=$OIDC_SUPERADMIN_EMAILS"

PARAMS="$params"

export TEMPLATE_URL PARAMS FROM_STACKS MAPS

exec "$SCRIPT_DIR/deploy-stack.sh"
