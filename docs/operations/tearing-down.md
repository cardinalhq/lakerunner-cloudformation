# Tearing down a Cardinal lakerunner install

Two layers, very different reversibility:

1. **Application layer** -- the `cardinal-lakerunner` stack. Stateless.
   `aws cloudformation delete-stack` removes everything it owns
   (including all stack-created SGs and IAM roles); nothing is retained.
2. **Infrastructure layer** -- the `cardinal-infrastructure` stack
   (RDS + RDS SG, S3 ingest bucket, Secrets Manager secrets, SSM
   parameters, SQS queue). Every customer-data-bearing resource carries
   `DeletionPolicy: Retain` (RDS uses `Snapshot`), so `delete-stack`
   removes the stack envelope but leaves the data resources behind.
   Wiping them is a destructive, operator-driven step.

A stack delete-then-redeploy of the application layer alone will pick
the infrastructure layer back up; a true decommission requires both
steps below.

## Layer 1: delete the lakerunner application stack

`cardinal-lakerunner` owns no `Retain` or `Snapshot` resources. The
template creates only stateless resources: ECS services, task
definitions, ALB, listeners, target groups, listener rules, IAM
roles, security groups, the Cloud Map private DNS namespace, and
CloudWatch log groups. All have `DeletionPolicy: Delete` (or no
policy, which AWS treats as `Delete`). The ECS cluster itself is
**not** in the stack and is not affected by `delete-stack`.

```sh
aws cloudformation delete-stack \
    --region <REGION> --stack-name cardinal-lakerunner

aws cloudformation wait stack-delete-complete \
    --region <REGION> --stack-name cardinal-lakerunner
```

ECS service deletion drains tasks gracefully; expect 1-2 minutes per
service. Total stack-delete wall time is typically 5-15 minutes.

Stack delete is sufficient if you intend to redeploy the application
layer against the same infrastructure stack. The next install picks
up the same RDS / bucket / secrets / queue, so a fresh lakerunner
stack reaches a working state without any data migration.

A CloudFormation service role passed via `--role-arn` is not required
for delete, but is recommended for installs that used it during apply.

## Layer 2: wipe the data layer

Two paths:

### Easy mode: `cardinal-cleanup` stack

The recommended path is the `cardinal-cleanup` standalone CFN root,
which registers a one-shot Fargate task whose role is the only role
with the data-layer delete verbs. See [`cleanup.md`](cleanup.md). The
cleanup task script:

1. Drains and deletes the S3 ingest bucket.
2. Disables RDS deletion-protection, deletes the instance, deletes
   the subnet group.
3. Force-deletes the secrets and the SSM parameters.
4. Deletes the SQS queue.
5. Deletes the `cardinal-infrastructure` stack envelope (which now
   owns nothing).

### Manual mode

If you would rather not run the cleanup task, the resources can be
removed directly. After deleting the lakerunner stack:

```sh
REGION=<REGION>
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
BUCKET="cardinal-ingest-${ACCOUNT}-${REGION}"

# 1. Look up the CFN-generated RDS instance and subnet group ids.
RDS_ID=$(aws cloudformation describe-stack-resource \
    --stack-name cardinal-infrastructure --logical-resource-id DBInstance \
    --query 'StackResourceDetail.PhysicalResourceId' --output text)
SUBNET_GROUP=$(aws cloudformation describe-stack-resource \
    --stack-name cardinal-infrastructure --logical-resource-id DBSubnetGroup \
    --query 'StackResourceDetail.PhysicalResourceId' --output text)

# 2. RDS. Snapshot is taken automatically (Snapshot deletion policy);
#    drop --skip-final-snapshot only after pulling/keeping it.
aws rds modify-db-instance --region "$REGION" \
    --db-instance-identifier "$RDS_ID" \
    --no-deletion-protection --apply-immediately >/dev/null
aws rds delete-db-instance --region "$REGION" \
    --db-instance-identifier "$RDS_ID" \
    --delete-automated-backups >/dev/null
aws rds wait db-instance-deleted --region "$REGION" \
    --db-instance-identifier "$RDS_ID"
aws rds delete-db-subnet-group --region "$REGION" \
    --db-subnet-group-name "$SUBNET_GROUP"

# 3. S3 ingest bucket.
aws s3 rm "s3://$BUCKET" --recursive --region "$REGION"
aws s3api delete-bucket --bucket "$BUCKET" --region "$REGION"

# 4. Secrets.
for prefix in cardinal-license cardinal-admin-key; do
    arn=$(aws secretsmanager list-secrets --region "$REGION" \
        --filters Key=name,Values="$prefix" \
        --query 'SecretList[0].ARN' --output text)
    [ "$arn" = "None" ] || aws secretsmanager delete-secret --region "$REGION" \
        --secret-id "$arn" --force-delete-without-recovery >/dev/null
done

# 5. SSM parameters.
aws ssm delete-parameters --region "$REGION" \
    --names /cardinal/storage-profiles /cardinal/api-keys

# 6. Now the infra stack envelope is empty -- delete it.
aws cloudformation delete-stack --region "$REGION" \
    --stack-name cardinal-infrastructure
```

The ECS cluster and the VPC are customer-owned -- removing them is an
IT-side step (whatever tool created them).

## Recovery from `DELETE_FAILED`

If `cardinal-lakerunner` lands in `DELETE_FAILED`, the most common
cause is a resource still referenced by something the stack does not
own (e.g. an extra ENI on a SG, or a manually-attached IAM policy on
a stack-created role). Inspect:

```sh
aws cloudformation describe-stack-events \
    --region <REGION> --stack-name cardinal-lakerunner \
    --query 'StackEvents[?contains(ResourceStatus, `FAILED`)].[Timestamp,LogicalResourceId,ResourceStatusReason]' \
    --output table
```

For any resource that refuses to delete, the standard escape hatch is
`aws cloudformation delete-stack --retain-resources <LogicalId>` to
drop it from the stack and clean it up by hand.
