# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository overview

Generators (Python + troposphere) emit a set of **standalone** CloudFormation
stacks — no single customer-facing root. Deployment is driven by env-var-only
POSIX shell drivers in `scripts/`, each of which creates-or-updates one stack
and pulls its parameters from upstream stacks' Outputs.

Design context lives under `docs/superpowers/specs/` (specs are the source of
truth) and `docs/superpowers/plans/`. Operator-facing docs live under
`docs/operations/`. When in doubt, the newest spec wins.

## Stacks

Customer-facing, in install order:

| Stack | Template | Owns |
|---|---|---|
| `cardinal-lakerunner-infra-base` | `cardinal-lakerunner-infra-base.yaml` | ALB SG, 5 per-tier task SGs, execution role + 5 per-tier task roles, cooked S3 bucket, license/admin-key secrets |
| `cardinal-lakerunner-infra-rds` | `cardinal-lakerunner-infra-rds.yaml` | RDS SG, subnet group, master-credential secret, DB instance |
| `cardinal-satellite-infra-base` | `cardinal-satellite-infra-base.yaml` | Raw ingest bucket, SQS queue + DLQ, prefix-filtered S3→SQS notifications, cross-account access role |
| `cardinal-satellite-services` | `cardinal-satellite-services.yaml` | otel-collector + its own ALB (OTLP/HTTP 4318) |
| `cardinal-satellite-cwmetrics` | `cardinal-satellite-cwmetrics.yaml` | Optional: CloudWatch metric stream → Firehose → the raw bucket under `cwmetrics-raw/` |
| `cardinal-lakerunner-services` | `cardinal-lakerunner-services.yaml` | Application root; nests the seven children below |

Plus:

- `cardinal-cleanup.yaml` — standalone teardown stack: one Fargate task definition
  running the inline POSIX-sh script from `cleanup_script.SCRIPT` (in `EntryPoint`,
  not `Command`, so `ecs:RunTask` cannot substitute a command).
- `lrdev-vpc.yaml`, `lrdev-baseinfra.yaml` — **internal** test-env scaffolding
  (VPC; ECS Fargate cluster). Not customer-facing; they simulate the
  customer-supplied VPC and cluster in our test account. Driven from `dev-scripts/`.

The customer supplies the VPC/subnets and the ECS cluster. Everything else is
created by the stacks.

### `cardinal-lakerunner-services` children

| Template | Owns |
|---|---|
| `alb.yaml` | ALB, HTTPS 443, HTTPS 9443 (admin-api), HTTP 4318 listeners. ALB SG is a parameter |
| `cert.yaml` | Pass-through ACM/IAM cert ARN, or `AWS::IAM::ServerCertificate` from PEMs |
| `migration.yaml` | DB migration ECS service (runs migrator once, then idles) |
| `services-query.yaml` | query-api, query-worker |
| `services-process.yaml` | process-{logs,metrics,traces}, pubsub-sqs |
| `services-control.yaml` | one task, four containers: admin-api, sweeper, monitoring, alert-evaluator |
| `maestro.yaml` | Maestro + bundled DEX OIDC (five-container task) |

There is **no** otel child in the lakerunner stack — the collector lives in
`cardinal-satellite-services`.

`cardinal-lakerunner-services` creates **no infra** of its own — no ECS cluster,
security groups, IAM roles, databases, buckets, queues, secrets, or SSM
parameters. Every such value arrives as a parameter, driver-wired from the
infra stacks' Outputs.

Cross-stack wiring inside the root goes through `Fn::GetAtt childStack.Outputs.X`
→ child parameter. Sibling children never reference each other directly.
Cross-*stack* wiring between the six top-level stacks is done by the drivers
reading Outputs, not by CFN exports.

## Repo layout

```
src/cardinal_cfn/
  lakerunner_infra_base.py     # cardinal-lakerunner-infra-base.yaml
  lakerunner_infra_rds.py      # cardinal-lakerunner-infra-rds.yaml
  lakerunner_services.py       # cardinal-lakerunner-services.yaml (app root)
  satellite_infra_base.py      # cardinal-satellite-infra-base.yaml
  satellite_services.py        # cardinal-satellite-services.yaml
  satellite_cwmetrics.py       # cardinal-satellite-cwmetrics.yaml
  cardinal_cleanup.py          # cardinal-cleanup.yaml
  cleanup_script.py            # the inline teardown sh body
  lrdev_vpc.py                 # internal test-env VPC
  lrdev_baseinfra.py           # internal test-env ECS cluster
  children/                    # one module per nested child (+ services_common.py)
  install_id.py                # InstallIdShort/InstallIdLong (roots only)
  naming.py                    # cardinal_tags(), name/log-group/secret helpers
  parameters.py                # shared parameter / NoEcho / parameter-group helpers
  images.py                    # image-override parameter machinery
  image_manifest.py            # `manifest <stack>` / `suffix <key>` CLI
  policies.py                  # DeletionPolicy / UpdateReplacePolicy table
  listener_priorities.py       # pre-allocated ListenerRule priorities
  defaults.py                  # cardinal-defaults.yaml loader
scripts-src/
  build.sh                     # concatenates parts/<driver>.sh + parts/base.sh
  parts/base.sh                # the shared env-var-driven deploy engine
  parts/deploy-*.sh            # per-stack front halves
scripts/                       # GENERATED single-file drivers (committed)
dev-scripts/                   # internal-only wrappers + lrdev scaffolding
cardinal-defaults.yaml         # services, pinned images, capacity mode, api key seed
build.sh                       # generate everything, then cfn-lint
tests/unit/                    # helper-level tests
tests/templates/               # per-template assertions via cloud-radar
.github/workflows/{test,release}.yml
```

Generated artifacts land in `generated-templates/`, mirroring the S3 key layout:
the top-level templates, `cardinal-lakerunner/<child>.yaml` for the children, and
`{lakerunner,satellite,cleanup}-images.txt` image manifests.

`scripts/*.sh` are **generated** from `scripts-src/parts/` by `make scripts`
(also run by `build.sh`) and are committed. `tests/unit/test_deploy_stack_lint.py`
asserts they match a fresh build — regenerate and commit after editing any part.
Edit `scripts-src/parts/`, never `scripts/`.

## Key design rules

### Naming and tags

- Default to **CloudFormation-generated physical names** with a `Name` tag.
- Prefix is `cardinal-`. Use `chq-` only when an AWS name-length cap forces it.
- `cardinal_tags()` in `naming.py` is the single source of the common tag set
  (`Name`, `Project`, `Application`, `Component`, `ManagedBy`). Children pass
  `role=` (Name carries the install id); root stacks pass `component=`.
- Drivers additionally set stack-level tags, which CFN propagates to resource
  types the generators cannot tag directly (listeners, listener rules, Cloud
  Map, IAM server certificates). Security-group *rules* and Application Auto
  Scaling targets/policies remain untaggable.
- ECS services set `PropagateTags: SERVICE` so launched tasks carry the tags
  (Fargate cost allocation bills against task tags).
- Explicit physical names *only* where externally referenced — the S3 bucket
  names, and the `cardinal-license` / `cardinal-admin-key` / `cardinal-db-master`
  secrets. The base stack's IAM roles scope secret access by the
  `arn:...:secret:cardinal-*` name pattern, so those secret names are a contract.
- Never name RDS, ECS clusters/services, listener rules, log groups, or target
  groups — explicit names block in-place updates.

### Single-install assumption

The fixed `cardinal-*` names imply one Cardinal install per AWS account/region.
Customers running multiple installs use separate accounts (or regions).

### Multi-install isolation (within one root)

`InstallIdShort` (8 hex) and `InstallIdLong` (12 hex) are derived from the root
stack's `AWS::StackId` and propagated as parameters to every nested child.
**Children never compute these themselves** — `Ref(AWS::StackId)` in a child
returns the child's id, not the root's (`tests/unit/test_no_install_id_in_children.py`
enforces this).

```
UUID            = Fn::Select(2, Fn::Split("/", Ref(AWS::StackId)))
InstallIdShort  = Fn::Select(0, Fn::Split("-", UUID))
InstallIdLong   = Fn::Join("", [first two segments of UUID])
```

### Sensitive values

- Sensitive values **always** go to Secrets Manager. `AWS::SSM::Parameter`
  cannot be `SecureString` in CloudFormation.
- Parameters carrying secrets declare `NoEcho: true`.
- No org content is seeded by the stacks: admin-api seeds its first key from
  `cardinal-admin-key` via `ADMIN_INITIAL_API_KEY`, and Maestro is the sole
  owner of the org, its storage line, and its ingest key — provisioned at
  runtime through Lakerunner's `/api/v1/provision` admin API.

### List parameters into nested stacks

CloudFormation passes nested-stack parameters as strings. Lists like
`PrivateSubnets` cannot be reliably forwarded as `List<...>`. Convention: every
child declares such parameters as `String` (CSV) and uses `Fn::Split(",", ...)`
internally; the root joins with `Fn::Join(",", ...)` before passing.

### Lifecycle policies

Customer-data-bearing resources get `DeletionPolicy: Snapshot` (RDS) or `Retain`
(S3 buckets, license/admin/db-master secrets). Stateless resources are `Delete`.
The table lives in `policies.py` and is enforced by `apply_policy(resource, kind)`.

### ListenerRule priorities

Pre-allocated in `listener_priorities.py`; unique per listener across all stacks
attached to it. 400–999 reserved for new services.

| Service | Priority | Listener |
|---|---|---|
| query-api | 100 | 443 |
| query-api-extra | 105 | 443 (second slot; routes exceed 5 path patterns per condition) |
| maestro-dex | 210 | 443 |
| otel-grpc | 300 | satellite ALB 4318 |
| maestro-https | 49999 | 443 (catch-all `/*`; must be numerically highest so all others win) |
| admin-api-https | 1 | dedicated 9443 listener |

admin-api gets its own 9443 listener because the lakerunner binary serves its
embedded UI at `/` — a path-prefixed rule on 443 would break either the API (no
path stripping) or the UI's react-router.

### Migration (no Lambda)

`migration.yaml` runs the migrator as an **ECS service**, not a Lambda-backed
custom resource. Design: `docs/superpowers/specs/2026-05-12-no-lambda-migration-design.md`.

- Three containers: `configdb-init` (non-essential; `psql CREATE DATABASE configdb`
  if absent) → `migrator` (non-essential; `lakerunner migrate --databases=lrdb,configdb`;
  `dependsOn configdb-init=COMPLETE`) → `keepalive` (essential; sleeps;
  `dependsOn migrator=SUCCESS`).
- `keepalive` is the only essential container and ECS will not start it until
  `migrator` exits 0, so the service — and therefore `MigrationStack` — only
  reaches a stable state after migrations succeed. The service-tier stacks
  `DependsOn MigrationStack`. A failed migration → circuit breaker fails the
  service → the root rolls back. Loud, not silent.
- The migrator runs from the same image as the lakerunner tasks (single
  `LakerunnerImage` parameter), so the two cannot drift; an image change reruns
  the migrator before the service tiers update. Digest pinning via
  `image@sha256:...`; mutable tags like `:latest` are not supported.
- `DesiredCount` is hardcoded to `1` (~$3/month Fargate). An operator may
  `aws ecs update-service --desired-count 0`; that is harmless drift, re-applied
  on the next `LakerunnerImage` bump. The migrator must stay idempotent.

**There are no Lambdas anywhere in the product.** `cert.yaml` either forwards a
supplied ACM/IAM certificate ARN or creates an `AWS::IAM::ServerCertificate`
from PEMs (an ALB HTTPS listener accepts an IAM server-cert ARN like an ACM one).

### Service tier rule

The three service tier stacks own *only* per-service resources: ECS Service,
TaskDefinition, TargetGroup, ListenerRule, per-service log group, scalable
target/policy. Anything shared lives in `alb` or arrives as a parameter
(cluster, SGs, roles, secret ARNs). This keeps a future per-service-stack split
cheap.

### Process-tier autoscaling

`services-process` creates `process-{logs,metrics,traces}` at `min_replicas`
(from `cardinal-defaults.yaml`) and scales them on CPU via **native ECS
Application Auto Scaling** (`ScalableTarget` + target-tracking policy, mirroring
the Kubernetes HPA) up to the `Process*Replicas` cap (default 10 each). Those
parameters are the autoscaler *ceiling*, not the initial `DesiredCount` —
creating at the ceiling would launch ~3x the steady-state task count on every
deploy and can exhaust the account's Fargate vCPU quota. `pubsub-sqs` is not
autoscaled; `PubsubSqsReplicas` is its literal `DesiredCount`.

### Capacity mode

`lakerunner_capacity` in `cardinal-defaults.yaml` is a build-time knob for the
scale-out workers, applied via `services_common.capacity_provider_strategy()`:

- `ondemand` (default) — pure on-demand `FARGATE`.
- `fallback` — `Base=1` on-demand + weighted `FARGATE_SPOT` (4:1) scale-out.
- `spot` — pure `FARGATE_SPOT`; explicit opt-in only, deploy-unsafe.

Deploy-critical singletons (query-api, control, maestro, pubsub-sqs, migrator)
and the collector are always on-demand and ignore this knob.

## Build and testing

```sh
make install        # one-time: create .venv and install requirements.txt
make build          # ./build.sh: generate every template + image manifest +
                    # regenerate scripts/ drivers, then cfn-lint
make scripts        # regenerate scripts/ drivers only
make test           # all tests (helper unit + per-template)
make test-unit
make test-templates
make lint           # cfn-lint over generated-templates/
make check          # alias for `make test` (pre-push gate)
make clean
```

For debugging a single template (`PYTHONPATH=src`):

```sh
python3 -m cardinal_cfn.children.<child>       # a nested child
python3 -m cardinal_cfn.lakerunner_services    # the app root
python3 -m cardinal_cfn.satellite_infra_base   # etc. — one module per stack
python3 -m cardinal_cfn.image_manifest manifest lakerunner
```

Tests use pytest + cloud-radar; offline, no AWS credentials. All templates must
pass cfn-lint with no errors. Warnings are tolerable when explainable;
`.cfnlintrc` carries the project-wide ignores.

## Publishing

GitHub Actions on tag push (`v*`) builds, lints, tests, and publishes templates
**and the version-baked deploy drivers** to `cardinal-cfn-us-east-1` (the
source-of-truth bucket; S3 replication populates `cardinal-cfn-us-east-2` and
any other regional mirror out of band), plus a GitHub release with the drivers
attached.

```
https://cardinal-cfn-<region>.s3.<region>.amazonaws.com/lakerunner/<version>/<template>.yaml
s3://cardinal-cfn-<region>/lakerunner/<version>/scripts/deploy-*.sh
```

There is no `latest` — pin a tag. The drivers committed under `scripts/` bake
`STACK_VERSION=dev` and are for dev/test iteration; production uses the
release-pinned copies (or sets `STACK_VERSION=vX.Y.Z` explicitly).

Air-gapped customers mirror the `lakerunner/<version>/` prefix and set
`TEMPLATE_BASE_URL`, and point images at a private registry via `IMAGE_REGISTRY`
(see `docs/air-gapped-images.md`; the `*-images.txt` manifests are the mirror list).

### Changelog (required before every tag)

`CHANGELOG.md` records the operational and system-level changes an operator
needs when updating an existing install — new/changed parameters, changed
defaults, image bumps, IAM and security-group changes, resource replacements,
new manual steps. It is the upgrade guide, not an exhaustive code log.

Before pushing a `v*` release tag, add a `## vX.Y.Z` section for the new version
(newest first) and commit it. The tag is cut from that commit, so the rule is:
**no release tag without its changelog entry.** Keep entries operator-facing:
state the upgrade action (or "no upgrade action"), and flag anything that
replaces a data-bearing resource.

## Security considerations

- Never hardcode secrets — Secrets Manager only.
- All ECS tasks run with `AssignPublicIp: DISABLED`, in private subnets.
- Database connections require SSL (`LRDB_SSLMODE: require`).
- DB credentials are auto-generated into Secrets Manager.
- Per-tier IAM task roles follow least privilege; the contract is documented in
  `docs/operations/iam-roles.md` and enforced by tests.
- Satellite ingest is **pull-only**: a satellite account never pushes to the
  Lakerunner account. The only cross-account relationship is the access role's
  trust policy naming the Lakerunner principal.
- ECS rolling deployments use `MinimumHealthyPercent: 50`, `MaximumPercent: 200`,
  and the deployment circuit breaker, so a bad image bump rolls back.

## Coding style

- Follow existing coding style as much as practical.
- No trailing whitespace, no extra blank lines.
- All code formatted properly.
- All text-like files end with a final newline.
- Useful comments are welcome; verbosity should be minimal; document non-obvious code only.
- "Section" style comments are OK.
- Markdown unordered lists use `-` not `*`.
- Markdown ordered lists repeat `1.` for each item.
- Blank lines between markdown headers, code blocks, and other items.
- Never add advertisements for Claude or Anthropic to docs or commit messages.
- No emoji.
- If my coworker (user) asks me to change ECS containers to non-root, remind them that bind mounts will require root.
