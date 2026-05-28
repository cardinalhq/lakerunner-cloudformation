#!/usr/bin/env bash
# Deploy the lrdev-vpc stack -- internal test scaffolding that stands up
# customer-equivalent networking. NOT a customer-facing artifact. Use this in
# the lakerunner test account; production installs always bring their own VPC.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$SCRIPT_DIR/lib.sh"
LOG_TAG="lrdev-vpc"

REGION="${REGION:-us-east-1}"
VERSION="${VERSION:?VERSION is required (e.g. v0.0.80)}"
STACK_NAME="${STACK_NAME:-lrdev-vpc}"
TEMPLATE_BUCKET="${TEMPLATE_BUCKET:-cardinal-cfn-${REGION}}"

ENVIRONMENT_NAME="${ENVIRONMENT_NAME:-lrdev}"
VPC_CIDR="${VPC_CIDR:-10.0.0.0/16}"
CREATE_NAT_GATEWAY="${CREATE_NAT_GATEWAY:-Yes}"
CREATE_INTERFACE_ENDPOINTS="${CREATE_INTERFACE_ENDPOINTS:-No}"

TEMPLATE_URL="https://${TEMPLATE_BUCKET}.s3.${REGION}.amazonaws.com/lakerunner/${VERSION}/lrdev-vpc.yaml"

preflight_aws "$REGION"
verify_template_published "$TEMPLATE_URL"

PARAMS_FILE="$(mktemp "${TMPDIR:-/tmp}/lrdev-vpc-params.XXXXXX.json")"
trap 'rm -f "$PARAMS_FILE"' EXIT

write_params_file "$PARAMS_FILE" \
  "EnvironmentName=$ENVIRONMENT_NAME" \
  "VpcCidr=$VPC_CIDR" \
  "CreateNatGateway=$CREATE_NAT_GATEWAY" \
  "CreateInterfaceEndpoints=$CREATE_INTERFACE_ENDPOINTS"

deploy_stack "$REGION" "$STACK_NAME" "$TEMPLATE_URL" "$PARAMS_FILE" ""

log "outputs:"
dump_outputs_as_env "$REGION" "$STACK_NAME"
