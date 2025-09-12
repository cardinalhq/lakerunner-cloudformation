# Bring Your Own (BYO) Resources Guide

This document outlines the manual steps required when using existing resources with the Lakerunner CloudFormation templates.

## Overview

The Lakerunner root template supports a "Bring Your Own" (BYO) approach where you can use existing AWS resources instead of creating new ones. When you choose to BYO resources, you must also provide an existing IAM task role with the appropriate permissions.

## BYO Requirements

### 1. VPC and Networking (Always Required)

The root template always requires VPC and subnet information, whether created by Part 1 (VPC) or existing:

- **VPC ID**: Must be a valid VPC in your account
- **Private Subnets**: Two private subnets in different AZs for database and ECS tasks
- **Public Subnets**: Two public subnets in different AZs (optional, but recommended for ALB)

### 2. S3 Storage (When CreateS3Storage=No)

**Required Resources:**

- S3 bucket for data ingestion
- SQS queue for S3 notifications

**Required Configuration:**

- Bucket should have proper lifecycle policies for `*-raw` prefixes
- S3 event notifications configured to send to SQS queue for prefixes:
  - `otel-raw/`
  - `logs-raw/`
  - `metrics-raw/`

**Manual Setup:**

1. Create S3 bucket with appropriate lifecycle rules
2. Create SQS queue for notifications (required for Lakerunner processing)
3. Configure S3 bucket notifications to send ObjectCreated events to SQS queue
4. Set up SQS queue policy to allow S3 service to send messages

### 3. RDS Database (When CreateRDS=No)

**Required Resources:**

- PostgreSQL database (Aurora or RDS instance)
- AWS Secrets Manager secret containing database credentials

**Required Configuration:**

- Database must be accessible from ECS tasks in private subnets
- Secret must contain JSON with `username` and `password` fields
- Database security group must allow connections from ECS task security group

**Manual Setup:**

1. Create PostgreSQL database in private subnets
2. Create Secrets Manager secret with database credentials:

   ```json
   {
     "username": "your_db_username",
     "password": "your_db_password"
   }
   ```

3. Configure database security group to allow inbound connections on port 5432
4. Ensure database is in same VPC as ECS tasks

### 4. ECS Task Role (When Using Any BYO Resources)

**Required Resource:**

- IAM role that ECS tasks can assume
- Role must have all necessary policies attached

**Required Policies:**

For S3 Storage access:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:ListBucket"
      ],
      "Resource": [
        "arn:aws:s3:::your-bucket-name",
        "arn:aws:s3:::your-bucket-name/*"
      ]
    },
    {
      "Effect": "Allow",
      "Action": [
        "sqs:ReceiveMessage",
        "sqs:DeleteMessage",
        "sqs:GetQueueAttributes"
      ],
      "Resource": "arn:aws:sqs:region:account:your-queue-name"
    }
  ]
}
```

For RDS Database access:

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
        "arn:aws:secretsmanager:region:account:secret:your-db-secret-name",
        "arn:aws:secretsmanager:region:account:secret:your-db-secret-name*"
      ]
    }
  ]
}
```

Base ECS Task permissions (always required):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "ssm:GetParameter",
        "ssm:GetParameters"
      ],
      "Resource": [
        "arn:aws:ssm:region:account:parameter/lakerunner/*",
        "arn:aws:ssm:region:account:parameter/your-stack-name-*"
      ]
    }
  ]
}
```

**Trust Policy** (required):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Service": "ecs-tasks.amazonaws.com"
      },
      "Action": "sts:AssumeRole"
    }
  ]
}
```

## Deployment Parameters

When deploying the root template with BYO resources, provide these parameters:

### Required for BYO S3

- `CreateS3Storage`: "No"
- `ExistingBucketArn`: ARN of your existing S3 bucket
- `ExistingTaskRoleArn`: ARN of your existing ECS task role

### Required for BYO RDS

- `CreateRDS`: "No"
- `ExistingDatabaseEndpoint`: Hostname of your existing database
- `ExistingDatabaseSecretArn`: ARN of your Secrets Manager secret
- `ExistingTaskRoleArn`: ARN of your existing ECS task role

## Example Parameter File

```json
{
  "ParameterKey": "TemplateBaseUrl",
  "ParameterValue": "https://s3.amazonaws.com/your-bucket/templates"
},
{
  "ParameterKey": "VPCId",
  "ParameterValue": "vpc-12345678"
},
{
  "ParameterKey": "PrivateSubnet1Id",
  "ParameterValue": "subnet-12345678"
},
{
  "ParameterKey": "PrivateSubnet2Id",
  "ParameterValue": "subnet-87654321"
},
{
  "ParameterKey": "PublicSubnet1Id",
  "ParameterValue": "subnet-abcdef12"
},
{
  "ParameterKey": "PublicSubnet2Id",
  "ParameterValue": "subnet-21fedcba"
},
{
  "ParameterKey": "CreateS3Storage",
  "ParameterValue": "No"
},
{
  "ParameterKey": "CreateRDS",
  "ParameterValue": "No"
},
{
  "ParameterKey": "ExistingBucketArn",
  "ParameterValue": "arn:aws:s3:::my-existing-bucket"
},
{
  "ParameterKey": "ExistingDatabaseEndpoint",
  "ParameterValue": "my-db.cluster-xyz.us-west-2.rds.amazonaws.com"
},
{
  "ParameterKey": "ExistingDatabaseSecretArn",
  "ParameterValue": "arn:aws:secretsmanager:us-west-2:123456789012:secret:my-db-secret-AbCdEf"
},
{
  "ParameterKey": "ExistingTaskRoleArn",
  "ParameterValue": "arn:aws:iam::123456789012:role/my-existing-ecs-task-role"
}
```

## Validation

After deployment, verify that:

1. ECS tasks can successfully assume the provided task role
2. Tasks can access S3 bucket and SQS queue (if BYO S3)
3. Tasks can retrieve database credentials from Secrets Manager (if BYO RDS)
4. Tasks can connect to the database using retrieved credentials
5. All CloudFormation stack outputs are correctly populated

## Troubleshooting

**Common Issues:**

1. **Permission Denied Errors**: Verify the task role has all required policies attached
2. **Database Connection Failures**: Check security group rules and network ACLs
3. **S3 Access Denied**: Ensure bucket policy allows access from the task role
4. **Secret Access Denied**: Verify Secrets Manager resource policy and IAM permissions

**Debugging Steps:**

1. Check CloudFormation stack events for detailed error messages
2. Review ECS task logs in CloudWatch Logs
3. Test IAM permissions using AWS CLI with the task role
4. Validate network connectivity between ECS tasks and resources
