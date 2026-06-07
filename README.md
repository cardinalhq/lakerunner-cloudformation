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

## Stacks (install order)

```
1. cardinal-lakerunner-infra-base   IAM roles, security groups, cooked S3 bucket, license/admin secrets
2. cardinal-lakerunner-infra-rds    RDS Postgres
3. cardinal-satellite-infra-base    raw ingest bucket + SQS + cross-account access role
4. cardinal-satellite-services      OTLP collector (its own ALB)
5. cardinal-lakerunner-services     app ALB + query/process/control + maestro/dex (root + nested children)
```

Each driver reads upstream stacks' outputs by name, so order matters.
`cardinal-lakerunner-services` is a root template with nested children: `alb`,
`cert`, `migration`, `services-query`, `services-process`, `services-control`,
`otel`, `maestro`.

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
