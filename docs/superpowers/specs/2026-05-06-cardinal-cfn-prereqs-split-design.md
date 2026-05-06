# Cardinal CFN -- customer-supplied prereqs, data-setup Lambda, parameterized lakerunner

Design spec for a customer environment where:

- The customer's IT pre-creates **all** IAM roles and security groups
  Cardinal needs and supplies the ARNs/IDs as parameters. Cardinal's
  CFN never creates IAM roles or SGs.
- The data-bearing infrastructure (RDS, S3 ingest bucket, SQS ingest
  queue, secrets, SSM parameters) is created by a **Python Lambda**
  whose execution role the customer also supplies. The Lambda is
  invokable either via a thin CFN custom-resource wrapper or via
  ``aws lambda invoke`` directly.
- The lakerunner application stack keeps its existing layout
  (`cardinal-lakerunner.yaml` root + twelve nested children) with one
  surface-level change: every IAM role and SG it currently creates
  becomes a parameter, and the data, storage, and config nested children
  are removed (their outputs become parameters threaded through the
  root from the Lambda's output).

This spec supersedes the earlier shell-script + flat-CFN approach in
the same file (visible in git history). The pivot was driven by the
customer's IT policy: they will not run shell scripts as a privileged
identity, but they are willing to grant a powerful role to a Lambda.

## Goals

- No CFN-generated IAM roles or SGs. All trust/policy decisions stay
  with the customer's IT, who delivers ARNs and IDs as parameters.
- The data-bearing resources are created once by a Lambda and never
  updated by Cardinal tooling thereafter. Customer's IT handles future
  changes via break-glass.
- The lakerunner application stays freely deployable / updatable /
  deletable by whatever role Jenkins runs as, since that role no
  longer creates IAM or RDS or S3.
- A customer who wants a single all-powerful role can pass that ARN
  for every role parameter and the templates work unchanged. A
  customer who wants per-role separation passes different ARNs.

## Non-goals

- Multi-install isolation in a single AWS account. One install per
  account+region; physical names use plain ``cardinal-*`` /
  ``cardinal/*`` prefixes.
- Customer-supplied KMS keys.
- A vendor-provided Jenkinsfile or any tool the customer cannot run
  on their own infrastructure.
- Auto-rotation of any secret created by the data Lambda (operator
  rotates manually if required).

## Layers

```
[External -- customer's IT, never touched by Cardinal tooling]
    IAM roles:
      - TaskRoleArn               (running ECS tasks)
      - ExecutionRoleArn          (ECS task launch + secrets/ssm resolve)
      - MigrationLambdaRoleArn    (one-shot RunTask on migrator)
      - DataSetupLambdaRoleArn    (the data Lambda's own execution role)
      - CertLambdaRoleArn         (optional, only when importing PEM)
    Security groups:
      - TaskSgId                  (ECS tasks)
      - AlbSgId                   (ALB)
      - DbSgId                    (RDS)

    The customer's IT creates these from a vendor-supplied policy
    cookbook (docs/operations/required-roles.md, generated from
    iam_policies.py). They may collapse multiple ARNs onto a single
    powerful role; the templates do not care.
        |
        v
[Cardinal data-setup Lambda]
    Python Lambda (in this repo, packaged as a zip during `make build`).
    Idempotent ensure_* steps create:
      - RDS Postgres + DB master secret
      - S3 ingest bucket + lifecycle + S3->SQS notification
      - SQS ingest queue + queue policy
      - cardinal-license / -internal-keys / -admin-key /
        -maestro-db secrets
      - /cardinal/storage-profiles, /cardinal/api-keys SSM params
    Output: a JSON document indexed by CFN parameter name (DB endpoint,
    secret ARNs, bucket name, queue URL/ARN, etc.).

    Invocation: either
      (a) cardinal-data-setup.yaml (CFN), a tiny stack that creates
          the Lambda function and a CFN custom resource that invokes
          it on stack-create, returning the JSON as stack outputs; or
      (b) `aws lambda invoke` against the deployed Lambda directly,
          piping the response into a parameter file.
        |
        v
[Cardinal lakerunner CFN stack -- existing root + nested children]
    Layout unchanged from current main. Per-child changes:

      Removed (their work moved to the Lambda):
        - children/database.py
        - children/storage.py
        - children/config.py

      Modified -- stop creating IAM and SGs, take ARNs/IDs as params:
        - children/cluster.py    (drops ExecutionRole + TaskSG creation)
        - children/alb.py        (drops AlbSG creation)
        - children/cert.py       (drops CertLambdaRole creation)
        - children/migration.py  (drops MigrationLambdaRole + MigratorTaskRole)
        - children/services_*.py (drop per-service task roles, all use
                                  the shared TaskRoleArn parameter)
        - children/maestro.py    (drops MaestroTaskRole)
        - children/otel.py       (drops OtelTaskRole)

      Threaded through root.py as new parameters:
        TaskRoleArn, ExecutionRoleArn, MigrationLambdaRoleArn,
        CertLambdaRoleArn (optional), TaskSgId, AlbSgId, DbSgId,
        DbEndpoint, DbPort, DbName, DbMasterSecretArn,
        MaestroDbSecretArn, IngestBucketName, IngestQueueUrl,
        IngestQueueArn, LicenseSecretArn, InternalKeysSecretArn,
        AdminKeySecretArn, StorageProfilesParamName, ApiKeysParamName.

      Existing parameters (image overrides, replica/cpu/memory
      sizing, certificate config, etc.) are unchanged.
```

## Lambda contract

`src/cardinal_cfn/data_setup_lambda/` ships a Python 3.11 Lambda
handler with the following shape:

```python
def handler(event: dict, context) -> dict:
    """Idempotent ensure_* sequence over RDS, S3, SQS, Secrets, SSM.

    event:
      RequestType:        "Create" | "Update" | "Delete"  (when invoked
                          via CFN custom resource; otherwise omitted)
      Region:             AWS region (default: from AWS_REGION env var)
      VpcId:              VPC ID (for DB subnet group lookup)
      PrivateSubnets:     ["subnet-a", "subnet-b", ...]
      DbSgId:             SG ID applied to the RDS instance
      LicenseData:        license JSON as a string (passed via parameter,
                          NoEcho on the wrapper stack)
      DexAdminEmail:      string
      DexAdminPasswordHash: string
      OidcSuperadminEmails: comma-separated string
      DbInstanceClass:    default "db.t3.medium"
      DbAllocatedStorage: default 100
      BucketLifecycleDays: default 7

    returns:
      {
        "DbEndpoint": ..., "DbPort": ..., "DbName": ...,
        "DbMasterSecretArn": ..., "MaestroDbSecretArn": ...,
        "IngestBucketName": ..., "IngestQueueUrl": ...,
        "IngestQueueArn": ...,
        "LicenseSecretArn": ..., "InternalKeysSecretArn": ...,
        "AdminKeySecretArn": ...,
        "StorageProfilesParamName": ..., "ApiKeysParamName": ...
      }

    On RequestType == "Delete", the handler is a no-op by default:
    the data resources are intentionally retained so an accidental
    stack delete does not destroy customer data. A future flag can
    opt the Lambda into actual teardown (the role has the
    permissions; the policy decision is the customer's).
    """
```

The handler is broken into one ``ensure_*`` function per resource,
matching the shape from the prior shell-script approach. Each
``ensure_*`` does a describe-then-act so a partial run on re-execution
converges. Ordering is enforced by the call sequence in ``handler``:
SQS queue -> queue policy -> S3 bucket -> S3 lifecycle -> S3
block-public-access -> S3 notification; DB subnet group -> DB instance ->
wait-available -> master-secret connection JSON; license / internal-keys
/ admin-key / maestro-db secrets; SSM parameters.

If the Lambda fails mid-way, the customer re-invokes it; the
``ensure_*`` checks skip already-completed steps. The Lambda's
execution role is granted update/delete on every resource it manages
(the customer accepts that scope on the Lambda role since the Lambda
code is auditable), so partial-state recovery does not require
out-of-band IT involvement -- the Lambda can drop and recreate any
resource it owns.

## Naming contract

Same as the prior version of this spec, with the addition that the
naming contract is now enforced between the Lambda's ``ensure_*``
arguments and the lakerunner template's parameter expectations:

| Resource | Physical name | Created by | Used by |
|---|---|---|---|
| ECS cluster | `cardinal` | lakerunner stack (`cluster` child) | task role's ECS condition; migration Lambda's ECS condition |
| S3 ingest bucket | `cardinal-ingest-${AccountId}-${Region}` | data-setup Lambda | task role (S3 RW) |
| SQS ingest queue | `cardinal-ingest` | data-setup Lambda | task role (SQS RW) |
| Migration ECS task definition family | `cardinal-migrator` | lakerunner stack (`migration` child) | migration Lambda's RunTask |
| Per-service log groups | `/cardinal/<service>` | lakerunner stack (per-services children) | task role (CW Logs) |
| SSM params | `/cardinal/storage-profiles`, `/cardinal/api-keys` | data-setup Lambda | task / execution roles (SSM read on `/cardinal/*`) |
| Secrets | `cardinal-*` (e.g., `cardinal-db-master`) | data-setup Lambda | task / execution roles (secretsmanager read on `cardinal-*`) |

A unit test (`test_naming_contract.py`) compares the literal bucket /
queue / cluster / family / log-group / SSM / secret strings the Lambda
writes against the values the lakerunner templates parameterize on.

## Required-roles cookbook

`docs/operations/required-roles.md` lists each customer-supplied role,
its trust policy, and its required inline-policy contents. The doc is
generated from `src/cardinal_cfn/iam_policies.py` (existing) by a
build-time helper, so the doc and the customer-runtime requirement
can never drift.

The cookbook covers:
- TaskRole (ECS-tasks trust; S3 RW, SQS RW, SSM read on `/cardinal/*`,
  secrets read on `cardinal-*`, CW Logs writes to `/cardinal/*`,
  ecs:Describe/Update on the cluster, bedrock invoke on
  foundation-model/*).
- ExecutionRole (ECS-tasks trust; AWS-managed
  `AmazonECSTaskExecutionRolePolicy` plus secrets read on `cardinal-*`
  plus SSM read on `/cardinal/*`).
- MigrationLambdaRole (lambda trust; logs:* on \*, ecs:RunTask on
  migrator family with cluster condition, ecs:DescribeTasks with
  cluster condition, iam:PassRole on TaskRole and ExecutionRole).
- DataSetupLambdaRole (lambda trust; logs:* on \*, plus the broad
  RDS/S3/SQS/Secrets/SSM permissions the Lambda needs to create the
  data layer).
- CertLambdaRole (optional; lambda trust; logs:* on \*, acm:Import,
  acm:Delete/AddTags/RemoveTags on cert ARNs).

The cookbook explicitly notes that a customer who wants the simple
path can grant a single role with the union of the above and pass
that ARN for every role parameter.

## Open questions

None blocking. The pivot is captured; everything else is implementation.
