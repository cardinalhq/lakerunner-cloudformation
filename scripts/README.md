# scripts/ — per-stack deploy drivers

These are the customer-facing, environment-variable-driven CloudFormation deploy
drivers for the **per-stack** Cardinal Lakerunner model. Each is self-contained
(POSIX `sh` + AWS CLI v2 + `jq`) and creates-or-updates one stack:

| Driver | Stack |
|---|---|
| `deploy-lakerunner-infra-base.sh` | `cardinal-lakerunner-infra-base` (IAM roles, security groups, cooked bucket, license/admin secrets) |
| `deploy-lakerunner-infra-rds.sh` | `cardinal-lakerunner-infra-rds` (RDS Postgres) |
| `deploy-satellite-infra-base.sh` | `cardinal-satellite-infra-base` (raw ingest bucket + SQS + cross-account access role) |
| `deploy-satellite-services.sh` | `cardinal-satellite-services` (OTLP collector) |
| `deploy-lakerunner-services.sh` | `cardinal-lakerunner-services` (ALB, query/process/control, maestro+dex) |

Install order and the full env-var contract per driver are in
[`docs/operations/production-deploy.md`](../docs/operations/production-deploy.md).

## Versioning: `dev` vs a release

The copies committed in this directory bake `STACK_VERSION=dev` and resolve
templates from a development prefix — they are for **building and dev/test
iteration**, not production.

For **production**, do not run the committed `dev` copies. Instead:

- Download the **version-pinned** drivers from the
  [GitHub Releases page](https://github.com/cardinalhq/lakerunner-cloudformation/releases)
  (each release attaches `deploy-*.sh` with `STACK_VERSION=<that version>` baked
  in), or pull them from
  `s3://cardinal-cfn-<region>/lakerunner/<version>/scripts/`, **or**
- run a committed copy with `STACK_VERSION=vX.Y.Z` set explicitly (it also sets
  the matching `TEMPLATE_BASE_URL` so the nested templates resolve).

The release pipeline (`.github/workflows/release.yml`) bakes the tag version
into these drivers and publishes the templates + drivers together to S3 and the
GitHub release, as a unit.

## `deploy-lakerunner-services.sh` primary ingest queue env vars

The services driver reads the primary ingest queue from the satellite-infra-base
stack and wires it directly into the lakerunner-services stack parameters.

| Env var (driver) | Stack parameter | Source |
|---|---|---|
| (from stack output) | `QueueUrl` | `RawQueueUrl` output of `SATELLITE_INFRA_BASE_STACK` |
| (from stack output) | `QueueRoleArn` | `LakerunnerAccessRoleArn` output of `SATELLITE_INFRA_BASE_STACK` |

The `pubsub-sqs` container receives these as `SQS_QUEUE_URL` and `SQS_ROLE_ARN`
env vars. An empty `QueueUrl` idles the pubsub-sqs service.

## Related

- [`docs/operations/production-deploy.md`](../docs/operations/production-deploy.md) — the production install path (admins).
- [`docs/operations/dev-environment.md`](../docs/operations/dev-environment.md) — reproduce a dev/test environment, validate an upgrade, burn it down.
- [`dev-scripts/`](../dev-scripts/) — internal-only wrappers and the `lrdev-*` test scaffolding (VPC + ECS cluster); not customer-facing.
