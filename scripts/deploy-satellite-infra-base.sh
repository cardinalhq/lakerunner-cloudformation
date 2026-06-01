#!/bin/sh
# Jenkins job 3: deploy the cardinal-satellite-infra-base stack (one per
# satellite ingest account/region).
#
# Upstream: the lakerunner-infra-base stack.  This stack's LakerunnerPrincipal
# parameter does NOT match any infra-base output name, so it is wired via an
# explicit --map LakerunnerPrincipal=ProcessRoleArn: the satellite trusts the
# lakerunner process role to assume into the satellite ingest access role.
#
# Thin wrapper over deploy-stack.sh.

set -eu

SCRIPT_DIR=$(cd -- "$(dirname -- "$0")" >/dev/null 2>&1 && pwd)
DEFAULT_TEMPLATE_BASE_URL="https://cardinal-cfn.s3.us-east-2.amazonaws.com/lakerunner"
TEMPLATE_KEY="cardinal-satellite-infra-base.yaml"

stack_name=""
region=""
version=""
template_base_url="$DEFAULT_TEMPLATE_BASE_URL"
deployer_role_arn=""
no_execute=""

infra_base_stack=""
external_id=""
raw_bucket_name=""
raw_bucket_lifecycle_days=""

usage() {
    cat <<'EOF'
Usage: deploy-satellite-infra-base.sh --stack-name NAME --region REGION --version VER \
           --infra-base-stack NAME [options]

Required:
  --stack-name NAME           Stack to create/update.
  --region REGION             AWS region.
  --version VERSION           Published template tag.
  --infra-base-stack NAME     Upstream lakerunner-infra-base stack.  Its
                              ProcessRoleArn output is mapped to this stack's
                              LakerunnerPrincipal parameter.

Add-ins (template defaults are fine if omitted):
  --external-id ID            Optional STS ExternalId for the assume-role trust.
  --raw-bucket-name NAME      Optional explicit raw ingest bucket name.
  --raw-bucket-lifecycle-days N

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
        --infra-base-stack) infra_base_stack="$2"; shift 2 ;;
        --external-id) external_id="$2"; shift 2 ;;
        --raw-bucket-name) raw_bucket_name="$2"; shift 2 ;;
        --raw-bucket-lifecycle-days) raw_bucket_lifecycle_days="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "[deploy-satellite-infra-base] ERROR: unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

if [ -z "$stack_name" ] || [ -z "$region" ] || [ -z "$version" ] || [ -z "$infra_base_stack" ]; then
    usage >&2
    echo "[deploy-satellite-infra-base] ERROR: --stack-name, --region, --version, and --infra-base-stack are required" >&2
    exit 2
fi

template_url="$template_base_url/$version/$TEMPLATE_KEY"

set -- --stack-name "$stack_name" --template-url "$template_url" --region "$region"
set -- "$@" --from-stack "$infra_base_stack"
set -- "$@" --map "LakerunnerPrincipal=ProcessRoleArn"
[ -n "$deployer_role_arn" ] && set -- "$@" --deployer-role-arn "$deployer_role_arn"
[ -n "$no_execute" ] && set -- "$@" "$no_execute"

[ -n "$external_id" ] && set -- "$@" --param "ExternalId=$external_id"
[ -n "$raw_bucket_name" ] && set -- "$@" --param "RawBucketName=$raw_bucket_name"
[ -n "$raw_bucket_lifecycle_days" ] && set -- "$@" --param "RawBucketLifecycleDays=$raw_bucket_lifecycle_days"

exec "$SCRIPT_DIR/deploy-stack.sh" "$@"
