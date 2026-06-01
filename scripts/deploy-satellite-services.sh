#!/bin/sh
# Jenkins job 4: deploy the cardinal-satellite-services stack (the same-account
# otel collector that performs ingest into the satellite raw bucket/queue).
#
# Upstream:
#   - satellite-infra-base : RawBucketName output -> RawBucketName param.
#   - lakerunner-infra-base : LicenseSecretArn output -> LicenseSecretArn param.
# Both output names match the parameter names, so plain --from-stack pulls wire
# them up.  OtelReplicas defaults to 1 here (the collector config must change
# before scaling past one replica -- see docs/operations/jenkins-chained-deploy.md).
#
# Thin wrapper over deploy-stack.sh.

set -eu

SCRIPT_DIR=$(cd -- "$(dirname -- "$0")" >/dev/null 2>&1 && pwd)
DEFAULT_TEMPLATE_BASE_URL="https://cardinal-cfn.s3.us-east-2.amazonaws.com/lakerunner"
TEMPLATE_KEY="cardinal-satellite-services.yaml"

stack_name=""
region=""
version=""
template_base_url="$DEFAULT_TEMPLATE_BASE_URL"
deployer_role_arn=""
no_execute=""

satellite_infra_base_stack=""
infra_base_stack=""
vpc_id=""
alb_subnets_csv=""
task_subnets_csv=""
ecs_cluster_arn=""
alb_scheme=""
ingest_source_cidr=""
otel_replicas="1"   # default: single replica; >1 needs a collector config change

usage() {
    cat <<'EOF'
Usage: deploy-satellite-services.sh --stack-name NAME --region REGION --version VER \
           --satellite-infra-base-stack NAME --infra-base-stack NAME [options]

Required:
  --stack-name NAME                  Stack to create/update.
  --region REGION                    AWS region.
  --version VERSION                  Published template tag.
  --satellite-infra-base-stack NAME  Upstream (RawBucketName).
  --infra-base-stack NAME            Upstream lakerunner-infra-base (LicenseSecretArn).
  --vpc-id VPC
  --alb-subnets-csv CSV              Subnets for the collector ALB.
  --task-subnets-csv CSV             Subnets for the collector tasks.
  --ecs-cluster-arn ARN              ECS cluster for the collector.

Add-ins (template defaults are fine if omitted):
  --alb-scheme SCHEME                internet-facing | internal.
  --ingest-source-cidr CIDR          Allowed source CIDR for the collector ALB.
  --otel-replicas N                  Collector replica count (default 1; >1
                                     requires a collector config change first).

Common:
  --template-base-url URL            Default: https://cardinal-cfn.s3.us-east-2.amazonaws.com/lakerunner
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
        --satellite-infra-base-stack) satellite_infra_base_stack="$2"; shift 2 ;;
        --infra-base-stack) infra_base_stack="$2"; shift 2 ;;
        --vpc-id) vpc_id="$2"; shift 2 ;;
        --alb-subnets-csv) alb_subnets_csv="$2"; shift 2 ;;
        --task-subnets-csv) task_subnets_csv="$2"; shift 2 ;;
        --ecs-cluster-arn) ecs_cluster_arn="$2"; shift 2 ;;
        --alb-scheme) alb_scheme="$2"; shift 2 ;;
        --ingest-source-cidr) ingest_source_cidr="$2"; shift 2 ;;
        --otel-replicas) otel_replicas="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "[deploy-satellite-services] ERROR: unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

if [ -z "$stack_name" ] || [ -z "$region" ] || [ -z "$version" ] \
    || [ -z "$satellite_infra_base_stack" ] || [ -z "$infra_base_stack" ]; then
    usage >&2
    echo "[deploy-satellite-services] ERROR: --stack-name, --region, --version, --satellite-infra-base-stack, and --infra-base-stack are required" >&2
    exit 2
fi

template_url="$template_base_url/$version/$TEMPLATE_KEY"

set -- --stack-name "$stack_name" --template-url "$template_url" --region "$region"
set -- "$@" --from-stack "$satellite_infra_base_stack"
set -- "$@" --from-stack "$infra_base_stack"
[ -n "$deployer_role_arn" ] && set -- "$@" --deployer-role-arn "$deployer_role_arn"
[ -n "$no_execute" ] && set -- "$@" "$no_execute"

[ -n "$vpc_id" ] && set -- "$@" --param "VpcId=$vpc_id"
[ -n "$alb_subnets_csv" ] && set -- "$@" --param "AlbSubnetsCsv=$alb_subnets_csv"
[ -n "$task_subnets_csv" ] && set -- "$@" --param "TaskSubnetsCsv=$task_subnets_csv"
[ -n "$ecs_cluster_arn" ] && set -- "$@" --param "EcsClusterArn=$ecs_cluster_arn"
[ -n "$alb_scheme" ] && set -- "$@" --param "AlbScheme=$alb_scheme"
[ -n "$ingest_source_cidr" ] && set -- "$@" --param "IngestSourceCidr=$ingest_source_cidr"
[ -n "$otel_replicas" ] && set -- "$@" --param "OtelReplicas=$otel_replicas"

exec "$SCRIPT_DIR/deploy-stack.sh" "$@"
