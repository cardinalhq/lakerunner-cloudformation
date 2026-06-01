#!/bin/sh
# Jenkins job 1: deploy the cardinal-lakerunner-infra-base stack.
#
# This is the head of the chain -- it has no upstream stacks.  It owns the IAM
# roles, security groups, cooked bucket, license/admin secrets, and SSM params
# that every downstream stack consumes.
#
# Thin wrapper over deploy-stack.sh: composes the published template URL from
# --template-base-url + --version and forwards this stack's add-in parameters.

set -eu

SCRIPT_DIR=$(cd -- "$(dirname -- "$0")" >/dev/null 2>&1 && pwd)
DEFAULT_TEMPLATE_BASE_URL="https://cardinal-cfn.s3.us-east-2.amazonaws.com/lakerunner"
TEMPLATE_KEY="cardinal-lakerunner-infra-base.yaml"

stack_name=""
region=""
version=""
template_base_url="$DEFAULT_TEMPLATE_BASE_URL"
deployer_role_arn=""
no_execute=""

vpc_id=""
cluster_arn=""
alb_scheme=""
alb_cidr1=""
alb_cidr2=""
alb_cidr3=""
organization_id=""
initial_ingest_api_key=""
license_data_file=""
cooked_bucket_name=""
license_secret_name=""
admin_key_secret_name=""
api_keys_param_name=""
storage_profiles_param_name=""

usage() {
    cat <<'EOF'
Usage: deploy-lakerunner-infra-base.sh --stack-name NAME --region REGION --version VER [options]

Required:
  --stack-name NAME           Stack to create/update.
  --region REGION             AWS region.
  --version VERSION           Published template tag, e.g. v0.0.70.
  --vpc-id VPC                VPC for the security groups.
  --cluster-arn ARN           Customer-supplied ECS cluster ARN.
  --license-data-file PATH    Path to license JSON (seeds the license secret).

Add-ins (defaults in the template are fine if omitted):
  --alb-scheme SCHEME         internet-facing | internal.
  --alb-allowed-cidr1 CIDR    ALB ingress CIDR allowlist (up to three).
  --alb-allowed-cidr2 CIDR
  --alb-allowed-cidr3 CIDR
  --organization-id UUID      Canonical org id seeded into config.
  --initial-ingest-api-key K  Bootstrap ingest API key.
  --cooked-bucket-name NAME
  --license-secret-name NAME
  --admin-key-secret-name NAME
  --api-keys-param-name NAME
  --storage-profiles-param-name NAME

Common:
  --template-base-url URL     Default: https://cardinal-cfn.s3.us-east-2.amazonaws.com/lakerunner
  --deployer-role-arn ARN
  --no-execute
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --stack-name) stack_name="$2"; shift 2 ;;
        --region) region="$2"; shift 2 ;;
        --version) version="$2"; shift 2 ;;
        --template-base-url) template_base_url="$2"; shift 2 ;;
        --deployer-role-arn) deployer_role_arn="$2"; shift 2 ;;
        --no-execute) no_execute="--no-execute"; shift ;;
        --vpc-id) vpc_id="$2"; shift 2 ;;
        --cluster-arn) cluster_arn="$2"; shift 2 ;;
        --alb-scheme) alb_scheme="$2"; shift 2 ;;
        --alb-allowed-cidr1) alb_cidr1="$2"; shift 2 ;;
        --alb-allowed-cidr2) alb_cidr2="$2"; shift 2 ;;
        --alb-allowed-cidr3) alb_cidr3="$2"; shift 2 ;;
        --organization-id) organization_id="$2"; shift 2 ;;
        --initial-ingest-api-key) initial_ingest_api_key="$2"; shift 2 ;;
        --license-data-file) license_data_file="$2"; shift 2 ;;
        --cooked-bucket-name) cooked_bucket_name="$2"; shift 2 ;;
        --license-secret-name) license_secret_name="$2"; shift 2 ;;
        --admin-key-secret-name) admin_key_secret_name="$2"; shift 2 ;;
        --api-keys-param-name) api_keys_param_name="$2"; shift 2 ;;
        --storage-profiles-param-name) storage_profiles_param_name="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "[deploy-lakerunner-infra-base] ERROR: unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

if [ -z "$stack_name" ] || [ -z "$region" ] || [ -z "$version" ]; then
    usage >&2
    echo "[deploy-lakerunner-infra-base] ERROR: --stack-name, --region, and --version are required" >&2
    exit 2
fi

template_url="$template_base_url/$version/$TEMPLATE_KEY"

set -- --stack-name "$stack_name" --template-url "$template_url" --region "$region"
[ -n "$deployer_role_arn" ] && set -- "$@" --deployer-role-arn "$deployer_role_arn"
[ -n "$no_execute" ] && set -- "$@" "$no_execute"

[ -n "$vpc_id" ] && set -- "$@" --param "VpcId=$vpc_id"
[ -n "$cluster_arn" ] && set -- "$@" --param "ClusterArn=$cluster_arn"
[ -n "$alb_scheme" ] && set -- "$@" --param "AlbScheme=$alb_scheme"
[ -n "$alb_cidr1" ] && set -- "$@" --param "AlbAllowedCidr1=$alb_cidr1"
[ -n "$alb_cidr2" ] && set -- "$@" --param "AlbAllowedCidr2=$alb_cidr2"
[ -n "$alb_cidr3" ] && set -- "$@" --param "AlbAllowedCidr3=$alb_cidr3"
[ -n "$organization_id" ] && set -- "$@" --param "OrganizationId=$organization_id"
[ -n "$initial_ingest_api_key" ] && set -- "$@" --param "InitialIngestApiKey=$initial_ingest_api_key"
[ -n "$cooked_bucket_name" ] && set -- "$@" --param "CookedBucketName=$cooked_bucket_name"
[ -n "$license_secret_name" ] && set -- "$@" --param "LicenseSecretName=$license_secret_name"
[ -n "$admin_key_secret_name" ] && set -- "$@" --param "AdminKeySecretName=$admin_key_secret_name"
[ -n "$api_keys_param_name" ] && set -- "$@" --param "ApiKeysParamName=$api_keys_param_name"
[ -n "$storage_profiles_param_name" ] && set -- "$@" --param "StorageProfilesParamName=$storage_profiles_param_name"

if [ -n "$license_data_file" ]; then
    if [ ! -r "$license_data_file" ]; then
        echo "[deploy-lakerunner-infra-base] ERROR: cannot read --license-data-file: $license_data_file" >&2
        exit 2
    fi
    set -- "$@" --param "LicenseData=$(cat "$license_data_file")"
fi

exec "$SCRIPT_DIR/deploy-stack.sh" "$@"
