# Deploying with a CloudFormation service role

By default `aws cloudformation update-stack` runs with the calling identity's
permissions for both the apply and the rollback path. When an IAM-touching
update fails partway through, CloudFormation needs the same IAM write
permissions to roll back; if the operator lacks them, the stack lands in
`UPDATE_ROLLBACK_FAILED` and only an admin (or `--resources-to-skip`) can
recover it.

The `cardinal-deployer-role.yaml` template breaks that coupling. It creates a
service role that CloudFormation assumes for all stack operations, scoped to
exactly the AWS APIs the lakerunner templates touch. Operators only need
`cloudformation:*` and `iam:PassRole` on the deployer role itself.

## Quick start

```sh
# 1. Deploy the role once per account (use any region you like — the role is
#    global, but the stack must live somewhere).
aws cloudformation create-stack \
  --stack-name cardinal-cfn-deployer \
  --region us-east-1 \
  --template-url https://cardinal-cfn-us-east-1.s3.us-east-1.amazonaws.com/lakerunner/<version>/cardinal-deployer-role.yaml \
  --capabilities CAPABILITY_NAMED_IAM

# 2. Grab the ARN.
ROLE_ARN=$(aws cloudformation describe-stacks \
  --stack-name cardinal-cfn-deployer --region us-east-1 \
  --query 'Stacks[0].Outputs[?OutputKey==`DeployerRoleArn`].OutputValue' \
  --output text)

# 3. Use it for every cardinal-lakerunner update.
aws cloudformation update-stack \
  --stack-name cardinal-lakerunner \
  --region us-east-1 \
  --role-arn "$ROLE_ARN" \
  --template-url https://cardinal-cfn-us-east-1.s3.us-east-1.amazonaws.com/lakerunner/<version>/cardinal-lakerunner.yaml \
  --use-previous-parameters \
  --capabilities CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND
```

For initial creation use the same `--role-arn` flag with `create-stack`.

## Why this fixes the wedge

Without `--role-arn`:

1. Operator runs `update-stack` with their SSO identity.
2. CFN tries to update an inline IAM role policy. Operator lacks
   `iam:PutRolePolicy`. Update fails.
3. CFN starts rolling back. Rollback also calls `iam:PutRolePolicy` to put the
   old policy back. Same failure.
4. Stack: `UPDATE_ROLLBACK_FAILED`. Recovery requires an identity that does
   have `iam:PutRolePolicy`, or `--resources-to-skip` (which leaves the role
   on whatever inline policy is currently attached).

With `--role-arn`:

1. Operator runs `update-stack`. CFN assumes the deployer role, which has
   `iam:PutRolePolicy`.
2. Apply or rollback both succeed regardless of operator permissions.
3. Worst case is `UPDATE_FAILED` (recoverable by another `update-stack`),
   never `UPDATE_ROLLBACK_FAILED`.

## Hardening: `--disable-rollback`

For updates that touch IAM, consider:

```sh
aws cloudformation update-stack ... --disable-rollback
```

A failed apply leaves the stack in `UPDATE_FAILED` rather than triggering an
automatic rollback. From there `update-stack` can fix forward. This is mainly
useful when *both* the apply and the rollback would fail (e.g. a code bug in
the new IAM policy that CFN can't put either way) — `--disable-rollback`
keeps the stack out of the harder-to-recover `UPDATE_ROLLBACK_FAILED` state.

## Customer-managed alternative

Customers who already manage IAM via Terraform / Pulumi / ClickOps don't have
to use this template. The policy in
[`src/cardinal_cfn/cardinal_deployer.py`](../../src/cardinal_cfn/cardinal_deployer.py)
is the source of truth for which actions the deployer role needs — copy it
into whatever IAM tooling owns roles in the customer account, attach the
result to a role that trusts `cloudformation.amazonaws.com`, and pass that
role's ARN to `update-stack --role-arn`. The template is the convenience
path; the policy contents are the contract.

When you change the lakerunner templates in a way that introduces a new AWS
resource type, update `_POLICY_STATEMENTS` in `cardinal_deployer.py` so
customers running the template get the new permission. The drift test in
`tests/templates/test_cardinal_deployer.py` will fail if the template adds a
resource type that isn't represented in the policy.

## Tearing down

The lakerunner stack owns no `Retain` or `Snapshot` resources, so
`delete-stack cardinal-lakerunner` cleans up everything it created
(including all stack-created SGs and IAM roles). The data layer (RDS,
S3 ingest, secrets, SSM, SQS) lives in the separate
`cardinal-infrastructure` stack and survives stack delete by design
via `Retain` / `Snapshot` policies. See
[`dev-environment.md`](dev-environment.md) for the teardown procedure.
