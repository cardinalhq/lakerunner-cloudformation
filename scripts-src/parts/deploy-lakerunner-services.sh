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
# Self-contained single-file driver: this front-half sets the engine env, then
# falls through into the engine embedded below by scripts-src/build.sh (do not
# edit the generated copy).  Pure environment-variable interface (no flags).

set -eu

DEFAULT_TEMPLATE_BASE_URL="https://cardinal-cfn-us-east-1.s3.us-east-1.amazonaws.com/lakerunner"
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
  ORGANIZATION_ID             Organization UUID for this install (operator-chosen,
                              no default). MUST match the value used on
                              lakerunner-infra-base and on every satellite.
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
  CERTIFICATE_BODY            PEM cert body (string).  Overrides auto-generation
                              (body + private key must be supplied together).
  CERTIFICATE_PRIVATE_KEY     PEM private key (string).
  CERTIFICATE_CHAIN           PEM chain (string, optional).
  CERTIFICATE_BODY_FILE       PEM cert body (path) -- fallback for CERTIFICATE_BODY.
  CERTIFICATE_PRIVATE_KEY_FILE PEM private key (path) -- fallback for CERTIFICATE_PRIVATE_KEY.
  CERTIFICATE_CHAIN_FILE      PEM chain (path) -- fallback for CERTIFICATE_CHAIN.
  DEX_ADMIN_EMAIL             (template default admin@cardinal.local).
  DEX_CLIENT_ID               (template default maestro-ui).
  OIDC_SUPERADMIN_EMAILS      (template default admin@cardinal.local).
  SATELLITE_SERVICES_STACK    Source of CollectorEndpoint for lakerunner self-
                              telemetry (default cardinal-satellite-services).
                              Self-telemetry is on by default: the wrapper reads
                              this stack's CollectorEndpoint output and passes it
                              as SelfTelemetryEndpoint.  If the stack or its
                              CollectorEndpoint output is absent, it warns and
                              leaves self-telemetry off (never blocks the deploy).
  SELF_TELEMETRY_ENDPOINT     Direct OTLP/HTTP endpoint override for self-
                              telemetry (e.g. http://<alb>:4318).  When non-empty,
                              takes precedence over the SATELLITE_SERVICES_STACK
                              pull.
  SERVICE_NAMESPACE_NAME      Cloud Map namespace (template default cardinal.local).
  PUBLIC_SUBNETS              Comma-separated public subnet ids (template default '').
  ALB_SCHEME                  internet-facing | internal (template default:
                              internal).  For internet-facing you must also set
                              PUBLIC_SUBNETS, and the ALB SG internet ingress is
                              enabled on the infra-base stack (its ALB_SCHEME /
                              ALB_ALLOWED_CIDR* settings).
  LAKERUNNER_IMAGE, MAESTRO_IMAGE, OTEL_IMAGE, DEX_IMAGE, DEX_INIT_IMAGE,
  DB_INIT_IMAGE               Image overrides (template defaults otherwise).
  PUBSUB_AUTOREGISTER         pubsub-sqs auto-registration of satellite buckets
                              (template default "true").  Set "false" to disable.
                              When true, unseen satellite raw-bucket orgs are
                              registered and cooked output is routed to the
                              central instance.
  PUBSUB_AUTOREGISTER_WRITES_TO_INSTANCE
                              Central cooked-bucket instance_num that auto-
                              registered orgs write to (default 1). Required
                              when PUBSUB_AUTOREGISTER=true.
  QUEUE_URL_<n>, QUEUE_REGION_<n>, QUEUE_ROLE_ARN_<n>
                              Additional satellite queues for pubsub-sqs, n=1..10.
                              Set QUEUE_URL_<n> (and optionally the matching
                              region / assume-role ARN) to add a queue beyond the
                              primary one.  QUEUE_REGION_<n> defaults to REGION.
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
[ -z "${ORGANIZATION_ID:-}" ] && missing="$missing ORGANIZATION_ID"
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
# Self-telemetry is on by default: the lakerunner account always runs a
# satellite collector, so a standard deploy gets data flowing with no extra
# operator config.  A non-empty SELF_TELEMETRY_ENDPOINT override wins; otherwise
# pull the CollectorEndpoint output from SATELLITE_SERVICES_STACK (default
# cardinal-satellite-services).  Resolution is GRACEFUL: a missing stack or a
# missing CollectorEndpoint output warns and leaves self-telemetry off -- it
# must never block the app deploy.
satellite_services_stack="${SATELLITE_SERVICES_STACK:-cardinal-satellite-services}"
self_telemetry_endpoint="${SELF_TELEMETRY_ENDPOINT:-}"
if [ -z "$self_telemetry_endpoint" ]; then
    if sat_services_outputs=$(aws cloudformation describe-stacks \
            --stack-name "$satellite_services_stack" \
            --region "$REGION" \
            --query 'Stacks[0].Outputs' \
            --output json 2>/dev/null); then
        self_telemetry_endpoint=$(printf '%s' "$sat_services_outputs" | jq -r '(.[] | select(.OutputKey == "CollectorEndpoint") | .OutputValue) // ""')
    fi
    if [ -z "$self_telemetry_endpoint" ]; then
        echo "[deploy-lakerunner-services] satellite collector endpoint not found in $satellite_services_stack; self-telemetry disabled" >&2
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
OrganizationId=$ORGANIZATION_ID
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

# --- Additional satellite queues (groups 1..10). -----------------------------
# pubsub-sqs consumes extra satellite queues from numbered env groups.  For each
# n, set QUEUE_URL_<n> (and optionally QUEUE_REGION_<n> / QUEUE_ROLE_ARN_<n>) to
# add one.  The ceiling mirrors MAX_ADDITIONAL_QUEUES in services_process.py.
n=1
while [ "$n" -le 10 ]; do
    eval "q_url=\${QUEUE_URL_$n:-}"
    if [ -n "$q_url" ]; then
        eval "q_region=\${QUEUE_REGION_$n:-}"
        eval "q_role=\${QUEUE_ROLE_ARN_$n:-}"
        params="$params
QueueUrl$n=$q_url"
        [ -n "$q_region" ] && params="$params
QueueRegion$n=$q_region"
        [ -n "$q_role" ] && params="$params
QueueRoleArn$n=$q_role"
    fi
    n=$((n + 1))
done

# --- Certificate handling. ---------------------------------------------------
# Cert PEM material reaches the template via FILE_PARAMS (multi-line safe), never
# inlined into the newline-delimited PARAMS string.  Operators supply each PEM as
# a direct string env var (CERTIFICATE_BODY / CERTIFICATE_PRIVATE_KEY /
# CERTIFICATE_CHAIN) -- written into a temp dir here -- or as a *_FILE path
# fallback.  The string form wins when both are set.
#
# Create-only auto-generation: the cert.yaml child builds an AWS::IAM::Server-
# Certificate from CertificateBody/CertificatePrivateKey when CertificateArn is
# empty.  A fresh self-signed PEM on every re-run would replace that cert and
# churn the ALB listener, so we generate it ONLY on first create:
#   - CERTIFICATE_ARN set            -> pass it (stable ARN, no churn).
#   - empty + PEM supplied           -> pass the supplied PEMs.
#   - empty + stack absent (CREATE)  -> generate a self-signed cert, pass it.
#   - empty + stack present (UPDATE) -> pass nothing; the engine resolves
#     CertificateBody/CertificatePrivateKey to UsePreviousValue, keeping the
#     existing IAM ServerCertificate untouched.
file_params=""
cert_dir=""
cert_body_path=""
cert_key_path=""
cert_chain_path=""

if [ -n "${CERTIFICATE_ARN:-}" ]; then
    params="$params
CertificateArn=$CERTIFICATE_ARN"
else
    # Resolve each PEM to a file path: the direct string env var (written into a
    # temp dir) wins; the matching *_FILE path is the fallback.
    if [ -n "${CERTIFICATE_BODY:-}" ]; then
        [ -n "$cert_dir" ] || cert_dir=$(mktemp -d)
        printf '%s\n' "$CERTIFICATE_BODY" > "$cert_dir/cert.pem"
        cert_body_path="$cert_dir/cert.pem"
    elif [ -n "${CERTIFICATE_BODY_FILE:-}" ]; then
        [ -r "$CERTIFICATE_BODY_FILE" ] || { echo "[deploy-lakerunner-services] ERROR: cannot read CERTIFICATE_BODY_FILE: $CERTIFICATE_BODY_FILE" >&2; exit 2; }
        cert_body_path="$CERTIFICATE_BODY_FILE"
    fi
    if [ -n "${CERTIFICATE_PRIVATE_KEY:-}" ]; then
        [ -n "$cert_dir" ] || cert_dir=$(mktemp -d)
        printf '%s\n' "$CERTIFICATE_PRIVATE_KEY" > "$cert_dir/key.pem"
        cert_key_path="$cert_dir/key.pem"
    elif [ -n "${CERTIFICATE_PRIVATE_KEY_FILE:-}" ]; then
        [ -r "$CERTIFICATE_PRIVATE_KEY_FILE" ] || { echo "[deploy-lakerunner-services] ERROR: cannot read CERTIFICATE_PRIVATE_KEY_FILE: $CERTIFICATE_PRIVATE_KEY_FILE" >&2; exit 2; }
        cert_key_path="$CERTIFICATE_PRIVATE_KEY_FILE"
    fi
    if [ -n "${CERTIFICATE_CHAIN:-}" ]; then
        [ -n "$cert_dir" ] || cert_dir=$(mktemp -d)
        printf '%s\n' "$CERTIFICATE_CHAIN" > "$cert_dir/chain.pem"
        cert_chain_path="$cert_dir/chain.pem"
    elif [ -n "${CERTIFICATE_CHAIN_FILE:-}" ]; then
        [ -r "$CERTIFICATE_CHAIN_FILE" ] || { echo "[deploy-lakerunner-services] ERROR: cannot read CERTIFICATE_CHAIN_FILE: $CERTIFICATE_CHAIN_FILE" >&2; exit 2; }
        cert_chain_path="$CERTIFICATE_CHAIN_FILE"
    fi

    if [ -n "$cert_body_path" ] || [ -n "$cert_key_path" ]; then
        # Supplied PEM: body and key must come together.
        [ -n "$cert_body_path" ] || { echo "[deploy-lakerunner-services] ERROR: private key supplied without a certificate body (set CERTIFICATE_BODY or CERTIFICATE_BODY_FILE)" >&2; exit 2; }
        [ -n "$cert_key_path" ] || { echo "[deploy-lakerunner-services] ERROR: certificate body supplied without a private key (set CERTIFICATE_PRIVATE_KEY or CERTIFICATE_PRIVATE_KEY_FILE)" >&2; exit 2; }
        file_params="CertificateBody=$cert_body_path
CertificatePrivateKey=$cert_key_path"
        if [ -n "$cert_chain_path" ]; then
            file_params="$file_params
CertificateChain=$cert_chain_path"
        fi
    else
        # No ARN, no PEM.  Generate only on first create (stack absent).
        if aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" >/dev/null 2>&1; then
            echo "[deploy-lakerunner-services] stack exists; keeping the existing self-signed cert (no regeneration)" >&2
        else
            if ! command -v openssl >/dev/null 2>&1; then
                echo "[deploy-lakerunner-services] ERROR: openssl is required to auto-generate a self-signed cert; install openssl or set CERTIFICATE_ARN / CERTIFICATE_BODY+CERTIFICATE_PRIVATE_KEY (or their *_FILE variants)" >&2
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
fi

[ -n "${DEX_ADMIN_EMAIL:-}" ] && params="$params
DexAdminEmail=$DEX_ADMIN_EMAIL"
[ -n "${DEX_ADMIN_PASSWORD_HASH:-}" ] && params="$params
DexAdminPasswordHash=$DEX_ADMIN_PASSWORD_HASH"
[ -n "${DEX_CLIENT_ID:-}" ] && params="$params
DexClientId=$DEX_CLIENT_ID"
[ -n "${OIDC_SUPERADMIN_EMAILS:-}" ] && params="$params
OidcSuperadminEmails=$OIDC_SUPERADMIN_EMAILS"

[ -n "${PUBSUB_AUTOREGISTER:-}" ] && params="$params
PubsubAutoRegister=$PUBSUB_AUTOREGISTER"
[ -n "${PUBSUB_AUTOREGISTER_WRITES_TO_INSTANCE:-}" ] && params="$params
PubsubAutoRegisterWritesToInstance=$PUBSUB_AUTOREGISTER_WRITES_TO_INSTANCE"

PARAMS="$params"
FILE_PARAMS="$file_params"

export TEMPLATE_URL PARAMS FILE_PARAMS FROM_STACKS MAPS
