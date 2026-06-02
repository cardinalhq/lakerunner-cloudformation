#!/usr/bin/env bash
# Jenkins-friendly wrapper around dev-scripts/cleanup-lakerunner.sh.
#
# The underlying driver deploys the cardinal-cleanup stack, runs the privileged
# Fargate task that drains ECS services, deletes the cardinal-lakerunner stack,
# wipes the cardinal-* data layer (S3 / RDS / SQS / secrets / SSM) with
# ownership-tag enforcement, and self-deletes the cleanup stack. This wrapper
# only translates ENV vars -> CLI flags so a Jenkins job can run with a single
# env block.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$SCRIPT_DIR/lib.sh"
LOG_TAG="run-cleanup"

REGION="${REGION:-us-east-1}"
VERSION="${VERSION:?VERSION is required (e.g. v0.0.80)}"

# Required customer-side identifiers for the cleanup task ENI + IAM.
require_env CLUSTER_NAME
require_env PRIVATE_SUBNETS
require_env TASK_SG_ID
require_env CLEANUP_TASK_ROLE_ARN
require_env CLEANUP_EXECUTION_ROLE_ARN
require_env DEPLOYER_ROLE_ARN

LAKERUNNER_STACK_NAME="${LAKERUNNER_STACK_NAME:-cardinal-lakerunner}"
INFRA_STACK_NAME="${INFRA_STACK_NAME:-cardinal-infrastructure}"
CLEANUP_STACK_NAME="${CLEANUP_STACK_NAME:-cardinal-cleanup}"
TEMPLATE_BUCKET="${TEMPLATE_BUCKET:-cardinal-cfn-${REGION}}"
TEMPLATE_BASE_URL="${TEMPLATE_BASE_URL:-https://${TEMPLATE_BUCKET}.s3.${REGION}.amazonaws.com/lakerunner}"

# Safety: cleanup is destructive. Refuse to run unless the operator explicitly
# sets CONFIRM=DELETE in the environment (Jenkins job parameter).
if [ "${CONFIRM:-}" != "DELETE" ]; then
  die 'set CONFIRM=DELETE to confirm. This wipes RDS, S3, SQS, secrets, SSM.'
fi

preflight_aws "$REGION"

WAIT_FLAG=()
if [ "${WAIT_SELF_DELETE:-false}" = "true" ]; then
  WAIT_FLAG+=("--wait-self-delete")
fi

log "delegating to $SCRIPT_DIR/cleanup-lakerunner.sh"
exec "$SCRIPT_DIR/cleanup-lakerunner.sh" \
  --region "$REGION" \
  --version "$VERSION" \
  --cluster-name "$CLUSTER_NAME" \
  --private-subnets "$PRIVATE_SUBNETS" \
  --task-sg-id "$TASK_SG_ID" \
  --cleanup-task-role-arn "$CLEANUP_TASK_ROLE_ARN" \
  --cleanup-execution-role-arn "$CLEANUP_EXECUTION_ROLE_ARN" \
  --deployer-role-arn "$DEPLOYER_ROLE_ARN" \
  --lakerunner-stack-name "$LAKERUNNER_STACK_NAME" \
  --infra-stack-name "$INFRA_STACK_NAME" \
  --cleanup-stack-name "$CLEANUP_STACK_NAME" \
  --template-base-url "$TEMPLATE_BASE_URL" \
  "${WAIT_FLAG[@]}" \
  --yes
