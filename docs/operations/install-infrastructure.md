# Install part 1: infrastructure

First of two install steps. Deploys `cardinal-infrastructure.yaml`,
which creates the **data** resources the lakerunner application stack
needs but does not manage itself: an RDS Postgres instance + its
security group, an S3 ingest bucket, an SQS ingest queue, the Secrets
Manager secrets that hold the license / admin / DB credentials, and
two SSM parameters.

The compute plane (ECS cluster) is pre-created by the customer and
passed into `cardinal-lakerunner` as `ClusterName` / `ClusterArn`. The
Cloud Map private DNS namespace is created by the lakerunner stack
itself; nothing about it lives at the infra layer.

Continue with [`install-lakerunner.md`](install-lakerunner.md) once
this stack reaches `CREATE_COMPLETE`.

## Layer ownership at a glance

| Layer | Owned by | Where it lives |
|---|---|---|
| ECS cluster | Customer's IT (out of band) | Pre-created; identifiers passed as `cardinal-lakerunner` parameters. |
| VPC + private subnets | Customer's IT (out of band) | Passed as parameters into both stacks. |
| Data layer (RDS + RDS SG, S3, SQS, secrets, SSM) | `cardinal-infrastructure` stack | All resources retained on stack delete. **This document.** |
| Application layer (ALB, ECS services, migration, cert, *all SGs and IAM roles*) | `cardinal-lakerunner` stack | Stateless. **Next document.** |

## Step 0: IT prereqs (one-time, out of band)

The customer's IT team must pre-create:

- **An ECS cluster.** Any name is fine; capture the cluster name and
  the cluster ARN.
- **A VPC** with **at least two private subnets in distinct AZs**.

That is the entire IT prereq surface. **No IAM roles, no security
groups, and no Cloud Map namespace are required from IT.** The
infrastructure stack creates the RDS security group; the lakerunner
stack creates the ALB SG, six per-tier task SGs, six per-tier task
IAM roles, the shared ECS execution role, and the Cloud Map
namespace.

The deployer principal that runs `aws cloudformation create-stack`
needs the policy documented in
[`permissions-infrastructure.md`](permissions-infrastructure.md).

## Step 1: collect the inputs

| Parameter | Notes |
|---|---|
| `VpcId` | VPC the RDS instance lives in. Same VPC the lakerunner stack uses. |
| `PrivateSubnets` | CSV of subnet IDs across at least two AZs. |
| `LicenseData` | Cardinal license token (single line beginning with `z64:`). `NoEcho`. |
| `OrganizationId` | Canonical install-organization UUID. Same value goes into the lakerunner stack. Defaults to the canonical demo UUID. |

Optional sizing knobs:

| Parameter | Default | Notes |
|---|---|---|
| `DBEngineVersion` | `18.3` | PostgreSQL engine version. |
| `DBInstanceClass` | `db.r7g.large` | RDS instance class. |
| `DBAllocatedStorage` | `100` | RDS GiB. |
| `IngestBucketLifecycleDays` | `7` | Ingest object expiry. |

Recovery-only overrides (only set on a retry after a previous
partial-create that left an orphan with the same name):
`IngestBucketName`, `LicenseSecretName`, `AdminKeySecretName`,
`StorageProfilesParamName`, `ApiKeysParamName`. See
[`infrastructure-stack-parameters.md`](infrastructure-stack-parameters.md)
for the full reference.

## Step 2: deploy

```sh
aws cloudformation create-stack \
    --region <REGION> \
    --stack-name cardinal-infrastructure \
    --template-url https://cardinal-cfn-us-east-1.s3.us-east-1.amazonaws.com/lakerunner/<VERSION>/cardinal-infrastructure.yaml \
    --parameters \
        ParameterKey=VpcId,ParameterValue=vpc-... \
        ParameterKey=PrivateSubnets,ParameterValue=subnet-aaaa\\,subnet-bbbb \
        ParameterKey=LicenseData,ParameterValue=z64:...

aws cloudformation wait stack-create-complete \
    --region <REGION> --stack-name cardinal-infrastructure
```

Cold runs take 10-20 minutes -- RDS provisioning dominates.

Resources created with fixed names (one install per account/region):

- RDS instance: CFN-generated; the SG attached to it is `cardinal-rds-sg`.
- SQS queue: CFN-generated.
- S3 bucket: `cardinal-ingest-<account>-<region>`.
- Secrets: `cardinal-license`, `cardinal-admin-key` (the db-master secret
  is CFN-generated).
- SSM params: `/cardinal/storage-profiles`, `/cardinal/api-keys`.

## Step 3: harvest outputs

```sh
aws cloudformation describe-stacks \
    --region <REGION> --stack-name cardinal-infrastructure \
    --query 'Stacks[0].Outputs' --output table
```

The outputs feed straight into the `cardinal-lakerunner` stack as
parameters: `DbEndpoint`, `DbPort`, `DbName`, `DbMasterSecretArn`,
`RdsSecurityGroupId`, `IngestBucketName`, `IngestQueueUrl`,
`IngestQueueArn`, `LicenseSecretArn`, `AdminKeySecretArn`,
`StorageProfilesParamName`, `ApiKeysParamName`.

**Next:** [`install-lakerunner.md`](install-lakerunner.md).

## Failure recovery

Every data-bearing resource carries `DeletionPolicy: Retain` (RDS
uses `Snapshot`). A rollback after a partial create therefore
orphans whatever was already created. Explicitly-named resources
(the S3 ingest bucket, the two SSM parameters, the license +
admin-key secrets) will collide on retry. To recover:

1. Delete the failed stack -- orphaned resources stay put.
2. Either pass alternate values for the recovery-override
   parameters listed in Step 1, or manually delete the orphans via
   the console (Secrets Manager imposes a 7-day minimum recovery
   window unless you call `delete-secret --force-delete-without-recovery`).
3. Redeploy.

Resources without explicit names (RDS, SQS, DB subnet group, the
db-master secret, the RDS SG) are CFN-named and collide-free on
retry.

## Tearing down

The `cardinal-infrastructure` stack carries `DeletionPolicy: Retain`
on every customer-data-bearing resource, so a plain `delete-stack`
removes the stack envelope but leaves RDS, S3 + objects, secrets, SSM
params, and the RDS SG intact. Deliberate wipes are an operator-driven
step covered in [`tearing-down.md`](tearing-down.md).
