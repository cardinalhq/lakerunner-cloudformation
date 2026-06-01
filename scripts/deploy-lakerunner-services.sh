#!/bin/sh
# Jenkins job 5: deploy the cardinal-lakerunner-services stack (the application
# tier: query, process, control, otel, maestro).
#
# Upstream:
#   - lakerunner-infra-base : roles, security groups, secrets, SSM param names.
#   - lakerunner-infra-rds  : Db{Endpoint,MasterSecretArn,Name,Port}.
# All of those output names match the template's parameter names, so plain
# --from-stack pulls wire them up.
#
# Special case: PubsubSqsEnv is COMPUTED here.  It is not a single upstream
# output -- we read three outputs from the satellite-infra-base stack and
# assemble the env string the pubsub-sqs container expects:
#   SQS_QUEUE_URL=<RawQueueUrl>;SQS_REGION=<Region>;SQS_ROLE_ARN=<LakerunnerAccessRoleArn>
# then pass it via --param PubsubSqsEnv=... (highest precedence).
#
# OtelReplicas defaults to 0 here: in the satellite topology the same-account
# satellite collector performs ingest, so the lakerunner-tier otel collector is
# off by default.
#
# Thin wrapper over deploy-stack.sh.

set -eu

SCRIPT_DIR=$(cd -- "$(dirname -- "$0")" >/dev/null 2>&1 && pwd)
DEFAULT_TEMPLATE_BASE_URL="https://cardinal-cfn.s3.us-east-2.amazonaws.com/lakerunner"
TEMPLATE_KEY="cardinal-lakerunner-services.yaml"

stack_name=""
region=""
version=""
template_base_url="$DEFAULT_TEMPLATE_BASE_URL"
deployer_role_arn=""
no_execute=""

infra_base_stack=""
infra_rds_stack=""
satellite_infra_base_stack=""

vpc_id=""
cluster_arn=""
cluster_name=""
private_subnets=""
public_subnets=""
service_namespace_name=""
otel_replicas="0"   # default: lakerunner-tier collector off; satellite ingests

# Images.
lakerunner_image=""
maestro_image=""
otel_image=""
dex_image=""
dex_init_image=""
db_init_image=""

# Cert (ACM ARN, or PEM material).
certificate_arn=""
certificate_body_file=""
certificate_private_key_file=""
certificate_chain_file=""

# Dex / OIDC.
dex_admin_email=""
dex_admin_password_hash=""
dex_client_id=""
oidc_superadmin_emails=""

usage() {
    cat <<'EOF'
Usage: deploy-lakerunner-services.sh --stack-name NAME --region REGION --version VER \
           --infra-base-stack NAME --infra-rds-stack NAME \
           --satellite-infra-base-stack NAME [options]

Required:
  --stack-name NAME                  Stack to create/update.
  --region REGION                    AWS region.
  --version VERSION                  Published template tag.
  --infra-base-stack NAME            Upstream lakerunner-infra-base.
  --infra-rds-stack NAME             Upstream lakerunner-infra-rds.
  --satellite-infra-base-stack NAME  Source of RawQueueUrl/Region/
                                     LakerunnerAccessRoleArn for the computed
                                     PubsubSqsEnv.

Networking / cluster add-ins:
  --vpc-id VPC
  --cluster-arn ARN
  --cluster-name NAME
  --private-subnets CSV
  --public-subnets CSV
  --service-namespace-name NAME
  --otel-replicas N                  Default 0 (satellite collector ingests).

Images:
  --lakerunner-image REF             --maestro-image REF    --otel-image REF
  --dex-image REF                    --dex-init-image REF   --db-init-image REF

Cert (ACM ARN or PEM material):
  --certificate-arn ARN
  --certificate-body-file PATH       --certificate-private-key-file PATH
  --certificate-chain-file PATH

Dex / OIDC:
  --dex-admin-email EMAIL            --dex-admin-password-hash HASH
  --dex-client-id ID                 --oidc-superadmin-emails CSV

Common:
  --template-base-url URL            Default: https://cardinal-cfn.s3.us-east-2.amazonaws.com/lakerunner
                                     Also forwarded as the TemplateBaseUrl param.
  --deployer-role-arn ARN
  --no-execute
EOF
}

read_file_or_die() {
    p="$1"
    if [ ! -r "$p" ]; then
        echo "[deploy-lakerunner-services] ERROR: cannot read file: $p" >&2
        exit 2
    fi
    cat "$p"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --stack-name) stack_name="$2"; shift 2 ;;
        --region) region="$2"; shift 2 ;;
        --version) version="$2"; shift 2 ;;
        --template-base-url) template_base_url="$2"; shift 2 ;;
        --deployer-role-arn) deployer_role_arn="$2"; shift 2 ;;
        --no-execute) no_execute="--no-execute"; shift ;;
        --infra-base-stack) infra_base_stack="$2"; shift 2 ;;
        --infra-rds-stack) infra_rds_stack="$2"; shift 2 ;;
        --satellite-infra-base-stack) satellite_infra_base_stack="$2"; shift 2 ;;
        --vpc-id) vpc_id="$2"; shift 2 ;;
        --cluster-arn) cluster_arn="$2"; shift 2 ;;
        --cluster-name) cluster_name="$2"; shift 2 ;;
        --private-subnets) private_subnets="$2"; shift 2 ;;
        --public-subnets) public_subnets="$2"; shift 2 ;;
        --service-namespace-name) service_namespace_name="$2"; shift 2 ;;
        --otel-replicas) otel_replicas="$2"; shift 2 ;;
        --lakerunner-image) lakerunner_image="$2"; shift 2 ;;
        --maestro-image) maestro_image="$2"; shift 2 ;;
        --otel-image) otel_image="$2"; shift 2 ;;
        --dex-image) dex_image="$2"; shift 2 ;;
        --dex-init-image) dex_init_image="$2"; shift 2 ;;
        --db-init-image) db_init_image="$2"; shift 2 ;;
        --certificate-arn) certificate_arn="$2"; shift 2 ;;
        --certificate-body-file) certificate_body_file="$2"; shift 2 ;;
        --certificate-private-key-file) certificate_private_key_file="$2"; shift 2 ;;
        --certificate-chain-file) certificate_chain_file="$2"; shift 2 ;;
        --dex-admin-email) dex_admin_email="$2"; shift 2 ;;
        --dex-admin-password-hash) dex_admin_password_hash="$2"; shift 2 ;;
        --dex-client-id) dex_client_id="$2"; shift 2 ;;
        --oidc-superadmin-emails) oidc_superadmin_emails="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "[deploy-lakerunner-services] ERROR: unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

if [ -z "$stack_name" ] || [ -z "$region" ] || [ -z "$version" ] \
    || [ -z "$infra_base_stack" ] || [ -z "$infra_rds_stack" ] \
    || [ -z "$satellite_infra_base_stack" ]; then
    usage >&2
    echo "[deploy-lakerunner-services] ERROR: --stack-name, --region, --version, --infra-base-stack, --infra-rds-stack, and --satellite-infra-base-stack are required" >&2
    exit 2
fi

if ! command -v aws >/dev/null 2>&1 || ! command -v jq >/dev/null 2>&1; then
    echo "[deploy-lakerunner-services] ERROR: aws and jq are required" >&2
    exit 2
fi

template_url="$template_base_url/$version/$TEMPLATE_KEY"

# --- Compute PubsubSqsEnv from the satellite-infra-base stack outputs. -------
sat_outputs=$(aws cloudformation describe-stacks \
    --stack-name "$satellite_infra_base_stack" \
    --region "$region" \
    --query 'Stacks[0].Outputs' \
    --output json)

queue_url=$(printf '%s' "$sat_outputs" | jq -r '(.[] | select(.OutputKey == "RawQueueUrl") | .OutputValue) // ""')
sqs_region=$(printf '%s' "$sat_outputs" | jq -r '(.[] | select(.OutputKey == "Region") | .OutputValue) // ""')
role_arn=$(printf '%s' "$sat_outputs" | jq -r '(.[] | select(.OutputKey == "LakerunnerAccessRoleArn") | .OutputValue) // ""')

if [ -z "$queue_url" ] || [ -z "$sqs_region" ] || [ -z "$role_arn" ]; then
    echo "[deploy-lakerunner-services] ERROR: satellite-infra-base stack '$satellite_infra_base_stack' is missing one of RawQueueUrl/Region/LakerunnerAccessRoleArn outputs" >&2
    exit 2
fi

pubsub_sqs_env="SQS_QUEUE_URL=$queue_url;SQS_REGION=$sqs_region;SQS_ROLE_ARN=$role_arn"

# --- Assemble deploy-stack.sh invocation. ------------------------------------
set -- --stack-name "$stack_name" --template-url "$template_url" --region "$region"
set -- "$@" --from-stack "$infra_base_stack"
set -- "$@" --from-stack "$infra_rds_stack"
[ -n "$deployer_role_arn" ] && set -- "$@" --deployer-role-arn "$deployer_role_arn"
[ -n "$no_execute" ] && set -- "$@" "$no_execute"

set -- "$@" --param "PubsubSqsEnv=$pubsub_sqs_env"
# TemplateBaseUrl must track the version we deploy so nested children load from
# the matching prefix.
set -- "$@" --param "TemplateBaseUrl=$template_base_url/$version/cardinal-lakerunner/"

[ -n "$vpc_id" ] && set -- "$@" --param "VpcId=$vpc_id"
[ -n "$cluster_arn" ] && set -- "$@" --param "ClusterArn=$cluster_arn"
[ -n "$cluster_name" ] && set -- "$@" --param "ClusterName=$cluster_name"
[ -n "$private_subnets" ] && set -- "$@" --param "PrivateSubnets=$private_subnets"
[ -n "$public_subnets" ] && set -- "$@" --param "PublicSubnets=$public_subnets"
[ -n "$service_namespace_name" ] && set -- "$@" --param "ServiceNamespaceName=$service_namespace_name"
[ -n "$otel_replicas" ] && set -- "$@" --param "OtelReplicas=$otel_replicas"

[ -n "$lakerunner_image" ] && set -- "$@" --param "LakerunnerImage=$lakerunner_image"
[ -n "$maestro_image" ] && set -- "$@" --param "MaestroImage=$maestro_image"
[ -n "$otel_image" ] && set -- "$@" --param "OtelImage=$otel_image"
[ -n "$dex_image" ] && set -- "$@" --param "DexImage=$dex_image"
[ -n "$dex_init_image" ] && set -- "$@" --param "DexInitImage=$dex_init_image"
[ -n "$db_init_image" ] && set -- "$@" --param "DbInitImage=$db_init_image"

[ -n "$certificate_arn" ] && set -- "$@" --param "CertificateArn=$certificate_arn"
[ -n "$certificate_body_file" ] && set -- "$@" --param "CertificateBody=$(read_file_or_die "$certificate_body_file")"
[ -n "$certificate_private_key_file" ] && set -- "$@" --param "CertificatePrivateKey=$(read_file_or_die "$certificate_private_key_file")"
[ -n "$certificate_chain_file" ] && set -- "$@" --param "CertificateChain=$(read_file_or_die "$certificate_chain_file")"

[ -n "$dex_admin_email" ] && set -- "$@" --param "DexAdminEmail=$dex_admin_email"
[ -n "$dex_admin_password_hash" ] && set -- "$@" --param "DexAdminPasswordHash=$dex_admin_password_hash"
[ -n "$dex_client_id" ] && set -- "$@" --param "DexClientId=$dex_client_id"
[ -n "$oidc_superadmin_emails" ] && set -- "$@" --param "OidcSuperadminEmails=$oidc_superadmin_emails"

exec "$SCRIPT_DIR/deploy-stack.sh" "$@"
