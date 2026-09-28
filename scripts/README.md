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

## Versioning

Every driver bakes a default `STACK_VERSION`: the newest `## vX.Y.Z` entry in
[`CHANGELOG.md`](../CHANGELOG.md). A changelog entry is committed before its
tag is cut, so the copies on a release tag are exactly the published drivers
for that version, and the release pipeline (`.github/workflows/release.yml`)
fails a stable tag whose committed drivers do not match.

On `main` the newest entry can be ahead of the last published tag (merged,
not yet tagged). For production, use a release's drivers:

- download them from the
  [GitHub Releases page](https://github.com/cardinalhq/lakerunner-cloudformation/releases)
  or `s3://cardinal-cfn-<region>/lakerunner/<version>/scripts/`,
- check out the release tag and run `scripts/` from there, **or**
- run a committed copy with `STACK_VERSION=vX.Y.Z` set explicitly (it also sets
  the matching `TEMPLATE_BASE_URL` so the nested templates resolve).

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
