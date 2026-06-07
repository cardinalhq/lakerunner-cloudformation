# Dev environment: reproduce, validate an upgrade, burn down

How we (and future coworkers) stand up a throwaway Cardinal Lakerunner install
in the test account to validate a release/upgrade, confirm self-telemetry flows
end-to-end, then tear it down cleanly. The production path is
[`production-deploy.md`](production-deploy.md); this doc adds the BYO-simulation
scaffolding, concrete env, validation checks, and teardown.

> Single-install assumption: the stacks use fixed `cardinal-*` resource names,
> so only one install per account/region. A "fresh" install requires the
> teardown wipe below first.

## 0. Scaffolding (simulates customer BYO VPC + cluster)

Deploy once and keep across re-installs (`lrdev-*` are the simulated
customer-supplied VPC and ECS cluster):

```sh
REGION=us-east-1 VERSION=vX.Y.Z dev-scripts/deploy-lrdev-vpc.sh
REGION=us-east-1 VERSION=vX.Y.Z dev-scripts/deploy-lrdev-baseinfra.sh
```

Collect their outputs for the env block below: `lrdev-vpc` → `VpcId`,
`PublicSubnetsCsv`, `PrivateSubnetsCsv`; `lrdev-baseinfra` → `ClusterArn`,
`ClusterName`.

## 1. Pick a version

Set `STACK_VERSION` to a published release (`vX.Y.Z`) to validate that release,
or publish your branch's templates+drivers to the test bucket
(`s3://cardinal-cfn-test-<account>-<region>/lakerunner/<label>/`) and point
`TEMPLATE_BASE_URL`/`STACK_VERSION` there for pre-release iteration.

## 2. Shared env

```sh
export REGION=us-east-1
export STACK_VERSION=vX.Y.Z
export VPC_ID=<lrdev-vpc VpcId>
export PRIVATE_SUBNETS=<lrdev-vpc PrivateSubnetsCsv>
export PUBLIC_SUBNETS=<lrdev-vpc PublicSubnetsCsv>
export CLUSTER_ARN=<lrdev-baseinfra ClusterArn>
export CLUSTER_NAME=<lrdev-baseinfra ClusterName>
export ORGANIZATION_ID=12340000-0000-4000-8000-000000000000   # any UUID; must match steps 4 & 5
```

## 3. Install (ordered) — public Maestro, private collector

Full env contract per driver is in [`production-deploy.md`](production-deploy.md);
this is the dev preference set. Run each, waiting for `CREATE/UPDATE_COMPLETE`
before the next (later drivers read earlier stacks' outputs).

```sh
# 1. infra-base — app ALB PUBLIC
STACK_NAME=cardinal-lakerunner-infra-base \
  LICENSE_DATA_FILE=./license.txt \
  ALB_SCHEME=internet-facing ALB_ALLOWED_CIDR1=0.0.0.0/0 \
  scripts/deploy-lakerunner-infra-base.sh

# 2. infra-rds
STACK_NAME=cardinal-lakerunner-infra-rds \
  INFRA_BASE_STACK=cardinal-lakerunner-infra-base \
  scripts/deploy-lakerunner-infra-rds.sh

# 3. satellite-infra-base — LAKERUNNER_PRINCIPAL = infra-base ProcessRoleArn
LP=$(aws cloudformation describe-stacks --region "$REGION" \
  --stack-name cardinal-lakerunner-infra-base \
  --query "Stacks[0].Outputs[?OutputKey=='ProcessRoleArn'].OutputValue|[0]" --output text)
STACK_NAME=cardinal-satellite-infra-base LAKERUNNER_PRINCIPAL="$LP" \
  scripts/deploy-satellite-infra-base.sh

# 4. satellite-services — collector PRIVATE (internal ALB, private subnets)
STACK_NAME=cardinal-satellite-services \
  SATELLITE_INFRA_BASE_STACK=cardinal-satellite-infra-base \
  ORGANIZATION_ID="$ORGANIZATION_ID" ECS_CLUSTER_ARN="$CLUSTER_ARN" \
  ALB_SCHEME=internal ALB_SUBNETS="$PRIVATE_SUBNETS" TASK_SUBNETS="$PRIVATE_SUBNETS" \
  scripts/deploy-satellite-services.sh

# 5. lakerunner-services — app ALB PUBLIC; self-telemetry auto-wired to the collector
STACK_NAME=cardinal-lakerunner-services \
  INFRA_BASE_STACK=cardinal-lakerunner-infra-base \
  INFRA_RDS_STACK=cardinal-lakerunner-infra-rds \
  SATELLITE_INFRA_BASE_STACK=cardinal-satellite-infra-base \
  ORGANIZATION_ID="$ORGANIZATION_ID" \
  ALB_SCHEME=internet-facing \
  DEX_ADMIN_EMAIL=you@example.com OIDC_SUPERADMIN_EMAILS=you@example.com \
  DEX_ADMIN_PASSWORD_HASH='<bcrypt hash>' \
  scripts/deploy-lakerunner-services.sh
```

`DEX_ADMIN_PASSWORD_HASH` is required and is re-applied on every services deploy
(it sets the Maestro admin password). Generate one with
`htpasswd -bnBC 10 "" '<password>' | cut -d: -f2`.

## 4. Validate (self-telemetry end-to-end)

```sh
ACCT=$(aws sts get-caller-identity --query Account --output text)
# Raw self-telemetry landing (logs/metrics/traces under the org):
aws s3 ls s3://cardinal-otel-raw-$ACCT-$REGION/otel-raw/$ORGANIZATION_ID/ --recursive | tail
# Ingest pipeline cooking (expect failed=0, logs/metrics/traces processed):
aws logs tail /cardinal/pubsub-sqs --region "$REGION" --since 5m | grep "Pubsub processing stats" | tail -1
# Cooked output present:
aws s3 ls s3://cardinal-cooked-$ACCT-$REGION/ --recursive | wc -l
# Public Maestro reachable, then log in and confirm a query returns lakerunner-* telemetry:
ALB=$(aws cloudformation describe-stacks --region "$REGION" --stack-name cardinal-lakerunner-services \
  --query "Stacks[0].Outputs[?OutputKey=='AlbDnsName'].OutputValue|[0]" --output text)
curl -ksS -o /dev/null -w '%{http_code}\n' "https://$ALB/"          # 200
curl -ksS -o /dev/null -w '%{http_code}\n' "https://$ALB/dex/healthz" # 200
```

Goal met when the raw bucket has logs/metrics/traces under your org, pubsub
shows `... 0 failed`, the cooked bucket is non-empty, and logging into Maestro
(the public ALB) shows logs/traces/metrics from the `lakerunner-*` services.

> If `pubsub-sqs` logs `organization does not exist` and cooking never starts on
> a brand-new install, that is the maestro↔admin-api provisioning cold-start
> race (fixed in maestro v1.53.1 / conductor #998). On a pre-fix maestro, force
> a new maestro deployment to re-run provisioning:
> `aws ecs update-service --cluster <cluster> --service <MaestroService> --force-new-deployment`.

## 5. Burn it down

Delete the five stacks in **reverse** dependency order, then remove the
retained/fixed-name survivors that would block a future re-create. (The legacy
`dev-scripts/cleanup-lakerunner.sh` / `teardown-lakerunner.sh` target the old
monolithic stack names and do **not** fit the per-stack model — use this manual
sequence; a per-stack teardown script is a future follow-up.)

```sh
for s in cardinal-lakerunner-services cardinal-satellite-services \
         cardinal-satellite-infra-base cardinal-lakerunner-infra-rds \
         cardinal-lakerunner-infra-base; do
  aws cloudformation delete-stack --stack-name "$s" --region "$REGION"
  aws cloudformation wait stack-delete-complete --stack-name "$s" --region "$REGION"
done

ACCT=$(aws sts get-caller-identity --query Account --output text)
# A non-empty bucket blocks its stack delete — empty first if a delete failed:
#   aws s3 rm s3://cardinal-otel-raw-$ACCT-$REGION --recursive
#   aws s3 rm s3://cardinal-cooked-$ACCT-$REGION  --recursive  (then retry the delete)

# Wipe survivors (fixed cardinal-* names) so a fresh install can re-create them:
for sec in cardinal-license cardinal-admin-key cardinal-db-master; do
  aws secretsmanager delete-secret --secret-id "$sec" --force-delete-without-recovery --region "$REGION"
done
for b in cardinal-cooked-$ACCT-$REGION cardinal-otel-raw-$ACCT-$REGION; do
  aws s3 rb s3://$b --force
done
# Optional (cost only; no re-create collision): delete the RDS final snapshot.
```

`lrdev-vpc` and `lrdev-baseinfra` are intentionally left standing (BYO
scaffolding) for the next iteration.
