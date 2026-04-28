# Cardinal CloudFormation Refactor — Design

Date: 2026-04-28
Status: Draft (pending review)

## Goal

Replace the current 10-template flat layout with a small number of customer-facing root templates that internally use nested stacks, so that:

- A normal customer deploys at most two templates: an optional VPC, then a single Lakerunner root.
- Each subsystem (RDS, ALB, ingest storage, ECS services, etc.) lives in its own nested stack with a small blast radius — failures and updates do not cascade.
- Two installs in the same AWS account/region coexist without resource-name collisions.
- The build, lint, and release pipeline are clean and CI-driven.

This is a fresh-install-only refactor. There is no obligation to migrate existing deployments. The current templates can be deleted in their entirety.

## Non-goals

- Live integration tests against AWS. Build-time validation (`cfn-lint`, `cloud-radar` unit tests) is the test surface for now.
- Migrating existing customer stacks. Fresh installs only.
- Switching CloudFormation generator tooling. Stay on troposphere.

## Scope

### Components retained

- VPC (separate optional template)
- Common infrastructure (split into focused sub-stacks: RDS, ingest storage, ALB, ECS cluster, config)
- Database migration (one-shot)
- Lakerunner microservices (split into three tier stacks)
- Maestro + bundled DEX OIDC
- OTEL collector

### Components removed

- Grafana service stack
- Alerting (SNS) stack
- Bedrock setup stack
- Debug utility stack

## Customer-facing entry points

Two root templates, deployed independently:

1. `cardinal-vpc.yaml` — optional. Customers using their own VPC skip this.
2. `cardinal-lakerunner.yaml` — the application. Composed of nested stacks for every subsystem.

Both templates are published to a public, vendor-managed S3 bucket. Customers deploy by pasting the S3 URL into the CloudFormation console.

## Naming and multi-install isolation

### Prefix policy

- Primary prefix: `cardinal-`.
- Fallback prefix: `chq-` only when an AWS resource name length cap forces it.
- Applied to `Name` tags and to physical names (where physical names are used at all).

### Physical names vs. tags

Default to CloudFormation-generated physical names with `Name` tags carrying the human-readable label. Use explicit physical names only where there is a concrete external reason (the bucket name needs to be predictable, a secret needs to be referenced by name from outside the stack, etc.). Never give RDS, ECS clusters, ECS services, listener rules, log groups, or other "frequently updated" resources explicit names — explicit names block in-place updates that require replacement.

Resources where the spec opts into an explicit name (each must include `InstallIdLong`):

- `AWS::SSM::Parameter` — `Name` is required by AWS and changing it forces replacement; we use `cardinal/<install-id-long>/<key>` (e.g. `cardinal/1a2b3c4d5e6f/storage-profiles`).
- `AWS::S3::Bucket` — `BucketName` is *not* required by CloudFormation, but we choose to set it for predictability and easier customer ops. Pattern: `cardinal-ingest-${AWS::AccountId}-${AWS::Region}-<install-id-long>`. All-lowercase, well under the 63-char limit, globally unique because it includes account+region+install-id.
- `AWS::SecretsManager::Secret` (only the secrets that are referenced from outside the stack — license, admin API key) — `Name: cardinal/<install-id-long>/<purpose>`. Internal-only secrets (DB master password, internal service keys) keep AWS-generated physical names.

Resources where AWS *forbids* one of the modes we wanted:

- `AWS::SSM::Parameter` does not support `SecureString` via CloudFormation. Anything sensitive (license content, internal service keys, admin API key, DB credentials) lives in **Secrets Manager**, not SSM. The `config` stack stores non-sensitive configuration in plain SSM `String` parameters and sensitive values in Secrets Manager.

### Install-id

Every install gets a deterministic per-install suffix derived from the **root** stack's `AWS::StackId`. Two values are computed:

- `InstallIdShort` — first 8 hex chars of the UUID. Used in `Name` tags and console-readable labels where brevity helps.
- `InstallIdLong` — first 12 hex chars of the UUID. Used in physical names that have hard global-uniqueness requirements (S3 bucket name) or are awkward to rename (Secret names, SSM parameter names).

```
UUID            = Fn::Select(2, Fn::Split("/", Ref(AWS::StackId)))
InstallIdShort  = Fn::Select(0, Fn::Split("-", UUID))                 # 8 hex chars
InstallIdLong   = Fn::Join("", [                                       # 12 hex chars (8 + 4)
                    Fn::Select(0, Fn::Split("-", UUID)),
                    Fn::Select(1, Fn::Split("-", UUID))
                  ])
```

Both values are hex-only and case-insensitive in practice. `AWS::StackId` UUIDs today are returned lowercase, but CloudFormation does not formally guarantee case, so any consumer that requires lowercase (S3 bucket names, IAM role paths, etc.) gets explicit lowercasing applied by the generator on the customer-facing string templates — not on `InstallId*` itself, which is propagated as-is between CFN templates. `InstallIdShort` is human-readable; `InstallIdLong` gives ~48 bits of entropy — sufficient for any name uniqueness need within an AWS account/region.

`AWS::StackId` is an ARN; splitting on `/` and taking index 2 yields the UUID. **Critical:** in a nested child stack, `Ref(AWS::StackId)` returns the *child's* stack id, not the root's. Both ids must be computed *only* in the root template and passed as parameters to every nested stack. The generator enforces this by exposing `InstallIdShort` and `InstallIdLong` only as parameters of children, never as in-line `Ref`s.

`InstallId*` is stable across stack updates. It changes only when the customer deletes and re-creates the root stack. Two installs in the same account/region produce disjoint resource sets.

### Stability and update behavior

The install suffix is computed from `AWS::StackId`, which is fixed for the lifetime of a stack. Stack updates do not regenerate it. Renaming an existing physical resource is therefore avoided in practice: any property change that *would* require replacement (e.g. changing a Secret's `Name`) will only happen if the customer recreates the root stack, which already creates fresh resources end-to-end.

## Architecture

### Approach: thin root, fat children

The root template `cardinal-lakerunner.yaml` is an orchestrator. It contains:

- Customer-facing parameters and `Metadata` for console parameter groups
- Conditions (e.g. `HasPublicSubnets`)
- The `InstallId` derivation
- One `AWS::CloudFormation::Stack` resource per nested child, with `TemplateURL` pointing at the vendor S3 bucket and parameters threaded through
- Top-level `Outputs` (ALB DNS name, Maestro URL, query API URL)

The root contains no application resources. Every concrete resource (RDS, ECS, S3, IAM, etc.) lives in a nested child stack.

### Nested-stack inventory

| # | Template | Owns | Depends on |
|---|---|---|---|
| 1 | `cluster.yaml` | ECS cluster, base TaskSG, shared execution role, base log group | none |
| 2 | `database.yaml` | RDS subnet group, DB instance, DB master secret | none |
| 3 | `storage.yaml` | S3 ingest bucket + lifecycle, SQS queue + policy, S3 → SQS notification | none |
| 4 | `alb.yaml` | ALB, default 443 listener, ALB security group | cluster (for TaskSG ingress wiring — see below) |
| 5 | `config.yaml` | Storage profiles SSM (String), API keys SSM (String, non-sensitive), license Secret, internal service keys Secret, admin API key Secret | none |
| 6 | `migration.yaml` | One-shot DB migration custom resource (Lambda-backed) | cluster, database, config |
| 7 | `services-query.yaml` | query-api, query-worker (per-service task roles, target groups, listener rules) | cluster, database, storage, alb, config, migration |
| 8 | `services-process.yaml` | process-logs, process-metrics, process-traces, pubsub-sqs | cluster, database, storage, config, migration |
| 9 | `services-control.yaml` | sweeper, monitoring, admin-api, alert-evaluator | cluster, database, storage, alb, config, migration |
| 10 | `otel.yaml` | otel-collector ECS service, target group, listener rule | cluster, storage, alb, config |
| 11 | `maestro.yaml` | maestro task, bundled DEX OIDC, listener rules | cluster, database, alb, config |

### Dependency graph

Foundations (mostly independent, deploy in parallel):

- `cluster`
- `database`
- `storage`
- `config`

Then:

- `alb` ← cluster (because `alb` owns the ALB-to-TaskSG ingress rule, and `cluster` owns TaskSG)
- `migration` ← cluster, database, config
- `services-query` ← cluster, database, storage, alb, config, migration
- `services-process` ← cluster, database, storage, config, migration
- `services-control` ← cluster, database, storage, alb, config, migration
- `otel` ← cluster, storage, alb, config
- `maestro` ← cluster, database, alb, config, migration

`otel` does not depend on `migration` because it writes to S3/SQS and never touches the lakerunner database schema. All other application-layer stacks wait on `migration`.

### Initial-create vs. update blast radius

The "thin root, fat children" architecture isolates the blast radius of *updates* — a failed update to one nested stack rolls back only that stack, leaving siblings unchanged. Initial *create* is different: if any child create fails, the whole root rolls back. The blast-radius advantage applies after the first successful deploy. This is documented expectation, not a defect, but the spec must call it out so neither the implementer nor the customer assumes day-zero failures are surgical.

### Cross-stack wiring

CloudFormation does not allow sibling nested stacks to reference each other directly. Wiring goes through the root: child A produces `Outputs`, the root reads them via `Fn::GetAtt childStackA.Outputs.Foo`, and the root passes that as a parameter to child B.

Root template generator code centralizes this wiring so that adding a new dependency between children is a one-line change.

**List-typed parameters and nested stacks.** CloudFormation passes nested-stack parameters as strings. List parameters such as `PrivateSubnets` and `PublicSubnets` cannot be reliably forwarded as `List<AWS::EC2::Subnet::Id>` from root → child; the conversion is unstable and AWS warns against it. The convention is: every child template that needs a subnet list declares the parameter as `String` (a comma-separated list), and the child uses `Fn::Split(",", ...)` internally. The root template joins lists with `Fn::Join(",", ...)` before passing.

### Service tier model

Services are split into tiers `query`, `process`, `control` (Approach B from brainstorming).

Tier rationale:

- **query** is interactive and latency-sensitive (user-facing API + workers fanning out subqueries).
- **process** is throughput-bound (data pipeline ingest workers).
- **control** is small, long-lived background services (sweeper, monitoring, admin-api, alert-evaluator).

A failure or update churn in one tier never touches the others.

### Future split to per-service stacks

The tier-stack design is structured so that splitting a tier into per-service stacks later (Approach C from brainstorming) only recreates the services in the affected tier *if* the move can use AWS Stack Refactor (`move-resource`). Without Stack Refactor support for a given resource type, moving between stacks is delete-then-recreate, with associated downtime. AWS Stack Refactor's per-resource support matrix evolves; the implementer should re-check it before executing a B → C move.

The hard rule that makes the move tractable: a tier stack only owns per-service resources (ECS Service, TaskDefinition, TargetGroup, ListenerRule, per-service log group, per-service task role, per-service SG ingress). Anything shared across services lives in `cluster`, `alb`, or `config`.

**ListenerRule priority allocation.** ListenerRule `Priority` must be unique per Listener — and that constraint is enforced across *all stacks* attached to that listener, not just within one stack. The design pre-allocates priority ranges per service so future per-service splits don't collide:

| Service | Listener priority |
|---|---|
| query-api | 100 |
| admin-api | 110 |
| maestro (HTTPS) | 200 |
| maestro (DEX OIDC paths) | 210 |
| otel-collector (gRPC if exposed via ALB) | 300 |
| (reserved) | 400–999 for future services |

Each priority is hard-coded into the per-service ListenerRule in its current tier stack and stays the same when the service is later moved into its own stack. No two services share a priority. If a new service is added, it gets a fresh priority from the reserved pool.

### Migration custom resource semantics

`migration.yaml` runs the lakerunner DB migrator as a one-shot ECS task triggered by a Lambda-backed custom resource. Behavior contract:

- **PhysicalResourceId** is stable: `cardinal-migration-<InstallIdLong>`. CFN must never observe an ID change between Create and Update calls — that would force a delete-and-recreate cycle.
- **Trigger property:** the custom resource has a `MigrationVersion` input property whose value is the lakerunner image **digest** (`sha256:...`), never a tag. Tags are mutable and would make change sets nondeterministic. CFN runs Update only when this property changes; otherwise the update is a no-op. Image bump → new digest → migrator runs.
- **Create:** runs the migration ECS task to completion (lakerunner's migrator is already idempotent; applies any pending migrations). Returns success on exit-code 0; failure surfaces the task logs in the CFN error.
- **Update:** if `MigrationVersion` changed, runs the migrator again. Otherwise no-op (Lambda returns the existing PhysicalResourceId immediately).
- **Delete:** no-op. Migrations are not "uncreated"; rolling back schema is operator-driven, not stack-driven.
- **Lambda permissions:** scoped to running this one ECS task definition + reading its task role + writing its log group. Nothing else.

### IAM model

- One **shared execution role** in `cluster.yaml`. ECS uses it to pull images and write logs. All services share it.
- One **per-service task role** in each tier stack, scoped to exactly the resources that service needs (e.g. `process-logs` reads only the `logs-raw/` S3 prefix). No service has broader permissions than necessary.
- Maestro, OTEL, and migration each define their own task roles in their own stacks.

## Customer parameter surface (root)

### Required

- `VpcId`
- `PrivateSubnets` (List<AWS::EC2::Subnet::Id>)
- `LicenseData`

### Networking

- `PublicSubnets` (List<AWS::EC2::Subnet::Id>, default empty — required only if `AlbScheme=internet-facing`)
- `AlbScheme` — `internal` (default) or `internet-facing`

### Sizing (with defaults from `lakerunner-stack-defaults.yaml`)

- Per-service replicas / CPU / memory for `query-api`, `query-worker`, `process-logs`, `process-metrics`, `process-traces`, `pubsub-sqs`, `maestro`
- RDS instance class, allocated storage, engine version

### Image overrides (all optional, public ECR defaults)

- `LakerunnerImage`
- `MaestroImage`
- `OtelCollectorImage`
- `MigrationImage`
- `DexImage`
- Any additional images we ship (kept in lockstep with current image-override surface)

### Advanced

- `ApiKeysOverride` (YAML)
- `StorageProfilesOverride` (YAML)
- `TemplateBaseUrl` — defaults to the public Cardinal S3 bucket, override for air-gapped customers who mirror templates to their own bucket

### Console layout

`Metadata.AWS::CloudFormation::Interface.ParameterGroups`:

1. Networking
2. Sizing
3. Images
4. Advanced

Customer cognitive load: networking and license are mandatory; everything else has a working default.

### Sensitive parameters

`LicenseData`, `ApiKeysOverride`, and `StorageProfilesOverride` (which can carry credentials) are declared with `NoEcho: true` so they don't leak into CloudFormation event/resource metadata or the console history.

### Updating `TemplateBaseUrl`

`TemplateBaseUrl` is a knob that affects every nested-stack `TemplateURL` simultaneously. Changing it during an update is a hierarchy-wide blast event. The spec recommends customers do not change it after the initial deploy except to swap between the public Cardinal bucket and a customer-owned mirror — and treat such a change as a planned upgrade with a maintenance window.

### Resource lifecycle policies

Customer-data-bearing resources get conservative deletion behavior so a stack delete does not destroy data:

| Resource | DeletionPolicy | UpdateReplacePolicy | Reason |
|---|---|---|---|
| RDS DB instance | `Snapshot` | `Snapshot` | Final snapshot lets the customer recover |
| S3 ingest bucket | `Retain` (with note that it must be empty for stack delete to succeed; bucket lifecycle continues to prune) | `Retain` | Don't lose customer's ingested data on accidental delete |
| DB master secret | `Retain` | `Retain` | Avoid orphaning RDS access if recreate-needed |
| Internal service keys secret | `Delete` | `Delete` | Regenerated on recreate; no recovery value |
| Admin API key secret | `Retain` | `Retain` | Operators may have wired this into external systems |
| SQS ingest queue | `Delete` | `Delete` | Stateless |
| ALB / TargetGroups / ListenerRules | `Delete` | default | Stateless |
| ECS cluster / services / task defs | `Delete` | default | Stateless |
| CloudWatch log groups | `Delete` (with explicit retention period set on the group) | default | Logs naturally roll over |

These defaults are baked into the generator. Customers can override on a per-stack basis if they want different policies, but the design doesn't expose this as a parameter.

### ECS update behavior

ECS services use the default rolling deployment with `MinimumHealthyPercent=50`, `MaximumPercent=200`, and `DeploymentCircuitBreaker.Enable=true` so that a bad image bump rolls back automatically. Blue/green via CodeDeploy is out of scope for now (added complexity, no current customer asking).

## Repo / generator code layout

```
src/
  cardinal_cfn/
    __init__.py
    common.py            # install-id, tag helpers, naming helpers
    parameters.py        # shared Parameter / Condition definitions
    images.py            # image override parameter machinery
    children/
      cluster.py
      database.py
      storage.py
      alb.py
      config.py
      migration.py
      services_query.py
      services_process.py
      services_control.py
      otel.py
      maestro.py
    root.py              # parent template
  cardinal_vpc.py        # standalone VPC template
generated-templates/
  cardinal-vpc.yaml
  cardinal-lakerunner.yaml
  cardinal-lakerunner/
    cluster.yaml
    database.yaml
    storage.yaml
    alb.yaml
    config.yaml
    migration.yaml
    services-query.yaml
    services-process.yaml
    services-control.yaml
    otel.yaml
    maestro.yaml
```

The `generated-templates/cardinal-lakerunner/` subdirectory mirrors the S3 key layout exactly so the publish step is a single `aws s3 sync`.

### Files deleted from the existing repo

- `src/lakerunner_alerting.py`
- `src/lakerunner_bedrock_setup.py`
- `src/lakerunner_common.py`
- `src/lakerunner_debug_utility.py`
- `src/lakerunner_grafana_service.py`
- `src/lakerunner_maestro_service.py`
- `src/lakerunner_migration.py`
- `src/lakerunner_otel_collector_service.py`
- `src/lakerunner_services.py`
- `src/lakerunner_vpc.py`
- `lakerunner-grafana-defaults.yaml`
- All existing tests (rewritten against the new structure)
- `update-images.sh`, `test-common.yaml` (obsolete)

### Defaults file consolidation

- `lakerunner-stack-defaults.yaml` → `cardinal-defaults.yaml` (services, images, api-keys, storage-profiles)
- `lakerunner-maestro-defaults.yaml` → folded into `cardinal-defaults.yaml` under a `maestro:` key
- `otel-stack-defaults.yaml` → folded into `cardinal-defaults.yaml` under an `otel:` key
- Grafana defaults file deleted

One defaults file is easier for a customer to fork and re-skin than four.

## Build pipeline

`build.sh` (or `make build`):

1. Activate venv, install requirements
2. Clear `generated-templates/`
3. Run `python3 -m cardinal_cfn.root --output generated-templates/` — writes the root template plus all eleven child templates to the mirrored layout
4. Run `python3 src/cardinal_vpc.py > generated-templates/cardinal-vpc.yaml`
5. Run `cfn-lint generated-templates/**/*.yaml`
6. Run `pytest tests/`

`make all` runs build + lint + tests.

## Release pipeline

### Public S3 bucket

Provisioned in `terraform-deployments/aws/production/`:

- Bucket: `cardinal-cfn` (or the closest available name — confirmed at TF-apply time)
- Public-read on objects via bucket policy; PutObject restricted to the GitHub Actions OIDC role
- Versioning: enabled
- Lifecycle: keep all versions of the most recent N releases; expire older non-current versions after 90 days
- Region: us-east-1 or us-east-2 (whichever the existing TF account standardizes on). The bucket is single-region; the customer-facing URL uses the regional S3 endpoint (see `Customer URL pattern` below). CloudFormation fetches over HTTPS from the regional endpoint regardless of where the deploy stack runs. This is expected to work and is the same pattern AWS Quick Starts use, but treat it as "validated for the regions we support" rather than a universal guarantee.
- TLS-only access enforced via bucket policy

### Bucket key layout

```
s3://cardinal-cfn/lakerunner/v1.20.0/cardinal-lakerunner.yaml
s3://cardinal-cfn/lakerunner/v1.20.0/cardinal-lakerunner/cluster.yaml
s3://cardinal-cfn/lakerunner/v1.20.0/cardinal-lakerunner/database.yaml
... (and so on for every child)
s3://cardinal-cfn/lakerunner/v1.20.0/cardinal-vpc.yaml

s3://cardinal-cfn/lakerunner/latest/...
```

The `latest/` prefix is a copy of the most recently released version. Customers who want a moving target use `latest`; customers who want pinning use the explicit version.

### CI flow

GitHub Actions workflow on tag push (`v*`):

1. Check out the tag
1. Activate venv, install deps
1. Run `make all` (build + lint + tests)
1. Determine version from tag
1. Assume the publish role via OIDC (already provisioned in `terraform-deployments/aws/production/`)
1. `aws s3 sync generated-templates/ s3://cardinal-cfn/lakerunner/${VERSION}/`
1. `aws s3 sync generated-templates/ s3://cardinal-cfn/lakerunner/latest/`
1. Create a GitHub Release with the customer-facing root template URL in the body

The TemplateBaseUrl in the root defaults to `https://cardinal-cfn.s3.<bucket-region>.amazonaws.com/lakerunner/${VERSION}/`. The generator stamps both `<bucket-region>` and `${VERSION}` into the default at build time so downloaded templates are version-locked and use a stable regional endpoint.

### Customer URL pattern

```
https://cardinal-cfn.s3.<bucket-region>.amazonaws.com/lakerunner/v1.20.0/cardinal-lakerunner.yaml
```

The regional S3 endpoint is required (path-style `s3.amazonaws.com/...` URLs are brittle for CloudFormation across regions). Pasted into the CloudFormation console "Amazon S3 URL" field, deploy, done.

If demand emerges for cross-region replication (low-latency template fetches in regions far from the bucket), add it later and emit per-region URLs in the GitHub release notes. Out of scope for v1.

## CloudFormation quota accounting

CloudFormation enforces hard limits on stack size and structure. The proposed architecture is well inside all of them:

| Limit | AWS max | Worst-case in this design | Headroom |
|---|---|---|---|
| Nested stack depth | 5 | 2 (root → child) | 3 |
| Resources per stack | 500 | ~12 nested-stack resources in root; ~25 per child (largest is `services-control`) | >>20× |
| Parameters per stack | 60 | ~28 on root; ~20 on largest child | 2× |
| Outputs per stack | 200 | ~8 on root; ~12 on largest foundation child | 16× |
| Mappings per stack | 200 | 0 | n/a |
| Stack body size (rendered template) | 1 MiB | largest current child template is well under 200 KiB | 5× |
| Template URL must be S3 | required | satisfied (vendor public bucket) | n/a |

If a single child grows past ~50 resources, that's a signal to split it (e.g. one of the `services-*` tier stacks getting too crowded → move toward Approach C for that tier).

## Test surface

- `cfn-lint` on every generated template, errors fail the build
- `pytest` with `cloud-radar` per-template tests:
  - Root template: parameter wiring, child stack references, version stamping
  - Each child: required resources present, exports/outputs match expectations, conditions evaluate correctly
  - Cross-template: parameter signatures align (root passes what child expects)
- No live AWS tests (deferred)
- Test cost: zero (everything offline)

## Out of scope (explicitly)

- Migration / upgrade path from existing 10-template deployments
- Live integration tests
- CloudFront in front of the S3 bucket (S3-direct HTTPS is sufficient for CloudFormation fetches)
- Cross-region replication of the public bucket
- IAM policy compaction / least-privilege audit beyond what already exists in the current codebase (preserved as-is, not expanded)

## Open questions

None at design-doc time. All architecture decisions are locked.

## Risk register

| Risk | Mitigation |
|---|---|
| Naming an S3 bucket `cardinal-cfn` collides with an existing global name | Confirmed at TF-apply time; fall back to `cardinalhq-cfn-public` |
| RDS replacement on accidental property change | Avoid explicit `DBInstanceIdentifier`; rely on auto-generated names + `Name` tag |
| Two installs in same VPC need disjoint resource names | `InstallIdShort`/`InstallIdLong` suffix on every named/tagged resource |
| Air-gapped customer cannot reach public S3 | `TemplateBaseUrl` parameter lets them mirror templates locally |
| Nested-stack output graph cycle | Dependency graph above is a DAG; root template generator validates no cycles at build time |
| Tier-stack drift makes B → C migration hard | Hard rule: tier stacks own only per-service resources; shared infra in foundation stacks. Enforced in code review. |
| Sibling stack hidden dep (e.g. `alb` ingress against TaskSG owned by `cluster`) gets missed | Cross-stack wiring lives in a single root generator function with an explicit dependency map; build fails if a child references an output not declared in its `dependsOn` list |
| Initial deploy fails partway and rolls everything back | Documented (`Initial-create vs. update blast radius`); customer rerun creates fresh resources; install-id changes naturally on recreate |
| ListenerRule priority collision between current tier stacks and future per-service stacks | Pre-allocated priority ranges per service, documented in the B → C section |
| Sensitive parameters echoed in console / events | `NoEcho: true` on `LicenseData`, `ApiKeysOverride`, `StorageProfilesOverride` |
| Customer expects `SecureString` in SSM | Spec routes all sensitive values through Secrets Manager; SSM only for non-sensitive config |
| Cross-region template fetch failure | Use regional S3 endpoint URLs everywhere; defer cross-region replication until demanded |
