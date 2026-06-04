#!/bin/sh
# Jenkins job 3: deploy the cardinal-satellite-infra-base stack (one per
# satellite ingest account/region).
#
# No upstream stack pull: a satellite may live in a DIFFERENT account than the
# central lakerunner install, where describe-stacks cannot reach the
# lakerunner-infra-base stack.  The central principal the satellite trusts is
# supplied directly as LAKERUNNER_PRINCIPAL (the lakerunner process role ARN,
# read once out of band) -> LakerunnerPrincipal param.  The satellite then
# trusts that role to assume into the satellite ingest access role.
#
# Self-contained single-file driver: this front-half sets the engine env, then
# falls through into the engine embedded below by scripts-src/build.sh (do not
# edit the generated copy).  Pure environment-variable interface (no flags).

set -eu

DEFAULT_TEMPLATE_BASE_URL="https://cardinal-cfn-us-east-1.s3.us-east-1.amazonaws.com/lakerunner"
TEMPLATE_KEY="cardinal-satellite-infra-base.yaml"
# Baked at publish time (scripts-src/build.sh).  STACK_VERSION defaults to this.
DEFAULT_STACK_VERSION="@@STACK_VERSION@@"

usage() {
    cat <<EOF
deploy-satellite-infra-base.sh -- deploy the cardinal-satellite-infra-base stack.

All inputs come from environment variables (no flags).

Required:
  STACK_NAME          Stack to create/update.
  REGION              AWS region (never defaulted; must be set explicitly).
  LAKERUNNER_PRINCIPAL  ARN of the central lakerunner process role (its
                      ProcessRoleArn output) allowed to assume the satellite
                      access role.  Read once out of band; works cross-account.

Optional (template defaults preserved when unset):
  STACK_VERSION              Published template version to deploy. Default: the
                             version baked into this driver ($DEFAULT_STACK_VERSION).
                             (VERSION is accepted as a legacy alias.)
  EXTERNAL_ID                Optional STS ExternalId for the assume-role trust.
  RAW_BUCKET_NAME            Optional explicit raw ingest bucket name.
  RAW_BUCKET_LIFECYCLE_DAYS  (template default 7).
  CONFIGURE_BUCKET_PUBLIC_ACCESS_BLOCK
                             'true' to set the raw bucket's S3 Block Public
                             Access config (template default 'false': not set).
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
[ -z "${LAKERUNNER_PRINCIPAL:-}" ] && missing="$missing LAKERUNNER_PRINCIPAL"
if [ -n "$missing" ]; then
    usage >&2
    echo "[deploy-satellite-infra-base] ERROR: missing required: $(echo "$missing" | sed 's/^ //; s/ /, /g')" >&2
    exit 2
fi

template_base_url="${TEMPLATE_BASE_URL:-$DEFAULT_TEMPLATE_BASE_URL}"
# STACK_VERSION (preferred) or the legacy VERSION alias, else the baked default.
stack_version="${STACK_VERSION:-${VERSION:-$DEFAULT_STACK_VERSION}}"

TEMPLATE_URL="$template_base_url/$stack_version/$TEMPLATE_KEY"
# No upstream-stack pull (cross-account safe): the central principal arrives
# directly, never mapped from a stack output.
FROM_STACKS=""
MAPS=""

params="LakerunnerPrincipal=$LAKERUNNER_PRINCIPAL
"
[ -n "${EXTERNAL_ID:-}" ] && params="${params}ExternalId=$EXTERNAL_ID
"
[ -n "${RAW_BUCKET_NAME:-}" ] && params="${params}RawBucketName=$RAW_BUCKET_NAME
"
[ -n "${RAW_BUCKET_LIFECYCLE_DAYS:-}" ] && params="${params}RawBucketLifecycleDays=$RAW_BUCKET_LIFECYCLE_DAYS
"
[ -n "${CONFIGURE_BUCKET_PUBLIC_ACCESS_BLOCK:-}" ] && params="${params}ConfigureBucketPublicAccessBlock=$CONFIGURE_BUCKET_PUBLIC_ACCESS_BLOCK
"

PARAMS="$params"

export TEMPLATE_URL PARAMS FROM_STACKS MAPS
