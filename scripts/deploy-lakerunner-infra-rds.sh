#!/bin/sh
# Jenkins job 2: deploy the cardinal-lakerunner-infra-rds stack.
#
# Upstream: the lakerunner-infra-base stack supplies the per-service security
# group ids (Control/Maestro/Migration/Process/Query SecurityGroupId outputs),
# which this stack uses to authorize DB ingress.  Those output names match this
# template's parameter names, so a plain --from-stack pull wires them up.
#
# Thin wrapper over deploy-stack.sh.

set -eu

SCRIPT_DIR=$(cd -- "$(dirname -- "$0")" >/dev/null 2>&1 && pwd)
DEFAULT_TEMPLATE_BASE_URL="https://cardinal-cfn.s3.us-east-2.amazonaws.com/lakerunner"
TEMPLATE_KEY="cardinal-lakerunner-infra-rds.yaml"

stack_name=""
region=""
version=""
template_base_url="$DEFAULT_TEMPLATE_BASE_URL"
deployer_role_arn=""
no_execute=""

infra_base_stack=""
vpc_id=""
private_subnets_csv=""
db_engine_version=""
db_instance_class=""
db_allocated_storage=""

usage() {
    cat <<'EOF'
Usage: deploy-lakerunner-infra-rds.sh --stack-name NAME --region REGION --version VER \
           --infra-base-stack NAME [options]

Required:
  --stack-name NAME           Stack to create/update.
  --region REGION             AWS region.
  --version VERSION           Published template tag.
  --infra-base-stack NAME     Upstream lakerunner-infra-base stack (supplies the
                              service security-group ids).
  --vpc-id VPC
  --private-subnets-csv CSV   Comma-separated private subnet ids for the DB.

Add-ins (template defaults are fine if omitted):
  --db-engine-version VER
  --db-instance-class CLASS
  --db-allocated-storage GB

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
        --vpc-id) vpc_id="$2"; shift 2 ;;
        --private-subnets-csv) private_subnets_csv="$2"; shift 2 ;;
        --db-engine-version) db_engine_version="$2"; shift 2 ;;
        --db-instance-class) db_instance_class="$2"; shift 2 ;;
        --db-allocated-storage) db_allocated_storage="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "[deploy-lakerunner-infra-rds] ERROR: unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

if [ -z "$stack_name" ] || [ -z "$region" ] || [ -z "$version" ] || [ -z "$infra_base_stack" ]; then
    usage >&2
    echo "[deploy-lakerunner-infra-rds] ERROR: --stack-name, --region, --version, and --infra-base-stack are required" >&2
    exit 2
fi

template_url="$template_base_url/$version/$TEMPLATE_KEY"

set -- --stack-name "$stack_name" --template-url "$template_url" --region "$region"
set -- "$@" --from-stack "$infra_base_stack"
[ -n "$deployer_role_arn" ] && set -- "$@" --deployer-role-arn "$deployer_role_arn"
[ -n "$no_execute" ] && set -- "$@" "$no_execute"

[ -n "$vpc_id" ] && set -- "$@" --param "VpcId=$vpc_id"
[ -n "$private_subnets_csv" ] && set -- "$@" --param "PrivateSubnetsCsv=$private_subnets_csv"
[ -n "$db_engine_version" ] && set -- "$@" --param "DBEngineVersion=$db_engine_version"
[ -n "$db_instance_class" ] && set -- "$@" --param "DBInstanceClass=$db_instance_class"
[ -n "$db_allocated_storage" ] && set -- "$@" --param "DBAllocatedStorage=$db_allocated_storage"

exec "$SCRIPT_DIR/deploy-stack.sh" "$@"
