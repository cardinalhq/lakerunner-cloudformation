#!/usr/bin/env bash
# Deploy the lrdev-baseinfra stack -- internal test scaffolding that stands up
# a Fargate-capable ECS cluster (customer-equivalent). NOT a customer-facing
# artifact. Production installs always bring their own ECS cluster.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$SCRIPT_DIR/lib.sh"
LOG_TAG="lrdev-baseinfra"

REGION="${REGION:-us-east-1}"
VERSION="${VERSION:?VERSION is required (e.g. v0.0.80)}"
STACK_NAME="${STACK_NAME:-lrdev-baseinfra}"
TEMPLATE_BUCKET="${TEMPLATE_BUCKET:-cardinal-cfn-${REGION}}"

ENVIRONMENT_NAME="${ENVIRONMENT_NAME:-lrdev}"

TEMPLATE_URL="https://${TEMPLATE_BUCKET}.s3.${REGION}.amazonaws.com/lakerunner/${VERSION}/lrdev-baseinfra.yaml"

preflight_aws "$REGION"
verify_template_published "$TEMPLATE_URL"

PARAMS_FILE="$(mktemp "${TMPDIR:-/tmp}/lrdev-baseinfra-params.XXXXXX.json")"
trap 'rm -f "$PARAMS_FILE"' EXIT

write_params_file "$PARAMS_FILE" "EnvironmentName=$ENVIRONMENT_NAME"

deploy_stack "$REGION" "$STACK_NAME" "$TEMPLATE_URL" "$PARAMS_FILE" ""

log "outputs:"
dump_outputs_as_env "$REGION" "$STACK_NAME"
