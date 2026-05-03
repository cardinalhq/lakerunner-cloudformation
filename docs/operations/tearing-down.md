# Tearing down a lakerunner stack

`aws cloudformation delete-stack` is *not* the end of the story. Several
resources are intentionally retained so that a misclick or bad upgrade does
not destroy customer data. This page lists every survivor, why it was
retained, and how to clean it up when the install is really being
decommissioned.

The retention list is the policy table in
[`src/cardinal_cfn/policies.py`](../../src/cardinal_cfn/policies.py); this
page is the operator-facing companion.

## TL;DR

Use the companion script:

```sh
scripts/teardown-lakerunner.sh \
  --stack-name cardinal-lakerunner \
  --region us-east-2 \
  --deployer-role-arn arn:aws:iam::<acct>:role/cardinal-cfn-deployer \
  --yes
```

Without `--yes`, the script prints what it would delete and exits without
touching anything. With `--yes`, it deletes the stack, waits for
`DELETE_COMPLETE`, then drains the ingest bucket, force-deletes the retained
secrets, and deletes the RDS final snapshot.

## What survives `delete-stack`

| Resource | Why retained | How to delete |
|---|---|---|
| `cardinal-ingest-<account>-<region>-<InstallIdLong>` (S3 bucket) | `DeletionPolicy: Retain` — bucket holds ingested telemetry; deleting it is destructive and irreversible. | Drain object versions + delete markers, then `s3api delete-bucket`. |
| `cardinal/<InstallIdLong>/license` (Secrets Manager) | `DeletionPolicy: Retain` — vendor-issued license value, painful to re-fetch. | `secretsmanager delete-secret --force-delete-without-recovery`. |
| `cardinal/<InstallIdLong>/admin-api-key` (Secrets Manager) | `DeletionPolicy: Retain` — auto-generated admin API key; redeploying the stack would issue a different value. | `secretsmanager delete-secret --force-delete-without-recovery`. |
| `DbMasterSecret` (Secrets Manager, auto-named, tag `Name=cardinal-db-master-<InstallIdShort>`) | `DeletionPolicy: Retain` — paired with the snapshot so the snapshot can be restored later. | `secretsmanager delete-secret --force-delete-without-recovery`. |
| RDS final snapshot (CFN-named `<stack>-Db-<random>` after a `Snapshot`-policy delete) | `DeletionPolicy: Snapshot` — last-line defence against accidental delete-stack. | `rds describe-db-snapshots --db-instance-identifier <id>` then `rds delete-db-snapshot`. |

`internal-service-keys`, the SSM parameters, all log groups, the SQS queue,
the ALB, the ECS cluster, and the RDS DB instance itself are *not* retained —
`delete-stack` removes them. Orphan log groups can occasionally show up if a
service held one open at delete time; they will roll off via their retention
policy on their own.

## Why secrets need `--force-delete-without-recovery`

Secrets Manager normally schedules deletion 7-30 days out so that a
mistakenly deleted secret can be restored. For a real tear-down that
recovery window is just billing for nothing. `--force-delete-without-recovery`
removes the secret immediately and skips the window.

If you want the recovery window (the install might come back), drop the flag
and pass `--recovery-window-in-days N` instead.

## Permissions model

The `cardinal-cfn-deployer` role's trust policy only allows
`cloudformation.amazonaws.com` to assume it. The deployer role is therefore
only useful on the `delete-stack` call itself; the post-stack cleanup
(bucket drain, secret force-delete, snapshot delete) runs as the calling
identity.

The deployer role *itself* needs `rds:CreateDBSnapshot` /
`rds:DescribeDBSnapshots` so that CloudFormation can take the final RDS
snapshot during stack deletion (issue #62, fixed in PR #65). Without those
permissions the stack lands in `DELETE_FAILED` and recovery requires admin
credentials.

## Manual procedure (without the script)

If you cannot run the script (air-gapped, custom IAM, etc.), the same flow
in raw AWS CLI:

```sh
STACK=cardinal-lakerunner
REGION=us-east-2
ROLE=arn:aws:iam::111122223333:role/cardinal-cfn-deployer

# 1. Capture identifiers BEFORE deleting — they are not recoverable afterward.
INSTALL_ID_LONG=$(aws cloudformation describe-stacks \
  --stack-name "$STACK" --region "$REGION" \
  --query 'Stacks[0].Outputs[?OutputKey==`InstallIdLong`].OutputValue' \
  --output text)

STORAGE_STACK=$(aws cloudformation describe-stack-resource \
  --stack-name "$STACK" --logical-resource-id Storage \
  --region "$REGION" \
  --query 'StackResourceDetail.PhysicalResourceId' --output text)
BUCKET=$(aws cloudformation describe-stacks \
  --stack-name "$STORAGE_STACK" --region "$REGION" \
  --query 'Stacks[0].Outputs[?OutputKey==`BucketName`].OutputValue' \
  --output text)

DATABASE_STACK=$(aws cloudformation describe-stack-resource \
  --stack-name "$STACK" --logical-resource-id Database \
  --region "$REGION" \
  --query 'StackResourceDetail.PhysicalResourceId' --output text)
DB_ID=$(aws cloudformation describe-stack-resource \
  --stack-name "$DATABASE_STACK" --logical-resource-id Db \
  --region "$REGION" \
  --query 'StackResourceDetail.PhysicalResourceId' --output text)
DB_SECRET=$(aws cloudformation describe-stacks \
  --stack-name "$DATABASE_STACK" --region "$REGION" \
  --query 'Stacks[0].Outputs[?OutputKey==`DbSecretArn`].OutputValue' \
  --output text)

CONFIG_STACK=$(aws cloudformation describe-stack-resource \
  --stack-name "$STACK" --logical-resource-id Config \
  --region "$REGION" \
  --query 'StackResourceDetail.PhysicalResourceId' --output text)
LICENSE_SECRET=$(aws cloudformation describe-stacks \
  --stack-name "$CONFIG_STACK" --region "$REGION" \
  --query 'Stacks[0].Outputs[?OutputKey==`LicenseSecretArn`].OutputValue' \
  --output text)
ADMIN_SECRET=$(aws cloudformation describe-stacks \
  --stack-name "$CONFIG_STACK" --region "$REGION" \
  --query 'Stacks[0].Outputs[?OutputKey==`AdminApiKeySecretArn`].OutputValue' \
  --output text)

# 2. Delete the stack.
aws cloudformation delete-stack \
  --stack-name "$STACK" --region "$REGION" --role-arn "$ROLE"
aws cloudformation wait stack-delete-complete \
  --stack-name "$STACK" --region "$REGION"

# 3. Drain + delete the bucket (handles versioned objects).
aws s3api list-object-versions --bucket "$BUCKET" --region "$REGION" \
  --query '{Objects: (Versions || `[]`) + (DeleteMarkers || `[]`) | [*].{Key:Key,VersionId:VersionId}}' \
  --output json > /tmp/del.json
aws s3api delete-objects --bucket "$BUCKET" --region "$REGION" \
  --delete file:///tmp/del.json
aws s3api delete-bucket --bucket "$BUCKET" --region "$REGION"

# 4. Force-delete the three retained secrets.
for ARN in "$LICENSE_SECRET" "$ADMIN_SECRET" "$DB_SECRET"; do
  aws secretsmanager delete-secret --secret-id "$ARN" \
    --region "$REGION" --force-delete-without-recovery
done

# 5. Delete the RDS final snapshot.
for SNAP in $(aws rds describe-db-snapshots \
                --db-instance-identifier "$DB_ID" \
                --snapshot-type manual --region "$REGION" \
                --query 'DBSnapshots[*].DBSnapshotIdentifier' \
                --output text); do
  aws rds delete-db-snapshot --db-snapshot-identifier "$SNAP" --region "$REGION"
done
```

The bucket-drain step in (3) only handles a single page of object versions.
For populated buckets, loop until `list-object-versions` returns an empty
list. The script handles this automatically.

## What if the stack is already in `DELETE_FAILED`?

`DELETE_FAILED` after a `delete-stack` typically means CloudFormation could
not delete some resource it owns (most often the RDS instance, because the
deployer role lacked `rds:CreateDBSnapshot`). Recovery:

1. Update the deployer role from the latest template (PR #65 added the
   missing snapshot perms). See [`deploying.md`](deploying.md) for the
   `update-stack` flow.
2. Re-run `delete-stack` with the updated role; it will retry the failing
   resource.

If a specific resource can't be deleted at all (e.g., a manually-modified
SG), use `delete-stack --retain-resources <LogicalId>` to drop it from the
stack and clean it up by hand afterwards.
