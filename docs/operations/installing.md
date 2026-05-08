# Installing Cardinal lakerunner

Install is two ordered CloudFormation stacks. Each has its own
runbook:

1. [`install-infrastructure.md`](install-infrastructure.md) --
   `cardinal-data-setup` stack. Creates RDS, S3 ingest, SQS, secrets,
   SSM. Run first.
2. [`install-lakerunner.md`](install-lakerunner.md) --
   `cardinal-lakerunner` stack. Creates ECS, ALB, services. Run
   second; consumes the data-setup outputs as inputs.

The optional `cardinal-vpc` stack is a convenience for ephemeral test
environments only -- production installs always bring their own VPC.

For the broader operational topics:

- [`required-roles.md`](required-roles.md) -- the IAM cookbook for
  the IT-side prereqs.
- [`certificates.md`](certificates.md) -- TLS certificate options.
- [`permissions-infrastructure.md`](permissions-infrastructure.md) --
  what the deployer principal needs.
- [`permissions-lakerunner.md`](permissions-lakerunner.md) -- what
  the running application has access to.
- [`deploying.md`](deploying.md) -- using a CloudFormation service
  role to avoid rollback-permission wedges.
- [`tearing-down.md`](tearing-down.md) -- layered teardown procedure.
- [`jenkins-deploy.md`](jenkins-deploy.md) -- pre-pivot Jenkinsfile
  for the lakerunner stack (legacy installs).
- [`end-to-end-test-plan.md`](end-to-end-test-plan.md) -- runtime
  verification checklist.
