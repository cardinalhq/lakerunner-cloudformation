# Cross-account remote ingest

This document is for operators adding telemetry from a **second AWS account** to
an existing Cardinal lakerunner install. It covers two templates:

- `cardinal-remote-ingest.yaml` — deployed in the **main** (lakerunner) account.
  Creates an S3 bucket, a cross-account writer IAM role, and the bucket-to-SQS
  notification.
- `cardinal-remote-collector.yaml` — deployed in the **second** account. An
  ALB-fronted otel collector that assumes the writer role and writes telemetry
  to the main-account bucket.

Design: `docs/superpowers/specs/2026-05-29-cross-account-remote-ingest-design.md`.

## How it works

The bucket lives in the main account, in the **same region as the lakerunner
SQS queue** (S3-to-SQS notifications must be same-region). The remote collector
assumes a main-account writer role and writes objects into that bucket; because
it writes as a main-account principal, the objects are owned by the main account
and lakerunner reads them with no special permission. The bucket notifies the
existing lakerunner SQS queue, and the normal `pubsub-sqs` -> `process-*`
pipeline ingests each object.

Only the **write** crosses the account boundary, via STS assume-role.

```
remote app --OTLP--> remote ALB :4318 --> remote otel collector
   --(assume main writer role)--> PutObject  main-account bucket
   --> S3 ObjectCreated --> main SQS --> lakerunner ingest (under the bucket's org)
```

## Prerequisites

- `cardinal-infrastructure` and `cardinal-lakerunner` are already deployed in the
  main account.
- The infrastructure stack is on a template version that includes the broadened
  queue policy (the `cardinal-remote-ingest-*` notification grant). If your
  install predates this, update the `cardinal-infrastructure` stack first.
- You have both account IDs and the main-account SQS ingest queue ARN
  (the `cardinal-infrastructure` stack's `IngestQueueArn` output).
- The second account already has a VPC, private subnets (2+ AZs), and an ECS
  Fargate cluster. This template does not create those.

## Step 1: Deploy `cardinal-remote-ingest` in the main account

1. Open the CloudFormation console in the **main** account, same region as the
   lakerunner install.
1. Create a stack from `cardinal-remote-ingest.yaml`.
1. Parameters:
   - `RemoteAccountId`: the 12-digit second account ID.
   - `OrgId`: the lakerunner organization this account's telemetry belongs to.
     One org per remote bucket.
   - `QueueArn`: the `cardinal-infrastructure` stack's `IngestQueueArn` output.
   - `BucketName`: leave blank to use `cardinal-remote-ingest-<RemoteAccountId>`.
     Any override **must** keep the `cardinal-remote-ingest-` prefix or the
     bucket notification will be rejected (the infra queue policy grants by that
     prefix).
   - `CollectorName`: leave as `lakerunner` unless you have a reason to change it
     (it must match the remote collector's `CollectorName`).
   - `IngestBucketLifecycleDays`: object GC backstop (default 7).
1. This stack creates an IAM role; acknowledge the IAM capability when prompted.
1. After it completes, record the outputs: `BucketName`, `BucketRegion`,
   `WriterRoleArn`, and `StorageProfileSnippet`.

## Step 2: Deploy `cardinal-remote-collector` in the second account

1. Open the CloudFormation console in the **second** account.
1. Create a stack from `cardinal-remote-collector.yaml`.
1. Parameters:
   - `VpcId`, `PrivateSubnetsCsv`, `ClusterArn`: the second account's existing
     VPC, private subnets, and ECS cluster.
   - `WriterRoleArn`, `BucketName`, `BucketRegion`: paste from Step 1's outputs.
     `BucketRegion` is the main/lakerunner region — not the second account's
     region.
   - `OrgId`, `CollectorName`: match the values used in Step 1.
   - `OtlpIngressCidr`: narrow to the CIDR of the senders that will reach the
     internal ALB on port 4318 (default `10.0.0.0/8`).
1. This stack creates a **named** IAM role (`cardinal-remote-otel-<region>`, so
   the main-account writer role's trust condition matches it). Acknowledge the
   named-IAM capability (`CAPABILITY_NAMED_IAM`) when prompted.
1. After it completes, point your OTLP senders at the `OtelExternalUrl` output
   (`http://<alb-dns>:4318`).

Only one remote collector per region per account is supported by the default
role name. To run more, redeploy `cardinal-remote-ingest` with a different
`BucketName` and adjust as needed.

## Step 3: Register the storage profile and re-run the migrator

Lakerunner attributes each ingested object to an org by looking up its bucket in
the storage profiles. The new bucket needs a profile entry.

1. Take the `StorageProfileSnippet` output from Step 1.
1. Update the `cardinal-infrastructure` stack: append the snippet to the
   `AdditionalStorageProfilesYaml` parameter (it accepts one or more YAML list
   items) and apply the change. This updates the storage-profiles SSM parameter.
1. Re-run the migrator so it re-imports the storage profiles into `configdb`.
   Either bump `LakerunnerImage` on the `cardinal-lakerunner` stack, or cycle the
   migrator service (`aws ecs update-service --desired-count 0` then `1`). The
   migrator is idempotent.

Until the migrator re-imports, objects from the new bucket land in SQS but
lakerunner cannot attribute them to an org.

## Notes and limits

- **Region**: the bucket is pinned to the lakerunner region. If the second
  account is in another region, the collector writes cross-region (data-transfer
  cost). This is expected.
- **Encryption**: the bucket uses SSE-S3. If you switch a bucket to SSE-KMS, the
  writer role also needs `kms:GenerateDataKey` on the key.
- **License**: the remote collector runs a receive-to-S3 pipeline only and needs
  no license.
- **Teardown**: the bucket has `DeletionPolicy: Retain`. Empty and delete it by
  hand after deleting the stack if you want the data gone.
