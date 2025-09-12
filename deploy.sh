#!/bin/bash
set -e

# Configuration
STACK_NAME="${STACK_NAME:-lakerunner}"
S3_BUCKET="${S3_BUCKET:-}"
REGION="${AWS_REGION:-us-east-1}"

if [ -z "$S3_BUCKET" ]; then
    echo "Error: S3_BUCKET environment variable must be set"
    echo "Example: export S3_BUCKET=my-cloudformation-templates"
    exit 1
fi

echo "Deploying Lakerunner CloudFormation stack..."
echo "Stack Name: $STACK_NAME"
echo "S3 Bucket: $S3_BUCKET"
echo "Region: $REGION"
echo

# Upload nested templates to S3
echo "Uploading nested templates to S3..."
aws s3 cp generated-templates/lakerunner-vpc.yaml s3://$S3_BUCKET/templates/ --region $REGION

# Update TemplateBaseUrl in parameters file
TEMPLATE_BASE_URL="https://$S3_BUCKET.s3.$REGION.amazonaws.com/templates"
echo "Using Template Base URL: $TEMPLATE_BASE_URL"

# Create temporary parameters file with correct S3 URL
sed "s|https://your-s3-bucket.s3.amazonaws.com/templates|$TEMPLATE_BASE_URL|g" parameters.json > parameters-temp.json

# Deploy the stack
echo
echo "Deploying CloudFormation stack..."
aws cloudformation create-stack \
    --stack-name $STACK_NAME \
    --template-body file://generated-templates/lakerunner-root.yaml \
    --parameters file://parameters-temp.json \
    --capabilities CAPABILITY_IAM \
    --region $REGION

# Clean up temp file
rm -f parameters-temp.json

echo
echo "Stack deployment initiated. Monitor progress with:"
echo "aws cloudformation describe-stacks --stack-name $STACK_NAME --region $REGION"
echo "aws cloudformation wait stack-create-complete --stack-name $STACK_NAME --region $REGION"
