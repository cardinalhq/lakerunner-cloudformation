# Cardinal CFN — out-of-CFN prereqs + data setup, with two CFN stacks

Design spec for restructuring the lakerunner CloudFormation distribution to
match a customer environment where the deployer principal can create
resources but cannot update or delete IAM roles, RDS instances, S3 buckets,
secrets, or security groups — and where IT will not grant delete
permissions on those resource types under any circumstance.

This supersedes the partition assumptions in
`2026-04-28-cardinal-cfn-refactor-design.md`. The "two roots, twelve nested
children" target architecture from that spec is replaced by the layout
below. The IAM-role definitions, ListenerRule priority table, naming
conventions, lifecycle policy table, and the `cardinal-defaults.yaml`
loader from that spec all carry forward unchanged.

## Goals

- Two privileged-identity shell scripts (run once each) create everything
  the deployer principal cannot manage: IAM roles, security groups, RDS,
  S3 ingest bucket, SQS queue, secrets, SSM params, S3→SQS notification.
- After those run, the deployer principal — Jenkins's own IAM identity —
  drives two CFN stacks (`cardinal-infra-app`, `cardinal-lakerunner`),
  both containing only resource types the deployer can create, update,
  AND delete cleanly. Failed CREATEs always roll back successfully.
- The deployer principal does not need create/update/delete on IAM
  roles, security groups, RDS, S3 buckets, secrets, or SSM params. It
  does need `iam:PassRole` (scoped to the prereq role ARNs) and
  `iam:CreateServiceLinkedRole` for ECS / ELB.
- The lakerunner application can be created, updated, and deleted by the
  deployer freely — image bumps and sizing changes are the common cases.
- The customer's Jenkins job is a sequence of plain shell-script
  invocations. No vendor `Jenkinsfile`. The customer copy-pastes
  parameters between stages.
- Every resource — whether created by shell script or by CFN — carries
  a uniform tag set (see *Tagging*) so the customer can audit what
  belongs to the install regardless of which tool created it.

## Non-goals

- Multi-install isolation in a single AWS account. One install per
  account+region. The `InstallId` mechanism from the prior design is
  removed.
- Per-service IAM task roles. A single shared `cardinal-task-role` covers
  every ECS task. The trade-off (any task can read any `cardinal-*`
  secret) is documented in `permissions-lakerunner.md`.
- Customer-supplied KMS keys. AWS-managed keys for RDS, Secrets Manager,
  and S3.
- A `cardinal-deployer-role` template. Whatever role Jenkins runs as
  drives the CFN calls directly. Drop the `--role-arn` plumbing.
- CFN-managed updates of data-bearing resources. Once they exist, IT
  changes them out-of-band. The deployer never tries.

## Architecture

Four execution layers. Stages 1–2 are shell scripts run by a privileged
identity. Stages 3–4 are CFN stacks run by the deployer. Each stage
prints a paste-ready block of identifiers that the operator copies into
the next stage's parameters file. There is no `Fn::ImportValue`, no
nested-stack pass-through, no live API lookup inside any deploy script.

```
[shell script: cardinal-prereqs.sh]            (privileged identity, run once)
    creates 4 IAM roles + 3 SGs
    prints   role ARNs, SG IDs
        |
        v   (operator copies into next stage's flags / parameters file)
[shell script: cardinal-data-setup.sh]         (privileged identity, run once)
    creates RDS + DB master secret, S3 ingest bucket + lifecycle,
            SQS ingest queue + queue policy, S3->SQS notification,
            license / internal-keys / admin-key / maestro-db secrets,
            SSM params /cardinal/storage-profiles + /cardinal/api-keys
    prints   DB endpoint, bucket name, queue URL/ARN, secret ARNs,
             SSM param names
        |
        v   (operator copies into next stage's parameters file)
[CloudFormation: cardinal-infra-app]           (deployer, freely manageable)
    creates ECS cluster, ALB + listeners + rules + target groups,
            cloud-map namespace + service entries, log groups,
            optional cert-import Lambda
    outputs cluster ARN, ALB DNS, target group ARN per service,
            log group name per service, cloud-map service ARN per service
        |
        v   (operator copies into next stage's parameters file)
[CloudFormation: cardinal-lakerunner]          (deployer, freely manageable)
    creates ECS task definitions, ECS services,
            migration Lambda + custom resource
```

Why two shell scripts instead of one:

- `cardinal-prereqs.sh` creates IAM and SGs only. The data setup
  references both (DbSg goes on the RDS instance, the SGs identify
  trust boundaries). Running prereqs first means data-setup can validate
  the SGs exist before it begins.
- Different blast radii: prereqs is fast (seconds) and trivially
  reversible by hand; data-setup is slow (RDS create is 10–15 min) and
  involves resources IT will refuse to delete later. Keeping them in
  separate scripts means a prereqs-script bug can't accidentally cost
  the customer a wasted RDS create.

Why the data layer is a shell script instead of a CFN stack:

- The deployer principal cannot delete RDS, S3 buckets, secrets, or SSM
  params. CloudFormation rolls back failed CREATEs by deleting whatever
  it had created so far. Without delete permissions, a failed
  data-stack CREATE leaves a permanently broken stack and orphaned
  resources only IT can clean up. IT will never grant delete permissions
  to make rollback work.
- The data layer never gets updates from the deployer (IT changes are
  break-glass and out-of-band). CFN's value is declarative diff/apply;
  there is no diff/apply use case here.
- A shell script with explicit idempotency (each step checks "does this
  exist with the right config?" before creating) recovers from
  partial failures by re-running. No stuck-stack problem.

Why the app and lakerunner layers stay in CFN:

- Both contain only resource types the deployer can fully manage —
  ECS, ALB, target groups, listener rules, log groups, cloud-map,
  Lambda. Failed CREATEs roll back cleanly.
- The lakerunner stack's parameter surface (image tags, replica counts,
  CPU/memory) is the actual upgrade workflow — exactly where CFN's
  diff/apply earns its keep.

## Resource partition

### Shell script — `cardinal-prereqs.sh`

| Resource | Name | Trust / scope |
|---|---|---|
| `cardinal-task-role` | IAM role | `ecs-tasks.amazonaws.com`. Inline policy: S3 RW on the ingest bucket, SQS RW on the ingest queue, SSM read on `/cardinal/*`, Secrets Manager read on `cardinal-*`, CW Logs writes on `/cardinal/*`, `ecs:DescribeServices`/`UpdateService`/`ListTasks`/`DescribeTasks` (cluster-conditioned), `bedrock:InvokeModel*` on `foundation-model/*`. |
| `cardinal-execution-role` | IAM role | `ecs-tasks.amazonaws.com`. AWS-managed `AmazonECSTaskExecutionRolePolicy` + `secretsmanager:GetSecretValue` on `cardinal-*` + `ssm:GetParameter*` on `/cardinal/*`. |
| `cardinal-migration-lambda-role` | IAM role | `lambda.amazonaws.com`. `logs:*` on `*`, `ecs:RunTask`/`DescribeTasks` (cluster-conditioned), `iam:PassRole` on the task and execution role ARNs above. |
| `cardinal-cert-lambda-role` (optional) | IAM role | `lambda.amazonaws.com`. `logs:*`, `acm:ImportCertificate`, `acm:DeleteCertificate`/`AddTagsToCertificate`/`RemoveTagsFromCertificate`. Only created when `--cert-import` is passed. |
| `cardinal-task-sg` | SG | TCP 0-65535 from self (intra-cluster service-to-service via Cloud Map); TCP 0-65535 from `cardinal-alb-sg`. Egress all. |
| `cardinal-alb-sg` | SG | TCP 443 + TCP 9443 from `0.0.0.0/0` (= VPC-local since ALB is internal). Egress all. |
| `cardinal-db-sg` | SG | TCP 5432 from `cardinal-task-sg`. Egress all. |

Idempotency contract: matching resource → no-op, drifted resource →
exit 2 with a diff. There is no update path. If a policy must change,
IT removes the affected role/SG out-of-band and the operator re-runs
the script.

### Shell script — `cardinal-data-setup.sh`

| Group | Resources | Notes |
|---|---|---|
| Database | RDS Postgres `cardinal-db`, DB subnet group `cardinal-db-subnet-group`, DB master secret `cardinal-db-master`. | Master password generated by Secrets Manager (`get-random-password` then `create-secret`). The script writes a `SecretTargetAttachment`-equivalent shape into the secret JSON value (`{username, password, host, port, dbname, engine}`) once the DB is endpoint-stable. |
| Storage | S3 bucket `cardinal-ingest-${AccountId}-${Region}`, lifecycle rules (configurable retention days), SQS queue `cardinal-ingest`, queue policy granting `s3.amazonaws.com` SendMessage with `aws:SourceAccount` condition, S3→SQS bucket notification. | Ordering matters: queue first, queue policy second, then S3 bucket, then bucket notification (S3 validates the policy at notification-config time). |
| Config secrets | `cardinal-license` (NoEcho file input), `cardinal-internal-keys` (script generates), `cardinal-admin-key` (script generates), `cardinal-maestro-db` (script writes DB connection JSON). | Secrets Manager appends a 6-char random suffix to each ARN. The output JSON records the full ARN with suffix. |
| SSM params | `/cardinal/storage-profiles`, `/cardinal/api-keys`. | Plain `String` parameters (Secrets Manager handles the secret ones). |

Inputs (CLI flags):

- `--region`, `--vpc-id` (for DB subnet group lookup), `--private-subnets`
  (CSV of subnet IDs the DB subnet group covers).
- `--db-sg-id` (from prereqs output, applied to RDS).
- `--db-instance-class` (default `db.t3.medium`),
  `--db-allocated-storage` (default `100`).
- `--license-data-file` (path to license JSON; NoEcho-equivalent —
  read from disk, never echoed to stdout/stderr/logs).
- `--dex-admin-email`, `--dex-admin-password-hash-file`,
  `--oidc-superadmin-emails`. (DEX/OIDC config goes into
  `cardinal-internal-keys` or a dedicated maestro secret — TBD during
  implementation.)
- `--bucket-lifecycle-days` (default `7`).
- `--output-file ./cardinal-data-setup-output.json`.

Idempotency contract: same as prereqs. Each step independently checks
"does the named resource exist with the right config?" before creating.
Re-running after a partial failure picks up where it left off. Drift
detection: re-running on an installed deployment with different inputs
exits 2 with a diff (no automatic update path).

Output JSON: `{ "DbEndpoint": "...", "DbPort": 5432, "DbName": "lakerunner", "DbMasterSecretArn": "arn:...", "MaestroDbSecretArn": "arn:...", "IngestBucketName": "...", "IngestQueueUrl": "...", "IngestQueueArn": "...", "LicenseSecretArn": "...", "InternalKeysSecretArn": "...", "AdminKeySecretArn": "...", "StorageProfilesParamName": "/cardinal/storage-profiles", "ApiKeysParamName": "/cardinal/api-keys" }`.

### App stack — `cardinal-infra-app.yaml`

| Group | Resources |
|---|---|
| Cluster | ECS cluster `cardinal`, Cloud Map private DNS namespace, per-service Cloud Map service entries (one per ECS-service that exists in the lakerunner stack). |
| Load balancer | Internal ALB, 443 + 9443 listeners, per-service target groups (one per HTTP-fronted service), per-service listener rules with the priorities pinned in `listener_priorities.py`, optional cert-import Lambda + custom resource. |
| Logs | Per-service log groups (`/cardinal/<service>`) with retention pinned by the existing defaults table. |

Inputs (parameters):

- From shell-script outputs: `TaskSgId`, `AlbSgId`,
  `CertLambdaRoleArn` (only when PEM path).
- From customer: `VpcId`, `PrivateSubnets` (CSV), `CertificateArn`
  *or* `CertificateBody`/`CertificatePrivateKey`/`CertificateChain`
  (NoEcho), `LogRetentionDays`.

Outputs: `ClusterArn`, `ClusterName`, `AlbDnsName`,
`AlbHostedZoneId`, `<Service>TargetGroupArn` (one per HTTP-fronted
service), `<Service>LogGroupName` (one per ECS service),
`<Service>CloudMapServiceArn` (one per ECS service), `CertificateArn`
(pass-through for documentation when PEM path was used).

### Lakerunner stack — `cardinal-lakerunner.yaml`

| Group | Resources |
|---|---|
| Workloads | ECS task definitions and ECS services for the twelve services: query-api, query-worker, process-{logs,metrics,traces}, pubsub-sqs, sweeper, monitoring, admin-api, alert-evaluator, otel-collector, maestro (with bundled DEX). |
| Migration | Migration Lambda function (inline code), CFN custom resource that invokes `ecs:RunTask` on create and on `LakerunnerImage` parameter change. |

Inputs (parameters):

- From `cardinal-prereqs.sh` output: `TaskRoleArn`, `ExecutionRoleArn`,
  `MigrationLambdaRoleArn`, `TaskSgId`.
- From `cardinal-data-setup.sh` output: every value (DB endpoint, secret
  ARNs, SSM param names, bucket name, queue URL).
- From app-stack output: every value (cluster, target groups, log
  groups, cloud-map services).
- From customer: `PrivateSubnets` (CSV), per-service sizing
  (`QueryApiReplicas`, `QueryApiCpu`, `QueryApiMemory`, etc., the same
  set as today), and image parameters
  (`LakerunnerImage`, `MaestroImage`, `OtelImage`, `DexImage`).

Outputs: none required. (Customer-facing endpoints come from the app
stack's `AlbDnsName`.)

## Tagging

Every resource — script-created or CFN-created — carries the same tag
set:

| Tag | Value | Source |
|---|---|---|
| `Application` | `cardinal-lakerunner` | constant |
| `ManagedBy` | `cardinal-prereqs-script`, `cardinal-data-setup-script`, `cardinal-infra-app-stack`, or `cardinal-lakerunner-stack` | per-stage constant |
| `Component` | per-resource label, e.g. `task-role`, `task-sg`, `db`, `ingest-bucket`, `ingest-queue`, `cluster`, `alb`, `query-api-service` | per-resource |
| `Name` | `cardinal-<descriptor>` matching the physical name where one is set | per-resource |
| `cardinal:install-version` | the lakerunner template version that owns this resource (e.g., `v1.2.3`); tracks the version of the templates / scripts that last touched it | per-deploy |

Implementation:

- Both shell scripts pass the tag set on every `create-*` API call (or
  call `add-tags` / `tag-resource` immediately after for resource types
  that don't accept tags inline).
- Both CFN templates apply the tag set via troposphere's
  `Template.set_metadata`-style helper or per-resource `Tags=` arg
  (use the existing `naming.py` helper, generalized).
- A unit test (`test_tagging.py`) verifies the tag set the shell scripts
  apply matches the tag set the templates apply, modulo `ManagedBy`.

The `cardinal:install-version` tag is the only tag value the deployer
needs to be allowed to update — it changes on each lakerunner upgrade.
The script-created tags don't change after first set, since the scripts
don't run again.

## Naming contract

The shell scripts write IAM-policy resource ARNs at install time, before
any CFN stack exists. Those ARNs must match the resources the data-setup
script and the CFN templates later create. The match relies on
deterministic physical names locked in this design:

| Resource | Physical name | Created by | Used by IAM policy |
|---|---|---|---|
| ECS cluster | `cardinal` | app stack | `cardinal-task-role` (cluster-conditioned `ecs:Describe/Update/ListTasks`); `cardinal-migration-lambda-role` (cluster-conditioned `ecs:RunTask`). |
| S3 ingest bucket | `cardinal-ingest-${AccountId}-${Region}` | data-setup script | `cardinal-task-role` (S3 RW). |
| SQS ingest queue | `cardinal-ingest` | data-setup script | `cardinal-task-role` (SQS RW). |
| Migration ECS task definition family | `cardinal-migrator` | lakerunner stack | `cardinal-migration-lambda-role` (`ecs:RunTask` on `task-definition/cardinal-migrator:*`). |
| Per-service log groups | `/cardinal/<service>` | app stack | `cardinal-task-role` (CW Logs writes wildcarded as `/cardinal/*`). |
| SSM params | `/cardinal/storage-profiles`, `/cardinal/api-keys` | data-setup script | `cardinal-task-role` and `cardinal-execution-role` (`ssm:GetParameter*` on `/cardinal/*`). |
| Secrets | name pattern `cardinal-*` (e.g., `cardinal-db-master`, `cardinal-license`, `cardinal-internal-keys`, `cardinal-admin-key`, `cardinal-maestro-db`) — Secrets Manager appends a 6-char random suffix | data-setup script | `cardinal-task-role` and `cardinal-execution-role` (`secretsmanager:GetSecretValue` on `cardinal-*`). |

Two consequences:

- The app stack and the data-setup script **must declare these physical
  names explicitly** (counter to the existing CLAUDE.md preference for
  CFN-generated names). Locking the names is the cost of putting IAM
  out of CFN.
- The naming contract becomes a **shared invariant** across
  `prereqs/render.py`, `data_setup/render.py`, and the app and
  lakerunner template generators. A unit test
  (`test_naming_contract.py`) compares the ARN patterns the
  scripts/templates write against the physical names the
  scripts/templates declare, and fails the build on any drift.

## Deployer permissions

The deployer principal (Jenkins's IAM identity) does NOT need:

- Any `iam:*Role` action except `iam:PassRole` (see below).
- Any `ec2:*SecurityGroup*` action.
- Any `rds:*` action.
- Any `s3:CreateBucket` / `s3:DeleteBucket` / bucket-level write action
  (it does need `s3:GetBucketLocation` and `s3:ListBucket` for
  diagnostic purposes only).
- Any `secretsmanager:CreateSecret` / `DeleteSecret` / `UpdateSecret` —
  the deployer references existing secret ARNs but never modifies them.
- Any `ssm:PutParameter` / `DeleteParameter`.
- Any `sqs:CreateQueue` / `DeleteQueue` / `SetQueueAttributes` — the
  deployer references the existing queue ARN/URL but never modifies it.

The deployer DOES need:

- `iam:PassRole` on `cardinal-task-role`, `cardinal-execution-role`,
  `cardinal-migration-lambda-role`, `cardinal-cert-lambda-role`. This
  is what lets ECS task definitions, ECS services, and Lambda functions
  reference the pre-created roles. `iam:PassRole` is independent of
  create/update/delete grants and is the standard "consume a role"
  permission.
- `iam:CreateServiceLinkedRole` for `ecs.amazonaws.com` and
  `elasticloadbalancing.amazonaws.com` — fresh AWS accounts may not
  have the required `AWSServiceRoleFor*` roles yet, and CFN cannot
  create resources of those services without them. Alternative:
  customer's IT pre-creates the two service-linked roles (one-time, per
  account). RDS service-linked role is irrelevant since the data-setup
  script (privileged identity) creates the RDS instance.
- Full create / update / delete on every resource type the app and
  lakerunner templates touch: `cloudformation:*`, `ecs:*` (cluster,
  task def, service — with prefix-scoped resource ARNs where the API
  supports it), `elasticloadbalancingv2:*`, `logs:*` (on `/cardinal/*`),
  `servicediscovery:*`, `lambda:*` (on `cardinal-*` function names),
  `acm:*` (only when cert-import is in use), the `ec2:Describe*` calls
  CFN makes for VPC/subnet validation. All scoped to `cardinal-*` ARNs
  / `/cardinal/*` prefixes where the AWS API allows.

The rewritten `permissions-infrastructure.md` enumerates the full
deployer policy with these adjustments. Critically, every API action
in the deployer's policy is one the app and lakerunner stacks use
during normal CREATE / UPDATE / DELETE — so failed CREATEs roll back
cleanly without surprise permission gaps.

## Known risks and mitigations

### Out-of-band drift

Roles, SGs, RDS, S3 bucket, secrets, and SSM params live outside CFN's
control. If the customer's IT team modifies any of them — tightens an
inline policy, narrows an SG ingress, resizes the DB, rotates a secret
incompatibly — the templates keep deploying but services may fail at
runtime.

Mitigation:

- `cardinal-prereqs.sh` and `cardinal-data-setup.sh` both include a
  `--verify` mode that re-checks every resource they own against
  expected state without creating anything. Customer can run them as
  pre-deploy gates from Jenkins.
- The naming-contract test catches drift between the scripts' ARN
  assumptions and the templates' physical names at build time.
- The runbook documents that any IT-side change to a prereq, secret,
  bucket lifecycle, or DB sizing is a coordinated change with the
  vendor, not a freelance tweak.

### Failed initial run of `cardinal-data-setup.sh`

The script may fail mid-run (e.g., RDS create succeeds but bucket
creation fails because the bucket name was taken in a prior abandoned
attempt). Re-running picks up where it left off, but the operator must
understand the idempotency rules.

Mitigation:

- Each step in the script logs `creating X` / `X already exists, skipping`
  / `X exists but config differs, exit 2` clearly.
- The script has no auto-retry; the operator decides when to re-run.
- Output JSON is written incrementally (each successful step appends to
  it) so a partial run still produces something usable for follow-on
  diagnosis.
- Exit codes match prereqs: 0 success or full no-op, 1 AWS failure, 2
  drift detected.

### Single shared task role

A compromise of any single ECS task gives access to every `cardinal-*`
secret and the full ingest bucket. Documented as an accepted trade-off
in `permissions-lakerunner.md`. A future tightening — splitting back to
per-service roles — is a non-breaking change to the prereqs script and
a parameter-surface expansion on the lakerunner template (one
`TaskRoleArn` parameter per service instead of one shared); existing
installs continue to work with the single role.

### Tag drift after install-version bumps

`cardinal:install-version` is the one tag value that changes on each
lakerunner upgrade. The deployer can update it on app- and lakerunner-
stack resources but cannot update it on prereqs- or data-setup-created
resources. Accepted: the script-created resources keep the version tag
that was applied at first install, and the CFN-managed resources track
the current deploy. The runbook documents this so the customer doesn't
chase a "false drift" alert.

## Code layout

```
src/cardinal_cfn/
    __init__.py
    defaults.py             # unchanged: cardinal-defaults.yaml loader
    parameters.py           # unchanged: shared parameter / NoEcho helpers
    images.py               # unchanged: image-override parameter machinery
    policies.py             # unchanged: DeletionPolicy / UpdateReplacePolicy table
    listener_priorities.py  # unchanged
    naming.py               # simplified: tags + name conventions, no InstallId;
                            # exports the shared tag set used by both shell-
                            # script generators and CFN templates
    iam_policies.py         # NEW: shared inline-policy builders
    cardinal_vpc.py         # unchanged
    prereqs/
        __init__.py
        roles.py            # role/policy data structures
        security_groups.py  # SG/ingress data structures
        render.py           # generates cardinal-prereqs.sh
    data_setup/
        __init__.py
        rds.py              # RDS, subnet group, master secret
        storage.py          # S3 bucket + lifecycle, SQS queue + policy, S3->SQS
        secrets.py          # license / internal-keys / admin-key / maestro-db
        ssm.py              # SSM params
        render.py           # generates cardinal-data-setup.sh
    app/
        __init__.py
        cluster.py
        alb.py
        cert.py             # optional cert-import Lambda
        logs.py
        cloudmap.py
        root.py             # cardinal-infra-app root template
    lakerunner/
        __init__.py
        services_query.py
        services_process.py
        services_control.py
        otel.py
        maestro.py
        migration.py
        root.py             # cardinal-lakerunner root template
```

Removed:

- `src/cardinal_cfn/install_id.py`
- `src/cardinal_cfn/children/` (its modules are reshuffled into
  `data_setup/`, `app/`, `lakerunner/`)
- The current `root.py`

Each of the two new CFN templates is a single flat template — no
nested children. Resource counts stay well under the 500 hard cap (app
~40, lakerunner ~25).

`make build` produces:

```
generated-templates/
    cardinal-vpc.yaml
    cardinal-prereqs.sh                       # executable
    cardinal-prereqs-output-template.json
    cardinal-data-setup.sh                    # executable
    cardinal-data-setup-output-template.json
    cardinal-infra-app.yaml
    cardinal-lakerunner.yaml
docs/operations/install-parameters.md         # generated parameter table
```

## Operator scripts

All scripts are POSIX shell, depend only on `aws` v2 and `jq`, and are
published as part of the release tag so the customer's Jenkins job
pulls them by version.

### `cardinal-prereqs.sh` (NEW, runs once)

```
cardinal-prereqs.sh \
    --region us-east-2 \
    --vpc-id vpc-abc123 \
    --output-file ./cardinal-prereqs-output.json \
    [--cert-import] \
    [--name-prefix cardinal] \
    [--verify]                       # check existing state, don't create
```

- Idempotent: existing matching resource is a no-op, drifted resource
  exits 2 with a diff.
- Output JSON keyed by CFN parameter name. Same content also printed as
  a `Key=...,Value=...` block ready for `--parameter-overrides`.
- Exit codes: 0 success or no-op, 1 AWS failure, 2 input/preflight/drift.

### `cardinal-data-setup.sh` (NEW, runs once)

```
cardinal-data-setup.sh \
    --region us-east-2 \
    --vpc-id vpc-abc123 \
    --private-subnets subnet-a,subnet-b \
    --db-sg-id sg-... \                            # from prereqs output
    --license-data-file ./license.json \
    --dex-admin-email admin@example.com \
    --dex-admin-password-hash-file ./hash.txt \
    --oidc-superadmin-emails alice@x,bob@x \
    --output-file ./cardinal-data-setup-output.json \
    [--db-instance-class db.t3.medium] \
    [--db-allocated-storage 100] \
    [--bucket-lifecycle-days 7] \
    [--verify]
```

- Same idempotency contract as prereqs.
- Output JSON written incrementally; readable even after a partial run.
- Secrets read from file paths, never echoed.

### `deploy-cardinal-stack.sh` (NEW, generic — replaces deploy-lakerunner.sh)

```
deploy-cardinal-stack.sh \
    --kind app|lakerunner \
    --stack-name <NAME> \
    --region <REGION> \
    --version <vX.Y.Z> \
    --parameters-file <PATH>.json \
    [--no-execute] \
    [--template-base-url <URL>]
```

- One script for both CFN stacks. The `--kind` flag picks the
  template URL.
- Auto-detects create vs update via `describe-stacks`.
- Stale `REVIEW_IN_PROGRESS` / `ROLLBACK_COMPLETE` records are deleted
  before a fresh CREATE (same behavior as the current
  `deploy-lakerunner.sh`).
- `--parameters-file` is a JSON array of `{ParameterKey, ParameterValue}`
  fed to AWS CLI via `file://`.
- On UPDATE for `kind=lakerunner`, image params auto-refresh to the
  new template's defaults (same as today's `--refresh-image-defaults`
  default-on behavior). For `kind=app`, all params use previous values
  unless re-specified.
- `TemplateBaseUrl` is always overridden to track `--version`.
- Change set + describe + execute + wait, with no-op detection from the
  existing classifier.

### `teardown-cardinal-stack.sh` (NEW, generic)

```
teardown-cardinal-stack.sh \
    --kind app|lakerunner \
    --stack-name <NAME> \
    --region <REGION>
```

- Straight `delete-stack` + `wait` for either kind. Both stacks contain
  only resources the deployer can delete.
- Does NOT delete prereqs (roles, SGs) or data-setup resources (RDS,
  buckets, secrets) — those are out-of-CFN. A separate
  `delete-prereqs.sh` / `delete-data-setup.sh` pair (privileged
  identity, never automated) is provided for sandbox cleanup; not part
  of the standard runbook.

### Removed

- `scripts/deploy-lakerunner.sh`
- `scripts/teardown-lakerunner.sh`
- `jenkins/Jenkinsfile.lakerunner`
- All `--deployer-role-arn` / `--role-arn` plumbing across remaining
  scripts.

### Customer runbook

`docs/operations/installing.md` (NEW) walks through:

1. Privileged identity runs `cardinal-prereqs.sh`. Save output JSON.
2. Privileged identity runs `cardinal-data-setup.sh`. Save output JSON.
3. Deployer fills in `cardinal-app-params.example.json` from the two
   output JSONs + customer inputs. Run
   `deploy-cardinal-stack.sh --kind app`. Save outputs.
4. Deployer fills in `cardinal-lakerunner-params.example.json` from the
   two output JSONs + app-stack outputs + sizing/image inputs. Run
   `deploy-cardinal-stack.sh --kind lakerunner`.
5. Upgrade: deployer edits the lakerunner params file (typically just
   the image line), re-runs step 4.

Each `*-params.example.json` is generated alongside the templates: a
JSON file with every parameter, an empty `""` value, and a `_note` per
entry pointing at "prereqs output" / "data-setup output" / "app stack
output" / "customer input" / "default".

Existing docs that get rewritten or retired: `deploying.md`,
`jenkins-deploy.md`, `tearing-down.md`,
`permissions-infrastructure.md`, `permissions-lakerunner.md`,
`end-to-end-test-plan.md`.

## Testing

```
tests/
    conftest.py                       # unchanged
    unit/
        test_iam_policies.py          # NEW: shared policy builders
        test_naming.py                # adjusted, no InstallId
        test_naming_contract.py       # NEW: physical names match IAM ARNs
        test_tagging.py               # NEW: shell scripts and CFN templates
                                      #      apply the same tag set
        test_prereqs_render.py        # NEW: shell script generation
        test_data_setup_render.py     # NEW: shell script generation
        test_parameter_table.py       # NEW: install-parameters.md is in sync
                                      #      with each script/template's params
    templates/
        test_app_template.py          # NEW: cloud-radar assertions
        test_lakerunner_template.py   # NEW
    scripts/
        test_prereqs_script.py        # NEW: shellcheck + golden-file diff
        test_data_setup_script.py     # NEW
        test_deploy_script.py         # adapted from existing
                                      #     test_deploy_lakerunner_lint.py
                                      #     and the generic deploy script
```

Per-template tests retire alongside their generators. The new
per-template/per-script tests verify the handoff chain end to end:
prereqs script outputs align with data-setup script inputs; both
script outputs align with app-stack parameters; all of those align
with lakerunner-stack parameters. The full chain is testable without
a live AWS.

`make build` continues to run cfn-lint over every generated CFN
template. Both shell-script generator outputs are run through
`shellcheck`.

## Migration from the prior design

This spec replaces the in-flight refactor (`2026-04-28-...`). The
existing repo state is the "two roots, twelve nested children" target
from that spec. Migration:

1. New code (`prereqs/`, `data_setup/`, `app/`, `lakerunner/`
   packages, new scripts, new docs) lands alongside the existing code
   in a single PR series.
2. The existing `src/cardinal_cfn/children/` directory and current
   `root.py` are deleted at the end of the series.
3. The release-publishing workflow (`release.yml`) gains the two new
   templates + the two new shell scripts + the parameter example files;
   the single `cardinal-lakerunner.yaml` + `cardinal-lakerunner/`
   prefix layout is replaced.
4. Existing customer installs (if any) are not auto-migrated. Migration
   from a current-design install to the new layout is a documented
   manual procedure: tear down the lakerunner root keeping
   `Retain`-policied resources alive, hand-edit the surviving
   resources to match the new naming contract (rename buckets/queues
   /secrets if needed), then run the new scripts in `--verify` mode
   to confirm the existing resources are accepted, then create the
   two new CFN stacks afresh.

## Open questions

None blocking implementation. All architectural questions raised during
brainstorming have been resolved:

- Constraint nature: deployer can create, cannot update or delete IAM
  roles, RDS, S3 buckets, secrets, or SGs. IT will not grant delete
  permissions.
- Change cadence after install: customer's IT team handles changes via
  break-glass identity, out of band of our tooling.
- ALB DNS stability: required across upgrades. ALB lives in the app
  stack, never recreated by lakerunner.
- Multi-install isolation: dropped. One install per account+region.
- Cross-stack wiring: parameters only, manual copy. No `Fn::ImportValue`,
  no auto-fetch.
- Per-service IAM roles: collapsed into a single `cardinal-task-role`.
- Data layer in CFN vs script: shell script. CFN can't roll back failed
  CREATEs without delete permissions, and the data layer never gets
  diff/apply use anyway.
- Deployer role pattern: removed. Jenkins's own identity drives the
  CFN calls.
- Tagging: uniform tag set across script-created and CFN-created
  resources, enforced by a build-time test.
