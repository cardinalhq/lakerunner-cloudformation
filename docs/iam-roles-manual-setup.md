# IAM Roles - Manual AWS Console Setup Guide

This guide provides step-by-step instructions to manually create IAM roles and policies for Lakerunner services using the AWS Management Console.

## Prerequisites

- AWS account with IAM permissions
- Understanding of which services you'll be deploying
- [Secrets Setup](secrets-manual-setup.md) - To know which secrets to grant access to

## What This Creates

- ECS Task Execution Roles (for container management)
- ECS Task Roles (for application permissions)
- Lambda Execution Roles (if using Lambda)
- Cross-service trust relationships
- Managed and inline policies

## ECS Task Roles

### 1. ECS Task Execution Role

This role is used by ECS to pull images and write logs.

#### Create the ECS Task Execution Role

1. Navigate to **IAM → Roles**
1. Click **Create role**
1. Trusted entity type: **AWS service**
1. Use case: **Elastic Container Service → Elastic Container Service Task**
1. Click **Next**
1. Attach policies:
   - **AmazonECSTaskExecutionRolePolicy** (AWS managed)
1. Role name: `lakerunner-ecs-execution-role`
1. Description: `Allows ECS tasks to pull images and write logs`
1. Tags:
   - **Environment**: `lakerunner`
   - **Component**: `ECS`
   - **Type**: `ExecutionRole`
1. Create role

#### Add Secrets Access Policy

1. Select the created role
1. Click **Add permissions** → **Create inline policy**
1. Use JSON editor:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "SecretsManagerAccess",
      "Effect": "Allow",
      "Action": [
        "secretsmanager:GetSecretValue"
      ],
      "Resource": [
        "arn:aws:secretsmanager:{region}:{accountId}:secret:lakerunner-*"
      ]
    },
    {
      "Sid": "ParameterStoreAccess",
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
      "Sid": "KMSDecrypt",
      "Effect": "Allow",
      "Action": [
        "kms:Decrypt",
        "kms:DescribeKey"
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
    },
    {
      "Sid": "ECRAccess",
      "Effect": "Allow",
      "Action": [
        "ecr:GetAuthorizationToken",
        "ecr:BatchCheckLayerAvailability",
        "ecr:GetDownloadUrlForLayer",
        "ecr:BatchGetImage"
      ],
      "Resource": "*"
    }
  ]
}
```

1. Policy name: `lakerunner-execution-secrets-access`
1. Create policy

### 2. Data Processing Task Role

For services that process data (pubsub-sqs, ingest-*, compact-*, etc.)

#### Create the Data Processing Task Role

1. Navigate to **IAM → Roles**
1. Click **Create role**
1. Trusted entity type: **AWS service**
1. Use case: **Elastic Container Service → Elastic Container Service Task**
1. Click **Next** (don't attach policies yet)
1. Role name: `lakerunner-data-task-role`
1. Description: `Allows Lakerunner data processing services to access AWS resources`
1. Create role

#### Add Data Access Policy

1. Select the created role
1. Click **Add permissions** → **Create inline policy**
1. Use JSON editor:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "S3Access",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:ListBucket",
        "s3:GetBucketLocation",
        "s3:ListBucketMultipartUploads",
        "s3:AbortMultipartUpload",
        "s3:ListMultipartUploadParts"
      ],
      "Resource": [
        "arn:aws:s3:::lakerunner-*",
        "arn:aws:s3:::lakerunner-*/*"
      ]
    },
    {
      "Sid": "SQSAccess",
      "Effect": "Allow",
      "Action": [
        "sqs:ReceiveMessage",
        "sqs:DeleteMessage",
        "sqs:GetQueueAttributes",
        "sqs:ChangeMessageVisibility",
        "sqs:GetQueueUrl",
        "sqs:SendMessage"
      ],
      "Resource": "arn:aws:sqs:{region}:{accountId}:lakerunner-*"
    },
    {
      "Sid": "CloudWatchLogs",
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents",
        "logs:DescribeLogStreams"
      ],
      "Resource": "arn:aws:logs:{region}:{accountId}:*"
    },
    {
      "Sid": "CloudWatchMetrics",
      "Effect": "Allow",
      "Action": [
        "cloudwatch:PutMetricData"
      ],
      "Resource": "*",
      "Condition": {
        "StringEquals": {
          "cloudwatch:namespace": "Lakerunner"
        }
      }
    }
  ]
}
```

1. Policy name: `lakerunner-data-access`
1. Create policy

### 3. Query Service Task Role

For query-api and query-worker services:

#### Create the Query Service Task Role

1. Create role as before
1. Role name: `lakerunner-query-task-role`
1. Description: `Allows Lakerunner query services to access data and manage workers`

#### Add Query Service Policy

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "S3ReadAccess",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:ListBucket",
        "s3:GetBucketLocation"
      ],
      "Resource": [
        "arn:aws:s3:::lakerunner-*",
        "arn:aws:s3:::lakerunner-*/*"
      ]
    },
    {
      "Sid": "ECSServiceManagement",
      "Effect": "Allow",
      "Action": [
        "ecs:DescribeServices",
        "ecs:UpdateService",
        "ecs:DescribeTasks",
        "ecs:ListTasks"
      ],
      "Resource": "*",
      "Condition": {
        "StringEquals": {
          "ecs:cluster": "arn:aws:ecs:{region}:{accountId}:cluster/lakerunner-cluster"
        }
      }
    },
    {
      "Sid": "AutoScaling",
      "Effect": "Allow",
      "Action": [
        "application-autoscaling:DescribeScalableTargets",
        "application-autoscaling:DescribeScalingPolicies",
        "application-autoscaling:PutScalingPolicy",
        "application-autoscaling:RegisterScalableTarget"
      ],
      "Resource": "*"
    },
    {
      "Sid": "CloudWatchAccess",
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents",
        "cloudwatch:PutMetricData"
      ],
      "Resource": "*"
    }
  ]
}
```

### 4. Migration Task Role

For database migration tasks:

#### Create the Migration Task Role

1. Create role as before
1. Role name: `lakerunner-migration-task-role`
1. Description: `Allows database migration tasks to access credentials`

#### Add Migration Policy

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "SecretsAccess",
      "Effect": "Allow",
      "Action": [
        "secretsmanager:GetSecretValue"
      ],
      "Resource": [
        "arn:aws:secretsmanager:{region}:{accountId}:secret:lakerunner-database-secret-*"
      ]
    },
    {
      "Sid": "CloudWatchLogs",
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ],
      "Resource": [
        "arn:aws:logs:{region}:{accountId}:log-group:/ecs/lakerunner-migration:*"
      ]
    }
  ]
}
```

## Lambda Roles (If Using Lambda)

### 1. Migration Lambda Role

For automated migration execution:

1. Navigate to **IAM → Roles**
1. Click **Create role**
1. Trusted entity: **AWS service → Lambda**
1. Attach policy: **AWSLambdaBasicExecutionRole**
1. Role name: `lakerunner-migration-lambda-role`
1. Create role

#### Add ECS Task Execution Policy

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ECSTaskExecution",
      "Effect": "Allow",
      "Action": [
        "ecs:RunTask",
        "ecs:DescribeTasks",
        "ecs:StopTask"
      ],
      "Resource": "*"
    },
    {
      "Sid": "PassRole",
      "Effect": "Allow",
      "Action": [
        "iam:PassRole"
      ],
      "Resource": [
        "arn:aws:iam::{accountId}:role/lakerunner-*-execution-role",
        "arn:aws:iam::{accountId}:role/lakerunner-*-task-role"
      ]
    }
  ]
}
```

## EC2 Instance Roles (If Using EC2 for ECS)

### 1. ECS Instance Role

For EC2 instances running ECS agent:

1. Create role with **EC2** as trusted entity
1. Attach policies:
   - **AmazonEC2ContainerServiceforEC2Role**
   - **AmazonSSMManagedInstanceCore** (for Session Manager)
1. Role name: `lakerunner-ecs-instance-role`

### 2. Create Instance Profile

1. Navigate to **IAM → Roles**
1. Select `lakerunner-ecs-instance-role`
1. Go to **Instance profile** section
1. If not exists, create one with same name

## MSK Access Role (If Using Kafka)

### 1. Create MSK Access Role

For services accessing MSK:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "MSKClusterAccess",
      "Effect": "Allow",
      "Action": [
        "kafka-cluster:Connect",
        "kafka-cluster:AlterCluster",
        "kafka-cluster:DescribeCluster"
      ],
      "Resource": "arn:aws:kafka:{region}:{accountId}:cluster/lakerunner-msk/*"
    },
    {
      "Sid": "MSKTopicAccess",
      "Effect": "Allow",
      "Action": [
        "kafka-cluster:*Topic",
        "kafka-cluster:ReadData",
        "kafka-cluster:WriteData"
      ],
      "Resource": "arn:aws:kafka:{region}:{accountId}:topic/lakerunner-msk/*/*"
    },
    {
      "Sid": "MSKGroupAccess",
      "Effect": "Allow",
      "Action": [
        "kafka-cluster:AlterGroup",
        "kafka-cluster:DescribeGroup"
      ],
      "Resource": "arn:aws:kafka:{region}:{accountId}:group/lakerunner-msk/*/*"
    }
  ]
}
```

## Cross-Account Access (If Needed)

### 1. Create Cross-Account Role

For accessing resources in another account:

1. Create role with **Another AWS account** as trusted entity
1. Enter the trusted account ID
1. Add appropriate policies
1. Role name: `lakerunner-cross-account-role`

### 2. Update Trust Relationship

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "AWS": "arn:aws:iam::TRUSTED_ACCOUNT_ID:root"
      },
      "Action": "sts:AssumeRole",
      "Condition": {
        "StringEquals": {
          "sts:ExternalId": "unique-external-id"
        }
      }
    }
  ]
}
```

## Service-Linked Roles

Some AWS services create their own service-linked roles:

1. **ECS**: `AWSServiceRoleForECS`
1. **Auto Scaling**: `AWSServiceRoleForApplicationAutoScaling`
1. **RDS**: `AWSServiceRoleForRDS`

These are created automatically when you use the services.

## Testing IAM Roles

### 1. Test Role Assumption

```bash
# Test assuming a role
aws sts assume-role \
  --role-arn arn:aws:iam::{accountId}:role/lakerunner-data-task-role \
  --role-session-name test-session

# Use the credentials returned to test access
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export AWS_SESSION_TOKEN=...

# Test S3 access
aws s3 ls s3://lakerunner-{accountId}-{region}/
```

### 2. Use IAM Policy Simulator

1. Navigate to **IAM → Roles**
1. Select a role
1. Click **Policy simulator**
1. Test various actions against resources

## Outputs to Record

Document all created IAM roles:

- **Execution Role ARN**: `arn:aws:iam::{accountId}:role/lakerunner-ecs-execution-role`
- **Data Task Role ARN**: `arn:aws:iam::{accountId}:role/lakerunner-data-task-role`
- **Query Task Role ARN**: `arn:aws:iam::{accountId}:role/lakerunner-query-task-role`
- **Migration Task Role ARN**: `arn:aws:iam::{accountId}:role/lakerunner-migration-task-role`

## Next Steps

With IAM roles configured:

1. [Update Common Infra Documentation](common-infra-manual-setup.md) - Reference all component guides
1. [Services Setup](../services-manual-setup.md) - Deploy services using these roles

## Best Practices

### Security

1. **Principle of Least Privilege**: Only grant required permissions
1. **Use Conditions**: Restrict access with conditions when possible
1. **Avoid Wildcards**: Be specific with resource ARNs
1. **Regular Audits**: Review and remove unused roles
1. **MFA for Sensitive Roles**: Require MFA for assumption
1. **Use Temporary Credentials**: Avoid long-term access keys

### Organization

1. **Consistent Naming**: Use prefixes like `lakerunner-`
1. **Clear Descriptions**: Document role purpose
1. **Tag Everything**: Use tags for organization
1. **Separate by Function**: Different roles for different services
1. **Version Policies**: Keep track of policy changes

## Troubleshooting

### Common Issues

1. **Access Denied Errors:**
   - Check the specific action and resource in the error
   - Verify the role has the required permission
   - Look for explicit deny statements
   - Check resource-based policies
   - Verify trust relationship

1. **Cannot Assume Role:**
   - Check trust relationship allows the principal
   - Verify external ID if required
   - Check session tags if used
   - Ensure no SCPs blocking assumption

1. **Task Won't Start:**
   - Verify execution role can pull images
   - Check task role exists and is valid
   - Ensure PassRole permission for ECS

1. **Secrets Not Accessible:**
   - Verify secret ARN in policy matches actual secret
   - Check KMS permissions if using CMK
   - Ensure region is correct

1. **Policy Size Limits:**
   - Inline policy limit: 2048 characters per policy
   - Managed policy limit: 6144 characters
   - Consider using multiple policies or managed policies
