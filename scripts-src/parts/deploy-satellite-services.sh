#!/bin/sh
# Stack 4 of the deploy chain: the cardinal-satellite-services stack (the same-account
# otel collector that performs ingest into the satellite raw bucket/queue).
#
# Upstream: only the satellite's OWN paired stack (same account/region):
#   - satellite-infra-base : RawBucketName output -> RawBucketName param.
# The output name matches the parameter name, so a plain FROM_STACKS pull wires
# it up.  No pull from the central lakerunner-infra-base stack -- the collector
# needs no license and a satellite may live in a different account.
# OTEL_REPLICAS defaults to 1 here (the collector config must change before
# scaling past one replica -- see docs/operations/production-deploy.md).
#
# This driver is version-locked: the published template version and the otel
# collector image (repo + pinned tag/digest) are baked in at publish time, so
# the driver + stack are the supported deploy path (no console deploys).  The
# operator supplies only their image registry/prefix and (optionally) a
# different STACK_VERSION.
#
# Self-contained single-file driver: this front-half sets the engine env, then
# falls through into the engine embedded below by scripts-src/build.sh (do not
# edit the generated copy).  Pure environment-variable interface (no flags).

set -eu

DEFAULT_TEMPLATE_BASE_URL="https://cardinal-cfn-us-east-1.s3.us-east-1.amazonaws.com/lakerunner"
TEMPLATE_KEY="cardinal-satellite-services.yaml"
# Baked at publish time (scripts-src/build.sh).  STACK_VERSION defaults to this.
DEFAULT_STACK_VERSION="@@STACK_VERSION@@"
# Baked at publish time: the otel collector's registry-relative path (repo +
# pinned tag/digest).  Only the registry prefix is operator-supplied.
OTEL_IMAGE_SUFFIX="@@OTEL_IMAGE_SUFFIX@@"
DEFAULT_IMAGE_REGISTRY="public.ecr.aws"

# Resolve optional execution-role managed-policy ARNs into the global
# EXEC_EXTRA_ARNS (a CSV, possibly empty), from two operator inputs:
#   EXECUTION_ROLE_POLICY_ARNS         ready-made managed-policy ARNs (CSV) -- the
#                                      "proper ops" path; passed through as-is.
#   EXECUTION_ROLE_POLICY_JSON[_FILE]  a pasted IAM policy document (multi-line
#                                      ok).  CFN cannot attach a string policy
#                                      (no Lambda in this product), so the driver
#                                      flattens it (jq -c) and creates/updates a
#                                      customer-managed policy named
#                                      <STACK_NAME>-exec-extra, then attaches its
#                                      ARN via the stack's ManagedPolicyArns.
# Called directly (not in $()) so a validation error can exit the whole script.
resolve_exec_role_policy_arns() {
    EXEC_EXTRA_ARNS="${EXECUTION_ROLE_POLICY_ARNS:-}"
    _erp_json=""
    if [ -n "${EXECUTION_ROLE_POLICY_JSON:-}" ]; then
        _erp_json="$EXECUTION_ROLE_POLICY_JSON"
    elif [ -n "${EXECUTION_ROLE_POLICY_JSON_FILE:-}" ]; then
        [ -r "$EXECUTION_ROLE_POLICY_JSON_FILE" ] || { echo "[deploy-satellite-services] ERROR: cannot read EXECUTION_ROLE_POLICY_JSON_FILE: $EXECUTION_ROLE_POLICY_JSON_FILE" >&2; exit 2; }
        _erp_json=$(cat "$EXECUTION_ROLE_POLICY_JSON_FILE")
    fi
    [ -n "$_erp_json" ] || return 0

    command -v jq >/dev/null 2>&1 || { echo "[deploy-satellite-services] ERROR: jq is required for EXECUTION_ROLE_POLICY_JSON" >&2; exit 2; }
    _erp_doc=$(printf '%s' "$_erp_json" | jq -c . 2>/dev/null) || { echo "[deploy-satellite-services] ERROR: EXECUTION_ROLE_POLICY_JSON is not valid JSON" >&2; exit 2; }
    _erp_name="${STACK_NAME}-exec-extra"
    _erp_acct=$(aws sts get-caller-identity --query Account --output text)
    _erp_arn="arn:aws:iam::${_erp_acct}:policy/${_erp_name}"
    if _erp_def=$(aws iam get-policy --policy-arn "$_erp_arn" --query 'Policy.DefaultVersionId' --output text 2>/dev/null); then
        # IAM caps a policy at 5 versions; drop the non-default ones before adding.
        for _erp_v in $(aws iam list-policy-versions --policy-arn "$_erp_arn" --query 'Versions[].VersionId' --output text); do
            [ "$_erp_v" = "$_erp_def" ] && continue
            aws iam delete-policy-version --policy-arn "$_erp_arn" --version-id "$_erp_v" >/dev/null 2>&1 || true
        done
        aws iam create-policy-version --policy-arn "$_erp_arn" --policy-document "$_erp_doc" --set-as-default >/dev/null
        echo "[deploy-satellite-services] updated execution-role managed policy: $_erp_arn" >&2
    else
        aws iam create-policy --policy-name "$_erp_name" --policy-document "$_erp_doc" --description "Cardinal execution-role extra policy for $STACK_NAME" >/dev/null
        echo "[deploy-satellite-services] created execution-role managed policy: $_erp_arn" >&2
    fi
    if [ -n "$EXEC_EXTRA_ARNS" ]; then
        EXEC_EXTRA_ARNS="$EXEC_EXTRA_ARNS,$_erp_arn"
    else
        EXEC_EXTRA_ARNS="$_erp_arn"
    fi
}

usage() {
    cat <<EOF
deploy-satellite-services.sh -- deploy the cardinal-satellite-services stack.

All inputs come from environment variables (no flags).

Required:
  STACK_NAME                  Stack to create/update.
  REGION                      AWS region (never defaulted; must be set explicitly).
  SATELLITE_INFRA_BASE_STACK  Upstream satellite-infra-base (RawBucketName).
  ORGANIZATION_ID             Org UUID this satellite's telemetry is attributed to.
  VPC_ID                      VPC for the collector.
  ALB_SUBNETS                 Comma-separated subnets for the collector ALB.
  TASK_SUBNETS                Comma-separated subnets for the collector tasks.
  ECS_CLUSTER_ARN             ECS cluster for the collector.

Optional (template defaults preserved when unset):
  STACK_VERSION        Published template version to deploy. Default: the
                       version baked into this driver ($DEFAULT_STACK_VERSION).
                       (VERSION is accepted as a legacy alias.)
  IMAGE_REGISTRY       Registry (and optional namespace/prefix) the collector
                       image is pulled from -- e.g. an ECR pull-through cache
                       root like <acct>.dkr.ecr.<region>.amazonaws.com/aws-public.
                       The image path and pinned tag/digest are locked into this
                       driver; only this prefix is operator-supplied.
                       Default: $DEFAULT_IMAGE_REGISTRY (the public registry).
  ALB_SCHEME           internet-facing | internal (default internal).
  INGEST_SOURCE_CIDR   Allowed source CIDR for the collector ALB (template default 10.0.0.0/8).
  OTEL_REPLICAS        Collector replica count (default 1; >1 requires a
                       collector config change first).
  NAME_SUFFIX          Optional suffix appended to the stack's fixed physical
                       names (the /cardinal/otel-grpc log group) so a
                       second collector stack (e.g. dev + prod) can share an
                       account/region.  Max 16 chars, lowercase alphanumeric
                       and hyphens.  Leave unset on existing stacks.
  EXECUTION_ROLE_POLICY_ARNS   Comma-separated managed-policy ARNs to attach to
                       the collector execution role (e.g. ECR pull-through
                       import, cross-account ECR, KMS decrypt).
  EXECUTION_ROLE_POLICY_JSON   A pasted IAM policy document (multi-line ok). The
                       driver flattens it and creates/updates a customer managed
                       policy <STACK_NAME>-exec-extra, then attaches it. Requires
                       jq + iam:CreatePolicy/CreatePolicyVersion. Or use *_FILE.
  EXECUTION_ROLE_POLICY_JSON_FILE  Path fallback for EXECUTION_ROLE_POLICY_JSON.
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

# STACK_VERSION (preferred) or the legacy VERSION alias, else the baked default.
stack_version="${STACK_VERSION:-${VERSION:-$DEFAULT_STACK_VERSION}}"
# IMAGE_REGISTRY prefix + the baked, locked image path -> the literal OtelImage.
image_registry="${IMAGE_REGISTRY:-$DEFAULT_IMAGE_REGISTRY}"
otel_image="$image_registry/$OTEL_IMAGE_SUFFIX"

# Echo the inputs this script can actually see before validating, so a
# "missing required" failure is easy to diagnose.  The usual cause is a value
# set as a plain shell variable but not exported -- this child process then
# never receives it, and it shows as <unset> below.
echo "[deploy-satellite-services] inputs visible to this process:" >&2
for _v in STACK_NAME REGION STACK_VERSION VERSION SATELLITE_INFRA_BASE_STACK \
          ORGANIZATION_ID VPC_ID ALB_SUBNETS TASK_SUBNETS ECS_CLUSTER_ARN \
          ALB_SCHEME INGEST_SOURCE_CIDR OTEL_REPLICAS NAME_SUFFIX \
          IMAGE_REGISTRY TEMPLATE_BASE_URL DEPLOYER_ROLE_ARN NO_EXECUTE; do
    eval "_val=\${$_v:-}"
    printf '[deploy-satellite-services]   %-27s = %s\n' "$_v" "${_val:-<unset>}" >&2
done
echo "[deploy-satellite-services]   resolved STACK_VERSION       = $stack_version" >&2
echo "[deploy-satellite-services]   resolved OtelImage           = $otel_image" >&2

missing=""
[ -z "${STACK_NAME:-}" ] && missing="$missing STACK_NAME"
[ -z "${REGION:-}" ] && missing="$missing REGION"
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

TEMPLATE_URL="$template_base_url/$stack_version/$TEMPLATE_KEY"
FROM_STACKS="$SATELLITE_INFRA_BASE_STACK"
MAPS=""

params="OrganizationId=$ORGANIZATION_ID
VpcId=$VPC_ID
AlbSubnetsCsv=$ALB_SUBNETS
TaskSubnetsCsv=$TASK_SUBNETS
EcsClusterArn=$ECS_CLUSTER_ARN
OtelReplicas=$otel_replicas
OtelImage=$otel_image"
[ -n "${ALB_SCHEME:-}" ] && params="$params
AlbScheme=$ALB_SCHEME"
[ -n "${INGEST_SOURCE_CIDR:-}" ] && params="$params
IngestSourceCidr=$INGEST_SOURCE_CIDR"
[ -n "${NAME_SUFFIX:-}" ] && params="$params
NameSuffix=$NAME_SUFFIX"

# Optional execution-role extra managed policies (pasted JSON and/or ARNs).
EXEC_EXTRA_ARNS=""
resolve_exec_role_policy_arns
[ -n "$EXEC_EXTRA_ARNS" ] && params="$params
ExecutionRoleExtraPolicyArns=$EXEC_EXTRA_ARNS"

PARAMS="$params"

export TEMPLATE_URL PARAMS FROM_STACKS MAPS
