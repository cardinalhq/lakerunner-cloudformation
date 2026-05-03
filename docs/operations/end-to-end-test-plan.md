# End-to-end test plan: deploy, upgrade, tear down

This is the manual / semi-automated acceptance test for a release candidate of
the `cardinal-lakerunner` CloudFormation stack. It exercises the full
lifecycle from a clean account through deploy, runtime convergence, in-place
update, tear-down, and final cleanup of intentionally-retained resources.

## Scope

In scope:

- The `cardinal-lakerunner` root template and its twelve nested children.
- The `cardinal-cfn-deployer` role (assumed pre-existing — its lifecycle is
  separate).
- The customer VPC (assumed pre-existing — either `cardinal-vpc.yaml` from a
  prior run, or a customer-owned VPC).
- `scripts/teardown-lakerunner.sh` and the retained-resource cleanup it
  performs.

Out of scope:

- The `cardinal-vpc.yaml` template lifecycle (covered by a separate VPC test).
- The `cardinal-cfn-deployer` template lifecycle.
- Customer-paid traffic load tests (this is a deployment / lifecycle test).

## Pre-flight (run once before the whole pass)

Confirm before starting any of the test phases:

- AWS account ID, region, and profile are noted in the test log. Tear-down
  scripts target by region; getting this wrong is destructive.
- `cardinal-cfn-deployer` role exists and its ARN is captured. `aws iam
  get-role --role-name cardinal-cfn-deployer` should succeed.
- The role's policy has `rds:CreateDBSnapshot`, `rds:DescribeDBSnapshots`,
  and `s3:DeleteObject` (these were added in PRs #65 / #66 — older roles
  will fail tear-down). Re-deploy the role from the current
  `cardinal-deployer-role.yaml` if in doubt.
- VPC has at least two private subnets in distinct AZs and at least two
  public subnets (for an internet-facing ALB) — capture all subnet IDs.
- Test account has *no* leftover `cardinal-*` resources from prior runs.
  Run the discovery query from the "Tag-based discovery" check below; if
  it returns anything, run the final cleanup phase before starting.
- Release version under test is recorded (git SHA + tag, S3 prefix, image
  tags / digests).
- A scratch directory is created for captured state files; one
  `state-<phase>.json` per phase will be archived there.

## Phase 1: deploy from clean state

### 1a. Create the stack

- Template: `cardinal-lakerunner.yaml` from the release candidate's S3 prefix
  (or `--template-body` from the local `generated-templates/` dir for an
  unreleased SHA).
- Parameters: from the test-account `params.json` (see memory:
  "Deploy params location").
- Use `--role-arn $DEPLOYER_ROLE_ARN`. Without it, IAM-touching rollbacks
  can wedge the stack.
- Capabilities: `CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND`.

Confirm:

- `create-stack` returns a StackId without error.
- The `cardinal-cfn-deployer` role is the role recorded under the stack's
  `RoleARN` (visible in the console or via `describe-stacks`).

### 1b. Wait for create to complete

`aws cloudformation wait stack-create-complete --stack-name <name>`.

Confirm:

- Final `StackStatus` is `CREATE_COMPLETE`. Anything ending in `_FAILED` or
  `ROLLBACK_*` is a hard fail — capture all events
  (`describe-stack-events`) and the events of every nested child before
  retrying.
- Wall-clock time is within the expected window (10-25 minutes is typical;
  >40 minutes suggests something is stuck).
- Outputs are present and non-empty: `InstallIdShort`, `InstallIdLong`,
  `AlbDnsName`, `MaestroUrl` (if applicable).
- All twelve nested children reached `CREATE_COMPLETE` — list with
  `aws cloudformation list-stack-resources --stack-name <name>
   --query 'StackResourceSummaries[?ResourceType==`AWS::CloudFormation::Stack`]'`.

### 1c. Service convergence

For each ECS service (across `services-query`, `services-process`,
`services-control`, `otel`, `maestro`):

- `aws ecs describe-services` → `runningCount == desiredCount` and
  `deployments[0].rolloutState == COMPLETED`.
- `deployments` length is 1 (no in-flight rollout left over from create).
- Most recent log stream for the service has at least one application log
  line within the last 5 minutes (proves the task actually started its
  process, not just that ECS placed it).

For the database:

- `aws rds describe-db-instances` → `DBInstanceStatus == available`.
- `aws rds describe-db-instances --query 'DBInstances[0].TagList'` includes
  the standard cardinal tag set (see "Tag-based discovery" below).

For the migration:

- The migration custom resource shows `CREATE_COMPLETE` in stack events.
- The migration ECS task ran exactly once — find it in the cluster's
  stopped tasks, exit code 0.
- Stack event for the custom resource includes the
  `cardinal-migration-<InstallIdLong>` physical id.

For the ALB:

- `aws elbv2 describe-target-health` for every target group → all targets
  `healthy`.
- HTTP HEAD against `https://<AlbDnsName>/` returns a non-5xx status (the
  exact status depends on routing rules; the goal is "ALB + listener +
  target groups responsive").

For Maestro / DEX (if enabled):

- `https://<MaestroUrl>/api/me` returns 401 unauthenticated (proves the
  /api routes are reachable; the Dex OIDC gotchas live in memory).
- DEX login page renders at `/dex/auth`.

Capture: `state-phase1.json` from `scripts/teardown-lakerunner.sh
--internal-format-plan` (run with `--stack-name ... --region ...` but
without `--yes` — the dry-run prints the survivor list and exits 0). Save
the install id, bucket name, and DB instance id from the captured state.

### 1d. Tag-based discovery

Run a Resource Groups Tagging API query to confirm everything is
discoverable:

```sh
aws resourcegroupstaggingapi get-resources \
  --tag-filters Key=Project,Values=cardinal \
                Key=ManagedBy,Values=cardinal-cfn \
  --region $REGION \
  --query 'ResourceTagMappingList[].ResourceARN' --output text | wc -l
```

Confirm:

- The count is non-trivial (dozens of ARNs — at minimum the bucket, the
  queue, the DB, log groups, target groups, services, the cluster, the
  ALB, and the secrets).
- The Resource Groups Tagging API only supports prefix-`*` wildcards
  (`Values=cardinal-` matches `cardinal-anything`), so a single tag query
  cannot scope to a specific install via the `Name` tag. To narrow to
  *this* install, run the query above and then post-filter the ARN list
  for the captured `InstallIdShort`:

  ```sh
  aws resourcegroupstaggingapi get-resources \
    --tag-filters Key=Project,Values=cardinal \
                  Key=ManagedBy,Values=cardinal-cfn \
    --region $REGION \
    --query 'ResourceTagMappingList[].[ResourceARN, Tags[?Key==`Name`]|[0].Value]' \
    --output text \
    | grep "$INSTALL_ID_SHORT" | wc -l
  ```

  The filtered count should match what you'd expect for this install
  (bucket, queue, DB, target groups, services, cluster, ALB, secrets,
  log groups, SSM params).
- Spot-check the RDS DB and the ingest bucket directly by their
  deterministic names:

  ```sh
  aws rds describe-db-instances --region $REGION \
    --db-instance-identifier cardinal-lakerunner-databasestack-... \
    --query 'DBInstances[0].TagList'
  aws s3api get-bucket-tagging --region $REGION \
    --bucket cardinal-ingest-$ACCOUNT-$REGION-$INSTALL_ID_LONG
  ```

  Both should report `Project=cardinal`, `ManagedBy=cardinal-cfn`,
  `Component=database` / `Component=storage`, and a `Name` whose suffix
  is the captured `InstallIdShort` / `InstallIdLong`.
- Anything **without** these tags is a bug — file an issue against the
  child template that owns it.

### 1e. Multi-install discrimination

Deploy a second copy of the same release into the same account + region with
a different stack name (e.g. `cardinal-lakerunner-b`) and the same VPC.

Confirm:

- Both stacks reach `CREATE_COMPLETE`.
- The two installs have *different* `InstallIdShort` and `InstallIdLong`
  outputs (these are derived from each root's `AWS::StackId`, so collision
  is essentially impossible — the test is "did we propagate them
  correctly").
- A tag query for `Key=Name,Values=cardinal-*-<InstallIdShort-A>`
  returns only stack-A resources; the same query with the B id returns
  only stack-B resources. Zero overlap.
- Both DB instances exist in parallel and are independently identifiable
  by their `Name` tag.
- Both ingest buckets exist in parallel —
  `cardinal-ingest-<account>-<region>-<InstallIdLong-A>` and
  `...-<InstallIdLong-B>` — no name collision.
- SSM parameters for the two installs live under disjoint prefixes
  (`/cardinal/<InstallIdLong-A>/...` vs `/cardinal/<InstallIdLong-B>/...`).

If the dual-deploy passes, **delete stack B immediately** with the tear-down
script (Phase 3 procedure) before continuing to Phase 2 — the rest of the
test only exercises stack A. Confirm afterwards (via the same tag query)
that stack-A resources are untouched.

## Phase 2: in-place update (mimic upgrade)

The goal is to trigger enough resource churn to exercise the rolling
deployment path on every service tier without replacing the stateful
resources (DB, bucket, secrets).

### 2a. Pick a parameter delta

Use a delta that touches multiple service tiers and at least one shared
resource. Suggested set (any one of these is enough; doing all three
exercises more):

- Bump `LakerunnerImage` to a different tag/digest. This **also reruns the
  migration custom resource** (see CLAUDE.md). Records confirm the
  `MigrationVersion` trigger works.
- Change `MaxPods` (or the equivalent service desired-count parameter) on
  one service to force an ECS deployment.
- Edit a tunable env-var parameter (e.g. log level) to force a new task
  definition revision on every service that consumes it.

Avoid for this phase: parameter changes that *replace* the DB or the
bucket. Those are tested elsewhere (and are by design destructive).

### 2b. Apply the update

`aws cloudformation update-stack ... --role-arn $DEPLOYER_ROLE_ARN
--use-previous-parameters` with the changed parameter(s) overridden.

Confirm before waiting:

- A change set preview (or `update-stack` events) shows only the expected
  resource updates. Replacements of `AWS::RDS::DBInstance`,
  `AWS::S3::Bucket`, or any of the retained secrets are a hard fail —
  cancel the update.

### 2c. Wait for update to complete

`aws cloudformation wait stack-update-complete`.

Confirm:

- Final status is `UPDATE_COMPLETE`.
- For each service expected to restart: a *new* deployment appeared in
  `describe-services`, ran to `rolloutState == COMPLETED`, and the
  previous deployment was drained. `runningCount == desiredCount`
  throughout (no scale-to-zero).
- If the migration was expected to rerun (image change): the migration
  custom resource emitted a stack event in this update, and a new ECS
  task ran to exit code 0.
- DB instance identifier is unchanged from Phase 1c (no replacement).
- Ingest bucket name is unchanged from Phase 1c (no replacement).
- ALB DNS name is unchanged.
- All tags from Phase 1d are still present and unchanged on every
  resource. Re-run the same tag query and confirm the count and ARN list
  are unchanged.
- Re-run the smoke checks from Phase 1c (target health, /api/me, DEX
  page) — all should still pass.

## Phase 3: tear down

### 3a. Dry run

```sh
scripts/teardown-lakerunner.sh \
  --stack-name <name> --region <region>
```

(no `--yes` — dry run prints the plan and exits.)

Confirm:

- Plan output lists the bucket name, all three retained secrets ARNs, and
  the DB instance identifier — none of them blank or `(not found)`.
- The captured state matches the values archived in Phase 1.

### 3b. Run the tear-down

```sh
scripts/teardown-lakerunner.sh \
  --stack-name <name> --region <region> \
  --deployer-role-arn $DEPLOYER_ROLE_ARN \
  --yes
```

Confirm during run:

- `delete-stack` is called with `--role-arn`.
- The script blocks on `wait stack-delete-complete` (this is normal — can
  take 10+ minutes).
- After delete completes, the script drains the bucket, force-deletes the
  three secrets, and deletes the RDS final snapshot, in that order.
- Exit code is 0.

### 3c. Stack delete completed

Confirm:

- `describe-stacks --stack-name <name>` returns "does not exist" (the
  stack is fully gone, not in `DELETE_FAILED`).
- All twelve nested children are also gone.

### 3d. Resource sweep

Re-run the prefix tag query and post-filter for the captured
`InstallIdShort` (the RGT API does not support middle-wildcards):

```sh
aws resourcegroupstaggingapi get-resources \
  --tag-filters Key=Project,Values=cardinal \
                Key=ManagedBy,Values=cardinal-cfn \
  --region $REGION \
  --query 'ResourceTagMappingList[].[ResourceARN, Tags[?Key==`Name`]|[0].Value]' \
  --output text \
  | grep "$INSTALL_ID_SHORT"
```

Confirm:

- Result is empty. *Every* tagged resource for this install is gone.
- The RGT API caches tag mappings for some minutes after a resource is
  actually deleted. If the prefix query still returns ECS clusters /
  services / target groups for *this* install, cross-check with a live
  call — `aws ecs list-clusters | grep cardinal`, `aws elbv2
  describe-target-groups`, etc. — before treating it as a bug. Stale
  RGT entries age out within ~10 minutes.
- Live entries that survive after that window must be one of the
  documented survivors (ingest bucket, three secrets, RDS snapshot) —
  and the script should have already deleted those, so anything
  surviving is a bug in the script.

Spot-check by service:

- `aws s3api head-bucket --bucket
  cardinal-ingest-<account>-<region>-<InstallIdLong>` returns 404.
- `aws secretsmanager describe-secret --secret-id
  cardinal/<InstallIdLong>/license` returns `ResourceNotFoundException`
  (same for `admin-api-key`, and for the auto-named DB master secret by
  ARN captured in Phase 1).
- `aws rds describe-db-snapshots --db-snapshot-identifier <stack>-Db-*`
  returns no matches (snapshot deleted).
- `aws ecs list-clusters | grep cardinal` is empty.
- `aws elbv2 describe-load-balancers` shows no `cardinal-*` ALBs.
- `aws logs describe-log-groups --log-group-name-prefix /cardinal/`
  is empty (or contains only orphan log groups that are documented in
  `tearing-down.md` as expected).

## Phase 4: re-deploy and second tear-down

The first deploy used a clean account; the second deploy validates that
no leftover state from Phase 1-3 interferes (no name collisions, no
orphaned tags pinning anything in place).

Run Phases 1 and 3 again, end-to-end. Confirm:

- The new install gets a *different* `InstallIdShort` / `InstallIdLong`
  than the prior install (different StackId).
- All of Phase 1's checks pass.
- All of Phase 3's checks pass.

## Phase 5: final cleanup pass

Even after Phase 3 runs cleanly, sweep for anything that might have leaked:

- Tag query `Key=Project,Values=cardinal` across the region — should
  contain only entries that age out within ~10 minutes (stale RGT
  cache after delete) and ECS task-definition ARNs (AWS retains
  every TaskDefinition revision indefinitely; deregistering is a
  cosmetic clean-up, not a correctness issue). Anything else is a
  leak.
- Tag query `Key=ManagedBy,Values=cardinal-cfn` across the region —
  same caveats.
- `aws cloudformation list-stacks --stack-status-filter CREATE_COMPLETE
  UPDATE_COMPLETE DELETE_FAILED` filtered to `cardinal-*` — should show
  only `cardinal-cfn-deployer` and (if you deployed it) `cardinal-vpc`.
- Manual sweep for un-tagged remnants (rare but possible):
  - `aws s3 ls | grep cardinal-ingest`
  - `aws rds describe-db-snapshots --snapshot-type manual --query
    'DBSnapshots[?contains(DBSnapshotIdentifier,`cardinal`) ||
                       contains(DBSnapshotIdentifier,`Db`)]'`
  - `aws secretsmanager list-secrets --query
    'SecretList[?starts_with(Name,`cardinal/`)]'`
  - `aws logs describe-log-groups --log-group-name-prefix /cardinal/`

If anything turns up, document it in the test report and either delete
manually or file an issue against the script / templates depending on root
cause.

## Reporting

For each test run, capture:

- Release version (git SHA, tag, S3 prefix).
- AWS account ID and region.
- `InstallIdShort` and `InstallIdLong` for every install in the run.
- Wall-clock time per phase.
- Final pass/fail for each numbered confirm-bullet (or the failure mode).
- Any unexpected resources found in Phase 3d or Phase 5.

A run is **passing** only when every confirm-bullet in every phase passes.
A single failed bullet blocks the release.

## Open items to revisit

- The "two installs in the same VPC" sub-test in 1e shares subnets across
  installs. Confirm the security-group rules actually allow both sets of
  task ENIs to talk to both DBs (each DB SG should only accept from its
  own install's task SG). This validates the install-id-scoped SG rules
  in `cluster.yaml` / `database.yaml`.
- If the multi-install test ever fails on a name collision, the fix is
  *not* a new random tag — the existing `InstallIdShort`/`InstallIdLong`
  derived from `AWS::StackId` is already unique per stack. A collision
  means a child template hardcoded a name that should have included the
  install id. File against the offending child.
- Consider automating Phase 1d / 3d / 5 as a single Bash check script
  invoked at the end of each phase. The data inputs (region, install id)
  are already captured; the queries are stable.
- Per-install discovery currently relies on the `Name` tag carrying the
  `InstallIdShort` suffix, but the Resource Groups Tagging API only
  supports prefix wildcards, so post-filtering with `grep` is required.
  Adding a dedicated `InstallIdShort` (or `InstallId`) tag to every
  resource emitted by the templates would let a single tag query scope
  to a specific install without the post-filter step. Worth doing if
  test automation grows beyond ad-hoc shell.
