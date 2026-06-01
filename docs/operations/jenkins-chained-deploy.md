# Jenkins chained deploy (5-stack satellite-ingest topology)

The satellite-ingest topology splits the install into five CloudFormation
stacks, each deployed by its own Jenkins job. Every stack's Outputs were named
to match the next stack's Parameter names, so the chain is mostly automatic: a
downstream job pulls its upstream stack's Outputs and any Output whose key
equals a target parameter name supplies that parameter.

All six scripts are self-contained POSIX sh + AWS CLI v2 + jq (no Python at
runtime). They create the stack if missing, otherwise update it in place, via a
change set.

## Pure environment-variable interface (no flags)

Every script is driven entirely by environment variables in
`SCREAMING_SNAKE_CASE`; none of them take command-line flags. AWS credentials
are the only implicit input (from the usual AWS CLI sources); everything else is
explicit.

`REGION` is **required in every script** and is never defaulted — it does not
fall back to `AWS_DEFAULT_REGION` or the profile region. A wrong region is hard
for customers to undo, so it must be set explicitly or the script fails.

Each script collects all missing required variables up front, prints its usage
(with a `Required:` section above an `Optional:` section) to stderr, prints a
`missing required: VAR1, VAR2` line, and exits `2` before deploying anything.

## Per-job model

Each Jenkins job runs one thin wrapper. The wrapper reads its friendly env vars,
composes the published template URL from `TEMPLATE_BASE_URL` (default
`https://cardinal-cfn.s3.us-east-2.amazonaws.com/lakerunner`) and `VERSION`
(e.g. `v0.0.70`), sets `TEMPLATE_URL`, `FROM_STACKS`, `PARAMS`, `FILE_PARAMS`,
and `MAPS`, then invokes the generic driver `scripts/deploy-stack.sh` with the
environment inherited. Most wrappers `exec` the driver; `lakerunner-services`
runs it as a child so it can clean up an auto-generated cert temp dir on exit.

### deploy-stack.sh environment

| Variable | Req/Opt | Meaning |
|---|---|---|
| `STACK_NAME` | required | Stack to create or upgrade. |
| `TEMPLATE_URL` | required | S3 URL of the generated template. |
| `REGION` | required | AWS region (never defaulted). |
| `FROM_STACKS` | optional | Space-separated upstream stack names; an Output whose key equals a target parameter name supplies that parameter. |
| `PARAMS` | optional | Newline-separated `Key=Value` overrides (highest precedence). Newline- (not semicolon-) separated so values like `PubsubSqsEnv` that contain semicolons are safe. |
| `FILE_PARAMS` | optional | Newline-separated `ParamName=/path/to/file` entries. Each file's full content becomes that parameter's value, read via `jq --rawfile` so multi-line / PEM material is JSON-escaped correctly. Same explicit-override tier as `PARAMS`; if both set the same parameter, `PARAMS` wins. |
| `MAPS` | optional | Newline-separated `TargetParam=SourceOutputKey` entries. |
| `DEPLOYER_ROLE_ARN` | optional | Forwarded via `--role-arn` to `create-change-set`. |
| `NO_EXECUTE` | optional | Non-empty: create and describe the change set, then stop. |

### deploy-stack.sh resolution precedence

For each parameter in the target template (from `get-template-summary`):

1. `PARAMS Key=Value` explicit override (highest precedence)
1. `FILE_PARAMS ParamName=/path` file content (same tier as `PARAMS`; `PARAMS` wins on a clash)
1. `MAPS TargetParam=SourceOutputKey` value of the named upstream Output
1. matching `FROM_STACKS` Output (Output key == parameter name)
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
  wrapper sets `MAPS="LakerunnerPrincipal=ProcessRoleArn"`.
- `lakerunner-services` parameter `PubsubSqsEnv` is **computed**, not a single
  Output. The wrapper reads three Outputs from the `satellite-infra-base` stack
  and assembles:

  ```
  SQS_QUEUE_URL=<RawQueueUrl>;SQS_REGION=<Region>;SQS_ROLE_ARN=<LakerunnerAccessRoleArn>
  ```

  then passes it as a `PARAMS` line.

## Job 1: lakerunner-infra-base

`scripts/deploy-lakerunner-infra-base.sh` — head of the chain, no upstream.

| Variable | Req/Opt | Default |
|---|---|---|
| `STACK_NAME` | required | — |
| `REGION` | required | — |
| `VERSION` | required | — |
| `VPC_ID` | required | — |
| `CLUSTER_ARN` | required | — |
| `LICENSE_DATA_FILE` | required | path to license JSON, read into the license secret |
| `ALB_SCHEME` | optional | template: `internal` |
| `ALB_ALLOWED_CIDR1` | optional | template: `10.0.0.0/8` |
| `ALB_ALLOWED_CIDR2` | optional | template: `172.16.0.0/12` |
| `ALB_ALLOWED_CIDR3` | optional | template: `192.168.0.0/16` |
| `ORGANIZATION_ID` | optional | template default |
| `INITIAL_INGEST_API_KEY` | optional | template: empty |
| `COOKED_BUCKET_NAME` | optional | template: generated |
| `LICENSE_SECRET_NAME` | optional | template: `cardinal-license` |
| `ADMIN_KEY_SECRET_NAME` | optional | template: `cardinal-admin-key` |
| `API_KEYS_PARAM_NAME` | optional | template: `/cardinal/api-keys` |
| `STORAGE_PROFILES_PARAM_NAME` | optional | template: `/cardinal/storage-profiles` |
| `TEMPLATE_BASE_URL` | optional | `https://cardinal-cfn.s3.us-east-2.amazonaws.com/lakerunner` |
| `DEPLOYER_ROLE_ARN` | optional | unset |
| `NO_EXECUTE` | optional | unset |

```sh
STACK_NAME=cardinal-lakerunner-infra-base \
REGION=us-east-1 \
VERSION=v0.0.70 \
VPC_ID=vpc-0abc \
CLUSTER_ARN=arn:aws:ecs:us-east-1:111122223333:cluster/cardinal \
LICENSE_DATA_FILE=./license.json \
ALB_ALLOWED_CIDR1=10.0.0.0/8 \
./scripts/deploy-lakerunner-infra-base.sh
```

## Job 2: lakerunner-infra-rds

`scripts/deploy-lakerunner-infra-rds.sh` — pulls the five tier security-group
ids from the infra-base stack via `FROM_STACKS`.

| Variable | Req/Opt | Default |
|---|---|---|
| `STACK_NAME` | required | — |
| `REGION` | required | — |
| `VERSION` | required | — |
| `INFRA_BASE_STACK` | required | upstream lakerunner-infra-base stack name |
| `VPC_ID` | required | — |
| `PRIVATE_SUBNETS` | required | comma-separated private subnet ids |
| `DB_ENGINE_VERSION` | optional | template: `18.4` |
| `DB_INSTANCE_CLASS` | optional | template: `db.r7g.large` |
| `DB_ALLOCATED_STORAGE` | optional | template: `100` |
| `TEMPLATE_BASE_URL` | optional | `https://cardinal-cfn.s3.us-east-2.amazonaws.com/lakerunner` |
| `DEPLOYER_ROLE_ARN` | optional | unset |
| `NO_EXECUTE` | optional | unset |

```sh
STACK_NAME=cardinal-lakerunner-infra-rds \
REGION=us-east-1 \
VERSION=v0.0.70 \
INFRA_BASE_STACK=cardinal-lakerunner-infra-base \
VPC_ID=vpc-0abc \
PRIVATE_SUBNETS=subnet-1,subnet-2 \
./scripts/deploy-lakerunner-infra-rds.sh
```

## Job 3: satellite-infra-base (per ingest account/region)

`scripts/deploy-satellite-infra-base.sh` — pulls infra-base Outputs and maps
`LakerunnerPrincipal=ProcessRoleArn`.

| Variable | Req/Opt | Default |
|---|---|---|
| `STACK_NAME` | required | — |
| `REGION` | required | — |
| `VERSION` | required | — |
| `INFRA_BASE_STACK` | required | upstream lakerunner-infra-base stack name |
| `EXTERNAL_ID` | optional | template: empty |
| `RAW_BUCKET_NAME` | optional | template: generated |
| `RAW_BUCKET_LIFECYCLE_DAYS` | optional | template: `7` |
| `TEMPLATE_BASE_URL` | optional | `https://cardinal-cfn.s3.us-east-2.amazonaws.com/lakerunner` |
| `DEPLOYER_ROLE_ARN` | optional | unset |
| `NO_EXECUTE` | optional | unset |

```sh
STACK_NAME=cardinal-satellite-infra-base \
REGION=us-east-1 \
VERSION=v0.0.70 \
INFRA_BASE_STACK=cardinal-lakerunner-infra-base \
EXTERNAL_ID=myExternalId \
./scripts/deploy-satellite-infra-base.sh
```

## Job 4: satellite-services

`scripts/deploy-satellite-services.sh` — `OTEL_REPLICAS` defaults to `1`; the
collector config must change before scaling past one replica.

| Variable | Req/Opt | Default |
|---|---|---|
| `STACK_NAME` | required | — |
| `REGION` | required | — |
| `VERSION` | required | — |
| `SATELLITE_INFRA_BASE_STACK` | required | upstream satellite-infra-base (`RawBucketName`) |
| `INFRA_BASE_STACK` | required | upstream lakerunner-infra-base (`LicenseSecretArn`) |
| `VPC_ID` | required | — |
| `ALB_SUBNETS` | required | comma-separated subnets for the collector ALB |
| `TASK_SUBNETS` | required | comma-separated subnets for the collector tasks |
| `ECS_CLUSTER_ARN` | required | ECS cluster for the collector |
| `ALB_SCHEME` | optional | `internal` |
| `INGEST_SOURCE_CIDR` | optional | template: `10.0.0.0/8` |
| `OTEL_REPLICAS` | optional | `1` (>1 needs a collector config change first) |
| `TEMPLATE_BASE_URL` | optional | `https://cardinal-cfn.s3.us-east-2.amazonaws.com/lakerunner` |
| `DEPLOYER_ROLE_ARN` | optional | unset |
| `NO_EXECUTE` | optional | unset |

```sh
STACK_NAME=cardinal-satellite-services \
REGION=us-east-1 \
VERSION=v0.0.70 \
SATELLITE_INFRA_BASE_STACK=cardinal-satellite-infra-base \
INFRA_BASE_STACK=cardinal-lakerunner-infra-base \
VPC_ID=vpc-0abc \
ALB_SUBNETS=subnet-a,subnet-b \
TASK_SUBNETS=subnet-1,subnet-2 \
ECS_CLUSTER_ARN=arn:aws:ecs:us-east-1:111122223333:cluster/cardinal \
INGEST_SOURCE_CIDR=10.0.0.0/8 \
./scripts/deploy-satellite-services.sh
```

## Job 5: lakerunner-services

`scripts/deploy-lakerunner-services.sh` — pulls infra-base + infra-rds Outputs,
computes `PubsubSqsEnv` from the satellite stack, and forwards `TemplateBaseUrl`
so nested children load from the matching version prefix. `OTEL_REPLICAS`
defaults to `0` here: the same-account satellite collector does ingest, so the
lakerunner-tier collector is off by default.

Certificate: if `CERTIFICATE_ARN` is set, it is passed through unchanged (a
stable ARN, no churn). If it is empty and no PEM files are supplied, the wrapper
auto-generates a self-signed internal cert **only on first create** and passes
it via `FILE_PARAMS` (the `cert.yaml` child builds an `AWS::IAM::ServerCertificate`
from it). Browsers will warn on a self-signed cert — fine for internal/test. On
a re-run (the stack already exists, an UPDATE), the wrapper generates nothing and
passes no cert params, so `deploy-stack.sh` resolves `CertificateBody`/
`CertificatePrivateKey` to `UsePreviousValue` and the existing IAM
ServerCertificate (and the ALB listener) is left untouched. Nothing is committed
to the repo; the PEMs are generated per run into a temp dir that is removed on
exit. Set `CERTIFICATE_ARN` to use a real cert.

| Variable | Req/Opt | Default |
|---|---|---|
| `STACK_NAME` | required | — |
| `REGION` | required | — |
| `VERSION` | required | — |
| `INFRA_BASE_STACK` | required | upstream lakerunner-infra-base |
| `INFRA_RDS_STACK` | required | upstream lakerunner-infra-rds |
| `SATELLITE_INFRA_BASE_STACK` | required | source of `RawQueueUrl`/`Region`/`LakerunnerAccessRoleArn` |
| `CLUSTER_ARN` | required | ECS cluster ARN |
| `CLUSTER_NAME` | required | ECS cluster name (no upstream output for it) |
| `VPC_ID` | required | — |
| `PRIVATE_SUBNETS` | required | comma-separated private subnet ids |
| `CERTIFICATE_ARN` | optional | ACM/IAM cert ARN for the Maestro HTTPS listener. If unset, the wrapper auto-generates a self-signed internal cert **only on first create** (re-runs keep the existing cert — no churn). Set it to use a real cert. |
| `CERTIFICATE_BODY_FILE` | optional | PEM cert body path (overrides auto-generation; passed via `FILE_PARAMS`) |
| `CERTIFICATE_PRIVATE_KEY_FILE` | optional | PEM private key path (overrides auto-generation; passed via `FILE_PARAMS`) |
| `CERTIFICATE_CHAIN_FILE` | optional | PEM chain path (passed via `FILE_PARAMS`) |
| `DEX_ADMIN_EMAIL` | optional | template: `admin@cardinal.local` |
| `DEX_ADMIN_PASSWORD_HASH` | optional | template: empty — **needed for Maestro UI login** |
| `DEX_CLIENT_ID` | optional | template: `maestro-ui` |
| `OIDC_SUPERADMIN_EMAILS` | optional | template: `admin@cardinal.local` |
| `SERVICE_NAMESPACE_NAME` | optional | template: `cardinal.local` |
| `PUBLIC_SUBNETS` | optional | template: empty |
| `OTEL_REPLICAS` | optional | `0` |
| `LAKERUNNER_IMAGE` `MAESTRO_IMAGE` `OTEL_IMAGE` `DEX_IMAGE` `DEX_INIT_IMAGE` `DB_INIT_IMAGE` | optional | template defaults |
| `TEMPLATE_BASE_URL` | optional | `https://cardinal-cfn.s3.us-east-2.amazonaws.com/lakerunner` (also forwarded as `TemplateBaseUrl`) |
| `DEPLOYER_ROLE_ARN` | optional | unset |
| `NO_EXECUTE` | optional | unset |

```sh
STACK_NAME=cardinal-lakerunner-services \
REGION=us-east-1 \
VERSION=v0.0.70 \
INFRA_BASE_STACK=cardinal-lakerunner-infra-base \
INFRA_RDS_STACK=cardinal-lakerunner-infra-rds \
SATELLITE_INFRA_BASE_STACK=cardinal-satellite-infra-base \
CLUSTER_ARN=arn:aws:ecs:us-east-1:111122223333:cluster/cardinal \
CLUSTER_NAME=cardinal \
VPC_ID=vpc-0abc \
PRIVATE_SUBNETS=subnet-1,subnet-2 \
DEX_ADMIN_PASSWORD_HASH='$2y$10$...' \
./scripts/deploy-lakerunner-services.sh
```

## Notes

- `NO_EXECUTE` (any wrapper, set to any non-empty value) creates and describes
  the change set, then stops, leaving it in place for manual review.
- `DEPLOYER_ROLE_ARN` is forwarded to `create-change-set`; CloudFormation reuses
  that role during execution.
- Re-running a wrapper is idempotent: an existing stack is updated, a no-op
  change set is detected and discarded.
- A missing required variable fails fast: the script prints its usage and a
  `missing required: ...` line to stderr and exits `2` before any AWS call.
