#!/bin/sh
# Jenkins job 4: deploy the cardinal-satellite-services stack (the same-account
# otel collector that performs ingest into the satellite raw bucket/queue).
#
# Upstream:
#   - satellite-infra-base : RawBucketName output -> RawBucketName param.
#   - lakerunner-infra-base : LicenseSecretArn output -> LicenseSecretArn param.
# Both output names match the parameter names, so plain FROM_STACKS pulls wire
# them up.  OTEL_REPLICAS defaults to 1 here (the collector config must change
# before scaling past one replica -- see docs/operations/jenkins-chained-deploy.md).
#
# Thin wrapper over deploy-stack.sh.  Pure environment-variable interface.

set -eu

SCRIPT_DIR=$(cd -- "$(dirname -- "$0")" >/dev/null 2>&1 && pwd)
DEFAULT_TEMPLATE_BASE_URL="https://cardinal-cfn-us-east-1.s3.us-east-1.amazonaws.com/lakerunner"
TEMPLATE_KEY="cardinal-satellite-services.yaml"

usage() {
    cat <<EOF
deploy-satellite-services.sh -- deploy the cardinal-satellite-services stack.

All inputs come from environment variables (no flags).

Required:
  STACK_NAME                  Stack to create/update.
  REGION                      AWS region (never defaulted; must be set explicitly).
  VERSION                     Published template tag.
  SATELLITE_INFRA_BASE_STACK  Upstream satellite-infra-base (RawBucketName).
  INFRA_BASE_STACK            Upstream lakerunner-infra-base (LicenseSecretArn).
  VPC_ID                      VPC for the collector.
  ALB_SUBNETS                 Comma-separated subnets for the collector ALB.
  TASK_SUBNETS                Comma-separated subnets for the collector tasks.
  ECS_CLUSTER_ARN             ECS cluster for the collector.

Optional (template defaults preserved when unset):
  ALB_SCHEME           internet-facing | internal (default internal).
  INGEST_SOURCE_CIDR   Allowed source CIDR for the collector ALB (template default 10.0.0.0/8).
  OTEL_REPLICAS        Collector replica count (default 1; >1 requires a
                       collector config change first).
  TEMPLATE_BASE_URL    Default: $DEFAULT_TEMPLATE_BASE_URL
  DEPLOYER_ROLE_ARN    Passed to create-change-set.
  NO_EXECUTE           Non-empty: change-set only, do not execute.
EOF
}

case "${1:-}" in
    -h|--help) usage; exit 0 ;;
    "") : ;;
    *) echo "[deploy-satellite-services] ERROR: this script takes no arguments; configure it via environment variables" >&2; usage >&2; exit 2 ;;
esac

missing=""
[ -z "${STACK_NAME:-}" ] && missing="$missing STACK_NAME"
[ -z "${REGION:-}" ] && missing="$missing REGION"
[ -z "${VERSION:-}" ] && missing="$missing VERSION"
[ -z "${SATELLITE_INFRA_BASE_STACK:-}" ] && missing="$missing SATELLITE_INFRA_BASE_STACK"
[ -z "${INFRA_BASE_STACK:-}" ] && missing="$missing INFRA_BASE_STACK"
[ -z "${VPC_ID:-}" ] && missing="$missing VPC_ID"
[ -z "${ALB_SUBNETS:-}" ] && missing="$missing ALB_SUBNETS"
[ -z "${TASK_SUBNETS:-}" ] && missing="$missing TASK_SUBNETS"
[ -z "${ECS_CLUSTER_ARN:-}" ] && missing="$missing ECS_CLUSTER_ARN"
if [ -n "$missing" ]; then
    usage >&2
    echo "[deploy-satellite-services] ERROR: missing required: $(echo "$missing" | sed 's/^ //; s/ /, /g')" >&2
    exit 2
fi

template_base_url="${TEMPLATE_BASE_URL:-$DEFAULT_TEMPLATE_BASE_URL}"

# OTEL_REPLICAS defaults to 1 here (single replica; >1 needs a collector config
# change first).  Always passed so the wrapper default, not the template
# default, governs.
otel_replicas="${OTEL_REPLICAS:-1}"

TEMPLATE_URL="$template_base_url/$VERSION/$TEMPLATE_KEY"
FROM_STACKS="$SATELLITE_INFRA_BASE_STACK $INFRA_BASE_STACK"
MAPS=""

params="VpcId=$VPC_ID
AlbSubnetsCsv=$ALB_SUBNETS
TaskSubnetsCsv=$TASK_SUBNETS
EcsClusterArn=$ECS_CLUSTER_ARN
OtelReplicas=$otel_replicas"
[ -n "${ALB_SCHEME:-}" ] && params="$params
AlbScheme=$ALB_SCHEME"
[ -n "${INGEST_SOURCE_CIDR:-}" ] && params="$params
IngestSourceCidr=$INGEST_SOURCE_CIDR"

PARAMS="$params"

export TEMPLATE_URL PARAMS FROM_STACKS MAPS

exec "$SCRIPT_DIR/deploy-stack.sh"
