# Changelog

This file records **operational and system-level changes** to the Cardinal
Lakerunner CloudFormation stacks — what an operator updating an existing install
needs to know: new or changed parameters, changed defaults, new manual steps,
image bumps, IAM and security-group changes, and resource replacements. It is
not an exhaustive code log; see the git history and linked PRs for that.

Versions are the published Git tags / S3 prefixes (`v0.0.NNN`). To bring an
install up to date, read every entry from the version you are on up to your
target version and apply the noted upgrade actions. Earliest recorded version is
v0.0.114.

## v0.0.122

- **`deploy-satellite-services.sh` now echoes the inputs it can see.** Before
  validating, the driver prints every required and optional input visible to the
  process (value, or `<unset>`) to stderr, so a `missing required: ...` failure
  is easy to diagnose. The usual cause is a value set as a plain shell variable
  but not exported, so the driver — a separate process — never receives it and
  it shows as `<unset>`. No behavior or parameter change.
  - Upgrade action: none.

## v0.0.121

- **`PubsubAutoRegister` now defaults to `true`.** New `lakerunner-services`
  deploys auto-register unseen satellite raw-bucket orgs and route their cooked
  output to `PubsubAutoRegisterWritesToInstance` (default `1`) without an extra
  flag. (#186)
  - Upgrade action: none to keep it on. To preserve the old off-by-default
    behavior, pass `PUBSUB_AUTOREGISTER=false` (driver) / set
    `PubsubAutoRegister=false`. Existing stacks keep whatever value they were
    deployed with until you re-apply.
- **pubsub-sqs can now consume multiple satellite SQS queues.** Beyond the
  primary (group-0) `QueueUrl`/`QueueRoleArn`, `lakerunner-services` adds
  numbered queue groups 1..10: `QueueUrl<n>` / `QueueRegion<n>` / `QueueRoleArn<n>`
  params, emitted as `SQS_QUEUE_URL_<n>` / `SQS_REGION_<n>` / `SQS_ROLE_ARN_<n>`
  on the pubsub-sqs container only when set. Each group carries its own region
  and assume-role, so the central poller reaches satellite queues in other
  accounts/regions. Driver env: `QUEUE_URL_<n>` / `QUEUE_REGION_<n>` /
  `QUEUE_ROLE_ARN_<n>` (`QUEUE_REGION_<n>` defaults to `REGION`).
  - Upgrade action: none. Set the numbered env vars to add satellites; the
    ceiling is 10 (bump `MAX_ADDITIONAL_QUEUES` to raise it).

## v0.0.120

- **Satellite installs are fully decoupled from the central account.** A
  satellite (`satellite-infra-base` + `satellite-services`, deployed as a
  same-account/region pair) may now live in the **same or a different AWS
  account** than the central lakerunner install — no satellite driver reads the
  central `lakerunner-infra-base` stack (a cross-account `describe-stacks` that
  could never have worked). (#185)
  - **Collector no longer uses a license.** `satellite-services` drops the
    `LicenseSecretArn` parameter, the collector exec-role
    `secretsmanager:GetSecretValue`, and the `LICENSE_DATA` container secret —
    the otel-collector image needs no license.
  - **Central principal is supplied directly.** `deploy-satellite-infra-base.sh`
    now takes `LAKERUNNER_PRINCIPAL` (the central `ProcessRoleArn`, read once out
    of band) instead of mapping it from the `lakerunner-infra-base` stack, and no
    longer requires `INFRA_BASE_STACK`.
  - `deploy-satellite-services.sh` no longer requires `INFRA_BASE_STACK` (it only
    needed it for the license); it still pulls `RawBucketName` from the
    satellite's own paired `satellite-infra-base` in the same account.
  - Upgrade action: when redeploying a satellite, set `LAKERUNNER_PRINCIPAL` on
    the infra-base driver and drop `INFRA_BASE_STACK` from both satellite drivers.
    The collector's own task/exec roles are unchanged (always self-contained). No
    data-bearing resource is replaced.

## v0.0.119

- **Satellite self-telemetry traces now process (IAM widening).** The
  `cardinal-satellite-access` role gains `s3:PutObject` on the satellite raw
  bucket (Sid `RawBucketReadDelete` -> `RawBucketReadWriteDelete`) in
  `satellite-infra-base`. Previously satellite trace processing failed with
  `s3:PutObject AccessDenied` while logs and metrics succeeded — the lakerunner
  trace worklane writes cooked segments back to the raw bucket instead of
  following the cooked-bucket redirect that logs/metrics use. (#183)
  - Upgrade action: update the `satellite-infra-base` stack to apply the widened
    role. Only relevant if you run the satellite / self-telemetry path.
  - Caveat: until the lakerunner code splits read vs. write for the trace
    worklane, cooked trace parquet lands in the raw bucket (subject to its
    lifecycle). The grant is removable once that lands.

## v0.0.118

- **Internal cleanup, no template change.** Removed dead per-service
  `cpu`/`memory_mib`/`replicas` fields and a dead `monitoring.ingress` block from
  the merged control tier in `cardinal-defaults.yaml`. The generated
  `services-control` template is byte-identical to v0.0.117. No upgrade action.
  (#182)

## v0.0.117

- **Container image bump.** Default images updated: `lakerunner`
  v1.33.0 -> **v1.40.0**, `maestro` v1.46.4 -> **v1.50.0**. `otel` (v1.8.0) and
  `dex` (v0.1.0) unchanged. (#181)
  - The `lakerunner` image is shared by the service tasks and the DB migrator, so
    updating reruns the migrator (the `MigrationStack`) before the service-tier
    stacks update; the service tiers only deploy after migrations succeed, and a
    failed migration rolls the update back.
  - Upgrade action: none if you use the defaults. If you pin `LakerunnerImage` /
    `MaestroImage` via parameters, set them to the new versions explicitly.

## v0.0.116

- **Security groups no longer specify inline egress (SCP compatibility).** All
  security groups (application ALB and task SGs, RDS, satellite ALB/task, and the
  lrdev VPC-endpoint SG) drop their inline all-allow `SecurityGroupEgress` and
  keep AWS's auto-created default all-allow egress instead. Network behavior is
  identical. (#180)
  - Why it matters: with an inline egress rule, CloudFormation calls
    `ec2:RevokeSecurityGroupEgress` during SG creation to swap the default rule —
    even when the rule is byte-for-byte the default. Accounts whose SCP denies
    that revoke (e.g. Control Tower "deny VPC-destructive" guardrails) previously
    hit `CREATE_FAILED` on `infra-base`. This unblocks them.
  - Upgrade action: none for existing installs; relevant to new installs in
    SCP-restricted accounts.
- **New teardown tool (dev only).** `dev-scripts/sweep-stranded-resources.sh`
  deletes resources left in `DELETE_FAILED` / `DELETE_SKIPPED` after a failed
  stack delete, via a one-shot privileged Fargate task (requires a
  caller-supplied superadmin task role). Not published to customers. (#179)

## v0.0.115

- **S3 bucket Public Access Block is now opt-in (default off).** New parameter
  **`ConfigureBucketPublicAccessBlock`** (default `false`) on
  `lakerunner-infra-base` (cooked bucket) and `satellite-infra-base` (raw
  bucket). By default the `PublicAccessBlockConfiguration` property is omitted
  entirely; set the parameter to `true` to restore the explicit block. Driver
  passthrough: `CONFIGURE_BUCKET_PUBLIC_ACCESS_BLOCK`. (#178)
  - Why it matters: setting the block requires `s3:PutBucketPublicAccessBlock`,
    which some deployer roles lack — those deploys previously failed. Buckets stay
    non-public via AWS account/bucket default BPA; neither bucket carries any
    public policy or ACL.
  - Upgrade action: none required. If your org mandates an explicit per-bucket
    block and your deployer role holds the permission, set
    `ConfigureBucketPublicAccessBlock=true`.

## v0.0.114

- **Database engine reverted from Aurora back to a single RDS instance.**
  `cardinal-lakerunner-infra-rds` is once again an `AWS::RDS::DBInstance`
  (PostgreSQL, `db.r7g.large`, gp3, `DBAllocatedStorage` parameter), reverting
  the Aurora PostgreSQL cluster that shipped in v0.0.113. (#175)
  - **CRITICAL upgrade action — only if you deployed v0.0.113 (Aurora):**
    updating from v0.0.113 to v0.0.114 or later changes the database from an
    Aurora `DBCluster` to an RDS `DBInstance`. These are different resource types,
    so CloudFormation **removes the Aurora cluster and creates a new, empty RDS
    instance** — data does not migrate automatically. The Aurora cluster
    snapshots on removal per its deletion policy, but the new instance starts
    empty. Plan a data migration (snapshot/restore or dump/load) before updating.
  - Installs on v0.0.112 or earlier (already RDS) are unaffected — for them this
    is a no-op.
- **Deploy drivers accept license and cert material as direct strings (files now
  optional).** The chained per-stack drivers take secrets inline; the `_FILE`
  path variants remain as fallbacks (additive, non-breaking). (#177)
  - `infra-base`: `LICENSE_DATA` (the `z64:...` token as a string, primary) or
    `LICENSE_DATA_FILE` (path fallback) — one is required.
  - `services` (cert): `CERTIFICATE_BODY` / `CERTIFICATE_PRIVATE_KEY` /
    `CERTIFICATE_CHAIN` as PEM strings, with `CERTIFICATE_*_FILE` path fallbacks
    (the string wins when both are set). The create-only self-signed fallback is
    unchanged.
- **Script layout: `scripts/` is now customer-facing drivers only.** Lifecycle
  and dev tooling moved to `dev-scripts/` — including `cleanup-lakerunner.sh` and
  `teardown-lakerunner.sh` (the former `deploy-scripts/` was renamed to
  `dev-scripts/`). (#176)
  - Upgrade action: if you referenced `scripts/cleanup-lakerunner.sh` or
    `scripts/teardown-lakerunner.sh`, repoint to `dev-scripts/`.
