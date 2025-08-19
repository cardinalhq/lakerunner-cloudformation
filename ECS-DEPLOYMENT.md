# Lakerunner ECS Deployment Guide

This guide covers deploying Lakerunner on AWS using ECS Fargate with CloudFormation.

## Overview

The ECS deployment consists of four CloudFormation stacks that must be deployed in order:

1. **CommonInfra** - Core infrastructure (VPC, ECS cluster, database, S3, SQS)
2. **Migration** - Database migration task
3. **Services** - ECS Fargate services for Lakerunner
4. **Grafana** - Grafana service for monitoring (optional)

## Prerequisites

- AWS CLI configured with appropriate permissions
- Python 3.8+ with virtual environment
- VPC with private and public subnets (or use the templates to create one)

## Quick Start

1. **Generate Templates**
   ```bash
   ./build.sh
   ```
   Templates will be generated in `generated-templates/ecs/`

2. **Deploy CommonInfra Stack**
   ```bash
   aws cloudformation create-stack \
     --stack-name lakerunner-common \
     --template-body file://generated-templates/ecs/lakerunner-common.yaml \
     --parameters \
       ParameterKey=VpcId,ParameterValue=vpc-xxxxxxxx \
       ParameterKey=PrivateSubnets,ParameterValue="subnet-xxxxxxxx,subnet-yyyyyyyy" \
     --capabilities CAPABILITY_IAM
   ```

3. **Wait for CommonInfra to Complete**
   ```bash
   aws cloudformation wait stack-create-complete --stack-name lakerunner-common
   ```

4. **Deploy Migration Stack**
   ```bash
   aws cloudformation create-stack \
     --stack-name lakerunner-migration \
     --template-body file://generated-templates/ecs/lakerunner-migration.yaml \
     --parameters \
       ParameterKey=CommonInfraStackName,ParameterValue=lakerunner-common \
     --capabilities CAPABILITY_IAM
   ```

5. **Wait for Migration to Complete**
   ```bash
   aws cloudformation wait stack-create-complete --stack-name lakerunner-migration
   ```

6. **Deploy Services Stack**
   ```bash
   aws cloudformation create-stack \
     --stack-name lakerunner-services \
     --template-body file://generated-templates/ecs/lakerunner-services.yaml \
     --parameters \
       ParameterKey=CommonInfraStackName,ParameterValue=lakerunner-common \
     --capabilities CAPABILITY_IAM
   ```

7. **Deploy Grafana Stack (Optional)**
   ```bash
   aws cloudformation create-stack \
     --stack-name lakerunner-grafana \
     --template-body file://generated-templates/ecs/lakerunner-grafana-service.yaml \
     --parameters \
       ParameterKey=CommonInfraStackName,ParameterValue=lakerunner-common \
     --capabilities CAPABILITY_IAM
   ```

## Configuration

### Default Configuration

The deployment uses defaults from `lakerunner-stack-defaults.yaml`. You can override these by providing parameters:

- **ApiKeysOverride** - JSON string with custom API keys
- **StorageProfilesOverride** - JSON string with custom storage profiles

### Container Images

Override default container images with these parameters:

- **GoServicesImage** - Lakerunner Go services image
- **QueryApiImage** - Query API service image  
- **QueryWorkerImage** - Query worker service image
- **MigrationImage** - Database migration image
- **GrafanaImage** - Grafana image

### ALB Configuration

The CommonInfra stack can optionally create an Application Load Balancer:

- **CreateAlb** - Set to "Yes" to create ALB (default: "No")
- **AlbScheme** - "internet-facing" or "internal" (default: "internal")
- **PublicSubnets** - Required if AlbScheme is "internet-facing"

## Security

- All ECS tasks run in private subnets with no public IP
- Database credentials stored in AWS Secrets Manager
- Application secrets auto-generated (HMAC keys, Grafana passwords)
- Database connections use SSL (LRDB_SSLMODE: require)
- IAM roles follow principle of least privilege

## Networking

The deployment creates or uses:

- **VPC** - Existing VPC (provided as parameter)
- **Private Subnets** - For ECS tasks and database
- **Public Subnets** - For ALB (if internet-facing)
- **Security Groups** - Restrict traffic between components
- **NAT Gateway** - For private subnet internet access

## Monitoring

Services automatically log to CloudWatch with log groups:

- `/ecs/lakerunner-pubsub-sqs`
- `/ecs/lakerunner-query-api`
- `/ecs/lakerunner-query-worker`
- `/ecs/grafana` (if deployed)

## Scaling

ECS services are configured with:

- **Auto Scaling** - Based on CPU/memory utilization
- **Health Checks** - Application-specific health endpoints
- **Rolling Updates** - Zero-downtime deployments

## Storage

- **S3 Bucket** - For data lake storage with lifecycle policies
- **SQS Queue** - For event processing with S3 notifications
- **RDS PostgreSQL** - For metadata and query results

## Troubleshooting

### Common Issues

1. **Stack Creation Fails**
   - Check IAM permissions
   - Verify VPC and subnet parameters
   - Review CloudFormation events

2. **Services Won't Start**
   - Check ECS service events
   - Review CloudWatch logs
   - Verify database connectivity

3. **Database Connection Issues**
   - Ensure security groups allow PostgreSQL traffic
   - Check database credentials in Secrets Manager
   - Verify SSL configuration

### Useful Commands

```bash
# Check stack status
aws cloudformation describe-stacks --stack-name lakerunner-common

# View ECS service status
aws ecs describe-services --cluster lakerunner-cluster --services lakerunner-pubsub-sqs

# Check logs
aws logs describe-log-groups --log-group-name-prefix "/ecs/lakerunner"
```

## Cleanup

To remove the deployment:

```bash
# Delete stacks in reverse order
aws cloudformation delete-stack --stack-name lakerunner-grafana
aws cloudformation delete-stack --stack-name lakerunner-services
aws cloudformation delete-stack --stack-name lakerunner-migration
aws cloudformation delete-stack --stack-name lakerunner-common
```

## Air-Gapped Deployments

For environments without internet access:

1. Pull container images to private ECR repositories
2. Override image parameters with private ECR URLs
3. Configure VPC endpoints for AWS services
4. Use NAT instances instead of NAT gateways if needed

## Custom Configuration

To modify service configurations:

1. Edit `lakerunner-stack-defaults.yaml`
2. Regenerate templates with `./build.sh`
3. Update stacks with new templates

For advanced customization, modify the Python template files in `src/ecs/`.