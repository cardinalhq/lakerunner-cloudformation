# Cardinal Lakerunner CloudFormation

Deploy Cardinal Lakerunner on AWS ECS Fargate. The install is five
CloudFormation stacks, created in order by env-var-driven drivers in
[`scripts/`](scripts/). The customer brings their own VPC and ECS Fargate
cluster; the stacks create everything else (IAM roles, security groups, RDS, S3,
SQS, secrets, ALBs, ECS services).

## Where to start

- **Production install (admins):** [`docs/operations/production-deploy.md`](docs/operations/production-deploy.md)
- **Dev/test environment (reproduce, validate an upgrade, burn it down):** [`docs/operations/dev-environment.md`](docs/operations/dev-environment.md)
- **The drivers themselves:** [`scripts/README.md`](scripts/README.md)

## Stacks

**You provide:** a VPC with subnets, and an ECS Fargate cluster. The stacks
create everything else. Each driver reads upstream stacks' outputs by name, so
order matters.

### Base lakerunner install

The central application: API/query/process/control services, the database, and
the Maestro UI.

1. **`cardinal-lakerunner-infra-base`** (`deploy-lakerunner-infra-base.sh`) — creates IAM roles, security groups, the cooked S3 bucket, and the license/admin-key secrets.
1. **`cardinal-lakerunner-infra-rds`** (`deploy-lakerunner-infra-rds.sh`) — creates the RDS Postgres database.
1. **`cardinal-lakerunner-services`** (`deploy-lakerunner-services.sh`) — creates the app ALB, the query/process/control services, and maestro+dex.

`cardinal-lakerunner-services` is a root template with nested children: `alb`,
`cert`, `migration`, `services-query`, `services-process`, `services-control`,
`otel`, `maestro`.

### Satellite install

The OTLP ingest edge — a raw bucket/queue plus the collector that fills them.
May live in a different account from the base install; it only needs the base
install's process-role ARN as its trusted principal.

1. **`cardinal-satellite-infra-base`** (`deploy-satellite-infra-base.sh`) — creates the raw ingest S3 bucket, its SQS queue, and the cross-account access role the base install assumes.
1. **`cardinal-satellite-services`** (`deploy-satellite-services.sh`) — creates the OTLP collector and its own ALB.

### Single-account order

When the base and a satellite share one account, the dependencies interleave:
`satellite-infra-base` needs the base `ProcessRoleArn`, and
`lakerunner-services` needs the satellite's queue/role and collector endpoint.
Deploy in this order (see
[`docs/operations/production-deploy.md`](docs/operations/production-deploy.md)):

1. `cardinal-lakerunner-infra-base`
1. `cardinal-lakerunner-infra-rds`
1. `cardinal-satellite-infra-base`
1. `cardinal-satellite-services`
1. `cardinal-lakerunner-services`

## Published artifacts & versioning

Per release tag at
`https://cardinal-cfn-<region>.s3.<region>.amazonaws.com/lakerunner/<VERSION>/`
(templates) and `.../<VERSION>/scripts/` (version-baked drivers). The same
version-baked drivers are attached to each
[GitHub release](https://github.com/cardinalhq/lakerunner-cloudformation/releases).
There is no `latest` — pin a specific tag. The copies committed under `scripts/`
default to `STACK_VERSION=dev`; production uses the release-pinned copies (see
[`scripts/README.md`](scripts/README.md)).

## Air-gapped deployment

Mirror the `lakerunner/<VERSION>/` prefix to your own bucket and set
`TEMPLATE_BASE_URL`; point images at a private registry via `IMAGE_REGISTRY`.
See [`docs/air-gapped-images.md`](docs/air-gapped-images.md).

## Operational docs

- [`production-deploy.md`](docs/operations/production-deploy.md) — production install path (admins)
- [`dev-environment.md`](docs/operations/dev-environment.md) — dev/test install, self-telemetry validation, teardown
- [`certificates.md`](docs/operations/certificates.md) — TLS certificate options
- [`deploying.md`](docs/operations/deploying.md) — using a CloudFormation service role
- [`permissions-infrastructure.md`](docs/operations/permissions-infrastructure.md) — what the deployer principal needs
- [`permissions-lakerunner.md`](docs/operations/permissions-lakerunner.md) — what the running application has access to
- [`iam-roles.md`](docs/operations/iam-roles.md) — per-tier IAM role contract (test-enforced)
- [`air-gapped-images.md`](docs/air-gapped-images.md) — image mirroring / private registry
- [`dev-scripts/README.md`](dev-scripts/README.md) — internal `lrdev-*` scaffolding + wrappers

## Development

See [`README-BUILDING.md`](README-BUILDING.md) for generator instructions.
