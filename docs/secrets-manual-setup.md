# Secrets and Configuration - Manual AWS Console Setup Guide

This guide provides step-by-step instructions to manually create secrets and configuration parameters for Lakerunner using AWS Secrets Manager and Systems Manager Parameter Store.

## Prerequisites

- AWS account with appropriate permissions
- [RDS Setup](rds-manual-setup.md) - Database credentials created (if using RDS)

## What This Creates

- Application secrets in AWS Secrets Manager
- Configuration parameters in SSM Parameter Store
- API keys and authentication secrets
- Service configuration values

## Secrets Manager Setup

### 1. Database Credentials

If you created RDS through our guide, this already exists. Otherwise:

1. Navigate to **AWS Secrets Manager**
1. Click **Store a new secret**
1. Secret type: **Credentials for Amazon RDS database**
1. Credentials:
   - **Username**: `postgres` (or your username)
   - **Password**: Your secure password
1. Database:
   - **Server address**: Your RDS endpoint
   - **Database name**: `lakerunner`
   - **Port**: `5432`
1. Secret name: `lakerunner-database-secret`
1. Description: `RDS PostgreSQL credentials for Lakerunner`
1. Tags:
   - **Environment**: `lakerunner`
   - **Component**: `Database`
1. Store

### 2. API Keys

For API authentication:

1. Navigate to **AWS Secrets Manager**
1. Click **Store a new secret**
1. Secret type: **Other type of secret**
1. Secret key/value - **Plaintext**:

   ```json
   {
     "keys": [
       {
         "organization_id": "12340000-0000-4000-8000-000000000000",
         "api_key": "f70603aa00e6f67999cc66e336134887",
         "description": "Default organization API key"
       }
     ]
   }
   ```

   To generate a secure API key:

   ```bash
   uuidgen | tr -d '-' | tr '[:upper:]' '[:lower:]'
   ```

1. Secret name: `lakerunner-api-keys`
1. Description: `API keys for Lakerunner service authentication`
1. Tags:
   - **Environment**: `lakerunner`
   - **Component**: `API`
1. Store

### 3. Grafana Admin Password (Optional)

If deploying Grafana:

1. Navigate to **AWS Secrets Manager**
1. Click **Store a new secret**
1. Secret type: **Other type of secret**
1. Secret key/value - **Plaintext**:

   ```json
   {
     "username": "admin",
     "password": "GENERATE_SECURE_PASSWORD_HERE"
   }
   ```

1. Secret name: `lakerunner-grafana-admin`
1. Description: `Grafana admin credentials`
1. Store

### 4. Additional Service Secrets (As Needed)

For any additional services requiring secrets:

1. Follow the same pattern
1. Use consistent naming: `lakerunner-{service}-{type}`
1. Always tag appropriately
1. Use rotation where supported

## SSM Parameter Store Setup

### 1. Storage Configuration

1. Navigate to **Systems Manager → Parameter Store**
1. Click **Create parameter**
1. Details:
   - **Name**: `/lakerunner/storage-profiles`
   - **Description**: `Storage profiles for Lakerunner data`
   - **Tier**: Standard
   - **Type**: String
   - **Data type**: text
1. Value:

   ```json
   [
     {
       "bucket": "lakerunner-{accountId}-{region}",
       "cloud_provider": "aws",
       "collector_name": "lakerunner",
       "insecure_tls": false,
       "instance_num": 1,
       "organization_id": "12340000-0000-4000-8000-000000000000",
       "region": "{region}",
       "use_path_style": false
     }
   ]
   ```

1. Tags:
   - **Environment**: `lakerunner`
   - **Component**: `Storage`
1. Create

### 2. Database Configuration

Create parameters for database connection:

```bash
# Database host
/lakerunner/db/host = your-rds-endpoint.rds.amazonaws.com

# Database port
/lakerunner/db/port = 5432

# Database name
/lakerunner/db/name = lakerunner

# Config database name
/lakerunner/db/config-name = configdb

# SSL mode
/lakerunner/db/ssl-mode = require
```

For each parameter:

1. Navigate to **Systems Manager → Parameter Store**
1. Click **Create parameter**
1. Configure with values above
1. Type: **String**
1. Create

### 3. Service Configuration

Common service configuration parameters:

```bash
# Environment name
/lakerunner/environment = production

# AWS Region
/lakerunner/region = us-east-1

# Log level
/lakerunner/log-level = INFO

# Metrics enabled
/lakerunner/metrics/enabled = true

# Metrics prefix
/lakerunner/metrics/prefix = lakerunner
```

### 4. Network Configuration

Store network information:

```bash
# VPC ID
/lakerunner/vpc/id = vpc-xxxxxxxxx

# Private subnet IDs (StringList type)
/lakerunner/vpc/private-subnets = subnet-xxx,subnet-yyy

# Public subnet IDs (StringList type)
/lakerunner/vpc/public-subnets = subnet-aaa,subnet-bbb

# Security group IDs
/lakerunner/security-groups/compute = sg-xxxxxxxxx
/lakerunner/security-groups/database = sg-yyyyyyyyy
/lakerunner/security-groups/alb = sg-zzzzzzzzz
```

### 5. Container Configuration

Container image locations:

```bash
# Service images
/lakerunner/images/go-services = public.ecr.aws/cardinalhq.io/lakerunner:v1.2.2
/lakerunner/images/query-api = public.ecr.aws/cardinalhq.io/lakerunner/query-api:v1.2.1
/lakerunner/images/query-worker = public.ecr.aws/cardinalhq.io/lakerunner/query-worker:v1.2.1
```

### 6. Feature Flags (Optional)

Control feature rollout:

```bash
# Feature flags
/lakerunner/features/new-ingestion = false
/lakerunner/features/enhanced-metrics = true
/lakerunner/features/debug-mode = false
```

## IAM Permissions for Secrets Access

### 1. Task Execution Role Policy

For ECS tasks to read secrets:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "secretsmanager:GetSecretValue"
      ],
      "Resource": [
        "arn:aws:secretsmanager:{region}:{accountId}:secret:lakerunner-*"
      ]
    },
    {
      "Effect": "Allow",
      "Action": [
        "ssm:GetParameter",
        "ssm:GetParameters",
        "ssm:GetParametersByPath"
      ],
      "Resource": [
        "arn:aws:ssm:{region}:{accountId}:parameter/lakerunner/*"
      ]
    },
    {
      "Effect": "Allow",
      "Action": [
        "kms:Decrypt"
      ],
      "Resource": "*",
      "Condition": {
        "StringEquals": {
          "kms:ViaService": [
            "secretsmanager.{region}.amazonaws.com",
            "ssm.{region}.amazonaws.com"
          ]
        }
      }
    }
  ]
}
```

### 2. Application Role Policy

For applications to manage their own secrets:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "secretsmanager:GetSecretValue",
        "secretsmanager:UpdateSecret"
      ],
      "Resource": [
        "arn:aws:secretsmanager:{region}:{accountId}:secret:lakerunner-{service}-*"
      ]
    },
    {
      "Effect": "Allow",
      "Action": [
        "ssm:GetParameter",
        "ssm:PutParameter"
      ],
      "Resource": [
        "arn:aws:ssm:{region}:{accountId}:parameter/lakerunner/{service}/*"
      ]
    }
  ]
}
```

## Secret Rotation Setup (Optional)

### 1. Enable Automatic Rotation

For database password rotation:

1. Navigate to **Secrets Manager**
1. Select `lakerunner-database-secret`
1. Click **Edit rotation**
1. Enable automatic rotation:
   - **Rotation interval**: 30 days
   - **Rotation function**: Create new Lambda function
   - **Function name**: `lakerunner-db-rotation`
1. Save

### 2. Custom Rotation Function

For custom secrets, create a Lambda function:

```python
import boto3
import json

def lambda_handler(event, context):
    service = event['Service']
    token = event['Token']
    step = event['Step']

    secrets_client = boto3.client('secretsmanager')

    if step == "createSecret":
        # Generate new secret value
        new_secret = generate_new_secret()
        secrets_client.put_secret_value(
            SecretId=event['SecretId'],
            ClientRequestToken=token,
            SecretString=json.dumps(new_secret),
            VersionStages=['AWSPENDING']
        )

    elif step == "setSecret":
        # Update the service with new secret
        update_service_secret(new_secret)

    elif step == "testSecret":
        # Test the new secret works
        test_new_secret(new_secret)

    elif step == "finishSecret":
        # Mark new secret as current
        secrets_client.update_secret_version_stage(
            SecretId=event['SecretId'],
            VersionStage='AWSCURRENT',
            MoveToVersionId=token
        )
```

## Testing Secrets Access

### 1. Test from AWS CLI

```bash
# Test secret retrieval
aws secretsmanager get-secret-value \
  --secret-id lakerunner-hmac-secret \
  --query SecretString \
  --output text

# Test parameter retrieval
aws ssm get-parameter \
  --name /lakerunner/environment \
  --query Parameter.Value \
  --output text
```

### 2. Test from EC2/Container

```bash
# Using AWS SDK (Python example)
import boto3
import json

# Secrets Manager
secrets = boto3.client('secretsmanager')
response = secrets.get_secret_value(SecretId='lakerunner-hmac-secret')
secret = json.loads(response['SecretString'])
print(f"HMAC Key: {secret['key']}")

# Parameter Store
ssm = boto3.client('ssm')
response = ssm.get_parameter(Name='/lakerunner/environment')
print(f"Environment: {response['Parameter']['Value']}")
```

## Outputs to Record

After completing secrets setup:

- **Database Secret ARN**: `arn:aws:secretsmanager:{region}:{accountId}:secret:lakerunner-database-secret-xxx`
- **API Keys Secret ARN**: `arn:aws:secretsmanager:{region}:{accountId}:secret:lakerunner-api-keys-xxx`
- **Parameter Store Prefix**: `/lakerunner/`

## Next Steps

With secrets configured:

1. [IAM Roles Setup](iam-roles-manual-setup.md) - Configure permissions to access secrets
1. [Services Setup](../services-manual-setup.md) - Deploy services using these secrets

## Best Practices

### Security

1. **Never hardcode secrets** in code or configuration files
1. **Use different secrets** per environment (dev/staging/prod)
1. **Enable rotation** for database passwords
1. **Audit secret access** through CloudTrail
1. **Use KMS CMK** for encryption if required by compliance
1. **Limit secret access** to specific services/roles

### Organization

1. **Use consistent naming**: `{app}-{environment}-{type}`
1. **Tag all secrets** for cost allocation and management
1. **Document secret purpose** in description field
1. **Group related parameters** using paths in Parameter Store
1. **Version secrets** when updating

## Troubleshooting

### Common Issues

1. **Access denied to secret:**
   - Check IAM role has secretsmanager:GetSecretValue permission
   - Verify resource ARN in policy matches secret ARN
   - Check for explicit deny policies
   - Ensure KMS key permissions if using CMK

1. **Parameter not found:**
   - Verify parameter name and path
   - Check region is correct
   - Ensure IAM role has ssm:GetParameter permission

1. **Secret rotation fails:**
   - Check rotation Lambda has correct permissions
   - Verify network connectivity to target service
   - Review CloudWatch logs for rotation function
   - Ensure target supports password changes

1. **Resource issues:**
   - Review number of secrets
   - Check API call frequency
   - Consider using Parameter Store for non-sensitive data
   - Delete unused secrets and versions
