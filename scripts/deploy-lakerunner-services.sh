#!/bin/sh
# Jenkins job 5: deploy the cardinal-lakerunner-services stack (the application
# tier: query, process, control, maestro).
#
# Upstream:
#   - lakerunner-infra-base : roles, security groups, secrets, SSM param names.
#   - lakerunner-infra-rds  : Db{Endpoint,MasterSecretArn,Name,Port}.
# All of those output names match the template's parameter names, so plain
# FROM_STACKS pulls wire them up.
#
# Special case: QueueUrl and QueueRoleArn are pulled from the satellite-infra-
# base stack outputs (RawQueueUrl / LakerunnerAccessRoleArn) and passed via
# PARAMS lines (highest precedence). The pubsub-sqs container sets them as plain
# SQS_QUEUE_URL / SQS_ROLE_ARN env vars; the region is the stack's own
# AWS::Region, so no QueueRegion param is needed.
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
  SATELLITE_INFRA_BASE_STACK  Source of RawQueueUrl / LakerunnerAccessRoleArn
                              for the QueueUrl / QueueRoleArn params.
  CLUSTER_ARN                 ECS cluster ARN.
  CLUSTER_NAME                ECS cluster name (no upstream output for it).
  VPC_ID                      VPC for the services.
  PRIVATE_SUBNETS             Comma-separated private subnet ids.
  DEX_ADMIN_PASSWORD_HASH     bcrypt hash for the Maestro/DEX admin login.
                              REQUIRED: DEX will not start without it ("no
                              password hash provided") and MaestroService rolls
                              back.

Optional (template defaults preserved when unset):
  CERTIFICATE_ARN             ACM/IAM cert ARN for the Maestro HTTPS listener.
                              If unset, the script auto-generates a self-signed
                              internal cert ON FIRST CREATE only (browsers will
                              warn; fine for internal/test).  Re-runs (UPDATE)
                              keep the existing cert untouched -- no churn.  Set
                              CERTIFICATE_ARN to use a real cert.
  CERTIFICATE_BODY_FILE       PEM cert body (path).  Overrides auto-generation.
  CERTIFICATE_PRIVATE_KEY_FILE PEM private key (path).  Overrides auto-generation.
  CERTIFICATE_CHAIN_FILE      PEM chain (path).
  DEX_ADMIN_EMAIL             (template default admin@cardinal.local).
  DEX_CLIENT_ID               (template default maestro-ui).
  OIDC_SUPERADMIN_EMAILS      (template default admin@cardinal.local).
  SATELLITE_SERVICES_STACK    Source of CollectorEndpoint for lakerunner self-
                              telemetry.  When set, the wrapper reads the stack's
                              CollectorEndpoint output and passes it as
                              SelfTelemetryEndpoint.  When unset, self-telemetry
                              stays disabled (SelfTelemetryEndpoint defaults '').
  SELF_TELEMETRY_ENDPOINT     Direct OTLP/HTTP endpoint override for self-
                              telemetry (e.g. http://<alb>:4318).  Takes
                              precedence over the SATELLITE_SERVICES_STACK pull.
  SERVICE_NAMESPACE_NAME      Cloud Map namespace (template default cardinal.local).
  PUBLIC_SUBNETS              Comma-separated public subnet ids (template default '').
  ALB_SCHEME                  internet-facing | internal (template default:
                              internal).  For internet-facing you must also set
                              PUBLIC_SUBNETS, and the ALB SG internet ingress is
                              enabled on the infra-base stack (its ALB_SCHEME /
                              ALB_ALLOWED_CIDR* settings).
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
[ -z "${DEX_ADMIN_PASSWORD_HASH:-}" ] && missing="$missing DEX_ADMIN_PASSWORD_HASH"
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

TEMPLATE_URL="$template_base_url/$VERSION/$TEMPLATE_KEY"

# --- Read QueueUrl / QueueRoleArn from the satellite-infra-base stack. --------
sat_outputs=$(aws cloudformation describe-stacks \
    --stack-name "$SATELLITE_INFRA_BASE_STACK" \
    --region "$REGION" \
    --query 'Stacks[0].Outputs' \
    --output json)

queue_url=$(printf '%s' "$sat_outputs" | jq -r '(.[] | select(.OutputKey == "RawQueueUrl") | .OutputValue) // ""')
role_arn=$(printf '%s' "$sat_outputs" | jq -r '(.[] | select(.OutputKey == "LakerunnerAccessRoleArn") | .OutputValue) // ""')

if [ -z "$queue_url" ] || [ -z "$role_arn" ]; then
    echo "[deploy-lakerunner-services] ERROR: satellite-infra-base stack '$SATELLITE_INFRA_BASE_STACK' is missing one of RawQueueUrl/LakerunnerAccessRoleArn outputs" >&2
    exit 2
fi

# --- Resolve the self-telemetry OTLP/HTTP endpoint. --------------------------
# A direct SELF_TELEMETRY_ENDPOINT override wins; otherwise, when
# SATELLITE_SERVICES_STACK is set, pull its CollectorEndpoint output.  When
# neither is set, leave self_telemetry_endpoint empty so the template default
# (disabled) is preserved.
self_telemetry_endpoint="${SELF_TELEMETRY_ENDPOINT:-}"
if [ -z "$self_telemetry_endpoint" ] && [ -n "${SATELLITE_SERVICES_STACK:-}" ]; then
    sat_services_outputs=$(aws cloudformation describe-stacks \
        --stack-name "$SATELLITE_SERVICES_STACK" \
        --region "$REGION" \
        --query 'Stacks[0].Outputs' \
        --output json)
    self_telemetry_endpoint=$(printf '%s' "$sat_services_outputs" | jq -r '(.[] | select(.OutputKey == "CollectorEndpoint") | .OutputValue) // ""')
    if [ -z "$self_telemetry_endpoint" ]; then
        echo "[deploy-lakerunner-services] ERROR: satellite-services stack '$SATELLITE_SERVICES_STACK' is missing the CollectorEndpoint output" >&2
        exit 2
    fi
fi

# --- Compose the deploy-stack.sh environment. --------------------------------
FROM_STACKS="$INFRA_BASE_STACK $INFRA_RDS_STACK"
MAPS=""

# QueueUrl/QueueRoleArn and TemplateBaseUrl are always set.  TemplateBaseUrl
# must track the version we deploy so nested children load from the matching
# prefix.
params="QueueUrl=$queue_url
QueueRoleArn=$role_arn
TemplateBaseUrl=$template_base_url/$VERSION/cardinal-lakerunner/
ClusterArn=$CLUSTER_ARN
ClusterName=$CLUSTER_NAME
VpcId=$VPC_ID
PrivateSubnets=$PRIVATE_SUBNETS"

[ -n "${PUBLIC_SUBNETS:-}" ] && params="$params
PublicSubnets=$PUBLIC_SUBNETS"
[ -n "${ALB_SCHEME:-}" ] && params="$params
AlbScheme=$ALB_SCHEME"
[ -n "${SERVICE_NAMESPACE_NAME:-}" ] && params="$params
ServiceNamespaceName=$SERVICE_NAMESPACE_NAME"
[ -n "$self_telemetry_endpoint" ] && params="$params
SelfTelemetryEndpoint=$self_telemetry_endpoint"

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

# --- Certificate handling. ---------------------------------------------------
# Cert PEM material is passed via FILE_PARAMS (multi-line safe), never inlined
# into the newline-delimited PARAMS string.
#
# Create-only auto-generation: the cert.yaml child builds an AWS::IAM::Server-
# Certificate from CertificateBody/CertificatePrivateKey when CertificateArn is
# empty.  A fresh self-signed PEM on every re-run would replace that cert and
# churn the ALB listener, so we generate it ONLY on first create:
#   - CERTIFICATE_ARN set            -> pass it (stable ARN, no churn).
#   - empty + PEM files supplied     -> pass the supplied PEMs.
#   - empty + stack absent (CREATE)  -> generate a self-signed cert, pass it.
#   - empty + stack present (UPDATE) -> pass nothing; deploy-stack.sh resolves
#     CertificateBody/CertificatePrivateKey to UsePreviousValue, keeping the
#     existing IAM ServerCertificate untouched.
file_params=""
cert_dir=""

# Preserve and propagate the child's exit status: rm -rf would otherwise
# overwrite $? with its own 0, false-greening a FAILED deploy.
cleanup_cert() {
    status=$?
    [ -n "${cert_dir:-}" ] && rm -rf "$cert_dir"
    trap - EXIT
    exit "$status"
}
trap cleanup_cert EXIT INT TERM HUP

if [ -n "${CERTIFICATE_ARN:-}" ]; then
    params="$params
CertificateArn=$CERTIFICATE_ARN"
elif [ -n "${CERTIFICATE_BODY_FILE:-}" ] || [ -n "${CERTIFICATE_PRIVATE_KEY_FILE:-}" ]; then
    [ -r "${CERTIFICATE_BODY_FILE:-}" ] || { echo "[deploy-lakerunner-services] ERROR: cannot read CERTIFICATE_BODY_FILE: ${CERTIFICATE_BODY_FILE:-}" >&2; exit 2; }
    [ -r "${CERTIFICATE_PRIVATE_KEY_FILE:-}" ] || { echo "[deploy-lakerunner-services] ERROR: cannot read CERTIFICATE_PRIVATE_KEY_FILE: ${CERTIFICATE_PRIVATE_KEY_FILE:-}" >&2; exit 2; }
    file_params="CertificateBody=$CERTIFICATE_BODY_FILE
CertificatePrivateKey=$CERTIFICATE_PRIVATE_KEY_FILE"
    if [ -n "${CERTIFICATE_CHAIN_FILE:-}" ]; then
        [ -r "$CERTIFICATE_CHAIN_FILE" ] || { echo "[deploy-lakerunner-services] ERROR: cannot read CERTIFICATE_CHAIN_FILE: $CERTIFICATE_CHAIN_FILE" >&2; exit 2; }
        file_params="$file_params
CertificateChain=$CERTIFICATE_CHAIN_FILE"
    fi
else
    # No ARN, no PEM files.  Generate only on first create (stack absent).
    if aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" >/dev/null 2>&1; then
        echo "[deploy-lakerunner-services] stack exists; keeping the existing self-signed cert (no regeneration)" >&2
    else
        if ! command -v openssl >/dev/null 2>&1; then
            echo "[deploy-lakerunner-services] ERROR: openssl is required to auto-generate a self-signed cert; install openssl or set CERTIFICATE_ARN / CERTIFICATE_*_FILE" >&2
            exit 2
        fi
        echo "[deploy-lakerunner-services] no CERTIFICATE_ARN and first create; generating a self-signed internal cert" >&2
        cert_dir=$(mktemp -d)
        if ! openssl req -x509 -newkey rsa:2048 -nodes \
                -keyout "$cert_dir/key.pem" -out "$cert_dir/cert.pem" \
                -days 825 -subj "/CN=cardinal.test" \
                -addext "subjectAltName=DNS:cardinal.test,DNS:*.cardinal.internal" 2>/dev/null; then
            echo "[deploy-lakerunner-services] ERROR: openssl failed to generate the self-signed cert" >&2
            exit 1
        fi
        file_params="CertificateBody=$cert_dir/cert.pem
CertificatePrivateKey=$cert_dir/key.pem"
    fi
fi

[ -n "${DEX_ADMIN_EMAIL:-}" ] && params="$params
DexAdminEmail=$DEX_ADMIN_EMAIL"
[ -n "${DEX_ADMIN_PASSWORD_HASH:-}" ] && params="$params
DexAdminPasswordHash=$DEX_ADMIN_PASSWORD_HASH"
[ -n "${DEX_CLIENT_ID:-}" ] && params="$params
DexClientId=$DEX_CLIENT_ID"
[ -n "${OIDC_SUPERADMIN_EMAILS:-}" ] && params="$params
OidcSuperadminEmails=$OIDC_SUPERADMIN_EMAILS"

PARAMS="$params"
FILE_PARAMS="$file_params"

export TEMPLATE_URL PARAMS FILE_PARAMS FROM_STACKS MAPS

# Not exec: deploy-stack.sh must read the generated cert PEMs before the
# cleanup_cert EXIT trap removes the temp dir, so run it as a child and forward
# its exit code.  (When no cert was generated, cert_dir is empty and the trap
# is a no-op.)
"$SCRIPT_DIR/deploy-stack.sh"
