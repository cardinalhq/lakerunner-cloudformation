# dev-scripts/

Internal-only tooling for our test account: the `lrdev-*` scaffolding that
simulates a customer's BYO VPC + ECS cluster, plus some lifecycle helpers. Not
customer-facing and not published. The customer-facing per-stack deploy drivers
live in [`scripts/`](../scripts/).

**The canonical dev/test workflow** (stand up an environment, validate an
upgrade, burn it down) is
[`docs/operations/dev-environment.md`](../docs/operations/dev-environment.md).
Use that; this file just documents the individual helpers.

## Scaffolding (current — use these)

Stand up a test VPC + ECS cluster to stand in for customer BYO infrastructure.
Deploy once and keep across re-installs.

### deploy-lrdev-vpc.sh

| Var | Required | Default | Notes |
|---|---|---|---|
| `VERSION` | yes | -- | Published template tag (e.g. `v0.0.80`). |
| `REGION` | no | `us-east-1` | |
| `STACK_NAME` | no | `lrdev-vpc` | |
| `TEMPLATE_BUCKET` | no | `cardinal-cfn-${REGION}` | |
| `ENVIRONMENT_NAME` | no | `lrdev` | Used in resource Name tags. |
| `VPC_CIDR` | no | `10.0.0.0/16` | |
| `CREATE_NAT_GATEWAY` | no | `Yes` | `No` saves ~$30/mo but blocks private-subnet egress. |
| `CREATE_INTERFACE_ENDPOINTS` | no | `No` | `Yes` adds ~$7/endpoint/month per AZ. |

Outputs: `VpcId`, `PublicSubnetsCsv`, `PrivateSubnetsCsv`.

### deploy-lrdev-baseinfra.sh

| Var | Required | Default | Notes |
|---|---|---|---|
| `VERSION` | yes | -- | Published template tag. |
| `REGION` | no | `us-east-1` | |
| `STACK_NAME` | no | `lrdev-baseinfra` | |
| `TEMPLATE_BUCKET` | no | `cardinal-cfn-${REGION}` | |
| `ENVIRONMENT_NAME` | no | `lrdev` | |

Outputs: `ClusterArn`, `ClusterName`.

## teardown-cardinal.sh (current)

Tears down a per-stack install: deletes the five `cardinal-*` stacks in
reverse-dependency order, then wipes the retained fixed-name survivors (the
`cardinal-license` / `cardinal-admin-key` / `cardinal-db-master` secrets and the
cooked / otel-raw buckets) so a fresh install can re-create them. Leaves the VPC
and ECS cluster intact. Idempotent; gated behind `CONFIRM=DELETE`.

```sh
REGION=us-east-1 dev-scripts/teardown-cardinal.sh                 # plan only
REGION=us-east-1 CONFIRM=DELETE dev-scripts/teardown-cardinal.sh  # execute
```

Env: `KEEP_SECRETS` / `KEEP_BUCKETS` / `DELETE_SNAPSHOTS`, stack-name overrides,
`DEPLOYER_ROLE_ARN`. See `--help` and
[`dev-environment.md`](../docs/operations/dev-environment.md).

## sweep-stranded-resources.sh (current)

When a stack delete fails part-way and leaves `DELETE_FAILED` /
`DELETE_SKIPPED` resources, this mops them up: it registers a one-shot
privileged ECS task (no CFN stack) that discovers stranded resources from the
stack and deletes them by type (IAM roles, security groups, secrets, SSM
parameters, S3 buckets), then deregisters itself. Runs under a caller-supplied
`--task-role-arn` and self-skips its own ENI security group.

## Legacy (do not use for the per-stack model)

`deploy-cardinal-infrastructure.sh`, `deploy-cardinal-lakerunner.sh`,
`run-cleanup.sh`, `cleanup-lakerunner.sh`, and `teardown-lakerunner.sh` target
the retired **monolithic** `cardinal-infrastructure` + `cardinal-lakerunner`
stacks and do **not** match the current per-stack model. For the per-stack
teardown, use `teardown-cardinal.sh` (above).
