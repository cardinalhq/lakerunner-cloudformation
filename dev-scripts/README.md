# dev-scripts/

Internal/dev tooling: Jenkins-friendly wrappers for the product stacks, the
`lrdev-*` test scaffolding, and the lifecycle drivers (`cleanup-lakerunner.sh`,
`teardown-lakerunner.sh`). Not customer-facing and not published — the
customer-facing per-stack deploy drivers live in `scripts/`. Each wrapper is
parameterised entirely through environment variables, with no customer-specific
identifiers baked into the defaults; an operator points a Jenkins job at the
script and supplies the env block.

These are thin wrappers. The actual deploy logic lives in
`scripts/deploy-lakerunner.sh` and `dev-scripts/cleanup-lakerunner.sh` (the
self-contained drivers), or in straight `aws cloudformation` calls for the
infrastructure / lrdev tiers. The wrappers here exist to make the input shape
uniform and to keep customer values out of the repo.

## Order of operations

For a fresh install in an empty account:

1. **VPC** -- customer brings their own. The test environment uses the
   internal `lrdev-vpc` stack (`deploy-lrdev-vpc.sh`); production installs
   skip this step.
2. **ECS cluster** -- customer brings their own. The test environment uses
   `lrdev-baseinfra` (`deploy-lrdev-baseinfra.sh`); production installs skip
   this step.
3. **`cardinal-infrastructure`** -- creates the RDS / S3 / SQS / Secrets /
   SSM data layer (`deploy-cardinal-infrastructure.sh`).
4. **`cardinal-lakerunner`** -- creates the application tier; reads the
   infrastructure stack's outputs at runtime
   (`deploy-cardinal-lakerunner.sh`).

To tear an install down end-to-end, run `run-cleanup.sh`. It launches the
cardinal-cleanup Fargate task, which drains ECS services, deletes the
lakerunner stack, wipes the cardinal-* data layer with ownership-tag
enforcement, then self-deletes its own stack.

When a stack delete fails part-way and leaves resources in `DELETE_FAILED` /
`DELETE_SKIPPED`, `sweep-stranded-resources.sh` mops them up. It is a single
self-contained file (the Fargate container body is embedded in it as a quoted
heredoc): it registers a one-shot privileged task directly via the ECS API (no
CFN stack), the task discovers the stranded resources from the stack and
deletes them by type (IAM roles, security groups, secrets, SSM parameters, S3
buckets), and the driver deregisters + deletes the task definition once the
task stops. The task runs under a caller-supplied "superadmin" role
(`--task-role-arn`) and self-skips its own ENI security group.

## Scripts

| Script | Purpose | Customer-facing? |
|---|---|---|
| `deploy-lrdev-vpc.sh` | Stand up a test-only VPC (NAT GW + 2 AZs). | No -- internal test scaffolding. |
| `deploy-lrdev-baseinfra.sh` | Stand up a test-only ECS Fargate cluster. | No -- internal test scaffolding. |
| `deploy-cardinal-infrastructure.sh` | Stand up RDS + S3 ingest + SQS + secrets + SSM. | Yes. |
| `deploy-cardinal-lakerunner.sh` | Stand up the application tier (ALB + 12 ECS services + DB migration). | Yes. |
| `run-cleanup.sh` | Run the cleanup task; wipe data layer + tear down stacks. | Yes. |
| `lib.sh` | Shared helpers (logging, preflight, deploy + monitor). | Sourced; do not execute. |

## Env vars per script

### deploy-lrdev-vpc.sh

| Var | Required | Default | Notes |
|---|---|---|---|
| `VERSION` | yes | -- | Published template tag (e.g. `v0.0.80`). |
| `REGION` | no | `us-east-1` | |
| `STACK_NAME` | no | `lrdev-vpc` | |
| `TEMPLATE_BUCKET` | no | `cardinal-cfn-${REGION}` | |
| `ENVIRONMENT_NAME` | no | `lrdev` | Used in resource Name tags. |
| `VPC_CIDR` | no | `10.0.0.0/16` | |
| `CREATE_NAT_GATEWAY` | no | `Yes` | `No` saves ~$30/mo but blocks private-subnet egress. |
| `CREATE_INTERFACE_ENDPOINTS` | no | `No` | `Yes` adds ~$7/endpoint/month per AZ. |

### deploy-lrdev-baseinfra.sh

| Var | Required | Default | Notes |
|---|---|---|---|
| `VERSION` | yes | -- | Published template tag. |
| `REGION` | no | `us-east-1` | |
| `STACK_NAME` | no | `lrdev-baseinfra` | |
| `TEMPLATE_BUCKET` | no | `cardinal-cfn-${REGION}` | |
| `ENVIRONMENT_NAME` | no | `lrdev` | |

### deploy-cardinal-infrastructure.sh

| Var | Required | Default | Notes |
|---|---|---|---|
| `VERSION` | yes | -- | |
| `VPC_ID` | yes | -- | Customer VPC, must contain `PRIVATE_SUBNETS`. |
| `PRIVATE_SUBNETS` | yes | -- | CSV of >=2 subnet IDs in distinct AZs. |
| `LICENSE_DATA` | yes | -- | Cardinal license token (`z64:...`). Marked NoEcho in the template. |
| `REGION` | no | `us-east-1` | |
| `STACK_NAME` | no | `cardinal-infrastructure` | |
| `TEMPLATE_BUCKET` | no | `cardinal-cfn-${REGION}` | |
| `DB_ENGINE_VERSION` | no | `18.3` | PostgreSQL engine version. |
| `DB_INSTANCE_CLASS` | no | `db.t3.medium` | |
| `DB_ALLOCATED_STORAGE` | no | `100` | GiB. |
| `INGEST_BUCKET_LIFECYCLE_DAYS` | no | `7` | Auto-delete S3 objects after N days. |
| `ORGANIZATION_ID` | no | canonical UUID | Match across infra and lakerunner. |

### deploy-cardinal-lakerunner.sh

| Var | Required | Default | Notes |
|---|---|---|---|
| `VERSION` | yes | -- | |
| `VPC_ID` | yes | -- | Same VPC as cardinal-infrastructure. |
| `PRIVATE_SUBNETS` | yes | -- | Same CSV as cardinal-infrastructure. |
| `CLUSTER_NAME` | yes | -- | Customer's ECS Fargate cluster. |
| `CLUSTER_ARN` | yes | -- | Full ARN of the cluster. |
| `DEX_ADMIN_EMAIL` | yes | -- | DEX local-admin login email. |
| `DEX_ADMIN_PASSWORD_HASH` | yes | -- | bcrypt hash (`$2a$`/`$2b$`/`$2y$`). |
| `CERTIFICATE_ARN` | one-of | -- | ACM/IAM-server-cert ARN. Use **either** this **or** the PEM trio. |
| `CERTIFICATE_BODY` | one-of | -- | PEM body. Use with `CERTIFICATE_PRIVATE_KEY`. |
| `CERTIFICATE_PRIVATE_KEY` | one-of | -- | PEM key. |
| `CERTIFICATE_CHAIN` | no | empty | Optional intermediate-cert chain (PEM). |
| `OIDC_SUPERADMIN_EMAILS` | no | `$DEX_ADMIN_EMAIL` | CSV of emails granted superadmin on first login. |
| `REGION` | no | `us-east-1` | |
| `STACK_NAME` | no | `cardinal-lakerunner` | |
| `INFRA_STACK_NAME` | no | `cardinal-infrastructure` | Outputs are read live from here. |
| `SERVICE_NAMESPACE_NAME` | no | `cardinal.local` | Cloud Map private DNS namespace. |
| `ORGANIZATION_ID` | no | canonical UUID | Must match the infra stack. |

### run-cleanup.sh

Destructive. Refuses to run unless `CONFIRM=DELETE` is set. Delegates to
`dev-scripts/cleanup-lakerunner.sh`.

| Var | Required | Default | Notes |
|---|---|---|---|
| `CONFIRM` | yes | -- | Must be the literal string `DELETE`. |
| `VERSION` | yes | -- | |
| `CLUSTER_NAME` | yes | -- | |
| `PRIVATE_SUBNETS` | yes | -- | Subnets for the cleanup task ENI. |
| `TASK_SG_ID` | yes | -- | Security group for the cleanup task ENI. |
| `CLEANUP_TASK_ROLE_ARN` | yes | -- | Privileged role the cleanup task assumes. |
| `CLEANUP_EXECUTION_ROLE_ARN` | yes | -- | Fargate execution role. |
| `DEPLOYER_ROLE_ARN` | yes | -- | CFN service role used to delete the lakerunner stack. |
| `LAKERUNNER_STACK_NAME` | no | `cardinal-lakerunner` | |
| `INFRA_STACK_NAME` | no | `cardinal-infrastructure` | Cleanup discovers retained-resource physical IDs from this stack before deleting it. |
| `CLEANUP_STACK_NAME` | no | `cardinal-cleanup` | |
| `WAIT_SELF_DELETE` | no | `false` | Set to `true` to block until cleanup stack self-deletes. |

> **`TASK_SG_ID` must not be a `cardinal-lakerunner`-owned SG.** If it is, the cleanup task's ENI keeps the SG alive, and step 1's `delete-stack cardinal-lakerunner` deadlocks waiting for the SG to free. Use a customer-supplied / VPC-level SG that doesn't belong to the install you're tearing down.

## Jenkins job topology (recommended)

- One job per script. The job's "Inject environment variables" step supplies
  the required vars; the script is the build step.
- All four deploy scripts are idempotent (auto-detect create vs update), so a
  Jenkins job can deploy the same version repeatedly without operator
  intervention.
- Pin every job to a specific `VERSION` -- there is no `latest` tag.
- For the cleanup job, gate the `CONFIRM=DELETE` env var on a Jenkins
  password-style parameter so an accidental run is impossible.
- Capture each script's stdout/stderr (the wrapper streams stack events as
  they happen; the log is the source of truth).

## Sanity checks performed by every script

1. `aws sts get-caller-identity` succeeds in the requested region.
2. The published template URL HEAD-200s before the stack call is issued.
3. The stack is in a state we can act on (`DOES_NOT_EXIST`,
   `ROLLBACK_COMPLETE`, `*_COMPLETE`); abort on anything else.
4. For `deploy-cardinal-lakerunner.sh`: the infra stack must be in a
   `*_COMPLETE` state and every required output must be non-empty.
5. For `run-cleanup.sh`: `CONFIRM=DELETE` must be set explicitly.
