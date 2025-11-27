# AWS Bedrock Permissions for MCP Combined Service

This document explains the AWS Bedrock permissions configured for the lakerunner-mcp-combined CloudFormation stack.

## Overview

The MCP combined service uses AWS Bedrock for:
1. **LLM inference** - Claude Sonnet 4.5 models
2. **Embeddings** - Amazon Titan Embeddings v2

The CloudFormation stack automatically creates an ECS task role with the necessary Bedrock permissions.

## Task Role ARN

After deploying the stack, the task role ARN is available in the CloudFormation outputs:

```bash
# Get the task role ARN from stack outputs
aws cloudformation describe-stacks \
  --stack-name your-mcp-stack-name \
  --query 'Stacks[0].Outputs[?OutputKey==`TaskRoleArn`].OutputValue' \
  --output text
```

## Bedrock Permissions Policy

The task role has the following IAM policy attached:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "BedrockModelInvokeAccess",
      "Effect": "Allow",
      "Action": [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream"
      ],
      "Resource": [
        "arn:aws:bedrock:*::foundation-model/us.anthropic.claude-sonnet-4-5-*",
        "arn:aws:bedrock:*::foundation-model/amazon.titan-embed-text-v2:0"
      ]
    }
  ]
}
```

This policy is also available in the CloudFormation stack outputs:

```bash
# Get the Bedrock permissions policy from stack outputs
aws cloudformation describe-stacks \
  --stack-name your-mcp-stack-name \
  --query 'Stacks[0].Outputs[?OutputKey==`BedrockPermissionsPolicy`].OutputValue' \
  --output text
```

## Granting Additional Permissions to the Task Role

If your MCP combined service needs access to other AWS services (e.g., S3, DynamoDB, SQS), you can attach additional policies to the task role.

### Method 1: Using AWS CLI

```bash
# Get the task role ARN
TASK_ROLE_ARN=$(aws cloudformation describe-stacks \
  --stack-name your-mcp-stack-name \
  --query 'Stacks[0].Outputs[?OutputKey==`TaskRoleArn`].OutputValue' \
  --output text)

# Extract role name from ARN
ROLE_NAME=$(echo $TASK_ROLE_ARN | awk -F'/' '{print $NF}')

# Attach an AWS managed policy
aws iam attach-role-policy \
  --role-name $ROLE_NAME \
  --policy-arn arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess

# Or create and attach a custom inline policy
aws iam put-role-policy \
  --role-name $ROLE_NAME \
  --policy-name CustomS3Access \
  --policy-document file://custom-policy.json
```

### Method 2: Using CloudFormation

Create a separate CloudFormation stack that attaches policies to the task role:

```yaml
AWSTemplateFormatVersion: '2010-09-09'
Description: Additional permissions for MCP combined service

Parameters:
  McpStackName:
    Type: String
    Description: Name of the MCP combined stack

Resources:
  AdditionalTaskRolePolicy:
    Type: AWS::IAM::Policy
    Properties:
      PolicyName: McpAdditionalPermissions
      Roles:
        - Fn::Select:
            - 1
            - Fn::Split:
                - '/'
                - Fn::ImportValue: !Sub '${McpStackName}-TaskRoleArn'
      PolicyDocument:
        Version: '2012-10-17'
        Statement:
          - Sid: S3ReadAccess
            Effect: Allow
            Action:
              - s3:GetObject
              - s3:ListBucket
            Resource:
              - arn:aws:s3:::my-bucket/*
              - arn:aws:s3:::my-bucket
```

## Sample Additional Policy Documents

### S3 Access

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "S3ReadWriteAccess",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:ListBucket"
      ],
      "Resource": [
        "arn:aws:s3:::my-bucket/*",
        "arn:aws:s3:::my-bucket"
      ]
    }
  ]
}
```

### DynamoDB Access

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "DynamoDBAccess",
      "Effect": "Allow",
      "Action": [
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
        "dynamodb:Query",
        "dynamodb:Scan"
      ],
      "Resource": "arn:aws:dynamodb:*:*:table/MyTable"
    }
  ]
}
```

### SQS Access

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "SQSAccess",
      "Effect": "Allow",
      "Action": [
        "sqs:SendMessage",
        "sqs:ReceiveMessage",
        "sqs:DeleteMessage",
        "sqs:GetQueueAttributes"
      ],
      "Resource": "arn:aws:sqs:*:*:my-queue"
    }
  ]
}
```

## Granting Bedrock Permissions to Other Roles

If you need to grant the same Bedrock permissions to other IAM roles or users (e.g., for local development or testing):

### Using AWS CLI

```bash
# Create a policy document file
cat > bedrock-policy.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "BedrockModelInvokeAccess",
      "Effect": "Allow",
      "Action": [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream"
      ],
      "Resource": [
        "arn:aws:bedrock:*::foundation-model/us.anthropic.claude-sonnet-4-5-*",
        "arn:aws:bedrock:*::foundation-model/amazon.titan-embed-text-v2:0"
      ]
    }
  ]
}
EOF

# Create a managed policy
aws iam create-policy \
  --policy-name LakerunnerMcpBedrockAccess \
  --policy-document file://bedrock-policy.json

# Attach to a role
aws iam attach-role-policy \
  --role-name YourRoleName \
  --policy-arn arn:aws:iam::YOUR_ACCOUNT_ID:policy/LakerunnerMcpBedrockAccess

# Or attach to a user
aws iam attach-user-policy \
  --user-name YourUserName \
  --policy-arn arn:aws:iam::YOUR_ACCOUNT_ID:policy/LakerunnerMcpBedrockAccess
```

## Enabling Bedrock Models

Before the MCP combined service can use Bedrock models, you must enable them in your AWS account:

1. Navigate to the AWS Bedrock console
2. Go to "Model access" in the left sidebar
3. Click "Modify model access" or "Enable specific models"
4. Enable the following models:
   - **Claude Sonnet 4.5** (us.anthropic.claude-sonnet-4-5-*)
   - **Titan Embeddings v2** (amazon.titan-embed-text-v2:0)
5. Accept the EULA and submit the request

Alternatively, use the AWS CLI:

```bash
# Request access to Claude Sonnet 4.5
aws bedrock put-model-invocation-logging-configuration \
  --region us-east-1

# Check model access status
aws bedrock list-foundation-models \
  --region us-east-1 \
  --query 'modelSummaries[?contains(modelId, `claude-sonnet-4-5`) || contains(modelId, `titan-embed`)]'
```

## Bedrock Model ARN Format

The Bedrock model ARNs used by the service:

- **Claude Sonnet 4.5**: `arn:aws:bedrock:*::foundation-model/us.anthropic.claude-sonnet-4-5-*`
  - Wildcard allows for versioned models (e.g., `us.anthropic.claude-sonnet-4-5-20250929-v1:0`)
- **Titan Embeddings v2**: `arn:aws:bedrock:*::foundation-model/amazon.titan-embed-text-v2:0`

## Troubleshooting

### Access Denied Errors

If you see `bedrock:InvokeModel` access denied errors:

1. **Check model access**: Ensure the models are enabled in Bedrock console
2. **Check IAM permissions**: Verify the task role has the Bedrock policy attached
3. **Check region**: Bedrock models may not be available in all regions
4. **Check model ARN**: Ensure the model ARN in the policy matches the model being invoked

### Debugging IAM Permissions

```bash
# Get the task role ARN
TASK_ROLE_ARN=$(aws cloudformation describe-stacks \
  --stack-name your-mcp-stack-name \
  --query 'Stacks[0].Outputs[?OutputKey==`TaskRoleArn`].OutputValue' \
  --output text)

ROLE_NAME=$(echo $TASK_ROLE_ARN | awk -F'/' '{print $NF}')

# List attached managed policies
aws iam list-attached-role-policies --role-name $ROLE_NAME

# List inline policies
aws iam list-role-policies --role-name $ROLE_NAME

# Get inline policy document
aws iam get-role-policy \
  --role-name $ROLE_NAME \
  --policy-name BedrockAccess
```

## Security Best Practices

1. **Principle of least privilege**: Only grant permissions to the specific Bedrock models needed
2. **Use resource-based policies**: Limit access to specific model ARNs, not all Bedrock models
3. **Monitor usage**: Use CloudWatch and CloudTrail to monitor Bedrock API calls
4. **Rotate credentials**: If using IAM users, rotate access keys regularly
5. **Use VPC endpoints**: Consider using VPC endpoints for Bedrock to keep traffic within AWS network

## Cost Considerations

AWS Bedrock charges are based on:
- **Input tokens**: Text sent to the model
- **Output tokens**: Text generated by the model
- **Embedding dimensions**: For Titan Embeddings

Monitor costs using:
```bash
# Check Bedrock usage (requires Cost Explorer API access)
aws ce get-cost-and-usage \
  --time-period Start=2025-01-01,End=2025-01-31 \
  --granularity MONTHLY \
  --metrics BlendedCost \
  --filter file://bedrock-filter.json
```

Where `bedrock-filter.json`:
```json
{
  "Dimensions": {
    "Key": "SERVICE",
    "Values": ["Amazon Bedrock"]
  }
}
```

## References

- [AWS Bedrock Documentation](https://docs.aws.amazon.com/bedrock/)
- [AWS IAM Policies](https://docs.aws.amazon.com/IAM/latest/UserGuide/access_policies.html)
- [ECS Task IAM Roles](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task-iam-roles.html)
- [Bedrock Model Access](https://docs.aws.amazon.com/bedrock/latest/userguide/model-access.html)
