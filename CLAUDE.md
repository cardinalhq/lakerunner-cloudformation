# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository overview

Generators (Python + troposphere) emit one customer-facing CloudFormation root (the application stack) plus eight nested children, and an optional VPC root. All infra is provisioned by `scripts/data-setup.sh`, not by CloudFormation. Design context lives in:

- `docs/superpowers/specs/2026-04-28-cardinal-cfn-refactor-design.md` — design spec (source of truth)
- `docs/superpowers/plans/2026-04-28-cardinal-cfn-refactor.md` — phased implementation plan

When in doubt, the design spec wins.

## Target architecture

Two customer-facing CloudFormation root templates:

- `lrdev-vpc.yaml` — internal test-env VPC scaffolding (not customer-facing; we use it to simulate a customer-supplied VPC in our test account)
- `lrdev-baseinfra.yaml` — internal test-env base-infrastructure scaffolding (ECS Fargate cluster; simulates a customer-supplied cluster)
- `cardinal-lakerunner.yaml` — application root, composed of eight nested children

Plus a single shell driver:

- `scripts/data-setup.sh` — raw-AWS-CLI data provisioner. Creates RDS, S3 ingest, SQS, the three `cardinal-*` Secrets Manager secrets (`-db-master`, `-license`, `-admin-key`), and the two SSM parameters. Idempotent; emits a JSON document on stdout whose keys map 1:1 to the lakerunner stack's infra-setup parameters. The customer supplies all IAM roles, security groups, the ECS cluster, and the Cloud Map private DNS namespace out-of-band; they are inputs to the script, which forwards their identifiers into the JSON output.

The lakerunner root nests these children — application-tier resources only:

| # | Template | Owns |
|---|---|---|
| 1 | `alb.yaml` | ALB, default 443 listener (ALB SG is customer-supplied) |
| 2 | `cert.yaml` | Optional cert installer (pass-through ACM/IAM ARN, or `AWS::IAM::ServerCertificate` from PEMs) |
| 3 | `migration.yaml` | DB migration ECS service (runs migrator once, then idles) |
| 4 | `services-query.yaml` | query-api, query-worker |
| 5 | `services-process.yaml` | process-{logs,metrics,traces}, pubsub-sqs |
| 6 | `services-control.yaml` | sweeper, monitoring, admin-api, alert-evaluator |
| 7 | `otel.yaml` | otel-collector |
| 8 | `maestro.yaml` | Maestro + bundled DEX OIDC |

The lakerunner stack creates **no infra** of its own — no ECS cluster, no security groups, no IAM roles, no databases, no buckets, no queues, no secrets, no SSM parameters. Every such resource is created by `data-setup.sh` (or by the customer) and threaded into the stack as a parameter.

Cross-stack wiring goes through the root via `Fn::GetAtt childStack.Outputs.X` → child parameter. Sibling children never reference each other directly.

## Repo / generator code layout

```
src/
  cardinal_cfn/
    __init__.py
    install_id.py            # InstallIdShort/InstallIdLong derivation (root only)
    naming.py                # Tag conventions, name-tag helpers
    parameters.py            # Shared parameter / NoEcho / parameter-group helpers
    images.py                # Image-override parameter machinery
    policies.py              # DeletionPolicy / UpdateReplacePolicy table
    listener_priorities.py   # Pre-allocated ListenerRule priorities (B → C safe)
    defaults.py              # cardinal-defaults.yaml loader
    children/                # one module per nested-stack child
    root.py                  # parent template generator
    lrdev_vpc.py             # internal test-env VPC generator (lrdev-vpc.yaml)
    lrdev_baseinfra.py       # internal test-env ECS cluster generator (lrdev-baseinfra.yaml)
scripts/
  data-setup.sh              # infra provisioner (raw AWS CLI; idempotent)
  deploy-lakerunner.sh       # CFN deploy driver
  teardown-lakerunner.sh     # CFN teardown driver
cardinal-defaults.yaml       # consolidated defaults (services, images, maestro, otel)
tests/
  conftest.py                # adds src/ to sys.path; no pip install -e needed
  unit/                      # helper-level tests
  templates/                 # per-template assertions via cloud-radar
.github/workflows/
  test.yml                   # PR/main test runner
  release.yml                # publish to S3 on tag
```

Generated templates go to `generated-templates/`, mirroring the S3 key layout:

```
generated-templates/
  lrdev-vpc.yaml
  lrdev-baseinfra.yaml
  cardinal-lakerunner.yaml             # the root
  cardinal-lakerunner/                 # the children, mirrors S3 prefix
    alb.yaml
    cert.yaml
    migration.yaml
    services-query.yaml
    services-process.yaml
    services-control.yaml
    otel.yaml
    maestro.yaml
```

## Key design rules

### Naming and tags

- Default to **CloudFormation-generated physical names** with a `Name` tag.
- Prefix is `cardinal-`. Use `chq-` only when an AWS resource name length cap forces it.
- Explicit physical names *only* where externally referenced — SSM Parameter names (AWS-required), the S3 ingest bucket name (predictability), license/admin secrets (referenced from outside the stack).
- Never name RDS, ECS clusters/services, listener rules, log groups, target groups — explicit names block in-place updates.

### Single-install assumption

The fixed `cardinal-*` resource names the script creates (RDS instance, S3 bucket, SQS queue, secrets, SSM params) imply one Cardinal install per AWS account/region. The ECS cluster + Cloud Map namespace names are customer-chosen but should be picked with the same constraint in mind. Customers running multiple installs should use separate accounts (or separate regions).

### Multi-install isolation (lakerunner-internal)

Within a single account/region, `InstallIdShort` (8 hex) and `InstallIdLong` (12 hex) are derived from the root stack's `AWS::StackId` and propagated as parameters to every nested child. **Children never compute these themselves** — `Ref(AWS::StackId)` in a child returns the child's id, not the root's.

```
UUID            = Fn::Select(2, Fn::Split("/", Ref(AWS::StackId)))
InstallIdShort  = Fn::Select(0, Fn::Split("-", UUID))
InstallIdLong   = Fn::Join("", [first two segments of UUID])
```

### Sensitive values

- Sensitive values **always** go to Secrets Manager. `AWS::SSM::Parameter` cannot be `SecureString` in CloudFormation.
- Parameters carrying secrets (`LicenseData`, `ApiKeysOverride`, `StorageProfilesOverride`) declare `NoEcho: true`.

### List parameters into nested stacks

CloudFormation passes nested-stack parameters as strings. Lists like `PrivateSubnets` cannot be reliably forwarded as `List<...>`. The convention: every child declares such parameters as `String` (CSV) and uses `Fn::Split(",", ...)` internally. The root joins with `Fn::Join(",", ...)` before passing.

### Lifecycle policies

Customer-data-bearing resources get `DeletionPolicy: Snapshot` (RDS) or `Retain` (S3 ingest, license/admin secrets). Stateless resources are `Delete`. The exact table lives in `src/cardinal_cfn/policies.py` and is enforced by `apply_policy(resource, kind)`.

### ListenerRule priorities

Pre-allocated, registered in `src/cardinal_cfn/listener_priorities.py`. Each service's priority stays the same when the service is later moved into its own per-service stack — preventing collisions during the future B → C split.

| Service | Priority |
|---|---|
| query-api | 100 |
| admin-api | 110 |
| maestro-https | 200 |
| maestro-dex | 210 |
| otel-grpc | 300 |
| (reserved) | 400-999 |

### Migration (no Lambda)

`migration.yaml` runs the lakerunner DB migrator as an **ECS service**, not a Lambda-backed custom resource (some target environments cannot run Lambda). Design: `docs/superpowers/specs/2026-05-12-no-lambda-migration-design.md`.

- The migrator task definition has three containers: `configdb-init` (non-essential; `psql CREATE DATABASE configdb` if absent) → `migrator` (non-essential; `lakerunner migrate --databases=lrdb,configdb`; `dependsOn configdb-init=COMPLETE`) → `keepalive` (essential; sleeps; `dependsOn migrator=SUCCESS`).
- Because `keepalive` is the only essential container and ECS won't start it until `migrator` exits 0, the task — and therefore the `MigratorService`, and therefore the `MigrationStack` nested stack — only reaches a stable state after migrations succeed. The service-tier stacks `DependsOn MigrationStack`, so they only deploy after migrations run. A failed migration → `keepalive` never starts → the ECS deployment circuit breaker fails the service → `MigrationStack` fails → the parent stack rolls back.
- The migrator runs from the same image as the lakerunner service tasks (single `LakerunnerImage` parameter), so the two cannot drift; an image change redeploys `MigratorService` (rerunning the migrator) before the service-tier stacks update. Customers who want digest pinning use `image@sha256:...`; mutable tags like `:latest` are not supported.
- `DesiredCount` is hardcoded to `1` (~$3/month Fargate). An operator may `aws ecs update-service --desired-count 0` to reclaim the slot — harmless CFN drift, re-applied on the next `LakerunnerImage` bump. The migrator must stay idempotent (a stray task recycle reruns it as a no-op).
- There are no Lambdas anywhere in the product. `cert.yaml` either forwards a supplied ACM/IAM certificate ARN, or — when `CertificateArn` is empty and PEM material is supplied — creates an `AWS::IAM::ServerCertificate` (an ALB HTTPS listener accepts an IAM server-cert ARN like an ACM one).

### Service tier rule (B → C safe)

The three service tier stacks (`services-query`, `services-process`, `services-control`) own *only* per-service resources: ECS Service, TaskDefinition, TargetGroup, ListenerRule, per-service log group. Anything shared across services lives in `alb` or arrives as an infra-setup parameter (cluster, task SG, task/execution roles, secret ARNs, SSM param names). This rule keeps the door open to a future per-service-stack split with minimal disruption.

### Process-tier autoscaling

`services-process` creates `process-{logs,metrics,traces}` at one replica (`min_replicas` from `cardinal-defaults.yaml`). The `monitoring` service in `services-control` scales them up to the `ProcessLogsReplicas` / `ProcessMetricsReplicas` / `ProcessTracesReplicas` cap (default 10 each) via `ecs:UpdateService`. Those parameters are the autoscaler *ceiling*, not the initial `DesiredCount` — creating the services at the ceiling would launch ~3x the steady-state task count on every deploy and can exhaust the account's Fargate vCPU quota. `pubsub-sqs` is not autoscaled; `PubsubSqsReplicas` (default 1) is its literal `DesiredCount`. The root forwards the same `Process*Replicas` Refs to both `services-process` (where they are now unused — kept symmetric with the customer-facing surface) and `services-control` (the autoscaler max).

## Build and testing

Canonical workflow uses the Makefile:

```sh
make install        # one-time: create .venv and install requirements.txt
make build          # generate every template into generated-templates/, then cfn-lint
make test           # all tests (helper unit + per-template)
make test-unit      # helper unit tests only
make test-templates # cloud-radar per-template assertions only
make lint           # cfn-lint over generated-templates/
make check          # alias for `make test` (pre-push gate)
```

For debugging a single template, generators can be invoked directly (PYTHONPATH=src):

```sh
python3 -m cardinal_cfn.children.<child>   # emits child YAML to stdout
python3 -m cardinal_cfn.root               # emits root YAML
python3 -m cardinal_cfn.lrdev_vpc          # emits internal lrdev VPC YAML
python3 -m cardinal_cfn.lrdev_baseinfra    # emits internal lrdev base-infra (ECS cluster) YAML
```

All templates must pass cfn-lint with no errors. Warnings are tolerable when explainable; `.cfnlintrc` carries the project-wide ignores.

## Publishing

GitHub Actions on tag push (`v*`) builds, lints, tests, and publishes to the vendor-managed public S3 buckets, provisioned in `terraform-deployments/aws/production/cloudformation-distribution.tf`. The buckets are region-suffixed, one per published region:

- `cardinal-cfn-us-east-1` (us-east-1)
- `cardinal-cfn-us-east-2` (us-east-2)

Customers paste the regional S3 URL for their region into the CloudFormation console (substitute the matching region in both the bucket name and the host):

```
https://cardinal-cfn-us-east-1.s3.us-east-1.amazonaws.com/lakerunner/<version>/cardinal-lakerunner.yaml
https://cardinal-cfn-us-east-2.s3.us-east-2.amazonaws.com/lakerunner/<version>/cardinal-lakerunner.yaml
```

Air-gapped customers override the `TemplateBaseUrl` parameter on the root stack to point at a customer-owned mirror. The `data-setup.sh` script is run by the customer's operator out-of-band and does not need to be hosted; it is committed under `scripts/`.

## Security considerations

- Never hardcode secrets — Secrets Manager only.
- All ECS tasks run with `AssignPublicIp: DISABLED`.
- Database connections require SSL (`LRDB_SSLMODE: require`).
- DB credentials are auto-generated into Secrets Manager.
- ECS task roles follow least privilege — each service has its own task role scoped to exactly the resources it needs.
- All tasks run in private subnets with no public IP assignment.
- ECS rolling deployments use `MinimumHealthyPercent: 50`, `MaximumPercent: 200`, and the deployment circuit breaker enabled, so a bad image bump rolls back automatically.

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
