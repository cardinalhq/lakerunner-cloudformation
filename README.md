# CardinalHQ's Lakerunner CloudFormation Templates

This repository contains Python-based CloudFormation templates for deploying Lakerunner on AWS ECS using Fargate. The templates are generated using Troposphere for better maintainability and air-gapped deployment support.

## Architecture

The deployment consists of three CloudFormation stacks that must be deployed in order:

1. **CommonInfra** - Core infrastructure (VPC resources, RDS, EFS, S3, SQS, optional ALB)
2. **Migration** - Database migration task (runs once during initial setup)
3. **Services** - ECS Fargate services for all Lakerunner microservices

## Prerequisites

1. **AWS Account** with appropriate permissions to create IAM roles, VPC resources, RDS, ECS, etc.
2. **Existing VPC** with:
   - At least 2 private subnets in different AZs (for RDS, ECS, EFS)
   - At least 2 public subnets in different AZs (for ALB, if enabled)
3. **Python 3.8+** and `pip` installed locally

## Generate Templates

1. Navigate to the troposphere directory:

   ```bash
   cd troposphere/
   ```

2. Generate all CloudFormation templates:

   ```bash
   ./build.sh
   ```

   This will create a virtual environment, install dependencies, and generate templates in `out/`:
   - `out/common_infra.yaml`
   - `out/migration_task.yaml`
   - `out/services.yaml`

## Deployment Steps

### Step 1: Deploy CommonInfra Stack

Deploy `out/common_infra.yaml` using the AWS Console or CLI. Required parameters:

- **VpcId** – The existing VPC ID to deploy into
- **PrivateSubnets** – List of private subnet IDs (minimum 2, different AZs)
- **PublicSubnets** – List of public subnet IDs (minimum 2, different AZs)

Optional parameters:

- **CreateAlb** – Set to "No" to skip ALB creation (default: "Yes")
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
- **CreateAlb** – Must match the CommonInfra CreateAlb setting ("Yes" or "No")

Optional parameters (for air-gapped deployments):

- **GoServicesImage** – Container image for Go services (default: public.ecr.aws/cardinalhq.io/lakerunner:latest)
- **QueryApiImage** – Container image for query-api (default: public.ecr.aws/cardinalhq.io/lakerunner/query-api:latest-dev)
- **QueryWorkerImage** – Container image for query-worker (default: public.ecr.aws/cardinalhq.io/lakerunner/query-worker:latest-dev)
- **GrafanaImage** – Container image for Grafana (default: grafana/grafana:latest)

**Example AWS CLI deployment:**

```bash
aws cloudformation create-stack \
  --stack-name lakerunner-services \
  --template-body file://out/services.yaml \
  --parameters ParameterKey=CommonInfraStackName,ParameterValue=lakerunner-common \
               ParameterKey=CreateAlb,ParameterValue=Yes \
  --capabilities CAPABILITY_IAM
```

## Access Points

After successful deployment:

- **Grafana Dashboard**: Access via ALB DNS name on port 3000 (if ALB enabled)
  - Username: `admin`
  - Password: Retrieve from AWS Secrets Manager using the GrafanaAdminSecretArn output
- **Query API**: Access via ALB DNS name on port 7101 (if ALB enabled)
- **S3 Bucket**: Upload data to the created bucket with appropriate prefixes

## Air-Gapped Deployments

For air-gapped environments:

1. Push required container images to your private registry
2. Override image parameters in Migration and Services stacks
3. Ensure your private registry is accessible from the VPC

## Load Balancer Configuration

When ALB is enabled (default), an application load balancer is created that points to:

- `query-api` service on port 7101
- `grafana` service on port 3000

No TLS is configured by default.

## Resources Created

1. Various IAM roles
1. Various security groups.
1. One RDS PostgreSQL cluster with a single node, sized in a small range for POC use.
1. One ECS cluster.
1. Various services deployed into that ECS cluster excuting on Fargate.
1. One Elastic File System, which will hold a small amount of durable data for Grafana.
1. One S3 bucket.
1. One SQS queue to receive notifications from that bucket.

## Next Steps

Send some files into the S3 bucket, either from CardinalHQ's OTEL collector, or raw Parquet or `json.gz` files.

The `otel-raw/` prefix is where the OTEL collector will write Parquet files.  This should not be used for other formats or sources, including other Parquet schema.

The `logs-raw/` prefix is where log files to be ingested shoudl be written.

It is recommended to set up lifetime rules for these prefixes.

Lakerunner will update the prefix `db/` with indexed Parquet files.  Reading these is fine, it is not a supported use case to write this format from outside Lakerunner.
