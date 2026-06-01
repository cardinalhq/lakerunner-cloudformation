#!/bin/sh
# Jenkins job 3: deploy the cardinal-satellite-infra-base stack (one per
# satellite ingest account/region).
#
# Upstream: the lakerunner-infra-base stack.  This stack's LakerunnerPrincipal
# parameter does NOT match any infra-base output name, so it is wired via an
# explicit MAPS LakerunnerPrincipal=ProcessRoleArn: the satellite trusts the
# lakerunner process role to assume into the satellite ingest access role.
#
# Thin wrapper over deploy-stack.sh.  Pure environment-variable interface.

set -eu

SCRIPT_DIR=$(cd -- "$(dirname -- "$0")" >/dev/null 2>&1 && pwd)
DEFAULT_TEMPLATE_BASE_URL="https://cardinal-cfn.s3.us-east-2.amazonaws.com/lakerunner"
TEMPLATE_KEY="cardinal-satellite-infra-base.yaml"

usage() {
    cat <<EOF
deploy-satellite-infra-base.sh -- deploy the cardinal-satellite-infra-base stack.

All inputs come from environment variables (no flags).

Required:
  STACK_NAME         Stack to create/update.
  REGION             AWS region (never defaulted; must be set explicitly).
  VERSION            Published template tag.
  INFRA_BASE_STACK   Upstream lakerunner-infra-base stack.  Its ProcessRoleArn
                     output is mapped to this stack's LakerunnerPrincipal param.

Optional (template defaults preserved when unset):
  EXTERNAL_ID                Optional STS ExternalId for the assume-role trust.
  RAW_BUCKET_NAME            Optional explicit raw ingest bucket name.
  RAW_BUCKET_LIFECYCLE_DAYS  (template default 7).
  TEMPLATE_BASE_URL          Default: $DEFAULT_TEMPLATE_BASE_URL
  DEPLOYER_ROLE_ARN          Passed to create-change-set.
  NO_EXECUTE                 Non-empty: change-set only, do not execute.
EOF
}

case "${1:-}" in
    -h|--help) usage; exit 0 ;;
    "") : ;;
    *) echo "[deploy-satellite-infra-base] ERROR: this script takes no arguments; configure it via environment variables" >&2; usage >&2; exit 2 ;;
esac

missing=""
[ -z "${STACK_NAME:-}" ] && missing="$missing STACK_NAME"
[ -z "${REGION:-}" ] && missing="$missing REGION"
[ -z "${VERSION:-}" ] && missing="$missing VERSION"
[ -z "${INFRA_BASE_STACK:-}" ] && missing="$missing INFRA_BASE_STACK"
if [ -n "$missing" ]; then
    usage >&2
    echo "[deploy-satellite-infra-base] ERROR: missing required: $(echo "$missing" | sed 's/^ //; s/ /, /g')" >&2
    exit 2
fi

template_base_url="${TEMPLATE_BASE_URL:-$DEFAULT_TEMPLATE_BASE_URL}"

TEMPLATE_URL="$template_base_url/$VERSION/$TEMPLATE_KEY"
FROM_STACKS="$INFRA_BASE_STACK"
MAPS="LakerunnerPrincipal=ProcessRoleArn"

params=""
[ -n "${EXTERNAL_ID:-}" ] && params="${params}ExternalId=$EXTERNAL_ID
"
[ -n "${RAW_BUCKET_NAME:-}" ] && params="${params}RawBucketName=$RAW_BUCKET_NAME
"
[ -n "${RAW_BUCKET_LIFECYCLE_DAYS:-}" ] && params="${params}RawBucketLifecycleDays=$RAW_BUCKET_LIFECYCLE_DAYS
"

PARAMS="$params"

export TEMPLATE_URL PARAMS FROM_STACKS MAPS

exec "$SCRIPT_DIR/deploy-stack.sh"
