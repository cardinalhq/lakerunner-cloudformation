# Installing Cardinal lakerunner

Install is one shell-script run followed by one CloudFormation stack:

1. [`install-infrastructure.md`](install-infrastructure.md) -- run
   `scripts/data-setup.sh`. Provisions RDS, S3 ingest, SQS, secrets,
   SSM, the ECS cluster, and the Cloud Map namespace via raw AWS CLI.
   Idempotent. Emits a JSON document that names every resource.
2. [`install-lakerunner.md`](install-lakerunner.md) --
   `cardinal-lakerunner` CloudFormation stack. Creates the ALB, ECS
   services, and migration custom resource. Consumes the script's
   JSON output as input parameters along with the customer-supplied
   IAM role ARNs and security group IDs.

The optional `cardinal-vpc` stack is a convenience for ephemeral test
environments only -- production installs always bring their own VPC.

For the broader operational topics:

- [`certificates.md`](certificates.md) -- TLS certificate options.
- [`permissions-infrastructure.md`](permissions-infrastructure.md) --
  what the operator running `data-setup.sh` needs.
- [`permissions-lakerunner.md`](permissions-lakerunner.md) -- what
  the running application has access to.
- [`deploying.md`](deploying.md) -- using a CloudFormation service
  role to avoid rollback-permission wedges.
- [`tearing-down.md`](tearing-down.md) -- layered teardown procedure.
- [`jenkins-deploy.md`](jenkins-deploy.md) -- pre-pivot Jenkinsfile
  for the lakerunner stack (legacy installs).
- [`end-to-end-test-plan.md`](end-to-end-test-plan.md) -- runtime
  verification checklist.
