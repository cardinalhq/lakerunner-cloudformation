# Cardinal Lakerunner CloudFormation

Deploy Cardinal lakerunner on AWS ECS Fargate via two CloudFormation
stacks. The install is parameter-driven from end to end -- no manual
steps between stacks beyond piping the first stack's outputs into the
second's parameters.

## Install flow

1. **IT prereqs (one-time, out of band).** Customer's IT pre-creates
   five IAM roles and three security groups using the cookbook in
   [`docs/operations/required-roles.md`](docs/operations/required-roles.md).
2. **Stack 1: infrastructure** -- `cardinal-data-setup`. Creates RDS,
   S3 ingest, SQS, secrets, SSM. Runbook:
   [`docs/operations/install-infrastructure.md`](docs/operations/install-infrastructure.md).
3. **Stack 2: application** -- `cardinal-lakerunner`. Creates ECS
   cluster, ALB, twelve services, custom-resource Lambdas. Consumes
   stack 1's outputs as inputs. Runbook:
   [`docs/operations/install-lakerunner.md`](docs/operations/install-lakerunner.md).

```
                +----------------+         +-----------------+
IT prereqs ---> | data-setup     | -- 13 outputs --> | lakerunner |
(roles + SGs)   | (data layer)   |         | (application)   |
                +----------------+         +-----------------+
                  RDS, S3, SQS,            ECS, ALB, services,
                  secrets, SSM             custom resources
```

The optional `cardinal-vpc` stack is for ephemeral test environments
only -- production installs always bring their own VPC.

## Published templates

Per release tag at `https://cardinal-cfn.s3.us-east-2.amazonaws.com/lakerunner/<VERSION>/`:

- `cardinal-data-setup.yaml` (+ `cardinal-data-setup-lambda.zip`)
- `cardinal-lakerunner.yaml` (+ nested children under `cardinal-lakerunner/`)
- `cardinal-vpc.yaml` (test only)
- `cardinal-deployer-role.yaml` (optional CFN service role)

There is no `latest` tag -- pin to a specific release tag (e.g.
`v0.0.41`) for reproducibility.

## Air-gapped deployment

Mirror the entire `lakerunner/<VERSION>/` prefix to your own S3 bucket
and override `TemplateBaseUrl` on the lakerunner stack and
`LambdaCodeS3Bucket` / `LambdaCodeS3Key` on the data-setup stack to
point at your bucket. Override the per-image parameters
(`LakerunnerImage`, `MaestroImage`, etc.) to point at your private
registry.

## Architecture

The `cardinal-lakerunner` root template orchestrates twelve nested
stacks:

- `cluster` -- ECS cluster + base log group
- `alb` -- ALB + listener + target groups
- `cert` -- optional ACM cert importer (only when shipping PEMs)
- `migration` -- one-shot DB migration via Lambda-backed custom resource
- `services-query` -- query-api, query-worker
- `services-process` -- process-{logs,metrics,traces}, pubsub-sqs
- `services-control` -- sweeper, monitoring, admin-api, alert-evaluator
- `otel` -- OTEL collector
- `maestro` -- Maestro + bundled DEX OIDC

The data layer (RDS, S3 ingest, SQS, license / DB / admin secrets, SSM
parameters) is created by the `cardinal-data-setup` Lambda outside any
CloudFormation stack and survives `delete-stack` of either stack by
design. See
[`docs/operations/tearing-down.md`](docs/operations/tearing-down.md)
for the layered teardown procedure.

## Operational docs

- [`install-infrastructure.md`](docs/operations/install-infrastructure.md)
  -- runbook: deploy the data-setup stack
- [`install-lakerunner.md`](docs/operations/install-lakerunner.md)
  -- runbook: deploy the application stack
- [`required-roles.md`](docs/operations/required-roles.md)
  -- IAM cookbook for IT prereqs
- [`certificates.md`](docs/operations/certificates.md)
  -- TLS certificate options
- [`permissions-infrastructure.md`](docs/operations/permissions-infrastructure.md)
  -- what the deployer principal needs
- [`permissions-lakerunner.md`](docs/operations/permissions-lakerunner.md)
  -- what the running application has access to
- [`deploying.md`](docs/operations/deploying.md)
  -- using a CloudFormation service role
- [`tearing-down.md`](docs/operations/tearing-down.md)
  -- layered teardown procedure
- [`jenkins-deploy.md`](docs/operations/jenkins-deploy.md)
  -- legacy pre-pivot Jenkinsfile (lakerunner stack only)
- [`end-to-end-test-plan.md`](docs/operations/end-to-end-test-plan.md)
  -- pre-pivot acceptance test plan (Jenkins-driven)

## Development

See [`README-BUILDING.md`](README-BUILDING.md) for generator
instructions.
