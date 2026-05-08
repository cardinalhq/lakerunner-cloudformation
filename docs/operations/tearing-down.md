# Tearing down a Cardinal lakerunner install

After the infra-script pivot, "tearing down" splits cleanly into two
independent layers, each with very different reversibility:

1. **Application layer** -- the `cardinal-lakerunner` stack. Stateless.
   `aws cloudformation delete-stack` removes everything it owns; nothing
   is retained.
2. **Infra layer** -- the resources `scripts/data-setup.sh` created
   (RDS, S3 ingest bucket, Secrets Manager secrets, SSM parameters,
   SQS queue, ECS cluster, Cloud Map namespace). These live
   **outside** any CloudFormation stack and survive deletion of the
   lakerunner stack. Wiping them is a destructive, customer-data-bearing
   operation that the operator must trigger explicitly.

Read both sections before deciding what to delete. A stack delete-then-
redeploy will pick the existing infra layer back up; a true
decommission requires step 2.

## Layer 1: delete the lakerunner application stack

`cardinal-lakerunner` owns no `Retain` or `Snapshot` resources. The
template creates only stateless application resources: ECS services,
task definitions, ALB, listeners, target groups, listener rules,
custom-resource Lambdas (cert-import + migration), and CloudWatch log
groups. All have `DeletionPolicy: Delete` (or no policy, which AWS
treats as `Delete`). The ECS cluster itself is **not** in the stack
and is not affected by `delete-stack`.

```sh
aws cloudformation delete-stack \
    --region <REGION> --stack-name cardinal-lakerunner

aws cloudformation wait stack-delete-complete \
    --region <REGION> --stack-name cardinal-lakerunner
```

Notable side effects:

- The cert-import custom-resource Lambda (used when `CertificateBody` /
  `CertificatePrivateKey` are provided instead of `CertificateArn`)
  receives a `RequestType: Delete` event during stack delete and calls
  `acm:DeleteCertificate`. It retries on `ResourceInUseException` for up
  to 14 minutes while the ALB releases the cert -- so the stack delete
  may sit on the cert custom resource for several minutes before
  finishing.
- The migration custom-resource Lambda is a no-op on `Delete`. Its
  function and the custom-resource record go away with the stack.
- ECS service deletion drains tasks gracefully; expect 1-2 minutes per
  service. Total stack-delete wall time is typically 5-15 minutes.
- The migration and cert custom-resource Lambda functions write to log
  groups named `/aws/lambda/cardinal-migration-<InstallIdLong>` and
  `/aws/lambda/cardinal-cert-<InstallIdLong>`. Those log groups are
  auto-created by the Lambda runtime on first write, not by
  CloudFormation, so `delete-stack` does not remove them. Harmless and
  small (the migration Lambda runs once at install; the cert Lambda
  runs at install + delete), but they will accumulate one orphan pair
  per `InstallIdLong` if you redeploy with new install IDs. Delete with
  `aws logs delete-log-group --log-group-name ...` if you want a
  pristine account.

Stack delete is sufficient if you intend to redeploy the application
layer against the same infra. The next install picks up the same RDS /
bucket / secrets / cluster, so a fresh lakerunner stack reaches a
working state without any data migration.

A CloudFormation service role passed via `--role-arn` is not required
for delete -- the lakerunner stack creates no IAM, so the
rollback-permissions wedge that motivates a service role does not
exist on the delete path.

## Layer 2: wipe the data layer

`scripts/data-setup.sh` has no `delete` mode. To actually wipe the
data layer the operator must remove the resources directly. The set
is fixed and named deterministically:

| Resource | Identifier |
|---|---|
| RDS instance | `cardinal-db` |
| S3 ingest bucket | `cardinal-ingest-<account>-<region>` |
| Secret: db master | `cardinal-db-master-*` (Secrets Manager auto-suffixed) |
| Secret: maestro db | `cardinal-maestro-db-*` |
| Secret: license | `cardinal-license-*` |
| Secret: internal keys | `cardinal-internal-keys-*` |
| Secret: admin key | `cardinal-admin-key-*` |
| SSM parameter | `/cardinal/storage-profiles` |
| SSM parameter | `/cardinal/api-keys` |
| SQS queue | `cardinal-ingest` |
| RDS subnet group | `cardinal-db-subnet-group` |

The ECS cluster and the Cloud Map namespace are customer-owned and
deliberately outside this list -- delete them only if you also want
those gone, and only **after** the lakerunner stack is gone so the
cluster is empty.

The operator's identity must hold the matching `Delete*` actions for
each resource type.

Manual procedure (raw AWS CLI). Run the lakerunner stack delete
**first** (Layer 1 above) so no ECS task is still using the data
resources or task SG.

```sh
REGION=<REGION>
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
BUCKET="cardinal-ingest-${ACCOUNT}-${REGION}"

# 1. RDS instance. The script creates the DB with DeletionProtection=True,
#    so disable that first. SkipFinalSnapshot means no final snapshot is
#    taken; drop it to keep one. Wait for delete to complete -- a
#    DBSubnetGroup cannot be deleted while any instance still uses it.
aws rds modify-db-instance \
    --region "$REGION" \
    --db-instance-identifier cardinal-db \
    --no-deletion-protection \
    --apply-immediately >/dev/null
aws rds delete-db-instance \
    --region "$REGION" \
    --db-instance-identifier cardinal-db \
    --skip-final-snapshot \
    --delete-automated-backups >/dev/null
aws rds wait db-instance-deleted \
    --region "$REGION" --db-instance-identifier cardinal-db

aws rds delete-db-subnet-group \
    --region "$REGION" --db-subnet-group-name cardinal-db-subnet-group

# 2. S3 ingest bucket. The script does not enable versioning, so the
#    simple recursive remove is sufficient.
aws s3 rm "s3://$BUCKET" --recursive --region "$REGION"
aws s3api delete-bucket --bucket "$BUCKET" --region "$REGION"

# 3. Secrets. Use --force-delete-without-recovery to skip the 7-30 day
#    recovery window; drop that flag (and add --recovery-window-in-days)
#    if you want it.
for prefix in cardinal-db-master cardinal-maestro-db cardinal-license \
              cardinal-internal-keys cardinal-admin-key; do
    arn=$(aws secretsmanager list-secrets --region "$REGION" \
        --filters Key=name,Values="$prefix" \
        --query 'SecretList[0].ARN' --output text)
    [ "$arn" = "None" ] && continue
    aws secretsmanager delete-secret --region "$REGION" \
        --secret-id "$arn" --force-delete-without-recovery >/dev/null
done

# 4. SSM parameters.
aws ssm delete-parameters --region "$REGION" \
    --names /cardinal/storage-profiles /cardinal/api-keys

# 5. SQS queue.
queue_url=$(aws sqs get-queue-url --region "$REGION" \
    --queue-name cardinal-ingest --query QueueUrl --output text)
aws sqs delete-queue --region "$REGION" --queue-url "$queue_url"
```

The ECS cluster and the Cloud Map namespace are customer-owned, so
removing them is an IT-side step (whatever IaC / console flow created
them in the first place). If you do need the raw CLI calls:

```sh
aws ecs delete-cluster --region "$REGION" --cluster <cluster-name>
aws servicediscovery delete-namespace --region "$REGION" --id <ns-id>
```

## What about the prereqs?

The IT-side IAM roles (one task role, one execution role, one migration
Lambda role, optionally one cert Lambda role) and the security groups
(`TaskSgId`, `AlbSgId`, `DbSgId`) are deliberately owned outside both
stacks -- the customer's IT team created them once. Removing them is an
IT-side operation that should match how IT created them (Terraform,
manual IAM, or whatever tool they use); Cardinal's tearing-down flow
does not touch them.

If you also want them gone:

```sh
for role in cardinal-task-role cardinal-execution-role \
            cardinal-migration-lambda-role \
            cardinal-cert-lambda-role; do
    aws iam delete-role-policy --role-name "$role" --policy-name cardinal-inline 2>/dev/null || true
    aws iam list-attached-role-policies --role-name "$role" \
        --query 'AttachedPolicies[*].PolicyArn' --output text \
    | tr '\t' '\n' | while read arn; do
        [ -n "$arn" ] && aws iam detach-role-policy \
            --role-name "$role" --policy-arn "$arn"
    done
    aws iam delete-role --role-name "$role" 2>/dev/null || true
done

# Security groups. AWS rejects deletion of any SG that is referenced
# by another SG's ingress rule. Reference graph:
#   - DbSg ingress from TaskSg     -> DbSg references TaskSg
#   - TaskSg ingress from self+AlbSg -> TaskSg references AlbSg
#   - AlbSg ingress from VPC CIDR   -> no SG reference
# So delete order is "things that reference, before things that are
# referenced": DbSg (referenced by nobody) -> TaskSg (no longer
# referenced once DbSg is gone) -> AlbSg (no longer referenced once
# TaskSg is gone).
for sg in cardinal-db-sg cardinal-task-sg cardinal-alb-sg; do
    id=$(aws ec2 describe-security-groups --region "$REGION" \
        --filters Name=group-name,Values="$sg" \
        --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null)
    [ "$id" = "None" ] && continue
    aws ec2 delete-security-group --region "$REGION" --group-id "$id"
done
```

The optional `cardinal-vpc` test stack -- only used in ephemeral test
environments -- is deleted with `aws cloudformation delete-stack`.

## Recovery from `DELETE_FAILED`

If `cardinal-lakerunner` lands in `DELETE_FAILED`, the most common
cause post-pivot is the cert-import Lambda timing out while the ALB
holds the imported ACM cert. Recovery:

1. Inspect the failing event:

    ```sh
    aws cloudformation describe-stack-events \
        --region <REGION> --stack-name cardinal-lakerunner \
        --query 'StackEvents[?contains(ResourceStatus, `FAILED`)].[Timestamp,LogicalResourceId,ResourceStatusReason]' \
        --output table
    ```

2. If the ALB / target groups have already gone but the cert is still
   stuck `InUse`, manually detach the cert from any remaining listener
   (there should be none after the ALB delete) or delete it directly:

    ```sh
    aws acm list-certificates --region <REGION> \
        --query 'CertificateSummaryList[?contains(SubjectAlternativeNameSummaries, `cardinal`)].[CertificateArn]'
    aws acm delete-certificate --certificate-arn <ARN> --region <REGION>
    ```

3. Re-run `delete-stack`. The cert custom resource will succeed on the
   second pass (the cert is already gone and `DeleteCertificate`
   returns success on a missing resource).

For any other resource that refuses to delete, the standard escape
hatch is `aws cloudformation delete-stack --retain-resources <LogicalId>`
to drop it from the stack and clean it up by hand.

## Legacy installs (pre-pivot)

Installs deployed before the `cardinal-cfn-prereqs-split` pivot still
embed the data layer (S3 ingest, RDS, license / admin / db-master
secrets) inside the `cardinal-lakerunner` stack with `Retain` /
`Snapshot` policies. For those installs the legacy companion script
`scripts/teardown-lakerunner.sh` still applies: it runs `delete-stack`,
then drains the retained bucket, force-deletes the retained secrets,
and removes the RDS final snapshot.

```sh
scripts/teardown-lakerunner.sh \
    --stack-name cardinal-lakerunner \
    --region <REGION> --yes
```

The script is safe on post-pivot installs too -- the post-delete
cleanup steps no-op when the legacy nested stacks (`StorageStack`,
`DatabaseStack`, `ConfigStack`) are absent. It is effectively a thin
wrapper around `delete-stack + wait` in that case.
