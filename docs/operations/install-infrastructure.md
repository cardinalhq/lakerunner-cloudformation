# Install part 1: infrastructure

This is the first of two install steps. It runs `scripts/data-setup.sh`,
which provisions every resource the lakerunner application stack needs
but does not manage itself: an RDS Postgres instance, an S3 ingest
bucket, an SQS ingest queue, the Secrets Manager secrets that hold the
license / admin / DB credentials, two SSM parameters, the ECS cluster,
and the Cloud Map private DNS namespace.

The script is the **only** supported infra path. There is no
CloudFormation wrapper for this layer; the resources it creates live
outside any stack and survive lakerunner-stack deletes. Continue with
[`install-lakerunner.md`](install-lakerunner.md) once the script
returns.

## Layer ownership at a glance

| Layer | Owned by | Where it lives |
|---|---|---|
| IAM roles + security groups | Customer's IT (out of band) | Pre-created. See "IT prereqs" below. |
| Infra layer (RDS, S3, SQS, secrets, SSM, ECS cluster, Cloud Map ns) | `scripts/data-setup.sh` (raw AWS CLI) | Resources live outside CFN. **This document.** |
| Application layer (ALB, ECS services, migration, cert) | `cardinal-lakerunner` stack | Stateless. Owned end-to-end by CFN. **Next document.** |

## Step 0: IT prereqs (one-time, out of band)

The customer's IT team must pre-create:

- **IAM roles** -- one task role and one execution role for every ECS
  task in the install (a single shared task role is supported, and
  recommended), one Lambda execution role for the migration custom
  resource, and -- only when using the PEM-import certificate path --
  one Lambda execution role for the cert importer. Trust principals
  are `ecs-tasks.amazonaws.com` for task roles and
  `lambda.amazonaws.com` for Lambda roles.
- **Two security groups** in the target VPC -- `TaskSgId` (applied to
  every ECS task) and `AlbSgId` (applied to the shared ALB). Plus a
  `DbSgId` referenced by the script when it creates RDS, with ingress
  from `TaskSgId` on port 5432.

The exact policies these roles need are documented under
[`permissions-lakerunner.md`](permissions-lakerunner.md). The operator
running `data-setup.sh` needs the broader policy described in
[`permissions-infrastructure.md`](permissions-infrastructure.md).

## Step 1: collect the inputs

`scripts/data-setup.sh` reads its configuration from environment
variables. The required ones:

| Env var | Notes |
|---|---|
| `REGION` | Target AWS region. |
| `VPC_ID` | Target VPC. |
| `PRIVATE_SUBNETS` | CSV of subnet IDs across at least two AZs. |
| `DB_SG_ID` | Pre-created DB security group. |
| `LICENSE_DATA` *or* `LICENSE_DATA_FILE` | Cardinal license token (single line beginning with `z64:`). Set `LICENSE_DATA` to the token itself, or `LICENSE_DATA_FILE` to a file containing it. `LICENSE_DATA` wins if both are set. |

Optional sizing knobs:

| Env var | Default | Notes |
|---|---|---|
| `DB_INSTANCE_CLASS` | `db.t3.medium` | RDS instance class. |
| `DB_ALLOCATED_STORAGE` | `100` | RDS GiB. |
| `BUCKET_LIFECYCLE_DAYS` | `7` | Ingest object expiry. |

Caller identity must have the data-setup permissions in
[`permissions-infrastructure.md`](permissions-infrastructure.md).

## Step 2: run the script

```sh
REGION=us-east-2 \
VPC_ID=vpc-... \
PRIVATE_SUBNETS=subnet-aaaa,subnet-bbbb \
DB_SG_ID=sg-... \
LICENSE_DATA="z64:..." \
    scripts/data-setup.sh > /tmp/infra-outputs.json
```

Cold runs take 10-20 minutes -- RDS provisioning dominates. The
script is idempotent: each step does describe-then-act on the
deterministic resource names below, so re-running after a partial
failure converges.

Resources created with fixed names:

- ECS cluster: `cardinal`
- Cloud Map namespace: `cardinal.local`
- RDS instance: `cardinal-db`
- SQS queue: `cardinal-ingest`
- S3 bucket: `cardinal-ingest-<account>-<region>`
- Secrets: `cardinal-db-master`, `cardinal-license`,
  `cardinal-internal-keys`, `cardinal-admin-key`, `cardinal-maestro-db`
- SSM params: `/cardinal/storage-profiles`, `/cardinal/api-keys`

These fixed names imply one Cardinal install per AWS account/region.

## Step 3: harvest outputs

The script prints a JSON document to stdout. Its keys map 1:1 to the
`cardinal-lakerunner` stack's "Infra-setup outputs" parameter group:

`DbEndpoint`, `DbPort`, `DbName`, `DbMasterSecretArn`,
`MaestroDbSecretArn`, `IngestBucketName`, `IngestQueueUrl`,
`IngestQueueArn`, `LicenseSecretArn`, `InternalKeysSecretArn`,
`AdminKeySecretArn`, `StorageProfilesParamName`, `ApiKeysParamName`,
`ClusterName`, `ClusterArn`, `ServiceNamespaceId`,
`ServiceNamespaceName`.

The next install step consumes these as direct inputs.

**Next:** [`install-lakerunner.md`](install-lakerunner.md).

## Failure recovery

The script is idempotent: every `ensure_*` function does
describe-then-act on a deterministic name, so re-invocation converges.
The caller's identity should have create + update + delete on every
resource the script manages so it can recover from partial state on
its own -- no IT break-glass involvement required.

Common failure modes:

- **VPC / subnets do not exist or are in the wrong region.** Script
  fails fast on `CreateDBSubnetGroup` or
  `CreatePrivateDnsNamespace`. Fix the env var, re-run.
- **`DB_SG_ID` does not exist.** Script fails fast on
  `CreateDBInstance`. Fix and re-run.
- **`LICENSE_DATA_FILE` is malformed.** The script creates the
  secret with the raw content; lakerunner services fail at runtime
  with a parse error. Overwrite the secret with the correct content
  via `aws secretsmanager put-secret-value` and restart the affected
  services. (Re-running the script will *not* overwrite an existing
  secret.)

## Tearing down

The script does not have a `delete` mode. Wiping the infra layer is a
deliberate, operator-driven step covered in
[`tearing-down.md`](tearing-down.md). For a redeploy against the
existing infra, just delete the application stack and re-run
[`install-lakerunner.md`](install-lakerunner.md).
