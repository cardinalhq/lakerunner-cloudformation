# Jenkins chained deploy (5-stack satellite-ingest topology)

The satellite-ingest topology splits the install into five CloudFormation
stacks, each deployed by its own Jenkins job. Every stack's Outputs were named
to match the next stack's Parameter names, so the chain is mostly automatic: a
downstream job pulls its upstream stack's Outputs and any Output whose key
equals a target parameter name supplies that parameter.

All six scripts are self-contained POSIX sh + AWS CLI v2 + jq (no Python at
runtime). They create the stack if missing, otherwise update it in place, via a
change set.

## Per-job model

Each Jenkins job runs one thin wrapper. The wrapper composes the published
template URL from `--template-base-url` (default
`https://cardinal-cfn.s3.us-east-2.amazonaws.com/lakerunner`) and `--version`
(e.g. `v0.0.70`), declares its upstream stack(s), and exposes only the add-in
parameters relevant to that stack. Everything else is forwarded to the generic
driver `scripts/deploy-stack.sh`.

### deploy-stack.sh resolution precedence

For each parameter in the target template (from `get-template-summary`):

1. `--param Key=Value` explicit override (highest precedence)
1. `--map TargetParam=SourceOutputKey` value of the named upstream Output
1. matching `--from-stack` Output (Output key == parameter name)
1. on UPDATE only: `UsePreviousValue: true` (carry the current stack value)
1. the template's `Default`
1. otherwise FAIL, listing the unresolved required parameters

## Deploy order

```
lakerunner-infra-base
    -> lakerunner-infra-rds
    -> [ satellite-infra-base -> satellite-services ]   (one pair per ingest account/region)
    -> lakerunner-services
```

`lakerunner-services` depends on both `lakerunner-infra-rds` and at least one
`satellite-infra-base` (for the computed `PubsubSqsEnv`), so it runs last.

## Two non-automatic mappings

- `satellite-infra-base` parameter `LakerunnerPrincipal` is mapped from the
  `lakerunner-infra-base` Output `ProcessRoleArn` (the satellite trusts the
  lakerunner process role to assume into the satellite access role). The
  wrapper adds `--map LakerunnerPrincipal=ProcessRoleArn`.
- `lakerunner-services` parameter `PubsubSqsEnv` is **computed**, not a single
  Output. The wrapper reads three Outputs from the `satellite-infra-base` stack
  and assembles:

  ```
  SQS_QUEUE_URL=<RawQueueUrl>;SQS_REGION=<Region>;SQS_ROLE_ARN=<LakerunnerAccessRoleArn>
  ```

  then passes it via `--param PubsubSqsEnv=...`.

## Example invocations

### Job 1: lakerunner-infra-base

```sh
scripts/deploy-lakerunner-infra-base.sh \
    --stack-name cardinal-lakerunner-infra-base \
    --region us-east-1 --version v0.0.70 \
    --vpc-id vpc-0abc \
    --cluster-arn arn:aws:ecs:us-east-1:111122223333:cluster/cardinal \
    --license-data-file ./license.json \
    --alb-allowed-cidr1 10.0.0.0/8
```

### Job 2: lakerunner-infra-rds

```sh
scripts/deploy-lakerunner-infra-rds.sh \
    --stack-name cardinal-lakerunner-infra-rds \
    --region us-east-1 --version v0.0.70 \
    --infra-base-stack cardinal-lakerunner-infra-base \
    --vpc-id vpc-0abc \
    --private-subnets-csv subnet-1,subnet-2
```

### Job 3: satellite-infra-base (per ingest account/region)

```sh
scripts/deploy-satellite-infra-base.sh \
    --stack-name cardinal-satellite-infra-base \
    --region us-east-1 --version v0.0.70 \
    --infra-base-stack cardinal-lakerunner-infra-base \
    --external-id myExternalId
```

### Job 4: satellite-services

`OtelReplicas` defaults to `1`; the collector config must change before scaling
past one replica.

```sh
scripts/deploy-satellite-services.sh \
    --stack-name cardinal-satellite-services \
    --region us-east-1 --version v0.0.70 \
    --satellite-infra-base-stack cardinal-satellite-infra-base \
    --infra-base-stack cardinal-lakerunner-infra-base \
    --vpc-id vpc-0abc \
    --alb-subnets-csv subnet-a,subnet-b \
    --task-subnets-csv subnet-1,subnet-2 \
    --ecs-cluster-arn arn:aws:ecs:us-east-1:111122223333:cluster/cardinal \
    --ingest-source-cidr 10.0.0.0/8
```

### Job 5: lakerunner-services

`OtelReplicas` defaults to `0` here: the same-account satellite collector does
ingest, so the lakerunner-tier collector is off by default.

```sh
scripts/deploy-lakerunner-services.sh \
    --stack-name cardinal-lakerunner-services \
    --region us-east-1 --version v0.0.70 \
    --infra-base-stack cardinal-lakerunner-infra-base \
    --infra-rds-stack cardinal-lakerunner-infra-rds \
    --satellite-infra-base-stack cardinal-satellite-infra-base \
    --vpc-id vpc-0abc \
    --cluster-arn arn:aws:ecs:us-east-1:111122223333:cluster/cardinal \
    --cluster-name cardinal \
    --private-subnets subnet-1,subnet-2 \
    --dex-admin-password-hash '$2y$10$...'
```

## Notes

- `--no-execute` (any wrapper) creates and describes the change set, then
  stops, leaving it in place for manual review.
- `--deployer-role-arn ARN` is forwarded to `create-change-set`; CloudFormation
  reuses that role during execution.
- Re-running a wrapper is idempotent: an existing stack is updated, a no-op
  change set is detected and discarded.
