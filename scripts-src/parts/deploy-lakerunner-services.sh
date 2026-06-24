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
# Satellite config: RawQueueUrl / RawBucketName / LakerunnerAccessRoleArn are
# pulled from the satellite-infra-base stack and used to synthesize the central
# collector entry in the MAESTRO_SATELLITE_CONFIG JSON, which is written to SSM
# /cardinal/satellites and passed as SatellitesParamName.
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
  SATELLITE_INFRA_BASE_STACK  Source of RawQueueUrl / RawBucketName /
                              LakerunnerAccessRoleArn for the central collector.
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
  SATELLITE_CONFIG            JSON string: operator satellite collectors, as an
                              { "organizations": { ... } } document.  This is a
                              single-install deployment: declare read-only/satellite
                              collectors under the INSTALL org (ORGANIZATION_ID)
                              ONLY -- any other org key is rejected.  Must NOT
                              declare a "normal" collector (the central collector is
                              auto-synthesized from the infra-base stack outputs).
                              Merged with the central collector before writing to SSM.
                              UPGRADE NOTE: move any old QUEUE_URL_<n>/
                              QUEUE_REGION_<n>/QUEUE_ROLE_ARN_<n> entries here;
                              CENTRAL_COLLECTOR_NAME must match the existing
                              install's collector name (default: lakerunner).
  SATELLITE_CONFIG_FILE       Path to a JSON file with the same content as
                              SATELLITE_CONFIG.  Fallback when SATELLITE_CONFIG is
                              unset.
  CENTRAL_COLLECTOR_NAME      Name for the central (normal) collector synthesized
                              from the install's infra-base outputs.  Default:
                              lakerunner.  MUST match the existing collector name
                              on upgrades (the upsert is keyed on this name).
  SATELLITES_PARAM_NAME       SSM parameter name to write the composed satellite
                              JSON to.  Default: /cardinal/satellites.
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

# --- Read RawQueueUrl / RawBucketName / LakerunnerAccessRoleArn from the satellite-infra-base stack. ---
sat_outputs=$(aws cloudformation describe-stacks \
    --stack-name "$SATELLITE_INFRA_BASE_STACK" \
    --region "$REGION" \
    --query 'Stacks[0].Outputs' \
    --output json)

queue_url=$(printf '%s' "$sat_outputs" | jq -r '(.[] | select(.OutputKey == "RawQueueUrl") | .OutputValue) // ""')
raw_bucket=$(printf '%s' "$sat_outputs" | jq -r '(.[] | select(.OutputKey == "RawBucketName") | .OutputValue) // ""')
role_arn=$(printf '%s' "$sat_outputs" | jq -r '(.[] | select(.OutputKey == "LakerunnerAccessRoleArn") | .OutputValue) // ""')

if [ -z "$queue_url" ] || [ -z "$raw_bucket" ]; then
    echo "[deploy-lakerunner-services] ERROR: satellite-infra-base stack '$SATELLITE_INFRA_BASE_STACK' is missing RawQueueUrl or RawBucketName output" >&2
    exit 2
fi

# --- Synthesize central collector + merge operator satellites -> SSM. ---------
# The central collector is always "normal" mode, keyed under the install org.
# role is included only when LakerunnerAccessRoleArn is non-empty (cross-account).
central_collector="${CENTRAL_COLLECTOR_NAME:-lakerunner}"
operator_json="${SATELLITE_CONFIG:-}"
if [ -z "$operator_json" ] && [ -n "${SATELLITE_CONFIG_FILE:-}" ]; then
    [ -r "$SATELLITE_CONFIG_FILE" ] || { echo "[deploy-lakerunner-services] ERROR: cannot read SATELLITE_CONFIG_FILE: $SATELLITE_CONFIG_FILE" >&2; exit 2; }
    operator_json=$(cat "$SATELLITE_CONFIG_FILE")
fi
operator_json="${operator_json:-{\"organizations\":{}}}"

# Single-install: every satellite must feed the install org.  Reject any
# operator-declared org key other than $ORGANIZATION_ID with a friendly message
# before the generic validation (which would otherwise emit a cryptic "0-normal"
# error for the foreign org).
other_orgs=$(printf '%s' "$operator_json" | jq -r --arg org "$ORGANIZATION_ID" \
    '[(.organizations // {} | keys[]) | select(. != $org)] | join(", ")' 2>&1) \
    || { echo "[deploy-lakerunner-services] ERROR parsing SATELLITE_CONFIG: $other_orgs" >&2; exit 2; }
if [ -n "$other_orgs" ]; then
    echo "[deploy-lakerunner-services] ERROR: SATELLITE_CONFIG may only define satellites under the install org $ORGANIZATION_ID; found other org key(s): $other_orgs. This is a single-install deployment -- all satellite raw buckets feed this org." >&2
    exit 2
fi

if [ -n "$role_arn" ]; then
    central_json=$(jq -n \
        --arg org "$ORGANIZATION_ID" --arg coll "$central_collector" \
        --arg bucket "$raw_bucket" --arg sqs "$queue_url" \
        --arg region "$REGION" --arg role "$role_arn" \
        '{organizations: {($org): {collectors: {($coll): {bucket:$bucket, sqsurl:$sqs, region:$region, mode:"normal", role:$role}}}}}')
else
    central_json=$(jq -n \
        --arg org "$ORGANIZATION_ID" --arg coll "$central_collector" \
        --arg bucket "$raw_bucket" --arg sqs "$queue_url" \
        --arg region "$REGION" \
        '{organizations: {($org): {collectors: {($coll): {bucket:$bucket, sqsurl:$sqs, region:$region, mode:"normal"}}}}}')
fi

# Merge operator satellites into central, unioning collectors maps per org.
# Rejects if operator declares a normal collector for the install org.
satellites_json=$(printf '%s' "$operator_json" | jq --argjson c "$central_json" '
    . as $op
    | ($c.organizations | keys[0]) as $org
    | (($op.organizations[$org].collectors // {}) | to_entries
       | map(select((.value.mode // "normal") == "normal")) | length) as $op_normals
    | if $op_normals > 0 then
        error("operator SATELLITE_CONFIG must not declare a normal collector for the install org \($org)")
      else . end
    | reduce ($op.organizations | to_entries[]) as $entry (
        $c;
        .organizations[$entry.key].collectors = (
            (.organizations[$entry.key].collectors // {}) + $entry.value.collectors
        )
      )
' 2>&1) || { echo "[deploy-lakerunner-services] ERROR composing satellite config: $satellites_json" >&2; exit 2; }

# Validate: each org must have exactly one normal collector.
bad=$(printf '%s' "$satellites_json" | jq -r '
    [.organizations | to_entries[] |
        {org:.key, normals: ([.value.collectors[] | select((.mode // "normal") == "normal")] | length)}
        | select(.normals != 1)
        | "\(.org):\(.normals)"] | join(", ")')
[ -n "$bad" ] && { echo "[deploy-lakerunner-services] ERROR: orgs without exactly one normal collector: $bad" >&2; exit 2; }

sat_param="${SATELLITES_PARAM_NAME:-/cardinal/satellites}"
aws ssm put-parameter --name "$sat_param" --type String --tier Advanced \
    --value "$satellites_json" --overwrite --region "$REGION" >/dev/null
echo "[deploy-lakerunner-services] wrote satellite config to SSM $sat_param" >&2

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

# SatellitesParamName and TemplateBaseUrl are always set.  TemplateBaseUrl
# must track the version we deploy so nested children load from the matching
# prefix.
params="SatellitesParamName=$sat_param
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

PARAMS="$params"
FILE_PARAMS="$file_params"

export TEMPLATE_URL PARAMS FILE_PARAMS FROM_STACKS MAPS
