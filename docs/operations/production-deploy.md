# Production deploy (admin)

The supported way to install Cardinal Lakerunner: five CloudFormation stacks,
deployed in order by the env-var-driven drivers in [`scripts/`](../../scripts/),
using **version-pinned** release artifacts. The customer brings their own VPC
and ECS Fargate cluster; everything else is created by these stacks.

For a throwaway test/upgrade environment instead, see
[`dev-environment.md`](dev-environment.md).

## Prerequisites

- A VPC with private subnets (and public subnets if the app ALB must be
  internet-facing), and an ECS Fargate cluster — both customer-supplied.
- AWS CLI v2 + `jq`, and credentials for the target account/region.
- A Cardinal **license** token (passed as `LICENSE_DATA` / `LICENSE_DATA_FILE`).
- A bcrypt **DEX admin password hash** (`DEX_ADMIN_PASSWORD_HASH`).
- A TLS certificate for the Maestro HTTPS listener — see
  [`certificates.md`](certificates.md) (ACM recommended; self-signed is
  auto-generated on first create if none supplied).
- Optional but recommended: a CloudFormation deployer service role — see
  [`deploying.md`](deploying.md) (`DEPLOYER_ROLE_ARN`).

## Get the version-pinned drivers

Do **not** use the `dev`-defaulted copies committed in `scripts/`. Download the
drivers for the version you are installing from the
[GitHub Releases page](https://github.com/cardinalhq/lakerunner-cloudformation/releases)
(each release attaches `deploy-*.sh` with `STACK_VERSION` baked in), or from
`s3://cardinal-cfn-<region>/lakerunner/<version>/scripts/`. Alternatively run a
committed copy with `STACK_VERSION=vX.Y.Z` set explicitly. See
[`scripts/README.md`](../../scripts/README.md).

## Install order

Each driver create-or-updates its stack and reads upstream stacks' outputs by
name, so they must run in this order:

```
1. cardinal-lakerunner-infra-base     (deploy-lakerunner-infra-base.sh)
2. cardinal-lakerunner-infra-rds      (deploy-lakerunner-infra-rds.sh)
3. cardinal-satellite-infra-base      (deploy-satellite-infra-base.sh)
4. cardinal-satellite-services        (deploy-satellite-services.sh)
5. cardinal-lakerunner-services       (deploy-lakerunner-services.sh)
```

Common env for every step: `REGION`, and either a version-pinned driver or
`STACK_VERSION=vX.Y.Z`. Use `NO_EXECUTE=1` on any step to produce a change set
without executing it.

### 1. infra-base

Creates the IAM roles, security groups, cooked S3 bucket, and the
`cardinal-license` / `cardinal-admin-key` secrets. No upstream.

- Required: `STACK_NAME`, `REGION`, `VPC_ID`, `CLUSTER_ARN`, `LICENSE_DATA` (or `LICENSE_DATA_FILE`).
- App ALB visibility (the ALB itself lives in step 5; its security-group ingress
  is here): `ALB_SCHEME=internal|internet-facing` (default `internal`),
  `ALB_ALLOWED_CIDR1..3` (default RFC1918). For a public app ALB set
  `ALB_SCHEME=internet-facing` and `ALB_ALLOWED_CIDR1=0.0.0.0/0` here.
- Optional: `COOKED_BUCKET_NAME`, `EXECUTION_ROLE_POLICY_ARNS`,
  `EXECUTION_ROLE_POLICY_JSON[_FILE]` (see [`air-gapped-images.md`](../air-gapped-images.md)).
- After it completes, note the `ProcessRoleArn` output — it is step 3's `LAKERUNNER_PRINCIPAL`.

### 2. infra-rds

Creates RDS Postgres. Pulls the task security-group IDs from infra-base.

- Required: `STACK_NAME`, `REGION`, `INFRA_BASE_STACK`, `VPC_ID`, `PRIVATE_SUBNETS` (CSV).
- Optional: `DB_ENGINE_VERSION` (default `18.4`), `DB_INSTANCE_CLASS` (default `db.r7g.large`), `DB_ALLOCATED_STORAGE` (default `100`).

### 3. satellite-infra-base

Creates the raw ingest bucket, its SQS queue, and the cross-account access role
the Lakerunner poller assumes. No CloudFormation upstream pull.

- Required: `STACK_NAME`, `REGION`, `LAKERUNNER_PRINCIPAL` (= infra-base `ProcessRoleArn`).
- Optional: `EXTERNAL_ID`, `RAW_BUCKET_NAME`, `RAW_BUCKET_LIFECYCLE_DAYS` (default `7`).
- Outputs consumed later: `RawQueueUrl`, `LakerunnerAccessRoleArn`, `RawBucketName`.

### 4. satellite-services (the collector)

Runs the OTLP collector behind its own ALB. Pulls `RawBucketName` from
satellite-infra-base.

- Required: `STACK_NAME`, `REGION`, `SATELLITE_INFRA_BASE_STACK`, `ORGANIZATION_ID`, `VPC_ID`, `ALB_SUBNETS` (CSV), `TASK_SUBNETS` (CSV), `ECS_CLUSTER_ARN`.
- Collector ALB visibility: `ALB_SCHEME=internal|internet-facing` (default `internal`). Keep the collector **internal** unless external senders must reach it; use private subnets for `ALB_SUBNETS`/`TASK_SUBNETS`.
- `ORGANIZATION_ID` **must be identical** here and in step 5.
- Output consumed later: `CollectorEndpoint` (e.g. `http://<alb>:4318`).

### 5. lakerunner-services (the application tier)

Creates the app ALB, the query/process/control services, and maestro+dex. Pulls
DB/role/SG outputs from infra-base + infra-rds, and `RawQueueUrl` /
`LakerunnerAccessRoleArn` from satellite-infra-base. Self-telemetry is wired
automatically by pulling `CollectorEndpoint` from satellite-services.

- Required: `STACK_NAME`, `REGION`, `INFRA_BASE_STACK`, `INFRA_RDS_STACK`, `SATELLITE_INFRA_BASE_STACK`, `ORGANIZATION_ID`, `CLUSTER_ARN`, `CLUSTER_NAME`, `VPC_ID`, `PRIVATE_SUBNETS` (CSV), `DEX_ADMIN_PASSWORD_HASH`.
- App ALB visibility: `ALB_SCHEME=internet-facing` + `PUBLIC_SUBNETS` (CSV) for a public Maestro; must agree with the infra-base `ALB_SCHEME`/CIDR settings.
- Certificate: `CERTIFICATE_ARN`, or `CERTIFICATE_BODY[_FILE]` + `CERTIFICATE_PRIVATE_KEY[_FILE]` (+ optional `CERTIFICATE_CHAIN[_FILE]`). Unset on first create → self-signed; on update the existing cert is kept. See [`certificates.md`](certificates.md).
- Self-telemetry: leave `SELF_TELEMETRY_ENDPOINT` unset to auto-pull the collector endpoint from `SATELLITE_SERVICES_STACK` (default `cardinal-satellite-services`); set it to override.
- Satellite collectors: the driver auto-synthesizes the central `lakerunner` collector from the install's bucket/queue/region. Additional read-only or satellite collectors are supplied via `SATELLITE_CONFIG` (inline JSON) or `SATELLITE_CONFIG_FILE` (path to a JSON file). The JSON shape is:
  ```json
  { "organizations": { "<org-uuid>": { "collectors": {
    "<name>": { "bucket": "...", "sqsurl": "...", "region": "...", "role": "(optional)",
                "mode": "read-only|satellite" } } } } }
  ```
  Do not declare a `normal` collector for the install org — the driver synthesizes one automatically. The merged JSON is written to SSM `SATELLITES_PARAM_NAME` (default `/cardinal/satellites`) and injected into Maestro as `MAESTRO_SATELLITE_CONFIG`.
- `CENTRAL_COLLECTOR_NAME` (default `lakerunner`): the collector name assigned to the auto-synthesized central entry. Must match the install's existing collector name on upgrade (see v1.5.0 CHANGELOG).
- `SATELLITES_PARAM_NAME` (default `/cardinal/satellites`): SSM parameter receiving the composed satellite config JSON.
- Optional: `DEX_ADMIN_EMAIL`, `OIDC_SUPERADMIN_EMAILS`, `DEX_CLIENT_ID`, `SERVICE_NAMESPACE_NAME`, `DB_INIT_IMAGE`, `IMAGE_REGISTRY` (see [`air-gapped-images.md`](../air-gapped-images.md)).

## Upgrades

Re-run the relevant driver(s) at the new `STACK_VERSION` (a version-pinned
driver from the new release). Drivers create-or-update; on update, parameters
you do not set carry their previous value. First-party image versions are baked
into each release's drivers/templates, so the version bump carries them.

## Teardown

The product assumes a single install per account/region. Tear down by deleting
the five stacks in reverse order and removing the retained, fixed-name
resources that block a future re-create. The exact sequence (and the survivors
to delete: `cardinal-license` / `cardinal-admin-key` / `cardinal-db-master`
secrets, the cooked + raw buckets, the RDS final snapshot) is documented under
"Burn it down" in [`dev-environment.md`](dev-environment.md).

## Reference

- [`certificates.md`](certificates.md) — TLS options.
- [`deploying.md`](deploying.md) — CloudFormation deployer service role.
- [`iam-roles.md`](iam-roles.md), [`permissions-infrastructure.md`](permissions-infrastructure.md), [`permissions-lakerunner.md`](permissions-lakerunner.md) — IAM contracts.
- [`air-gapped-images.md`](../air-gapped-images.md) — image mirroring / `IMAGE_REGISTRY`.
