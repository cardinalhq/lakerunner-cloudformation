#!/usr/bin/env bash
# Deploy the cardinal-infrastructure stack. Creates RDS, S3 ingest bucket,
# SQS queue, the cardinal-* Secrets Manager secrets, and the /cardinal/* SSM
# parameters.
#
# The data layer carries DeletionPolicy: Retain / Snapshot; deleting this
# stack will leave behind the RDS snapshot, the S3 bucket, and the
# license/admin secrets. Use deploy-scripts/run-cleanup.sh to actually wipe.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$SCRIPT_DIR/lib.sh"
LOG_TAG="cardinal-infra"

REGION="${REGION:-us-east-1}"
VERSION="${VERSION:?VERSION is required (e.g. v0.0.80)}"
STACK_NAME="${STACK_NAME:-cardinal-infrastructure}"
TEMPLATE_BUCKET="${TEMPLATE_BUCKET:-cardinal-cfn-${REGION}}"

# Required networking (no defaults -- customer-specific).
require_env VPC_ID
require_env PRIVATE_SUBNETS

# Required customer-specific values.
require_env LICENSE_DATA

# Optional sizing knobs (have sane defaults).
DB_ENGINE_VERSION="${DB_ENGINE_VERSION:-18.3}"
DB_INSTANCE_CLASS="${DB_INSTANCE_CLASS:-db.t3.medium}"
DB_ALLOCATED_STORAGE="${DB_ALLOCATED_STORAGE:-100}"
INGEST_BUCKET_LIFECYCLE_DAYS="${INGEST_BUCKET_LIFECYCLE_DAYS:-7}"
ORGANIZATION_ID="${ORGANIZATION_ID:-12340000-0000-4000-8000-000000000000}"

TEMPLATE_URL="https://${TEMPLATE_BUCKET}.s3.${REGION}.amazonaws.com/lakerunner/${VERSION}/cardinal-infrastructure.yaml"

preflight_aws "$REGION"
verify_template_published "$TEMPLATE_URL"

PARAMS_FILE="$(mktemp "${TMPDIR:-/tmp}/cardinal-infra-params.XXXXXX.json")"
trap 'rm -f "$PARAMS_FILE"' EXIT

write_params_file "$PARAMS_FILE" \
  "VpcId=$VPC_ID" \
  "PrivateSubnets=$PRIVATE_SUBNETS" \
  "LicenseData=$LICENSE_DATA" \
  "DBEngineVersion=$DB_ENGINE_VERSION" \
  "DBInstanceClass=$DB_INSTANCE_CLASS" \
  "DBAllocatedStorage=$DB_ALLOCATED_STORAGE" \
  "IngestBucketLifecycleDays=$INGEST_BUCKET_LIFECYCLE_DAYS" \
  "OrganizationId=$ORGANIZATION_ID"

deploy_stack "$REGION" "$STACK_NAME" "$TEMPLATE_URL" "$PARAMS_FILE" "CAPABILITY_NAMED_IAM"

log "outputs:"
dump_outputs_as_env "$REGION" "$STACK_NAME"
