# Services Stack - Manual AWS Console Setup Guide

This guide provides step-by-step instructions to manually create all ECS services, task definitions, and supporting resources provided by the Services CloudFormation stack using the AWS Management Console.

## Overview

The Services stack deploys multiple ECS Fargate services:

- **pubsub-sqs**: Processes S3 events from SQS queue
- **ingest-logs**: Ingests log data into storage
- **ingest-metrics**: Ingests metrics data
- **compact-logs**: Compacts stored log files
- **compact-metrics**: Compacts metrics files
- **rollup-metrics**: Creates metric rollups
- **sweeper**: Cleans up old data
- **query-api**: Query API endpoint (with ALB integration)
- **query-worker**: Query processing workers

## Prerequisites

- Completed CommonInfra and Migration stacks (or manual equivalents)
- ECS cluster
- VPC with private subnets
- Security groups configured
- Database migrated and accessible
- S3 bucket and SQS queue configured
- Secrets in Secrets Manager

## 1. Create Application Secrets

### 1.1 API Keys Secret

1. Navigate to **Secrets Manager**
1. Click **Store a new secret**
1. Select **Other type of secret**
1. Enter as plaintext:

   ```json
   {
     "keys": [
       {
         "organization_id": "12340000-0000-4000-8000-000000000000",
         "api_key": "f70603aa00e6f67999cc66e336134887"
       }
     ]
   }
   ```

1. Name: `lakerunner-api-keys`
1. Create secret

### 1.2 Storage Profiles Configuration

1. Navigate to **Systems Manager → Parameter Store**
1. Click **Create parameter**
1. Configure:
   - **Name**: `/lakerunner/storage-profiles`
   - **Type**: String
   - **Value**:

   ```json
   [{
     "bucket": "lakerunner-{accountId}-{region}",
     "cloud_provider": "aws",
     "collector_name": "lakerunner",
     "instance_num": 1,
     "organization_id": "12340000-0000-4000-8000-000000000000",
     "region": "{region}",
     "use_path_style": true
   }]
   ```

1. Create

## 2. Create CloudWatch Log Groups

Create log groups for each service:

```bash
# List of services
services=(
  "pubsub-sqs"
  "ingest-logs"
  "ingest-metrics"
  "compact-logs"
  "compact-metrics"
  "rollup-metrics"
  "sweeper"
  "query-api"
  "query-worker"
)

# Create log group for each service
for service in "${services[@]}"; do
  echo "/ecs/lakerunner-$service"
done
```

For each service, in CloudWatch:

1. Navigate to **CloudWatch → Log groups**
1. Click **Create log group**
1. Name: `/ecs/lakerunner-{service-name}`
1. Retention: 7 days
1. Create

## 3. Create IAM Roles

### 3.1 Shared ECS Task Execution Role

1. Navigate to **IAM → Roles**
1. Click **Create role**
1. Select **AWS service → Elastic Container Service → ECS Task**
1. Name: `lakerunner-services-execution-role`
1. Attach AWS managed policy:
   - `AmazonECSTaskExecutionRolePolicy`
1. Add inline policy:

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
        "ssm:GetParameters"
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

### 3.2 Task Roles for Each Service Type

#### Data Processing Services Role

For services: pubsub-sqs, ingest-logs, ingest-metrics, compact-logs, compact-metrics, rollup-metrics, sweeper

1. Create role: `lakerunner-data-services-task-role`
1. Trust relationship: ECS Tasks
1. Inline policy:

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
        "s3:ListBucket",
        "s3:GetBucketLocation",
        "s3:ListBucketMultipartUploads",
        "s3:AbortMultipartUpload"
      ],
      "Resource": [
        "arn:aws:s3:::lakerunner-{accountId}-{region}",
        "arn:aws:s3:::lakerunner-{accountId}-{region}/*"
      ]
    },
    {
      "Effect": "Allow",
      "Action": [
        "sqs:ReceiveMessage",
        "sqs:DeleteMessage",
        "sqs:GetQueueAttributes",
        "sqs:ChangeMessageVisibility",
        "sqs:GetQueueUrl"
      ],
      "Resource": "arn:aws:sqs:{region}:{accountId}:lakerunner-queue"
    },
    {
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ],
      "Resource": "arn:aws:logs:{region}:{accountId}:log-group:/ecs/lakerunner-*:*"
    }
  ]
}
```

#### Query Services Role

For services: query-api, query-worker

1. Create role: `lakerunner-query-services-task-role`
1. Trust relationship: ECS Tasks
1. Inline policy:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:ListBucket",
        "s3:GetBucketLocation"
      ],
      "Resource": [
        "arn:aws:s3:::lakerunner-{accountId}-{region}",
        "arn:aws:s3:::lakerunner-{accountId}-{region}/*"
      ]
    },
    {
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ],
      "Resource": "arn:aws:logs:{region}:{accountId}:log-group:/ecs/lakerunner-*:*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "ecs:DescribeServices",
        "ecs:UpdateService"
      ],
      "Resource": "*",
      "Condition": {
        "StringEquals": {
          "ecs:cluster": "arn:aws:ecs:{region}:{accountId}:cluster/lakerunner-cluster"
        }
      }
    }
  ]
}
```

## 4. Create ECS Task Definitions

For each service, create a task definition. Here's the pattern:

### 4.1 Data Processing Services Task Definition

Example for `lakerunner-pubsub-sqs`:

1. Navigate to **ECS → Task definitions**
1. Click **Create new task definition → Create new task definition with JSON**

```json
{
  "family": "lakerunner-pubsub-sqs",
  "networkMode": "awsvpc",
  "requiresCompatibilities": ["FARGATE"],
  "cpu": "1024",
  "memory": "2048",
  "executionRoleArn": "arn:aws:iam::{accountId}:role/lakerunner-services-execution-role",
  "taskRoleArn": "arn:aws:iam::{accountId}:role/lakerunner-data-services-task-role",
  "containerDefinitions": [
    {
      "name": "app",
      "image": "public.ecr.aws/cardinalhq.io/lakerunner:v1.2.2",
      "essential": true,
      "command": ["/app/bin/lakerunner", "pubsub", "sqs"],
      "environment": [
        {"name": "LRDB_HOST", "value": "{database-endpoint}"},
        {"name": "LRDB_PORT", "value": "5432"},
        {"name": "LRDB_DBNAME", "value": "lakerunner"},
        {"name": "LRDB_SSLMODE", "value": "require"},
        {"name": "CONFIG_DB_HOST", "value": "{database-endpoint}"},
        {"name": "CONFIG_DB_PORT", "value": "5432"},
        {"name": "CONFIG_DB_NAME", "value": "configdb"},
        {"name": "CONFIG_DB_SSL_MODE", "value": "require"},
        {"name": "SERVICE_NAME", "value": "lakerunner-pubsub-sqs"},
        {"name": "BUCKET_NAME", "value": "lakerunner-{accountId}-{region}"},
        {"name": "SQS_QUEUE_URL", "value": "https://sqs.{region}.amazonaws.com/{accountId}/lakerunner-queue"},
        {"name": "AWS_REGION", "value": "{region}"}
      ],
      "secrets": [
        {"name": "LRDB_USERNAME", "valueFrom": "arn:aws:secretsmanager:{region}:{accountId}:secret:lakerunner-database-secret:username::"},
        {"name": "LRDB_PASSWORD", "valueFrom": "arn:aws:secretsmanager:{region}:{accountId}:secret:lakerunner-database-secret:password::"},
        {"name": "CONFIG_DB_USERNAME", "valueFrom": "arn:aws:secretsmanager:{region}:{accountId}:secret:lakerunner-database-secret:username::"},
        {"name": "CONFIG_DB_PASSWORD", "valueFrom": "arn:aws:secretsmanager:{region}:{accountId}:secret:lakerunner-database-secret:password::"}
      ],
      "logConfiguration": {
        "logDriver": "awslogs",
        "options": {
          "awslogs-group": "/ecs/lakerunner-pubsub-sqs",
          "awslogs-region": "{region}",
          "awslogs-stream-prefix": "ecs"
        }
      },
      "healthCheck": {
        "command": ["CMD-SHELL", "/app/bin/lakerunner sysinfo || exit 1"],
        "interval": 30,
        "timeout": 5,
        "retries": 3,
        "startPeriod": 60
      }
    }
  ]
}
```

Repeat for each data processing service with appropriate:

- CPU/Memory values from defaults
- Command arguments
- Service name

### 4.2 Query API Task Definition

```json
{
  "family": "lakerunner-query-api",
  "networkMode": "awsvpc",
  "requiresCompatibilities": ["FARGATE"],
  "cpu": "2048",
  "memory": "8192",
  "executionRoleArn": "arn:aws:iam::{accountId}:role/lakerunner-services-execution-role",
  "taskRoleArn": "arn:aws:iam::{accountId}:role/lakerunner-query-services-task-role",
  "volumes": [
    {
      "name": "scratch",
      "host": {}
    }
  ],
  "containerDefinitions": [
    {
      "name": "app",
      "image": "public.ecr.aws/cardinalhq.io/lakerunner/query-api:v1.2.1",
      "essential": true,
      "portMappings": [
        {
          "containerPort": 7101,
          "protocol": "tcp"
        }
      ],
      "environment": [
        {"name": "LRDB_HOST", "value": "{database-endpoint}"},
        {"name": "LRDB_PORT", "value": "5432"},
        {"name": "LRDB_DBNAME", "value": "lakerunner"},
        {"name": "LRDB_SSLMODE", "value": "require"},
        {"name": "CONFIG_DB_HOST", "value": "{database-endpoint}"},
        {"name": "CONFIG_DB_PORT", "value": "5432"},
        {"name": "CONFIG_DB_NAME", "value": "configdb"},
        {"name": "CONFIG_DB_SSL_MODE", "value": "require"},
        {"name": "EXECUTION_ENVIRONMENT", "value": "ecs"},
        {"name": "QUERY_STACK", "value": "local"},
        {"name": "METRIC_PREFIX", "value": "lakerunner-query-api"},
        {"name": "NUM_MIN_QUERY_WORKERS", "value": "8"},
        {"name": "NUM_MAX_QUERY_WORKERS", "value": "8"},
        {"name": "SPRING_PROFILES_ACTIVE", "value": "aws"},
        {"name": "SERVICE_NAME", "value": "lakerunner-query-api"},
        {"name": "BUCKET_NAME", "value": "lakerunner-{accountId}-{region}"},
        {"name": "AWS_REGION", "value": "{region}"}
      ],
      "secrets": [
        {"name": "LRDB_USERNAME", "valueFrom": "arn:aws:secretsmanager:{region}:{accountId}:secret:lakerunner-database-secret:username::"},
        {"name": "LRDB_PASSWORD", "valueFrom": "arn:aws:secretsmanager:{region}:{accountId}:secret:lakerunner-database-secret:password::"},
        {"name": "CONFIG_DB_USERNAME", "valueFrom": "arn:aws:secretsmanager:{region}:{accountId}:secret:lakerunner-database-secret:username::"},
        {"name": "CONFIG_DB_PASSWORD", "valueFrom": "arn:aws:secretsmanager:{region}:{accountId}:secret:lakerunner-database-secret:password::"}
      ],
      "mountPoints": [
        {
          "sourceVolume": "scratch",
          "containerPath": "/db",
          "readOnly": false
        }
      ],
      "logConfiguration": {
        "logDriver": "awslogs",
        "options": {
          "awslogs-group": "/ecs/lakerunner-query-api",
          "awslogs-region": "{region}",
          "awslogs-stream-prefix": "ecs"
        }
      },
      "healthCheck": {
        "command": ["CMD-SHELL", "curl -f http://localhost:7101/ready || exit 1"],
        "interval": 30,
        "timeout": 5,
        "retries": 3,
        "startPeriod": 90
      }
    }
  ]
}
```

### 4.3 Query Worker Task Definition

Similar to Query API but:

- Different image: `public.ecr.aws/cardinalhq.io/lakerunner/query-worker:v1.2.1`
- Different environment variables (no NUM_MIN/MAX_QUERY_WORKERS)
- Service name: `lakerunner-query-worker`

## 5. Create Application Load Balancer (Optional)

If you want to expose the Query API:

### 5.1 Create Target Group

1. Navigate to **EC2 → Target Groups**
1. Click **Create target group**
1. Configuration:
   - **Target type**: IP addresses
   - **Target group name**: `lakerunner-query-api-tg`
   - **Protocol**: HTTP
   - **Port**: 7101
   - **VPC**: Your VPC
   - **Protocol version**: HTTP1
1. Health checks:
   - **Path**: `/ready`
   - **Interval**: 30 seconds
   - **Timeout**: 5 seconds
   - **Healthy threshold**: 2
   - **Unhealthy threshold**: 3
1. Create

### 5.2 Create Application Load Balancer

1. Navigate to **EC2 → Load Balancers**
1. Click **Create Load Balancer → Application Load Balancer**
1. Configuration:
   - **Name**: `lakerunner-alb`
   - **Scheme**: Internet-facing (or internal based on needs)
   - **IP address type**: IPv4
1. Network mapping:
   - **VPC**: Your VPC
   - **Subnets**: Select public subnets (at least 2 AZs)
1. Security groups:
   - Create new or select existing with:
     - Inbound: Port 80/443 from desired sources
     - Outbound: Port 7101 to VPC CIDR
1. Listeners:
   - **Protocol**: HTTP
   - **Port**: 80
   - **Default action**: Forward to `lakerunner-query-api-tg`
1. Create

## 6. Create ECS Services

For each task definition, create an ECS service:

### 6.1 Data Processing Services

Example for `lakerunner-pubsub-sqs`:

1. Navigate to **ECS → Clusters**
1. Select your cluster
1. Click **Create Service**
1. Configuration:
   - **Launch type**: FARGATE
   - **Task definition**: `lakerunner-pubsub-sqs:latest`
   - **Service name**: `lakerunner-pubsub-sqs`
   - **Number of tasks**: 1
1. Network configuration:
   - **Cluster VPC**: Your VPC
   - **Subnets**: Private subnets
   - **Security groups**: Compute security group
   - **Auto-assign public IP**: DISABLED
1. Create service

Repeat for all data processing services with appropriate replica counts:

- pubsub-sqs: 1 replica
- ingest-logs: 2 replicas
- ingest-metrics: 1 replica
- compact-logs: 4 replicas
- compact-metrics: 1 replica
- rollup-metrics: 1 replica
- sweeper: 1 replica

### 6.2 Query API Service (with ALB)

1. Navigate to **ECS → Clusters**
1. Select your cluster
1. Click **Create Service**
1. Configuration:
   - **Launch type**: FARGATE
   - **Task definition**: `lakerunner-query-api:latest`
   - **Service name**: `lakerunner-query-api`
   - **Number of tasks**: 1
1. Network configuration:
   - Same as above
1. Load balancing:
   - **Load balancer type**: Application Load Balancer
   - **Load balancer**: Select existing ALB
   - **Target group**: `lakerunner-query-api-tg`
1. Create service

### 6.3 Query Worker Service

Same as data processing services but with 8 replicas.

## 7. Service Auto-Scaling (Optional)

## 7. Testing and Validation

### 7.1 Verify Services Are Running

```bash
# Check all services are running
aws ecs list-services --cluster lakerunner-cluster --region {region}

# Check task status for each service
aws ecs describe-services \
  --cluster lakerunner-cluster \
  --services lakerunner-pubsub-sqs \
  --region {region}
```

### 7.2 Test Query API

```bash
# If using ALB
curl http://{alb-dns-name}/ready

# Direct service test (from within VPC)
curl http://{task-ip}:7101/ready
```

### 7.3 Check Logs

1. Navigate to **CloudWatch → Log groups**
1. Select `/ecs/lakerunner-{service-name}`
1. Check latest log streams for each service
1. Verify no errors during startup

### 7.4 Test Data Flow

1. Upload a test file to S3 bucket
1. Monitor SQS queue for message
1. Check pubsub-sqs service logs
1. Verify data processing through the pipeline

## 8. Troubleshooting

### Common Issues

1. **Services fail to start**:
   - Check task definition environment variables
   - Verify IAM role permissions
   - Check security group rules
   - Ensure database is accessible

1. **Services can't connect to database**:
   - Verify database endpoint in environment variables
   - Check database security group allows access
   - Confirm secrets are readable
   - Test SSL connectivity

1. **Query API not accessible via ALB**:
   - Check ALB security group rules
   - Verify target group health checks
   - Ensure service is registered with target group
   - Check task security group allows port 7101

1. **High memory usage**:
   - Review task definitions for appropriate sizing
   - Check for memory leaks in logs
   - Consider scaling horizontally

1. **S3 access denied**:
   - Verify task role has S3 permissions
   - Check bucket policy
   - Ensure correct bucket name in environment

## Notes

- Always use private subnets for ECS tasks
- Enable VPC endpoints for S3 and other AWS services
- Implement proper log retention policies
- Use tags for organization and management
- Regular backup of configuration and secrets
- Test disaster recovery procedures
