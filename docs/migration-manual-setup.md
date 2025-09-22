# Migration Stack - Manual AWS Console Setup Guide

This guide provides step-by-step instructions to manually run the database migration that would be executed by the Migration CloudFormation stack using the AWS Management Console.

## Overview

The Migration stack runs a one-time ECS Fargate task to:

- Initialize the Lakerunner database schema
- Create required tables and indices
- Set up initial configuration data
- Apply database migrations

## Prerequisites

- Completed CommonInfra setup (or equivalent manual setup)
- ECS cluster configured
- Database credentials in Secrets Manager
- VPC with private subnets
- Security groups configured for database access

## 1. Create CloudWatch Log Group

1. Navigate to **CloudWatch → Log groups**
1. Click **Create log group**
1. Configure:
   - **Name**: `/ecs/lakerunner-migration`
   - **Retention**: 7 days
1. Click **Create**

## 2. Create IAM Roles

### 2.1 ECS Task Execution Role for Migration

1. Navigate to **IAM → Roles**
1. Click **Create role**
1. Select **AWS service → Elastic Container Service → ECS Task**
1. Role name: `lakerunner-migration-execution-role`
1. Attach AWS managed policy:
   - `AmazonECSTaskExecutionRolePolicy`
1. Add inline policy for Secrets Manager and CloudWatch:

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
        "arn:aws:secretsmanager:{region}:{accountId}:secret:lakerunner-database-secret-*"
      ]
    },
    {
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ],
      "Resource": [
        "arn:aws:logs:{region}:{accountId}:log-group:/ecs/lakerunner-migration:*"
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
          "kms:ViaService": "secretsmanager.{region}.amazonaws.com"
        }
      }
    }
  ]
}
```

### 2.2 ECS Task Role for Migration

1. Navigate to **IAM → Roles**
1. Click **Create role**
1. Select **AWS service → Elastic Container Service → ECS Task**
1. Role name: `lakerunner-migration-task-role`
1. Add inline policy for database operations:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ],
      "Resource": [
        "arn:aws:logs:{region}:{accountId}:log-group:/ecs/lakerunner-migration:*"
      ]
    },
    {
      "Effect": "Allow",
      "Action": [
        "secretsmanager:GetSecretValue"
      ],
      "Resource": [
        "arn:aws:secretsmanager:{region}:{accountId}:secret:lakerunner-database-secret-*"
      ]
    }
  ]
}
```

## 3. Create ECS Task Definition

1. Navigate to **ECS → Task definitions**
1. Click **Create new task definition → Create new task definition with JSON**
1. Use the following JSON (replace placeholders):

```json
{
  "family": "lakerunner-migration",
  "networkMode": "awsvpc",
  "requiresCompatibilities": ["FARGATE"],
  "cpu": "512",
  "memory": "1024",
  "executionRoleArn": "arn:aws:iam::{accountId}:role/lakerunner-migration-execution-role",
  "taskRoleArn": "arn:aws:iam::{accountId}:role/lakerunner-migration-task-role",
  "containerDefinitions": [
    {
      "name": "migration",
      "image": "public.ecr.aws/cardinalhq.io/lakerunner:latest",
      "essential": true,
      "command": ["alembic", "upgrade", "head"],
      "environment": [
        {
          "name": "LRDB_HOST",
          "value": "{database-endpoint}"
        },
        {
          "name": "LRDB_PORT",
          "value": "5432"
        },
        {
          "name": "LRDB_DBNAME",
          "value": "lakerunner"
        },
        {
          "name": "LRDB_SSLMODE",
          "value": "require"
        },
        {
          "name": "CONFIG_DB_HOST",
          "value": "{database-endpoint}"
        },
        {
          "name": "CONFIG_DB_PORT",
          "value": "5432"
        },
        {
          "name": "CONFIG_DB_NAME",
          "value": "configdb"
        },
        {
          "name": "CONFIG_DB_SSL_MODE",
          "value": "require"
        }
      ],
      "secrets": [
        {
          "name": "LRDB_USERNAME",
          "valueFrom": "arn:aws:secretsmanager:{region}:{accountId}:secret:lakerunner-database-secret:username::"
        },
        {
          "name": "LRDB_PASSWORD",
          "valueFrom": "arn:aws:secretsmanager:{region}:{accountId}:secret:lakerunner-database-secret:password::"
        },
        {
          "name": "CONFIG_DB_USERNAME",
          "valueFrom": "arn:aws:secretsmanager:{region}:{accountId}:secret:lakerunner-database-secret:username::"
        },
        {
          "name": "CONFIG_DB_PASSWORD",
          "valueFrom": "arn:aws:secretsmanager:{region}:{accountId}:secret:lakerunner-database-secret:password::"
        }
      ],
      "logConfiguration": {
        "logDriver": "awslogs",
        "options": {
          "awslogs-group": "/ecs/lakerunner-migration",
          "awslogs-region": "{region}",
          "awslogs-stream-prefix": "ecs"
        }
      }
    }
  ]
}
```

1. Click **Create**

## 4. Run Migration Task Manually

### Option A: Using ECS Console

1. Navigate to **ECS → Clusters**
1. Select your cluster (e.g., `lakerunner-cluster`)
1. Go to **Tasks** tab
1. Click **Run new task**
1. Configure:
   - **Launch type**: FARGATE
   - **Task definition**: `lakerunner-migration:latest`
   - **Cluster VPC**: Your VPC
   - **Subnets**: Select private subnets
   - **Security groups**: Select the compute security group
   - **Auto-assign public IP**: DISABLED
1. Click **Run task**
1. Monitor the task:
   - Wait for status to change from PENDING → RUNNING → STOPPED
   - Check CloudWatch logs for migration output

### Option B: Using AWS CLI

```bash
aws ecs run-task \
  --cluster lakerunner-cluster \
  --task-definition lakerunner-migration:latest \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={
    subnets=[subnet-xxx,subnet-yyy],
    securityGroups=[sg-compute],
    assignPublicIp=DISABLED
  }" \
  --region {region}
```

## 5. Create Lambda Function for Automated Migration (Optional)

If you want to automate the migration task execution (similar to CloudFormation Custom Resource):

### 5.1 Create Lambda Execution Role

1. Navigate to **IAM → Roles**
1. Click **Create role**
1. Select **AWS service → Lambda**
1. Role name: `lakerunner-migration-lambda-role`
1. Attach policies:
   - `AWSLambdaBasicExecutionRole` (AWS managed)
1. Add inline policy for ECS task execution:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "ecs:RunTask",
        "ecs:DescribeTasks",
        "ecs:StopTask"
      ],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "iam:PassRole"
      ],
      "Resource": [
        "arn:aws:iam::{accountId}:role/lakerunner-migration-execution-role",
        "arn:aws:iam::{accountId}:role/lakerunner-migration-task-role"
      ]
    },
    {
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ],
      "Resource": "arn:aws:logs:{region}:{accountId}:*"
    }
  ]
}
```

### 5.2 Create Lambda Function

1. Navigate to **Lambda → Functions**
1. Click **Create function**
1. Configure:
   - **Name**: `lakerunner-run-migration`
   - **Runtime**: Python 3.11
   - **Role**: Use existing role → `lakerunner-migration-lambda-role`
1. Function code:

```python
import boto3
import json
import time
import os

ecs = boto3.client('ecs')

def lambda_handler(event, context):
    cluster_arn = os.environ['CLUSTER_ARN']
    task_definition_arn = os.environ['TASK_DEFINITION_ARN']
    subnet_ids = os.environ['SUBNET_IDS'].split(',')
    security_group_ids = os.environ['SECURITY_GROUP_IDS'].split(',')

    try:
        # Run the ECS task
        response = ecs.run_task(
            cluster=cluster_arn,
            taskDefinition=task_definition_arn,
            launchType='FARGATE',
            networkConfiguration={
                'awsvpcConfiguration': {
                    'subnets': subnet_ids,
                    'securityGroups': security_group_ids,
                    'assignPublicIp': 'DISABLED'
                }
            }
        )

        task_arn = response['tasks'][0]['taskArn']
        print(f"Started migration task: {task_arn}")

        # Wait for task to complete
        max_wait_time = 600  # 10 minutes
        start_time = time.time()

        while time.time() - start_time < max_wait_time:
            tasks = ecs.describe_tasks(
                cluster=cluster_arn,
                tasks=[task_arn]
            )

            if tasks['tasks']:
                task = tasks['tasks'][0]
                status = task['lastStatus']

                if status == 'STOPPED':
                    # Check exit code
                    if 'containers' in task:
                        container = task['containers'][0]
                        exit_code = container.get('exitCode', -1)

                        if exit_code == 0:
                            print("Migration completed successfully")
                            return {
                                'statusCode': 200,
                                'body': json.dumps('Migration completed successfully')
                            }
                        else:
                            print(f"Migration failed with exit code: {exit_code}")
                            return {
                                'statusCode': 500,
                                'body': json.dumps(f'Migration failed with exit code: {exit_code}')
                            }

                print(f"Task status: {status}")
                time.sleep(10)
            else:
                print("Task not found")
                break

        return {
            'statusCode': 500,
            'body': json.dumps('Migration task timeout')
        }

    except Exception as e:
        print(f"Error: {str(e)}")
        return {
            'statusCode': 500,
            'body': json.dumps(f'Error: {str(e)}')
        }
```

1. Environment variables:
   - `CLUSTER_ARN`: Your ECS cluster ARN
   - `TASK_DEFINITION_ARN`: Migration task definition ARN
   - `SUBNET_IDS`: Comma-separated private subnet IDs
   - `SECURITY_GROUP_IDS`: Comma-separated security group IDs

1. Timeout: Set to 15 minutes
1. Memory: 256 MB

## 6. Verify Migration

### 6.1 Check CloudWatch Logs

1. Navigate to **CloudWatch → Log groups**
1. Open `/ecs/lakerunner-migration`
1. Check the latest log stream for:
   - Database connection success
   - Schema creation messages
   - Migration completion status

### 6.2 Verify Database Schema

Connect to the database and verify tables were created:

```sql
-- Connect to PostgreSQL
psql -h {database-endpoint} -U postgres -d lakerunner

-- List all tables
\dt

-- Expected tables include:
-- - alembic_version (migration tracking)
-- - users
-- - projects
-- - datasets
-- - pipelines
-- - etc.

-- Check migration version
SELECT * FROM alembic_version;
```

## 7. Troubleshooting

### Common Issues and Solutions

1. **Task fails to start**:
   - Check ECS task execution role has proper permissions
   - Verify security groups allow database access
   - Ensure subnets have route to NAT gateway for internet access

1. **Database connection errors**:
   - Verify database endpoint is correct
   - Check security group rules allow port 5432
   - Confirm database secret contains correct credentials
   - Ensure SSL mode is set to 'require'

1. **Migration fails**:
   - Check CloudWatch logs for specific error messages
   - Verify database user has CREATE/ALTER permissions
   - Ensure database exists and is accessible
   - Check if migration was already run (idempotency)

1. **Task times out**:
   - Increase task CPU/memory if needed
   - Check if database is under heavy load
   - Verify network connectivity

### Manual Database Setup (If Migration Fails)

If automated migration fails, you can manually create the schema:

1. Connect to the database
1. Run the SQL scripts from the Lakerunner repository
1. Update the alembic_version table to mark migrations as complete

## 8. Clean Up After Migration

Once migration is successful:

1. **Stop any running migration tasks** to avoid duplicate runs
1. **Document the migration version** for future reference
1. **Keep the task definition** for future migrations/upgrades
1. **Monitor database** for proper operation

## Notes

- The migration is idempotent - running it multiple times is safe
- Always backup your database before running migrations in production
- Migration logs are retained for 7 days by default
- Consider running migration in a maintenance window for production systems
- Test migrations in a staging environment first
