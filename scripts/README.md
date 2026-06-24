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

## `deploy-lakerunner-services.sh` satellite config env vars

The services driver synthesizes a satellite-mapping JSON and writes it to SSM
before deploying the stack. Maestro reads it as `MAESTRO_SATELLITE_CONFIG`.

| Env var | Default | Purpose |
|---|---|---|
| `SATELLITE_CONFIG` | (empty) | Inline JSON `{ "organizations": { ... } }` of operator-supplied read-only/satellite collectors. Do not include a `normal` collector for the install org. |
| `SATELLITE_CONFIG_FILE` | (empty) | Path to a JSON file with the same shape (used when `SATELLITE_CONFIG` is unset). |
| `CENTRAL_COLLECTOR_NAME` | `lakerunner` | Collector name for the auto-synthesized central entry. On upgrade this must match the install's existing collector name or a duplicate `normal` rejection occurs. |
| `SATELLITES_PARAM_NAME` | `/cardinal/satellites` | SSM parameter name receiving the composed satellite config JSON. Passed to the stack as `SatellitesParamName`. |

The central `normal` collector (the install's own bucket + queue) is always
synthesized automatically from the stack outputs — operators supply only
read-only or satellite collectors in `SATELLITE_CONFIG`.

## Related

- [`docs/operations/production-deploy.md`](../docs/operations/production-deploy.md) — the production install path (admins).
- [`docs/operations/dev-environment.md`](../docs/operations/dev-environment.md) — reproduce a dev/test environment, validate an upgrade, burn it down.
- [`dev-scripts/`](../dev-scripts/) — internal-only wrappers and the `lrdev-*` test scaffolding (VPC + ECS cluster); not customer-facing.
