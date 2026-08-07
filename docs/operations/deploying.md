# Deploying with a CloudFormation service role

By default `aws cloudformation update-stack` runs with the calling identity's
permissions for both the apply and the rollback path. When an IAM-touching
update fails partway through, CloudFormation needs the same IAM write
permissions to roll back; if the operator lacks them, the stack lands in
`UPDATE_ROLLBACK_FAILED` and only an admin (or `--resources-to-skip`) can
recover it.

A CloudFormation **service role** breaks that coupling: CloudFormation assumes
it for every stack operation, so apply and rollback both run with the same
permissions regardless of who kicked off the deploy. Operators then only need
`cloudformation:*` and `iam:PassRole` on that role.

The role is customer-supplied. This repo no longer ships a template for it —
the `cardinal-deployer-role.yaml` stack was removed when infrastructure
provisioning moved out of CloudFormation, and there is no in-repo policy
document to copy. Build the role in whatever IAM tooling owns roles in the
account, trusting `cloudformation.amazonaws.com`.

## Using it

Every deploy driver takes the role ARN as `DEPLOYER_ROLE_ARN`:

```sh
DEPLOYER_ROLE_ARN=arn:aws:iam::<acct>:role/<role> \
STACK_NAME=cardinal-lakerunner-services ... \
  scripts/deploy-lakerunner-services.sh
```

The driver passes it to `create-change-set`; CloudFormation reuses the role for
`execute-change-set`, so it covers both create and update. It is the only
CloudFormation call that accepts `--role-arn` — see the `cfntool()` wrapper in
`scripts-src/parts/base.sh`.

## Scoping the policy

The role needs write access to every resource type the stacks create. The
authoritative list is the templates themselves; as of v1.7.x that is:

- CloudFormation (nested stacks), ECS (cluster-scoped services, task
  definitions, tagging), ELBv2, EC2 security groups, IAM roles and policies,
  Logs, S3, SQS, Secrets Manager, RDS, Service Discovery / Route 53,
  Application Auto Scaling, and — for the cwmetrics add-on — Firehose and
  CloudWatch metric streams.

When a template starts creating a new resource type, the role needs the
matching permissions before the next deploy, or the stack fails mid-update.

**Tag-conditioned roles:** the drivers set `Project` / `Application` /
`ManagedBy` as stack tags and the templates tag each resource (see
`src/cardinal_cfn/naming.py`). A role that gates writes on `aws:ResourceTag`
must still be able to modify *untagged* resources for the one upgrade that
first applies the tags — the resources cannot become compliant before that
update runs. Likewise a role constraining `aws:TagKeys` must permit every key
in the common set.

## Why this fixes the wedge

Without a service role:

1. Operator runs the deploy with their SSO identity.
2. CFN tries to update an inline IAM role policy. Operator lacks
   `iam:PutRolePolicy`. Update fails.
3. CFN starts rolling back. Rollback also calls `iam:PutRolePolicy` to put the
   old policy back. Same failure.
4. Stack: `UPDATE_ROLLBACK_FAILED`. Recovery requires an identity that does
   have `iam:PutRolePolicy`, or `--resources-to-skip` (which leaves the role
   on whatever inline policy is currently attached).

With one:

1. Operator runs the deploy. CFN assumes the deployer role, which has
   `iam:PutRolePolicy`.
2. Apply or rollback both succeed regardless of operator permissions.
3. Worst case is `UPDATE_FAILED` (recoverable by another deploy), never
   `UPDATE_ROLLBACK_FAILED`.

## Hardening: `--disable-rollback`

For updates that touch IAM, consider running the underlying
`aws cloudformation` call with `--disable-rollback`. A failed apply leaves the
stack in `UPDATE_FAILED` rather than triggering an automatic rollback, and a
later deploy can fix forward. This is mainly useful when *both* the apply and
the rollback would fail (e.g. a bad IAM policy CFN can't put either way) — it
keeps the stack out of the harder-to-recover `UPDATE_ROLLBACK_FAILED` state.

## Tearing down

Teardown deletes the five stacks in reverse dependency order and then removes
the retained, fixed-name survivors (the `cardinal-license` /
`cardinal-admin-key` / `cardinal-db-master` secrets, the cooked and raw
buckets, the RDS final snapshot) that would otherwise block a fresh install.
`cardinal-satellite-cwmetrics`, if deployed, is deleted first. The exact
sequence is under "Burn it down" in [`dev-environment.md`](dev-environment.md);
pass `DEPLOYER_ROLE_ARN` there too when a service role is in use.
