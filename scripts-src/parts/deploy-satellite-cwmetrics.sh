#!/bin/sh
# Optional add-on to a satellite: the cardinal-satellite-cwmetrics stack, which
# streams this account's CloudWatch metrics into the satellite's existing raw
# bucket under cwmetrics-raw/.
#
# Upstream: only the satellite's OWN paired stack (same account/region):
#   - satellite-infra-base : RawBucketName output -> RawBucketName param.
# The output name matches the parameter name, so a plain FROM_STACKS pull wires
# it up.  Nothing is pulled from the central lakerunner install -- this stack
# creates no queue and no cross-account role, so it needs no central identity.
#
# Deploy after satellite-infra-base.  Deploying it alongside
# satellite-services is fine and expected: the two write to different prefixes
# of the same bucket and share its queue and access role.
#
# Self-contained single-file driver: this front-half sets the engine env, then
# falls through into the engine embedded below by scripts-src/build.sh (do not
# edit the generated copy).  Pure environment-variable interface (no flags).

set -eu

DEFAULT_TEMPLATE_BASE_URL="https://cardinal-cfn-us-east-1.s3.us-east-1.amazonaws.com/lakerunner"
TEMPLATE_KEY="cardinal-satellite-cwmetrics.yaml"
# Baked at publish time (scripts-src/build.sh).  STACK_VERSION defaults to this.
DEFAULT_STACK_VERSION="@@STACK_VERSION@@"

usage() {
    cat <<EOF
deploy-satellite-cwmetrics.sh -- deploy the cardinal-satellite-cwmetrics stack.

All inputs come from environment variables (no flags).

Required:
  STACK_NAME                  Stack to create/update.
  REGION                      AWS region (never defaulted; must be set explicitly).
  SATELLITE_INFRA_BASE_STACK  Upstream satellite-infra-base (RawBucketName).
  ORGANIZATION_ID             Org UUID these metrics are attributed to.  Use the
                              same value as this account's satellite-services
                              stack.

Optional (template defaults preserved when unset):
  STACK_VERSION          Published template version to deploy. Default: the
                         version baked into this driver ($DEFAULT_STACK_VERSION).
                         (VERSION is accepted as a legacy alias.)
  EXCLUDE_NAMESPACE      One CloudWatch namespace to exclude (template default
                         AWS/Usage).  Empty string streams every namespace.
  FIREHOSE_BUFFER_SECONDS  Firehose buffering interval (template default 300).
  FIREHOSE_BUFFER_SIZE_MB  Firehose buffer size in MB (template default 64).
  NAME_SUFFIX            Optional suffix appended to the stack's fixed physical
                         names (the Firehose log group) so a second cwmetrics
                         stack can share an account.  Max 16 chars, lowercase
                         alphanumeric and hyphens.  Leave unset on existing
                         stacks: their names stay exactly as deployed.
  TEMPLATE_BASE_URL      Default: $DEFAULT_TEMPLATE_BASE_URL
  DEPLOYER_ROLE_ARN      Passed to create-change-set.
  NO_EXECUTE             Non-empty: change-set only, do not execute.
EOF
}

case "${1:-}" in
    -h|--help) usage; exit 0 ;;
    "") : ;;
    *) echo "[deploy-satellite-cwmetrics] ERROR: this script takes no arguments; configure it via environment variables" >&2; usage >&2; exit 2 ;;
esac

missing=""
[ -z "${STACK_NAME:-}" ] && missing="$missing STACK_NAME"
[ -z "${REGION:-}" ] && missing="$missing REGION"
[ -z "${SATELLITE_INFRA_BASE_STACK:-}" ] && missing="$missing SATELLITE_INFRA_BASE_STACK"
[ -z "${ORGANIZATION_ID:-}" ] && missing="$missing ORGANIZATION_ID"
if [ -n "$missing" ]; then
    usage >&2
    echo "[deploy-satellite-cwmetrics] ERROR: missing required: $(echo "$missing" | sed 's/^ //; s/ /, /g')" >&2
    exit 2
fi

template_base_url="${TEMPLATE_BASE_URL:-$DEFAULT_TEMPLATE_BASE_URL}"
# STACK_VERSION (preferred) or the legacy VERSION alias, else the baked default.
stack_version="${STACK_VERSION:-${VERSION:-$DEFAULT_STACK_VERSION}}"

TEMPLATE_URL="$template_base_url/$stack_version/$TEMPLATE_KEY"
FROM_STACKS="$SATELLITE_INFRA_BASE_STACK"
MAPS=""

params="OrganizationId=$ORGANIZATION_ID"
# EXCLUDE_NAMESPACE is checked with ${x+set} rather than -n: an explicitly
# empty value is meaningful (stream every namespace).
[ -n "${EXCLUDE_NAMESPACE+set}" ] && params="$params
ExcludeNamespace=$EXCLUDE_NAMESPACE"
[ -n "${FIREHOSE_BUFFER_SECONDS:-}" ] && params="$params
FirehoseBufferSeconds=$FIREHOSE_BUFFER_SECONDS"
[ -n "${FIREHOSE_BUFFER_SIZE_MB:-}" ] && params="$params
FirehoseBufferSizeMB=$FIREHOSE_BUFFER_SIZE_MB"
[ -n "${NAME_SUFFIX:-}" ] && params="$params
NameSuffix=$NAME_SUFFIX"

PARAMS="$params"

export TEMPLATE_URL PARAMS FROM_STACKS MAPS
