# CardinalHQ's Lakerunner Cloud Formation Sripts

This is an example CDK-based Cloud Formation deployment of Lakerunner in ECS using
Fargate.

## Usage

1. Install dependencies with `npm install`.
2. Run `npx cdk synth` to generate CloudFormation templates in `cdk.out/`.
3. Deploy the templates in your AWS account using the CloudFormation console or CLI.

When launching the stacks you will be prompted for a small set of parameters:

* **VpcId** – the existing VPC ID to deploy into.
* **PrivateSubnetIds** – comma separated list of private subnet IDs.
* **PublicSubnetIds** – comma separated list of public subnet IDs.
* **PrivateSubnetRouteTableIds** – comma separated route table IDs for the private subnets.
* **PublicSubnetRouteTableIds** – comma separated route table IDs for the public subnets.
* **DbSecretName** – optional name for the database secret (defaults to `lakerunner-pg-password`).

The account and region are automatically detected when the CloudFormation stack is created. No CDK bootstrap or credentials are required just to synthesize the templates.

## Requirements

1. A working VPC with public and private subnets defined.

An application load balancer will be created that points to the `query-api` service on on port 7101,
and the `grafana` service on port 3000.  No TLS is set up.

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
