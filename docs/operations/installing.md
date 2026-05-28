# Installing Cardinal lakerunner

Install is two CloudFormation stacks:

1. [`install-infrastructure.md`](install-infrastructure.md) -- deploy
   `cardinal-infrastructure.yaml`. Provisions RDS + its security group,
   S3 ingest bucket, SQS ingest queue, secrets, and SSM parameters. All
   data-bearing resources carry `Retain` / `Snapshot` policies.
2. [`install-lakerunner.md`](install-lakerunner.md) -- deploy
   `cardinal-lakerunner.yaml`. Creates the ALB, the ECS services, the
   DB-migration ECS service, the Cloud Map private DNS namespace, the
   ALB SG, six per-tier task SGs, and seven IAM roles (one shared
   execution role + six per-tier task roles). Consumes the
   infrastructure stack's outputs as parameters along with the
   customer-supplied VPC + ECS cluster identifiers.

Production installs always bring their own VPC. The repo also ships an
`lrdev-vpc` template that we use internally to stand up customer-equivalent
networking in our test account; it is not part of the customer install.

For the broader operational topics:

- [`certificates.md`](certificates.md) -- TLS certificate options.
- [`permissions-infrastructure.md`](permissions-infrastructure.md) --
  what the operator deploying the templates needs.
- [`permissions-lakerunner.md`](permissions-lakerunner.md) -- what
  the running application has access to.
- [`deploying.md`](deploying.md) -- using a CloudFormation service
  role to avoid rollback-permission wedges.
- [`tearing-down.md`](tearing-down.md) -- layered teardown procedure.
- [`cleanup.md`](cleanup.md) -- `cardinal-cleanup` stack for wiping a
  test install.
- [`jenkins-deploy.md`](jenkins-deploy.md) -- pre-pivot Jenkinsfile
  for the lakerunner stack (legacy installs).
- [`end-to-end-test-plan.md`](end-to-end-test-plan.md) -- runtime
  verification checklist.
