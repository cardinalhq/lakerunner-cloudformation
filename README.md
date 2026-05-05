# Cardinal Lakerunner CloudFormation

Deploy Lakerunner on AWS ECS Fargate via two CloudFormation templates: an optional VPC, and the application stack.

## Quick start (you have a VPC)

1. In the CloudFormation console, choose **Create stack** → **With new resources**.
1. **Amazon S3 URL:** `https://cardinal-cfn.s3.us-east-2.amazonaws.com/lakerunner/latest/cardinal-lakerunner.yaml`
1. Fill in `VpcId`, `PrivateSubnets`, `CertificateArn`, and `LicenseData`. Defaults work for the rest.
1. Create the stack.

The stack creates RDS, S3, SQS, ALB, all ECS services, runs the migration, and outputs the ALB DNS, query API URL, and Maestro URL.

## Quick start (you need a VPC)

Deploy `https://cardinal-cfn.s3.us-east-2.amazonaws.com/lakerunner/latest/cardinal-vpc.yaml` first, then use its `VpcId` and `PrivateSubnetsCsv` outputs as inputs to the `cardinal-lakerunner.yaml` deploy. The application stack runs entirely in private subnets behind an internal ALB; the VPC's `PublicSubnetsCsv` output is exposed for completeness but is not required by the application stack.

## Versioned URLs

Pin to an explicit release for reproducibility:

```
https://cardinal-cfn.s3.us-east-2.amazonaws.com/lakerunner/v1.20.0/cardinal-lakerunner.yaml
```

## Air-gapped deployment

Mirror the entire `lakerunner/<version>/` prefix to your own S3 bucket and pass `TemplateBaseUrl` pointing at your bucket. Override the per-image parameters (`LakerunnerImage`, `MaestroImage`, etc.) to point at your private registry.

## Architecture

The root template orchestrates eleven nested stacks:

- `cluster` — ECS cluster, task SG, execution role
- `database` — RDS PostgreSQL
- `storage` — S3 ingest bucket + SQS queue
- `alb` — ALB + HTTPS listener + ALB SG
- `config` — license, API keys, storage profiles (Secrets Manager + SSM)
- `migration` — one-shot DB migration via Lambda-backed custom resource
- `services-query` — query-api, query-worker
- `services-process` — process-{logs,metrics,traces}, pubsub-sqs
- `services-control` — sweeper, monitoring, admin-api, alert-evaluator
- `otel` — OTEL collector
- `maestro` — Maestro + bundled DEX OIDC

## Development

See `README-BUILDING.md` for generator instructions.
