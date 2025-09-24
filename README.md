# Lakerunner CloudFormation Deployment

This repository contains CloudFormation templates for deploying the core Lakerunner platform on AWS ECS using Fargate. Pre-generated templates are available in the `generated-templates/` directory for immediate use.

## Architecture

The core Lakerunner deployment consists of CloudFormation stacks that can be deployed in order:

**Option 1: Use Existing VPC**
1. **Common Infrastructure** (`lakerunner-common.yaml`) - RDS database, EFS, S3 bucket, SQS queue, and ALB
2. **ECS Setup** (`lakerunner-ecs-setup.yaml`) - Database and Kafka setup task that runs once during initial setup
3. **ECS Services** (`lakerunner-ecs-services.yaml`) - ECS Fargate services for all Lakerunner microservices

**Option 2: Create New VPC (Recommended for POCs)**
1. **VPC Infrastructure** (`lakerunner-vpc.yaml`) - Cost-optimized VPC with essential VPC endpoints
2. **Common Infrastructure** (`lakerunner-common.yaml`) - RDS database, EFS, S3 bucket, SQS queue, and ALB
3. **ECS Setup** (`lakerunner-ecs-setup.yaml`) - Database and Kafka setup task that runs once during initial setup
4. **ECS Services** (`lakerunner-ecs-services.yaml`) - ECS Fargate services for all Lakerunner microservices

## VPC Template (For POCs without existing VPC)

If you don't have a VPC ready, use the `lakerunner-vpc.yaml` template to create a cost-optimized VPC for your POC deployment:

### Security Design
- **NAT Gateway**: For ECS nodes to pull container images and reach internet services
- **VPC Endpoint - Secrets Manager**: For RDS password retrieval without internet routing
- **VPC Endpoint - CloudWatch Logs**: For ECS logging without internet egress charges
- **VPC Endpoint - ECS/ECR**: For container orchestration and image pulls from private subnets
- **S3 Gateway Endpoint**: For S3 access without internet data transfer costs
- **Private subnets only**: All Lakerunner services run without public IP addresses
- **Security groups**: VPC endpoints restricted to HTTPS (port 443) from VPC CIDR only

### Cost Optimization
- **Single NAT Gateway**: Shared across AZs instead of per-AZ (saves ~$45/month)
- **Minimal VPC endpoints**: Only essential AWS services to reduce interface endpoint costs
- **Gateway endpoints where available**: S3 uses free gateway endpoint instead of paid interface endpoint

### Deployment

```bash
aws cloudformation create-stack \
  --stack-name lakerunner-vpc \
  --template-body file://generated-templates/lakerunner-vpc.yaml \
  --parameters ParameterKey=EnvironmentName,ParameterValue=lakerunner \
               ParameterKey=VpcCidr,ParameterValue=10.0.0.0/16
```

### Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `EnvironmentName` | String | No | lakerunner | Environment name for resource naming |
| `VpcCidr` | String | No | 10.0.0.0/16 | CIDR block for the VPC |
| `CreateNatGateway` | String | No | Yes | Create NAT Gateway for private subnet internet access |

### Outputs

The VPC template exports these values for use by other stacks:
- `VpcId` - VPC ID  
- `PublicSubnets` - Comma-separated public subnet IDs
- `PrivateSubnets` - Comma-separated private subnet IDs
- `VPCEndpointSecurityGroupId` - Security group for VPC endpoints

When using the VPC template, pass the exported values to the Common Infrastructure stack:

```bash
# Get VPC outputs
VPC_ID=$(aws cloudformation describe-stacks --stack-name lakerunner-vpc --query 'Stacks[0].Outputs[?OutputKey==`VpcId`].OutputValue' --output text)
PRIVATE_SUBNETS=$(aws cloudformation describe-stacks --stack-name lakerunner-vpc --query 'Stacks[0].Outputs[?OutputKey==`PrivateSubnets`].OutputValue' --output text)

# Deploy common infrastructure
aws cloudformation create-stack \
  --stack-name lakerunner-common \
  --template-body file://generated-templates/lakerunner-common.yaml \
  --parameters ParameterKey=VpcId,ParameterValue=$VPC_ID \
               ParameterKey=PrivateSubnets,ParameterValue=$PRIVATE_SUBNETS \
  --capabilities CAPABILITY_IAM
```

## Quick Start

Pre-generated CloudFormation templates are available in the `generated-templates/` directory. These are region and account agnostic, and should deploy to any AWS account where you have sufficient permissions.

### Step 1: Deploy Common Infrastructure

Deploy `generated-templates/lakerunner-common.yaml` using the AWS Console or CLI. Required parameters:

- **VpcId** – VPC where resources will be created
- **PrivateSubnets** – Private subnet IDs (for ECS/RDS/EFS). Provide at least two in different AZs.

Optional parameters:

- **PublicSubnets** – Public subnet IDs (required only for internet-facing ALB)
- **AlbScheme** – Load balancer scheme: "internal" (default) or "internet-facing"
- **ApiKeysOverride** – Custom API keys configuration in YAML format
- **StorageProfilesOverride** – Custom storage profiles configuration in YAML format

**Example AWS CLI deployment:**

```bash
aws cloudformation create-stack \\
  --stack-name lakerunner-common \\
  --template-body file://generated-templates/lakerunner-common.yaml \\
  --parameters ParameterKey=VpcId,ParameterValue=vpc-12345678 \\
               ParameterKey=PrivateSubnets,ParameterValue="subnet-12345678,subnet-87654321" \\
  --capabilities CAPABILITY_IAM
```

### Step 2: Deploy ECS Setup Task

Deploy `generated-templates/lakerunner-ecs-setup.yaml` to run database and Kafka setup. Required parameters:

- **CommonInfraStackName** – Name of the CommonInfra stack (e.g., "lakerunner-common")

Optional parameters:

- **ContainerImage** – Container image for setup task (default: public.ecr.aws/cardinalhq.io/lakerunner:latest)

**Example AWS CLI deployment:**

```bash
aws cloudformation create-stack \\
  --stack-name lakerunner-ecs-setup \\
  --template-body file://generated-templates/lakerunner-ecs-setup.yaml \\
  --parameters ParameterKey=CommonInfraStackName,ParameterValue=lakerunner-common \\
  --capabilities CAPABILITY_IAM
```

### Step 3: Deploy ECS Services

Deploy `generated-templates/lakerunner-ecs-services.yaml` for all Lakerunner microservices. Required parameters:

- **CommonInfraStackName** – Name of the CommonInfra stack (e.g., "lakerunner-common")

Optional parameters:

- **OtelEndpoint** – OTEL collector HTTP endpoint URL (e.g., http://collector-dns:4318). Leave blank to disable OTLP telemetry export.
- Container image overrides for air-gapped deployments:
  - **GoServicesImage** – Image for Go services (default: public.ecr.aws/cardinalhq.io/lakerunner:latest)
  - **QueryApiImage** – Image for query-api (default: public.ecr.aws/cardinalhq.io/lakerunner/query-api:latest)
  - **QueryWorkerImage** – Image for query-worker (default: public.ecr.aws/cardinalhq.io/lakerunner/query-worker:latest)
  - **GrafanaImage** – Image for Grafana (default: grafana/grafana:latest)

**Example AWS CLI deployment:**

```bash
aws cloudformation create-stack \\
  --stack-name lakerunner-ecs-services \\
  --template-body file://generated-templates/lakerunner-ecs-services.yaml \\
  --parameters ParameterKey=CommonInfraStackName,ParameterValue=lakerunner-common \\
  --capabilities CAPABILITY_IAM
```

## Updating Container Images

When updating existing CloudFormation stacks, container image versions are not automatically updated to the new defaults. You must explicitly specify the image parameters during stack updates.

### Current Image Versions

- **Go Services**: `public.ecr.aws/cardinalhq.io/lakerunner:v1.2.1`
- **Query API**: `public.ecr.aws/cardinalhq.io/lakerunner/query-api:v1.2.1`  
- **Query Worker**: `public.ecr.aws/cardinalhq.io/lakerunner/query-worker:v1.2.1`
- **Migration**: `public.ecr.aws/cardinalhq.io/lakerunner:v1.2.1`

### Update Services Stack

```bash
aws cloudformation update-stack \\
  --stack-name lakerunner-services \\
  --template-body file://generated-templates/lakerunner-services.yaml \\
  --parameters \\
    ParameterKey=CommonInfraStackName,UsePreviousValue=true \\
    ParameterKey=GoServicesImage,ParameterValue=public.ecr.aws/cardinalhq.io/lakerunner:v1.2.1 \\
    ParameterKey=QueryApiImage,ParameterValue=public.ecr.aws/cardinalhq.io/lakerunner/query-api:v1.2.1 \\
    ParameterKey=QueryWorkerImage,ParameterValue=public.ecr.aws/cardinalhq.io/lakerunner/query-worker:v1.2.1 \\
  --capabilities CAPABILITY_IAM
```

### Update Migration Stack

```bash
aws cloudformation update-stack \\
  --stack-name lakerunner-migration \\
  --template-body file://generated-templates/lakerunner-migration.yaml \\
  --parameters \\
    ParameterKey=CommonInfraStackName,UsePreviousValue=true \\
    ParameterKey=ContainerImage,ParameterValue=public.ecr.aws/cardinalhq.io/lakerunner:v1.2.1 \\
  --capabilities CAPABILITY_IAM
```

### Helper Script

For convenience, you can use the provided helper script:

```bash
# Update services stack
./update-images.sh lakerunner-services services

# Update migration stack  
./update-images.sh lakerunner-migration migration
```

## OTLP Telemetry Support

The services stack supports optional OTLP (OpenTelemetry Protocol) telemetry export. When enabled, all Go services will export logs and metrics to the specified collector endpoint.

### Configuration

Add the `OtelEndpoint` parameter when deploying the services stack:

```bash
# Deploy services with OTLP telemetry enabled
aws cloudformation create-stack \
  --stack-name lakerunner-services \
  --template-body file://generated-templates/lakerunner-services.yaml \
  --parameters ParameterKey=CommonInfraStackName,ParameterValue=lakerunner-common \
               ParameterKey=OtelEndpoint,ParameterValue=http://collector-dns:4318 \
  --capabilities CAPABILITY_IAM
```

### Getting the Collector Endpoint

**External Collector**

For collectors outside the ECS cluster, provide the full endpoint URL:

- `http://my-collector.example.com:4318` - External HTTP collector
- `http://internal-lb-dns:4318` - Internal load balancer
- `http://10.0.1.100:4318` - Direct IP address

### Environment Variables

When `OtelEndpoint` is provided, these environment variables are automatically added to all Go services:

- `OTEL_EXPORTER_OTLP_ENDPOINT` - The collector endpoint URL
- `ENABLE_OTLP_TELEMETRY=true` - Enables telemetry export in the application

### Deployment Order

Deploy in this order:

1. `lakerunner-common` - Core infrastructure
2. `lakerunner-migration` - Database migration
3. `lakerunner-services` - Core services
4. `lakerunner-grafana-service` - Grafana dashboard (optional)

## Access Points

After successful deployment:

- **Grafana Dashboard**: Access via ALB DNS name on port 3000
  - Username: `lakerunner`
  - Password: Retrieve from AWS Secrets Manager using the GrafanaAdminSecretArn output
- **Query API**: Access via ALB DNS name on port 7101
- **S3 Bucket**: Upload data to the created bucket with appropriate prefixes

### Retrieving Grafana Password

```bash
# Get the secret ARN from stack outputs
SECRET_ARN=$(aws cloudformation describe-stacks \
  --stack-name lakerunner-services \
  --query 'Stacks[0].Outputs[?OutputKey==`GrafanaAdminSecretArn`].OutputValue' \
  --output text)

# Retrieve the password
aws secretsmanager get-secret-value \
  --secret-id $SECRET_ARN \
  --query 'SecretString' \
  --output text | jq -r '.password'
```

### Grafana Password Recovery

If you lose access to Grafana (forgot password, account locked, etc.), you can reset the entire Grafana configuration using the **Grafana Reset Token** feature.

#### How It Works

The `GrafanaResetToken` parameter allows you to wipe all Grafana data and start fresh with the original admin credentials. When you change this parameter value, Grafana will:

1. Delete all existing Grafana data (dashboards, users, settings, database)
2. Start with a clean database
3. Use the original admin credentials (`lakerunner` username with password from AWS Secrets Manager)

#### Reset Procedure

**To reset Grafana:**

```bash
# Update the stack with a reset token (use any unique value)
aws cloudformation update-stack \
  --stack-name lakerunner-services \
  --use-previous-template \
  --parameters \
    ParameterKey=CommonInfraStackName,UsePreviousValue=true \
    ParameterKey=GrafanaResetToken,ParameterValue="reset-$(date +%s)" \
  --capabilities CAPABILITY_IAM
```

**To return to normal operation (prevent accidental resets):**

```bash
# Clear the reset token after successful reset
aws cloudformation update-stack \
  --stack-name lakerunner-services \
  --use-previous-template \
  --parameters \
    ParameterKey=CommonInfraStackName,UsePreviousValue=true \
    ParameterKey=GrafanaResetToken,ParameterValue="" \
  --capabilities CAPABILITY_IAM
```

#### Reset Token Behavior

- **Empty token** (default): Normal operation, preserves all Grafana data
- **New token value**: Wipes Grafana data and resets to fresh state with original admin credentials
- **Same token value**: No action taken, data preserved
- **Token tracking**: The system remembers the last reset token to prevent accidental resets

#### Example Use Cases

1. **Password Recovery**: Set reset token, deploy, access with original credentials, clear token
2. **Clean Demo Environment**: Use reset token to quickly restore demo state
3. **Development Reset**: Reset during development to test fresh configurations
4. **User Management Reset**: Remove all custom users and return to single admin account

## Load Balancer Configuration

An Application Load Balancer is always created and points to:

- `query-api` service on port 7101
- `grafana` service on port 3000

The ALB can be configured as:

- **Internal** (default) - Only accessible from within the VPC
- **Internet-facing** - Accessible from the internet (requires public subnets)

## Services Architecture

Lakerunner consists of these microservices that process telemetry data:

- **pubsub-sqs** - Receives SQS notifications from S3 bucket
- **ingest-logs/metrics** - Process raw log and metric files
- **compact-logs/metrics** - Optimize storage format
- **rollup-metrics** - Pre-aggregate metrics for faster queries
- **sweeper** - Clean up temporary files
- **query-api** - REST API for data queries (ALB-attached)
- **query-worker** - Query execution engine
- **grafana** - Visualization dashboard (ALB-attached)

All services share:

- Common ECS task execution and task roles
- Unified secret injection from Secrets Manager and SSM
- Standardized logging to CloudWatch
- EFS mount for shared scratch space (/scratch)
- Health checks appropriate to service type (Go, Scala, cURL)
- Optional OTLP telemetry export to OpenTelemetry collectors

## Additional Documentation

This repository includes several specialized guides:

- **[OTEL Collector](README-OTEL-COLLECTOR.md)** - Dedicated telemetry ingestion service setup and configuration
- **[Building from Source](README-BUILDING.md)** - Development guide for modifying and building CloudFormation templates

## Stack Dependencies and Deployment Order

### All Available Stacks

This repository provides **4 CloudFormation stacks** with specific dependencies:

1. **Common Infrastructure** (`lakerunner-common.yaml`) - Core infrastructure
2. **Migration** (`lakerunner-migration.yaml`) - Database migration task
3. **Services** (`lakerunner-services.yaml`) - Core Lakerunner services
4. **Grafana Service** (`lakerunner-grafana-service.yaml`) - Optional Grafana dashboard

### Dependency Diagram

```
lakerunner-common (required)
├── lakerunner-migration (required after common)
├── lakerunner-services (required after common)
└── lakerunner-grafana-service (optional, after common + services)
```

### Deployment Scenarios

#### Minimal Deployment (Core Platform)

```bash
# 1. Core infrastructure
aws cloudformation create-stack --stack-name lakerunner-common ...

# 2. Database migration
aws cloudformation create-stack --stack-name lakerunner-migration ...

# 3. Services
aws cloudformation create-stack --stack-name lakerunner-services ...
```

#### Full Deployment (With Grafana Dashboard)

```bash
# 1. Core infrastructure
aws cloudformation create-stack --stack-name lakerunner-common ...

# 2. Database migration
aws cloudformation create-stack --stack-name lakerunner-migration ...

# 3. Services
aws cloudformation create-stack --stack-name lakerunner-services ...

# 4. Grafana dashboard (optional)
aws cloudformation create-stack --stack-name lakerunner-grafana \
  --parameters ParameterKey=CommonInfraStackName,ParameterValue="lakerunner-common" \
               ParameterKey=ServicesStackName,ParameterValue="lakerunner-services" ...
```

### Cross-Stack Resource Sharing

**Common Infrastructure exports:**

- VPC ID, Private/Public Subnets
- ECS Cluster ARN
- Database endpoint, port, credentials ARN
- EFS filesystem ID
- S3 bucket name/ARN
- Security groups

**Services imports from Common:**

- All infrastructure resources
- Database credentials for app configuration
- EFS for Grafana persistence

**Migration imports from Common:**

- Database connection details
- ECS cluster for task execution
- Network configuration

**OTEL Collector imports from Common:**

- VPC and networking for ALB
- ECS cluster for service deployment

**Demo Apps imports from:**

- Common: ECS cluster, networking, security groups
- Services: Task execution role, security groups
- OTEL Collector: Telemetry endpoint URL

## Complete Parameter Reference

### Common Infrastructure Stack Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `VpcId` | AWS::EC2::VPC::Id | Yes | - | VPC where resources will be created |
| `PrivateSubnets` | List<AWS::EC2::Subnet::Id> | Yes | - | Private subnet IDs for ECS/RDS/EFS (≥2 in different AZs) |
| `PublicSubnets` | List<AWS::EC2::Subnet::Id> | No | - | Public subnet IDs for internet-facing ALB (≥2 in different AZs) |
| `ApiKeysOverride` | String | No | "" | Custom API keys configuration in YAML format |
| `StorageProfilesOverride` | String | No | "" | Custom storage profiles configuration in YAML format |

### Migration Stack Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `CommonInfraStackName` | String | Yes | - | Name of the CommonInfra stack to import values from |
| `ContainerImage` | String | No | public.ecr.aws/cardinalhq.io/lakerunner:v1.2.1 | Migration container image |
| `Cpu` | String | No | "512" | Fargate CPU units (256/512/1024/2048/4096) |
| `MemoryMiB` | String | No | "1024" | Fargate Memory in MiB (512/1024/2048/3072/4096/5120/6144/7168/8192) |

### Services Stack Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `CommonInfraStackName` | String | Yes | - | Name of the CommonInfra stack to import values from |
| `AlbScheme` | String | No | "internal" | ALB scheme: "internal" or "internet-facing" |
| `OtelEndpoint` | String | No | "" | OTEL collector HTTP endpoint (e.g., http://collector-dns:4318) |
| `GrafanaResetToken` | String | No | "" | Change this value to reset Grafana data (wipe EFS volume) |
| `GoServicesImage` | String | No | public.ecr.aws/cardinalhq.io/lakerunner:v1.2.1 | Container image for Go services |
| `QueryApiImage` | String | No | public.ecr.aws/cardinalhq.io/lakerunner/query-api:latest | Container image for query-api service |
| `QueryWorkerImage` | String | No | public.ecr.aws/cardinalhq.io/lakerunner/query-worker:latest | Container image for query-worker service |
| `GrafanaImage` | String | No | grafana/grafana:latest | Container image for Grafana service |

### OTEL Collector Stack Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `CommonInfraStackName` | String | Yes | - | Name of the CommonInfra stack to import values from |
| `LoadBalancerType` | String | No | "internal" | ALB type: "internal" or "internet-facing" |
| `OrganizationId` | String | No | 12340000-0000-4000-8000-000000000000 | Organization ID for OTEL data routing |
| `CollectorName` | String | No | "lakerunner" | Collector name for OTEL data routing |
| `OtelCollectorImage` | String | No | public.ecr.aws/cardinalhq.io/cardinalhq-otel-collector:latest | OTEL collector container image |
| `OtelConfigYaml` | String | No | "" | Custom OTEL collector configuration in YAML format |

### Demo Sample Apps Stack Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `CommonInfraStackName` | String | Yes | - | Name of the CommonInfra stack to import values from |
| `ServicesStackName` | String | Yes | - | Name of the Services stack to import ALB target groups from |
| `OtelCollectorStackName` | String | Yes | - | Name of the OTEL Collector stack to get collector endpoint from |
| `SampleAppImage` | String | No | public.ecr.aws/cardinalhq.io/lakerunner-demo/sample-app:latest | Container image for sample-app service |

## Configuration

Default configurations are stored in `lakerunner-stack-defaults.yaml` and include:

- **API Keys** - Default organization and API key configurations
- **Storage Profiles** - S3 storage configuration for telemetry data
- **Container Images** - Default image locations for all services
- **Service Settings** - CPU, memory, replica counts, and environment variables

## Security Best Practices

- Database credentials stored in AWS Secrets Manager
- Application secrets (HMAC keys, Grafana passwords) auto-generated
- ECS task roles follow principle of least privilege
- All tasks run in private subnets with no public IP assignment
- SSL/TLS encryption for database connections (`LRDB_SSLMODE: require`)

## Troubleshooting

- All services log to CloudWatch under `/ecs/<service-name>`
- Health checks can be monitored via ECS console
- Database connectivity issues often indicate security group or subnet configuration problems
- ALB target health can be checked via EC2 console under Load Balancers

## Documentation Index

- **[README.md](README.md)** - Main deployment guide (this document)
- **[README-DEMO-APPS.md](README-DEMO-APPS.md)** - Demo applications and testing
- **[README-OTEL-COLLECTOR.md](README-OTEL-COLLECTOR.md)** - OTEL collector setup
- **[README-BUILDING.md](README-BUILDING.md)** - Building and development guide
