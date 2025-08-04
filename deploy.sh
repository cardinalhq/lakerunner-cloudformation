#!/usr/bin/env bash

set -euo pipefail

if [[ -z "${AWS_ACCOUNT:-}" || -z "${AWS_REGION:-}" ]]; then
  echo "ERROR: AWS_ACCOUNT and AWS_REGION must be set" >&2
  exit 1
fi

cdk bootstrap aws://$AWS_ACCOUNT/$AWS_REGION

cdk deploy CommonInfra MigrationStack --require-approval never

CLUSTER_ARN=$(aws cloudformation list-exports \
  --query "Exports[?Name=='CommonInfraClusterArn'].Value" --output text)
if [[ -z "$CLUSTER_ARN" ]]; then
  echo "ERROR: could not find CommonInfraClusterArn export" >&2
  exit 1
fi

TASK_DEF=$(aws ecs describe-task-definition \
  --task-definition lakerunner-migration \
  --query 'taskDefinition.taskDefinitionArn' \
  --output text)

SUBNETS=$(aws cloudformation list-exports \
  --query "Exports[?Name=='CommonInfraPrivateSubnetIds'].Value" \
  --output text)

echo "Using private subnets: $SUBNETS"

SG=$(aws cloudformation list-exports \
  --query "Exports[?Name=='CommonInfraTaskSecurityGroupId'].Value" \
  --output text)

if [[ -z "$SG" ]]; then
  echo "ERROR: could not find CommonInfraTaskSecurityGroupId export" >&2
  exit 1
fi

echo "Using task SG: $SG"

echo "Running DB migrations..."
TASK_ARN=$(aws ecs run-task \
  --cluster "$CLUSTER_ARN" \
  --launch-type FARGATE \
  --task-definition "$TASK_DEF" \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNETS],securityGroups=[$SG]}" \
  --query 'tasks[0].taskArn' --output text)

aws ecs wait tasks-stopped \
  --cluster "$CLUSTER_ARN" \
  --tasks "$TASK_ARN"

echo "Migration complete."

cdk deploy --all --require-approval never --concurrency 10
