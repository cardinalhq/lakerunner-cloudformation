# Cardinal Lakerunner CloudFormation

Deploy Cardinal lakerunner on AWS ECS Fargate via two CloudFormation
stacks. The install is parameter-driven from end to end -- no manual
steps between stacks beyond piping the first stack's outputs into the
second's parameters.

## Install flow

1. **IT prereqs (one-time, out of band).** Customer's IT pre-creates
   the **ECS cluster** and the **VPC + private subnets**. Nothing
   else. All security groups and IAM roles are created by the
   templates.
2. **Stack 1: infrastructure** -- `cardinal-infrastructure`. Creates
   RDS (+ its security group), S3 ingest bucket, SQS ingest queue,
   the `cardinal-*` secrets, and the two `/cardinal/*` SSM parameters.
   All carry `DeletionPolicy: Retain` / `Snapshot`. Runbook:
   [`docs/operations/install-infrastructure.md`](docs/operations/install-infrastructure.md).
3. **Stack 2: application** -- `cardinal-lakerunner`. Creates the
   Cloud Map private DNS namespace, the ALB + its security group, six
   per-tier task security groups, six per-tier IAM task roles + one
   shared execution role, twelve ECS services, and the DB-migration
   ECS service. Consumes stack 1's outputs as inputs. Runbook:
   [`docs/operations/install-lakerunner.md`](docs/operations/install-lakerunner.md).

```
              +-------------------+        +-----------------+
ECS cluster ->| infrastructure    | -- outputs --> | lakerunner |
VPC+subnets   | (RDS, S3, SQS,    |        | (application)   |
              |  secrets, SSM,    |        |                 |
              |  cardinal-rds-sg) |        | ECS, ALB,       |
              +-------------------+        | services, SGs,  |
                                           | IAM roles       |
                                           +-----------------+
```

Production installs always bring their own VPC. For our internal
ephemeral test environments the repo ships an `lrdev-vpc` template that
synthesises a customer-equivalent VPC; it is scaffolding for our test
account, not a customer-facing artifact.

## Published templates

Per release tag at `https://cardinal-cfn-us-east-1.s3.us-east-1.amazonaws.com/lakerunner/<VERSION>/`:

- `cardinal-infrastructure.yaml`
- `cardinal-lakerunner.yaml` (+ nested children under `cardinal-lakerunner/`)
- `cardinal-cleanup.yaml` (optional teardown task)
- `lrdev-vpc.yaml` (internal test scaffolding; customers ignore)

There is no `latest` tag -- pin to a specific release tag (e.g.
`v0.0.41`) for reproducibility.

## Air-gapped deployment

Mirror the entire `lakerunner/<VERSION>/` prefix to your own S3 bucket
and override `TemplateBaseUrl` on the lakerunner root to point at your
bucket. Override the per-image parameters (`LakerunnerImage`,
`MaestroImage`, etc.) to point at your private registry.

## Architecture

The `cardinal-lakerunner` root template orchestrates nine nested
stacks:

- `Security` -- ALB SG, six per-tier task SGs, six per-tier task IAM
  roles, the shared ECS execution role, and ingress rules into the
  infra-owned RDS SG.
- `Alb` -- ALB + listeners (443 / 9443 / 4318)
- `Cert` -- optional ACM cert importer (only when shipping PEMs)
- `Migration` -- one-shot DB migration via an ECS service
- `Query` -- query-api, query-worker
- `Process` -- process-{logs,metrics,traces}, pubsub-sqs
- `Control` -- sweeper, monitoring, admin-api, alert-evaluator
- `Otel` -- OTEL collector
- `Maestro` -- Maestro + bundled DEX OIDC

The data layer (RDS, S3 ingest, SQS, license / DB / admin secrets, SSM
parameters) is created by the `cardinal-infrastructure` stack with
`Retain` / `Snapshot` policies and survives `delete-stack` by design.
See [`docs/operations/tearing-down.md`](docs/operations/tearing-down.md)
for the layered teardown procedure.

## Operational docs

- [`install-infrastructure.md`](docs/operations/install-infrastructure.md)
  -- runbook: deploy the infrastructure stack
- [`install-lakerunner.md`](docs/operations/install-lakerunner.md)
  -- runbook: deploy the application stack
- [`certificates.md`](docs/operations/certificates.md)
  -- TLS certificate options
- [`permissions-infrastructure.md`](docs/operations/permissions-infrastructure.md)
  -- what the deployer principal needs
- [`permissions-lakerunner.md`](docs/operations/permissions-lakerunner.md)
  -- what the running application has access to
- [`iam-roles.md`](docs/operations/iam-roles.md)
  -- per-tier IAM role contract (what each container needs and why); test-enforced
- [`deploying.md`](docs/operations/deploying.md)
  -- using a CloudFormation service role
- [`tearing-down.md`](docs/operations/tearing-down.md)
  -- layered teardown procedure
- [`cleanup.md`](docs/operations/cleanup.md)
  -- `cardinal-cleanup` task for wiping a test install
- [`end-to-end-test-plan.md`](docs/operations/end-to-end-test-plan.md)
  -- pre-pivot acceptance test plan (Jenkins-driven)
- [`deploy-scripts/README.md`](deploy-scripts/README.md)
  -- env-var-driven wrappers for the four product stacks + the cleanup
  driver (intended for Jenkins job invocation)

## Development

See [`README-BUILDING.md`](README-BUILDING.md) for generator
instructions.
