# Lakerunner CloudFormation Deployment

This repository contains CloudFormation templates for deploying the core Lakerunner platform on AWS ECS using Fargate. Pre-generated templates are available in the `generated-templates/` directory for immediate use.

## Architecture

The core Lakerunner deployment consists of three CloudFormation stacks that must be deployed in order:

1. **Common Infrastructure** (`lakerunner-common.yaml`) - VPC resources, RDS database, EFS, S3 bucket, SQS queue, and ALB
2. **Migration** (`lakerunner-migration.yaml`) - Database migration task that runs once during initial setup  
3. **Services** (`lakerunner-services.yaml`) - ECS Fargate services for all Lakerunner microservices

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

### Step 2: Deploy Migration Task

Deploy `generated-templates/lakerunner-migration.yaml` to run database migrations. Required parameters:

- **CommonInfraStackName** – Name of the CommonInfra stack (e.g., "lakerunner-common")

Optional parameters:
- **MigrationImage** – Container image for migration task (default: public.ecr.aws/cardinalhq.io/lakerunner:latest)

**Example AWS CLI deployment:**

```bash
aws cloudformation create-stack \\
  --stack-name lakerunner-migration \\
  --template-body file://generated-templates/lakerunner-migration.yaml \\
  --parameters ParameterKey=CommonInfraStackName,ParameterValue=lakerunner-common \\
  --capabilities CAPABILITY_IAM
```

### Step 3: Deploy Services

Deploy `generated-templates/lakerunner-services.yaml` for all Lakerunner microservices. Required parameters:

- **CommonInfraStackName** – Name of the CommonInfra stack (e.g., "lakerunner-common")

Optional parameters:
- Container image overrides for air-gapped deployments:
  - **GoServicesImage** – Image for Go services (default: public.ecr.aws/cardinalhq.io/lakerunner:latest)
  - **QueryApiImage** – Image for query-api (default: public.ecr.aws/cardinalhq.io/lakerunner/query-api:latest)
  - **QueryWorkerImage** – Image for query-worker (default: public.ecr.aws/cardinalhq.io/lakerunner/query-worker:latest)
  - **GrafanaImage** – Image for Grafana (default: grafana/grafana:latest)

**Example AWS CLI deployment:**

```bash
aws cloudformation create-stack \\
  --stack-name lakerunner-services \\
  --template-body file://generated-templates/lakerunner-services.yaml \\
  --parameters ParameterKey=CommonInfraStackName,ParameterValue=lakerunner-common \\
  --capabilities CAPABILITY_IAM
```

## Access Points

After successful deployment:

- **Grafana Dashboard**: Access via ALB DNS name on port 3000
  - Username: `admin`
  - Password: Retrieve from AWS Secrets Manager using the GrafanaAdminSecretArn output
- **Query API**: Access via ALB DNS name on port 7101
- **S3 Bucket**: Upload data to the created bucket with appropriate prefixes

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

## Build System

If you need to modify the templates, use the included build system:

### Requirements

- Python 3.7+
- Virtual environment support

### Commands

1. Navigate to the repository root directory:
   ```bash
   cd lakerunner-cloudformation/
   ```

2. Generate all templates:
   ```bash
   ./build.sh
   ```

   This will:
   - Create a Python virtual environment
   - Install dependencies from `requirements.txt`
   - Generate templates in `generated-templates/` directory:
     - `generated-templates/lakerunner-common.yaml`
     - `generated-templates/lakerunner-migration.yaml` 
     - `generated-templates/lakerunner-services.yaml`
   - Validate templates with `cfn-lint`

### Template Structure

- **`src/common_infra.py`** - Core infrastructure template
- **`src/migration_task.py`** - Database migration template
- **`src/services.py`** - ECS services template
- **`lakerunner-stack-defaults.yaml`** - Configuration defaults for services and API keys
- **`build.sh`** - Build script that generates and validates all templates

## Demo Applications

For testing telemetry collection, see [README-DEMO-APPS.md](README-DEMO-APPS.md) for OTEL-instrumented demo applications and OTEL collector setup.

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