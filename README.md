# CardinalHQ's Lakerunner Cloud Formation Sripts

This is an example CDK-based Cloud Formation deployment of Lakerunner in ECS using
Fargate.

## Usage

1. Set the AWS_REGION environment variable to the target region for the deployment.
1. Set the AWS_ACCOUNT environment variable to the AWS account you are using.
1. Have working credentials for that account.  See below for the resources deployed.
1. run `./deploy.sh`
1. Wait patiently.  Starting from scratch, it will take at least 5 minutes to complete.

What this does:

1. Bootstraps CDK in your account, if needed.
1. Deploy "common infrastructure" which consists of some SGs, IAM roles, ECS cluster, RDS cluster, and EFS.
1. Run an ECS job will to configure and migrate the RDS database schema.
1. Deploy the various ECS services that make Lakerunner work.

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
