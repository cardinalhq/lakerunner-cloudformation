#!/bin/sh
# Stack 5 of the deploy chain: the cardinal-lakerunner-services stack (the application
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
# Baked at publish time (scripts-src/build.sh).  STACK_VERSION defaults to this.
DEFAULT_STACK_VERSION="@@STACK_VERSION@@"
DEFAULT_IMAGE_REGISTRY="public.ecr.aws"
# Baked, locked registry-relative paths (repo + pinned tag/digest) for the
# public-ECR images.  Only the registry prefix is operator-supplied.  db-init
# (official postgres psql client) is baked too -- this stack is always on
# public.ecr.aws -- so a redeploy always carries the pinned default;
# DB_INIT_IMAGE remains a full-URI escape hatch.
LAKERUNNER_IMAGE_SUFFIX="@@LAKERUNNER_IMAGE_SUFFIX@@"
MAESTRO_IMAGE_SUFFIX="@@MAESTRO_IMAGE_SUFFIX@@"
DEX_IMAGE_SUFFIX="@@DEX_IMAGE_SUFFIX@@"
DB_INIT_IMAGE_SUFFIX="@@DB_INIT_IMAGE_SUFFIX@@"

usage() {
    cat <<EOF
deploy-lakerunner-services.sh -- deploy the cardinal-lakerunner-services stack.

All inputs come from environment variables (no flags).

Required:
  STACK_NAME                  Stack to create/update.
  REGION                      AWS region (never defaulted; must be set explicitly).
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
  STACK_VERSION               Published template version to deploy. Default: the
                              version baked into this driver ($DEFAULT_STACK_VERSION).
                              (VERSION is accepted as a legacy alias.)
  IMAGE_REGISTRY              Registry (and optional namespace/prefix) the first-
                              party images are pulled from -- e.g. an ECR pull-
                              through cache root. The image paths and pinned
                              tags/digests for lakerunner, maestro and dex are
                              locked into this driver; only this prefix is
                              operator-supplied. Default: $DEFAULT_IMAGE_REGISTRY.
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
  DEX_EXTRA_USERS             JSON array of additional DEX login accounts, each
                              with an "email" and a bcrypt "hash" (optional
                              "username"/"userID").  Multi-line ok (flattened
                              before passing as the DexExtraUsers stack param).
                              Add a user's email to OIDC_SUPERADMIN_EMAILS to
                              make them a superadmin.
  DEX_EXTRA_USERS_FILE        Path to a JSON file with the same content --
                              fallback for DEX_EXTRA_USERS.
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
  PUBLIC_DNS_NAME             DNS name the install is reached at (e.g.
                              lakerunner.example.com), typically a CNAME the
                              operator points at the ALB (AlbDnsName stack
                              output).  Maestro/Dex OIDC issuer and redirect
                              URLs are derived from it, so the certificate must
                              match it.  Unset: the raw ALB DNS name is used.
  PROCESS_LOGS_MEMORY         Fargate task memory (MiB) for process-logs
                              (template default 4096).  Must be a valid Fargate
                              CPU/memory combo (at 1 vCPU: 2048-8192).  Unset
                              keeps the stack's current value on update (template
                              default on create) -- set it to apply a new size.
  PROCESS_METRICS_MEMORY      Fargate task memory (MiB) for process-metrics
                              (template default 2048).  Same combo rules; unset
                              keeps the current value.
  PROCESS_TRACES_MEMORY       Fargate task memory (MiB) for process-traces
                              (template default 2048).  Same combo rules; unset
                              keeps the current value.
  DB_INIT_IMAGE               Full image URI override for the db-init image
                              (official postgres psql client). Bypasses
                              IMAGE_REGISTRY. Default: the baked, pinned suffix
                              under IMAGE_REGISTRY (always passed to the stack).
  MIGRATION_FORCE_DIRTY       true | false (default true).  true: the lakerunner
                              migrator recovers a database left dirty by a
                              failed migration (rewinds to the previous version
                              and re-runs it); a no-op on a clean database.
                              Always passed, so unset means true on update too.
                              Set false for a LAKERUNNER_IMAGE older than
                              v1.92.0 (it rejects --force-dirty).  Changing it
                              re-runs the migrator.
  MIGRATION_SCALE_DOWN        auto | always | never (default auto).  The
                              migrator runs while the old service tasks are
                              still up, and its schema locks can deadlock
                              against them.  Before executing, the driver
                              scales the process and control services to zero
                              (suspending their autoscaling), and afterwards
                              restores their task counts and autoscaling --
                              on success, failure, or interrupt.  auto: only
                              when the change set touches the Migration child
                              (e.g. an image bump).  always: on every update.
                              never: leave them running.  Ingest pauses for
                              the migration; queries keep serving.  The
                              driver waits until every task's containers
                              have exited, which includes the admin-api
                              target group's deregistration delay.
  MIGRATION_DRAIN_TIMEOUT     Seconds to wait for the stopped services' tasks
                              to exit (default 900).  On timeout the driver
                              restores the services and does not execute.
  MIGRATION_DRAIN_POLL        Seconds between those checks (default 10).
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

migration_scale_down="${MIGRATION_SCALE_DOWN:-auto}"
case "$migration_scale_down" in
    auto|always|never) : ;;
    *) echo "[deploy-lakerunner-services] ERROR: MIGRATION_SCALE_DOWN must be auto, always, or never (got '$migration_scale_down')" >&2; exit 2 ;;
esac
migration_drain_timeout="${MIGRATION_DRAIN_TIMEOUT:-900}"
migration_drain_poll="${MIGRATION_DRAIN_POLL:-10}"
for v in "$migration_drain_timeout" "$migration_drain_poll"; do
    case "$v" in
        ''|*[!0-9]*) echo "[deploy-lakerunner-services] ERROR: MIGRATION_DRAIN_TIMEOUT and MIGRATION_DRAIN_POLL must be whole seconds (got '$v')" >&2; exit 2 ;;
    esac
done
migration_force_dirty="${MIGRATION_FORCE_DIRTY:-true}"
case "$migration_force_dirty" in
    true|false) : ;;
    *) echo "[deploy-lakerunner-services] ERROR: MIGRATION_FORCE_DIRTY must be true or false (got '$migration_force_dirty')" >&2; exit 2 ;;
esac

if ! command -v aws >/dev/null 2>&1 || ! command -v jq >/dev/null 2>&1; then
    echo "[deploy-lakerunner-services] ERROR: aws and jq are required" >&2
    exit 2
fi

template_base_url="${TEMPLATE_BASE_URL:-$DEFAULT_TEMPLATE_BASE_URL}"

# STACK_VERSION (preferred) or the legacy VERSION alias, else the baked default.
stack_version="${STACK_VERSION:-${VERSION:-$DEFAULT_STACK_VERSION}}"
# IMAGE_REGISTRY prefix + the baked, locked image paths -> literal image params.
image_registry="${IMAGE_REGISTRY:-$DEFAULT_IMAGE_REGISTRY}"
lakerunner_image="$image_registry/$LAKERUNNER_IMAGE_SUFFIX"
maestro_image="$image_registry/$MAESTRO_IMAGE_SUFFIX"
dex_image="$image_registry/$DEX_IMAGE_SUFFIX"
# db-init: the baked default tracks the registry prefix like the others; a
# full-URI DB_INIT_IMAGE wins when set (e.g. an unusual mirror layout).
db_init_image="${DB_INIT_IMAGE:-$image_registry/$DB_INIT_IMAGE_SUFFIX}"
echo "[deploy-lakerunner-services] resolved STACK_VERSION = $stack_version" >&2
echo "[deploy-lakerunner-services] resolved LakerunnerImage = $lakerunner_image" >&2
echo "[deploy-lakerunner-services] resolved MaestroImage    = $maestro_image" >&2
echo "[deploy-lakerunner-services] resolved DexImage        = $dex_image" >&2
echo "[deploy-lakerunner-services] resolved DbInitImage     = $db_init_image" >&2

TEMPLATE_URL="$template_base_url/$stack_version/$TEMPLATE_KEY"

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
TemplateBaseUrl=$template_base_url/$stack_version/cardinal-lakerunner/
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
[ -n "${PUBLIC_DNS_NAME:-}" ] && params="$params
PublicDnsName=$PUBLIC_DNS_NAME"
[ -n "$self_telemetry_endpoint" ] && params="$params
SelfTelemetryEndpoint=$self_telemetry_endpoint"

# Process-tier Fargate memory (MiB). Passed only when explicitly set, so an
# existing install's value carries forward on update unless the operator
# overrides it (like the images above, a bumped template default is otherwise
# never picked up on update -- but unlike the images we do NOT force these, to
# avoid clobbering an operator's deliberate sizing).
[ -n "${PROCESS_LOGS_MEMORY:-}" ] && params="$params
ProcessLogsMemory=$PROCESS_LOGS_MEMORY"
[ -n "${PROCESS_METRICS_MEMORY:-}" ] && params="$params
ProcessMetricsMemory=$PROCESS_METRICS_MEMORY"
[ -n "${PROCESS_TRACES_MEMORY:-}" ] && params="$params
ProcessTracesMemory=$PROCESS_TRACES_MEMORY"

params="$params
LakerunnerMigrateForceDirty=$migration_force_dirty"

# Public-ECR images: composed from IMAGE_REGISTRY + the baked, locked suffixes,
# always passed as literal params so a redeploy carries the pinned defaults
# (a stuck UsePreviousValue would never pick up a bumped default otherwise).
params="$params
LakerunnerImage=$lakerunner_image
MaestroImage=$maestro_image
DexImage=$dex_image
DbInitImage=$db_init_image"

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
        # No ARN, no PEM.  Generate a self-signed cert whenever the engine will
        # do a fresh CREATE: when the stack is absent, or when it is in a state
        # the engine deletes and recreates (REVIEW_IN_PROGRESS / ROLLBACK_COMPLETE
        # -- kept in sync with the recreate states in base.sh).  On an in-place
        # UPDATE the existing cert is left untouched (the engine resolves it to
        # UsePreviousValue) so the ALB HTTPS listener does not churn.
        #
        # A bare "does the stack exist?" check is wrong here: it skips generation
        # for a ROLLBACK_COMPLETE stack that the engine then recreates, leaving
        # CertificateArn empty and failing the listener with
        # "Certificate ARN '' is not valid".
        cert_stack_status=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" \
            --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo "")
        case "$cert_stack_status" in
            ""|REVIEW_IN_PROGRESS|ROLLBACK_COMPLETE)
                if ! command -v openssl >/dev/null 2>&1; then
                    echo "[deploy-lakerunner-services] ERROR: openssl is required to auto-generate a self-signed cert; install openssl or set CERTIFICATE_ARN / CERTIFICATE_BODY+CERTIFICATE_PRIVATE_KEY (or their *_FILE variants)" >&2
                    exit 2
                fi
                echo "[deploy-lakerunner-services] no CERTIFICATE_ARN and stack will be created (${cert_stack_status:-absent}); generating a self-signed internal cert" >&2
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
                ;;
            *)
                echo "[deploy-lakerunner-services] stack is $cert_stack_status (in-place update); keeping the existing self-signed cert (no regeneration)" >&2
                ;;
        esac
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

# Additional DEX login accounts.  Inline DEX_EXTRA_USERS rides PARAMS, which is
# newline-delimited -- but the value is JSON, where newlines are only ever
# insignificant whitespace, so a multi-line blob is flattened before appending.
# Inline wins when both forms are set.
if [ -n "${DEX_EXTRA_USERS:-}" ]; then
    params="$params
DexExtraUsers=$(printf '%s' "$DEX_EXTRA_USERS" | tr -d '\r\n')"
elif [ -n "${DEX_EXTRA_USERS_FILE:-}" ]; then
    [ -r "$DEX_EXTRA_USERS_FILE" ] || { echo "[deploy-lakerunner-services] ERROR: cannot read DEX_EXTRA_USERS_FILE: $DEX_EXTRA_USERS_FILE" >&2; exit 2; }
    if [ -n "$file_params" ]; then
        file_params="$file_params
DexExtraUsers=$DEX_EXTRA_USERS_FILE"
    else
        file_params="DexExtraUsers=$DEX_EXTRA_USERS_FILE"
    fi
fi

# --- Stop the lrdb writers while the migrator runs. --------------------------
# The Migration child updates before the service tiers (they DependsOn it), so
# the migrator runs while the old process/control tasks are still working the
# queues.  Its strong schema locks can deadlock against them and leave the
# database dirty.  The engine calls these hooks around execute-change-set.
# writer_state holds one line per stopped service:
#   <service-arn> <desired-count> <scalable-target-resource-id> <suspended-state-json|null>
writer_state=""

# ECS service ARNs owned by the nested stack at logical id $1 (none if absent).
nested_ecs_services() {
    nested=$(aws cloudformation describe-stack-resource \
        --stack-name "$STACK_NAME" \
        --logical-resource-id "$1" \
        --region "$REGION" \
        --query 'StackResourceDetail.PhysicalResourceId' \
        --output text 2>/dev/null || echo "")
    [ -n "$nested" ] && [ "$nested" != "None" ] || return 0
    aws cloudformation list-stack-resources \
        --stack-name "$nested" \
        --region "$REGION" \
        --query "StackResourceSummaries[?ResourceType=='AWS::ECS::Service'].PhysicalResourceId" \
        --output text
}

scale_down_writers() {
    [ "$mode" = "update" ] || return 0
    case "$migration_scale_down" in
        never)
            return 0
            ;;
        auto)
            migration_action=$(aws cloudformation describe-change-set \
                --stack-name "$STACK_NAME" \
                --change-set-name "$change_set_name" \
                --region "$REGION" \
                --query "Changes[?ResourceChange.LogicalResourceId=='Migration'].ResourceChange.Action" \
                --output text)
            if [ -z "$migration_action" ]; then
                echo "[deploy-lakerunner-services] change set does not touch the Migration child; leaving services running" >&2
                return 0
            fi
            ;;
    esac

    stopped=""
    for tier in Process Control; do
        for svc in $(nested_ecs_services "$tier"); do
            svc_name=${svc##*/}
            desired=$(aws ecs describe-services \
                --cluster "$CLUSTER_ARN" \
                --services "$svc" \
                --region "$REGION" \
                --query 'services[0].desiredCount' \
                --output text)
            resource_id="service/$CLUSTER_NAME/$svc_name"
            suspended=$(aws application-autoscaling describe-scalable-targets \
                --service-namespace ecs \
                --resource-ids "$resource_id" \
                --region "$REGION" \
                --query 'ScalableTargets[0].SuspendedState' \
                --output json | jq -c .)
            # Record before changing anything, so a failure part way through
            # still restores this service.
            writer_state="${writer_state}$svc $desired $resource_id $suspended
"
            if [ "$suspended" != "null" ]; then
                aws application-autoscaling register-scalable-target \
                    --service-namespace ecs \
                    --scalable-dimension ecs:service:DesiredCount \
                    --resource-id "$resource_id" \
                    --suspended-state DynamicScalingInSuspended=true,DynamicScalingOutSuspended=true,ScheduledScalingSuspended=true \
                    --region "$REGION" >/dev/null
            fi
            echo "[deploy-lakerunner-services] scaling $svc_name $desired -> 0 for the migration" >&2
            aws ecs update-service \
                --cluster "$CLUSTER_ARN" \
                --service "$svc" \
                --desired-count 0 \
                --region "$REGION" >/dev/null
            stopped="$stopped $svc"
        done
    done

    for svc in $stopped; do
        echo "[deploy-lakerunner-services] waiting for ${svc##*/} to stop" >&2
        aws ecs wait services-stable \
            --cluster "$CLUSTER_ARN" \
            --services "$svc" \
            --region "$REGION"
        wait_tasks_exited "$svc"
    done
}

# Number of the service's tasks whose containers may still be running.
# runningCount drops to 0 as soon as a task leaves RUNNING, but a task behind a
# target group then sits in DEACTIVATING for the deregistration delay with its
# containers still up; they only exit during STOPPING.
live_task_count() {
    tasks=""
    for desired in RUNNING STOPPED; do
        listed=$(aws ecs list-tasks --cluster "$CLUSTER_ARN" --service-name "${1##*/}" \
            --desired-status "$desired" --region "$REGION" \
            --query 'taskArns[]' --output text) || return 1
        tasks="$tasks
$listed"
    done
    tasks=$(printf '%s\n' "$tasks" | tr '\t' '\n' | grep -v -e '^$' -e '^None$' || true)
    [ -n "$tasks" ] || { echo 0; return 0; }
    counts=$(echo "$tasks" | xargs -n 100 aws ecs describe-tasks --cluster "$CLUSTER_ARN" \
        --region "$REGION" \
        --query "length(tasks[?lastStatus!='DEPROVISIONING' && lastStatus!='STOPPED'])" \
        --output text --tasks) || return 1
    echo "$counts" | awk '{n += $1} END {print n + 0}'
}

wait_tasks_exited() {
    waited=0
    while :; do
        live=$(live_task_count "$1") || {
            echo "[deploy-lakerunner-services] ERROR: could not list ${1##*/} tasks; not running the migration" >&2
            return 1
        }
        [ "$live" -eq 0 ] && return 0
        if [ "$waited" -ge "$migration_drain_timeout" ]; then
            echo "[deploy-lakerunner-services] ERROR: ${1##*/} still has $live task(s) running after ${waited}s; not running the migration" >&2
            return 1
        fi
        echo "[deploy-lakerunner-services] ${1##*/}: $live task(s) still draining" >&2
        sleep "$migration_drain_poll"
        waited=$((waited + migration_drain_poll))
    done
}

restore_writers() {
    [ -n "$writer_state" ] || return 0
    restore_failed=""
    while read -r svc desired resource_id suspended; do
        [ -n "$svc" ] || continue
        echo "[deploy-lakerunner-services] restoring ${svc##*/} to $desired" >&2
        aws ecs update-service \
            --cluster "$CLUSTER_ARN" \
            --service "$svc" \
            --desired-count "$desired" \
            --region "$REGION" >/dev/null || restore_failed="$restore_failed ${svc##*/}"
        if [ "$suspended" != "null" ]; then
            aws application-autoscaling register-scalable-target \
                --service-namespace ecs \
                --scalable-dimension ecs:service:DesiredCount \
                --resource-id "$resource_id" \
                --suspended-state "$suspended" \
                --region "$REGION" >/dev/null || restore_failed="$restore_failed ${svc##*/}(autoscaling)"
        fi
    done <<WRITERS
$writer_state
WRITERS
    writer_state=""
    if [ -n "$restore_failed" ]; then
        echo "[deploy-lakerunner-services] ERROR: could not restore:$restore_failed -- set their desired counts and autoscaling by hand" >&2
        return 1
    fi
}

# Read by the embedded engine below.
# shellcheck disable=SC2034
pre_execute_hook=scale_down_writers
# shellcheck disable=SC2034
post_execute_hook=restore_writers

PARAMS="$params"
FILE_PARAMS="$file_params"

export TEMPLATE_URL PARAMS FILE_PARAMS FROM_STACKS MAPS
