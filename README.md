# CardinalHQ's Lakerunner CloudFormation Templates

This repository contains CloudFormation templates for deploying Lakerunner on AWS ECS using Fargate. Pre-generated templates are available in the `troposphere/out/` directory for immediate use.

## Architecture

The deployment consists of three CloudFormation stacks that must be deployed in order:

1. **CommonInfra** - Core infrastructure (VPC resources, RDS, EFS, S3, SQS, ALB)
2. **Migration** - Database migration task (runs once during initial setup)  
3. **Services** - ECS Fargate services for all Lakerunner microservices

## Requirements

1. **AWS Account** with appropriate permissions to create IAM roles, VPC resources, RDS, ECS, etc.
2. **Existing VPC** with:
   - At least 2 private subnets in different AZs (for RDS, ECS, EFS)
   - At least 2 public subnets in different AZs (only required for internet-facing ALB)

## Installation

Pre-generated CloudFormation templates are available in the `troposphere/out/` directory. These are region and account agnostic, and should deploy to any AWS account where you have sufficient permissions.

### Deploy the stacks in this order:

1. `common_infra.yaml` (suggested stack name: "lakerunner-common")
2. `migration_task.yaml` (suggested stack name: "lakerunner-migration") 
3. `services.yaml` (suggested stack name: "lakerunner-services")

## Deployment Steps

### Step 1: Deploy CommonInfra Stack

Deploy `out/common_infra.yaml` using the AWS Console or CLI. Required parameters:

- **VpcId** – The existing VPC ID to deploy into  
- **PrivateSubnets** – List of private subnet IDs (minimum 2, different AZs)
- **PublicSubnets** – List of public subnet IDs (minimum 2, different AZs, only required for internet-facing ALB)

Optional parameters:

- **AlbScheme** – Load balancer scheme: "internal" (default) or "internet-facing"
- **ApiKeysOverride** – Custom API keys YAML (leave blank to use defaults)
- **StorageProfilesOverride** – Custom storage profiles YAML (leave blank to use defaults)

**Example AWS CLI deployment:**

```bash
aws cloudformation create-stack \
  --stack-name lakerunner-common \
  --template-body file://out/common_infra.yaml \
  --parameters ParameterKey=VpcId,ParameterValue=vpc-12345678 \
               ParameterKey=PrivateSubnets,ParameterValue="subnet-private1,subnet-private2" \
               ParameterKey=PublicSubnets,ParameterValue="subnet-public1,subnet-public2" \
               ParameterKey=AlbScheme,ParameterValue=internal \
  --capabilities CAPABILITY_IAM
```

Wait for stack creation to complete before proceeding.

### Step 2: Deploy Migration Stack

Deploy `out/migration_task.yaml` to run database migrations. Required parameters:

- **CommonInfraStackName** – Name of the CommonInfra stack (e.g., "lakerunner-common")

Optional parameters:

- **ContainerImage** – Migration container image (for air-gapped deployments)
- **Cpu** – Fargate CPU units (default: 512)
- **MemoryMiB** – Fargate memory MiB (default: 1024)

**Example AWS CLI deployment:**

```bash
aws cloudformation create-stack \
  --stack-name lakerunner-migration \
  --template-body file://out/migration_task.yaml \
  --parameters ParameterKey=CommonInfraStackName,ParameterValue=lakerunner-common \
  --capabilities CAPABILITY_IAM
```

The migration task will run automatically and the stack will complete when migrations finish successfully.

### Step 3: Deploy Services Stack

Deploy `out/services.yaml` for all Lakerunner microservices. Required parameters:

- **CommonInfraStackName** – Name of the CommonInfra stack (e.g., "lakerunner-common")

Optional parameters (for air-gapped deployments):

- **GoServicesImage** – Container image for Go services (default: public.ecr.aws/cardinalhq.io/lakerunner:latest)
- **QueryApiImage** – Container image for query-api (default: public.ecr.aws/cardinalhq.io/lakerunner/query-api:latest)
- **QueryWorkerImage** – Container image for query-worker (default: public.ecr.aws/cardinalhq.io/lakerunner/query-worker:latest)
- **GrafanaImage** – Container image for Grafana (default: grafana/grafana:latest)

**Example AWS CLI deployment:**

```bash
aws cloudformation create-stack \
  --stack-name lakerunner-services \
  --template-body file://out/services.yaml \
  --parameters ParameterKey=CommonInfraStackName,ParameterValue=lakerunner-common \
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
- **Internet-facing** - Accessible from the internet via public subnets

No TLS is configured by default.

## Air-Gapped Deployments

For air-gapped environments:

1. Push required container images to your private registry
2. Override image parameters in Migration and Services stacks
3. Ensure your private registry is accessible from the VPC

## Resources Created

The CloudFormation templates create the following AWS resources:

1. **IAM roles** - Task execution and task roles for ECS services
2. **Security groups** - For ALB, ECS tasks, RDS, and EFS access
3. **RDS PostgreSQL database** - Single instance sized for POC/testing use
4. **ECS cluster** - Fargate cluster hosting all Lakerunner microservices
5. **Elastic File System (EFS)** - Shared storage for Grafana data persistence
6. **S3 bucket** - Data storage with lifecycle rules and SQS notifications
7. **SQS queue** - Receives S3 object creation notifications
8. **Application Load Balancer** - Routes traffic to query-api and Grafana services

## Using Lakerunner

After deployment, you can start ingesting data by uploading files to the S3 bucket:

- **`otel-raw/`** prefix - OTEL collector Parquet files
- **`logs-raw/`** prefix - Log files for ingestion
- **`metrics-raw/`** prefix - Metric files for ingestion

Lakerunner will process these files and create indexed Parquet files under the `db/` prefix.

**Note**: It's recommended to set up S3 lifecycle rules for the raw data prefixes to manage storage costs.

## Development

If you need to modify the CloudFormation templates, you can regenerate them using the Python-based Troposphere scripts.

### Prerequisites

- Python 3.8 or newer
- `pip` package manager

### Generating Templates

1. Navigate to the troposphere directory:
   ```bash
   cd troposphere/
   ```

2. Generate all CloudFormation templates:
   ```bash
   ./build.sh
   ```

   This will:
   - Create a Python virtual environment
   - Install dependencies from `requirements.txt`
   - Generate templates in `out/` directory:
     - `out/common_infra.yaml`
     - `out/migration_task.yaml` 
     - `out/services.yaml`
   - Validate templates with `cfn-lint`

### Template Structure

- **`common_infra.py`** - Core infrastructure template
- **`migration_task.py`** - Database migration template
- **`services.py`** - ECS services template
- **`defaults.yaml`** - Configuration defaults for services and API keys
- **`build.sh`** - Build script that generates and validates all templates

### Modifying Templates

When making changes:

1. Edit the Python template files (`.py`)
2. Update `defaults.yaml` if adding new services or configurations
3. Run `./build.sh` to regenerate and validate templates
4. Test deploy the generated YAML files
