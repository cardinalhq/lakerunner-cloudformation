#!/bin/sh
# Jenkins job 4: deploy the cardinal-satellite-services stack (the same-account
# otel collector that performs ingest into the satellite raw bucket/queue).
#
# Upstream: only the satellite's OWN paired stack (same account/region):
#   - satellite-infra-base : RawBucketName output -> RawBucketName param.
# The output name matches the parameter name, so a plain FROM_STACKS pull wires
# it up.  No pull from the central lakerunner-infra-base stack -- the collector
# needs no license and a satellite may live in a different account.
# OTEL_REPLICAS defaults to 1 here (the collector config must change before
# scaling past one replica -- see docs/operations/jenkins-chained-deploy.md).
#
# Self-contained single-file driver: this front-half sets the engine env, then
# falls through into the engine embedded below by scripts-src/build.sh (do not
# edit the generated copy).  Pure environment-variable interface (no flags).

set -eu

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
  ORGANIZATION_ID             Org UUID this satellite's telemetry is attributed to.
  VPC_ID                      VPC for the collector.
  ALB_SUBNETS                 Comma-separated subnets for the collector ALB.
  TASK_SUBNETS                Comma-separated subnets for the collector tasks.
  ECS_CLUSTER_ARN             ECS cluster for the collector.

Optional (template defaults preserved when unset):
  ALB_SCHEME           internet-facing | internal (default internal).
  INGEST_SOURCE_CIDR   Allowed source CIDR for the collector ALB (template default 10.0.0.0/8).
  OTEL_REPLICAS        Collector replica count (default 1; >1 requires a
                       collector config change first).
  OTEL_IMAGE           Full image URI for the otel collector (e.g. an
                       air-gapped private mirror). Unset: the template's public
                       default. The public image to mirror is listed in
                       satellite-images.txt (see docs/air-gapped-images.md).
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

# Echo the inputs this script can actually see before validating, so a
# "missing required" failure is easy to diagnose.  The usual cause is a value
# set as a plain shell variable but not exported -- this child process then
# never receives it, and it shows as <unset> below.
echo "[deploy-satellite-services] inputs visible to this process:" >&2
for _v in STACK_NAME REGION VERSION SATELLITE_INFRA_BASE_STACK ORGANIZATION_ID \
          VPC_ID ALB_SUBNETS TASK_SUBNETS ECS_CLUSTER_ARN \
          ALB_SCHEME INGEST_SOURCE_CIDR OTEL_REPLICAS OTEL_IMAGE TEMPLATE_BASE_URL \
          DEPLOYER_ROLE_ARN NO_EXECUTE; do
    eval "_val=\${$_v:-}"
    printf '[deploy-satellite-services]   %-27s = %s\n' "$_v" "${_val:-<unset>}" >&2
done

missing=""
[ -z "${STACK_NAME:-}" ] && missing="$missing STACK_NAME"
[ -z "${REGION:-}" ] && missing="$missing REGION"
[ -z "${VERSION:-}" ] && missing="$missing VERSION"
[ -z "${SATELLITE_INFRA_BASE_STACK:-}" ] && missing="$missing SATELLITE_INFRA_BASE_STACK"
[ -z "${ORGANIZATION_ID:-}" ] && missing="$missing ORGANIZATION_ID"
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
FROM_STACKS="$SATELLITE_INFRA_BASE_STACK"
MAPS=""

params="OrganizationId=$ORGANIZATION_ID
VpcId=$VPC_ID
AlbSubnetsCsv=$ALB_SUBNETS
TaskSubnetsCsv=$TASK_SUBNETS
EcsClusterArn=$ECS_CLUSTER_ARN
OtelReplicas=$otel_replicas"
[ -n "${ALB_SCHEME:-}" ] && params="$params
AlbScheme=$ALB_SCHEME"
[ -n "${INGEST_SOURCE_CIDR:-}" ] && params="$params
IngestSourceCidr=$INGEST_SOURCE_CIDR"
# OTEL_IMAGE: full image URI override passed as the literal OtelImage param.
# Unset -> omitted, so the template's public default governs.
[ -n "${OTEL_IMAGE:-}" ] && params="$params
OtelImage=$OTEL_IMAGE"

PARAMS="$params"

export TEMPLATE_URL PARAMS FROM_STACKS MAPS
