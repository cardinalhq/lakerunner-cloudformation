#!/bin/sh
# Stack 2 of the deploy chain: the cardinal-lakerunner-infra-rds stack.
#
# Upstream: the lakerunner-infra-base stack supplies the per-service security
# group ids (Control/Maestro/Migration/Process/Query SecurityGroupId outputs),
# which this stack uses to authorize DB ingress.  Those output names match this
# template's parameter names, so a plain FROM_STACKS pull wires them up.
#
# Self-contained single-file driver: this front-half sets the engine env, then
# falls through into the engine embedded below by scripts-src/build.sh (do not
# edit the generated copy).  Pure environment-variable interface (no flags).

set -eu

DEFAULT_TEMPLATE_BASE_URL="https://cardinal-cfn-us-east-1.s3.us-east-1.amazonaws.com/lakerunner"
TEMPLATE_KEY="cardinal-lakerunner-infra-rds.yaml"
# Baked at publish time (scripts-src/build.sh).  STACK_VERSION defaults to this.
DEFAULT_STACK_VERSION="@@STACK_VERSION@@"

usage() {
    cat <<EOF
deploy-lakerunner-infra-rds.sh -- deploy the cardinal-lakerunner-infra-rds stack.

All inputs come from environment variables (no flags).

Required:
  STACK_NAME         Stack to create/update.
  REGION             AWS region (never defaulted; must be set explicitly).
  INFRA_BASE_STACK   Upstream lakerunner-infra-base stack (supplies the service
                     security-group ids via FROM_STACKS).
  VPC_ID             VPC for the DB.
  PRIVATE_SUBNETS    Comma-separated private subnet ids for the DB.

Optional (template defaults preserved when unset):
  STACK_VERSION         Published template version to deploy. Default: the
                        version baked into this driver ($DEFAULT_STACK_VERSION).
                        (VERSION is accepted as a legacy alias.)
  DB_ENGINE_VERSION     (template default 18.4).
  DB_INSTANCE_CLASS     (template default db.r7g.large).
  DB_ALLOCATED_STORAGE  (template default 100).
  TEMPLATE_BASE_URL     Default: $DEFAULT_TEMPLATE_BASE_URL
  DEPLOYER_ROLE_ARN     Passed to create-change-set.
  NO_EXECUTE            Non-empty: change-set only, do not execute.
EOF
}

case "${1:-}" in
    -h|--help) usage; exit 0 ;;
    "") : ;;
    *) echo "[deploy-lakerunner-infra-rds] ERROR: this script takes no arguments; configure it via environment variables" >&2; usage >&2; exit 2 ;;
esac

missing=""
[ -z "${STACK_NAME:-}" ] && missing="$missing STACK_NAME"
[ -z "${REGION:-}" ] && missing="$missing REGION"
[ -z "${INFRA_BASE_STACK:-}" ] && missing="$missing INFRA_BASE_STACK"
[ -z "${VPC_ID:-}" ] && missing="$missing VPC_ID"
[ -z "${PRIVATE_SUBNETS:-}" ] && missing="$missing PRIVATE_SUBNETS"
if [ -n "$missing" ]; then
    usage >&2
    echo "[deploy-lakerunner-infra-rds] ERROR: missing required: $(echo "$missing" | sed 's/^ //; s/ /, /g')" >&2
    exit 2
fi

template_base_url="${TEMPLATE_BASE_URL:-$DEFAULT_TEMPLATE_BASE_URL}"
# STACK_VERSION (preferred) or the legacy VERSION alias, else the baked default.
stack_version="${STACK_VERSION:-${VERSION:-$DEFAULT_STACK_VERSION}}"

TEMPLATE_URL="$template_base_url/$stack_version/$TEMPLATE_KEY"
FROM_STACKS="$INFRA_BASE_STACK"
MAPS=""

params="VpcId=$VPC_ID
PrivateSubnetsCsv=$PRIVATE_SUBNETS"
[ -n "${DB_ENGINE_VERSION:-}" ] && params="$params
DBEngineVersion=$DB_ENGINE_VERSION"
[ -n "${DB_INSTANCE_CLASS:-}" ] && params="$params
DBInstanceClass=$DB_INSTANCE_CLASS"
[ -n "${DB_ALLOCATED_STORAGE:-}" ] && params="$params
DBAllocatedStorage=$DB_ALLOCATED_STORAGE"

PARAMS="$params"

export TEMPLATE_URL PARAMS FROM_STACKS MAPS
