#!/bin/sh
# Enable Bedrock model access by accepting the Marketplace agreement.
# Run this once per model per AWS account/region before deploying.
#
# Usage:
#   ./scripts/enable-bedrock-model.sh [model-id] [region]
#
# Examples:
#   ./scripts/enable-bedrock-model.sh                                    # defaults: anthropic.claude-sonnet-4-6, us-east-1
#   ./scripts/enable-bedrock-model.sh anthropic.claude-sonnet-4-6 eu-west-1

MODEL_ID="${1:-anthropic.claude-sonnet-4-6}"
REGION="${2:-us-east-1}"

echo "Checking Bedrock model agreement for ${MODEL_ID} in ${REGION}..."

OFFER_TOKEN=$(aws bedrock list-foundation-model-agreement-offers \
  --model-id "$MODEL_ID" \
  --region "$REGION" \
  --query 'offers[0].offerToken' \
  --output text 2>/dev/null)

if [ -z "$OFFER_TOKEN" ] || [ "$OFFER_TOKEN" = "None" ]; then
  echo "No agreement offer found -- model may already be enabled or does not require one."
  exit 0
fi

echo "Accepting model agreement..."
aws bedrock create-foundation-model-agreement \
  --model-id "$MODEL_ID" \
  --offer-token "$OFFER_TOKEN" \
  --region "$REGION"

if [ $? -eq 0 ]; then
  echo "Model access enabled for ${MODEL_ID}. Allow up to 2 minutes to propagate."
else
  echo "Failed to enable model access. Check IAM permissions (bedrock:CreateFoundationModelAgreement, aws-marketplace:Subscribe)."
  exit 1
fi
