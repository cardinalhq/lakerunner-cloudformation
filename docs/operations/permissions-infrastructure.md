# Permissions — infrastructure (install-time)

What the **deployer principal** (CI role, human, or service account that
runs `cloudformation deploy` against the lakerunner templates) needs in
order to create, update, and tear down the AWS resources the templates
declare.

This is the "what does your platform team need to provision?" half of the
permissions story. Runtime permissions — the IAM roles assumed by the
running tasks themselves — live in `permissions-lakerunner.md`.

VPC is bring-your-own and intentionally excluded.

## Scope assumptions

- Resource ARNs match `cardinal-*` and `cardinal/${InstallIdLong}/*` where
  the AWS API supports name-prefix scoping.
- `iam:PassRole` is scoped to `arn:aws:iam::${account}:role/cardinal-*`.
- No `kms:*` is required — the templates rely on AWS-managed keys for
  RDS, Secrets Manager, and S3.
- The deployer never touches running data planes — it only creates,
  updates, and deletes infrastructure.

## API actions by AWS service

| Service | Why the deployer needs it | Minimum API actions |
|---|---|---|
| `cloudformation` | Create/update/delete the root stack and the 12 nested children; CFN reads the published S3 templates. | `CreateStack`, `UpdateStack`, `DeleteStack`, `DescribeStacks`, `DescribeStackEvents`, `DescribeStackResources`, `DescribeStackResource`, `GetTemplate`, `ListStacks`, `CreateChangeSet`, `DescribeChangeSet`, `ExecuteChangeSet`, `DeleteChangeSet` |
| `iam` | Create/delete the ~14 task roles, the shared execution role, and the 2 Lambda roles; pass them to ECS and Lambda. | `CreateRole`, `DeleteRole`, `GetRole`, `UpdateRole`, `PutRolePolicy`, `GetRolePolicy`, `DeleteRolePolicy`, `AttachRolePolicy`, `DetachRolePolicy`, `ListRolePolicies`, `ListAttachedRolePolicies`, `TagRole`, `UntagRole`, `PassRole` |
| `ecs` | Cluster, task definitions, services. | `CreateCluster`, `DeleteCluster`, `DescribeClusters`, `RegisterTaskDefinition`, `DeregisterTaskDefinition`, `DescribeTaskDefinition`, `CreateService`, `UpdateService`, `DeleteService`, `DescribeServices`, `ListServices`, `ListTasks`, `DescribeTasks`, `RunTask` (manual migrations only — CFN drives the install-time run via the migration Lambda), `TagResource` |
| `rds` | Postgres instance + subnet group. | `CreateDBInstance`, `DeleteDBInstance`, `ModifyDBInstance`, `DescribeDBInstances`, `CreateDBSubnetGroup`, `DeleteDBSubnetGroup`, `DescribeDBSubnetGroups`, `AddTagsToResource`, `RemoveTagsFromResource`, `ListTagsForResource` |
| `ec2` (security groups only) | Three SGs + their ingress rules. VPC, subnets, route tables are excluded. | `CreateSecurityGroup`, `DeleteSecurityGroup`, `DescribeSecurityGroups`, `AuthorizeSecurityGroupIngress`, `AuthorizeSecurityGroupEgress`, `RevokeSecurityGroupIngress`, `RevokeSecurityGroupEgress`, `CreateTags`, `DeleteTags`, `DescribeTags` |
| `elasticloadbalancingv2` | ALB, two listeners, target groups, listener rules. | `CreateLoadBalancer`, `DeleteLoadBalancer`, `DescribeLoadBalancers`, `ModifyLoadBalancerAttributes`, `CreateListener`, `DeleteListener`, `ModifyListener`, `DescribeListeners`, `CreateTargetGroup`, `DeleteTargetGroup`, `DescribeTargetGroups`, `ModifyTargetGroup`, `CreateRule`, `DeleteRule`, `ModifyRule`, `DescribeRules`, `AddTags`, `RemoveTags`, `DescribeTags` |
| `s3` | Ingest bucket + lifecycle + bucket-event notification. | `CreateBucket`, `DeleteBucket`, `PutBucketLifecycleConfiguration`, `GetBucketLifecycleConfiguration`, `PutBucketNotification`, `GetBucketNotification`, `PutBucketTagging`, `GetBucketTagging`, `GetBucketLocation`, `ListBucket` |
| `sqs` | Ingest queue + its resource policy. | `CreateQueue`, `DeleteQueue`, `SetQueueAttributes`, `GetQueueAttributes`, `GetQueueUrl`, `ListQueues`, `TagQueue`, `UntagQueue`, `ListQueueTags` |
| `secretsmanager` | DB master, maestro DB, license, internal-keys, admin-key. | `CreateSecret`, `DeleteSecret`, `UpdateSecret`, `DescribeSecret`, `GetRandomPassword`, `TagResource`, `UntagResource`, `ListSecrets` |
| `ssm` | Two config parameters under `parameter/cardinal/${InstallIdLong}/*`. | `PutParameter`, `DeleteParameter`, `GetParameter`, `GetParameters`, `DescribeParameters`, `AddTagsToResource`, `RemoveTagsFromResource`, `ListTagsForResource` |
| `logs` | Per-service log groups + retention. | `CreateLogGroup`, `DeleteLogGroup`, `PutRetentionPolicy`, `DescribeLogGroups`, `TagResource`, `UntagResource`, `ListTagsForResource` |
| `lambda` | Migration Lambda (always); cert-import Lambda (only when importing PEM certs). | `CreateFunction`, `DeleteFunction`, `UpdateFunctionCode`, `UpdateFunctionConfiguration`, `GetFunction`, `GetFunctionConfiguration`, `InvokeFunction`, `TagResource`, `UntagResource`, `ListTags` |
| `servicediscovery` | Cloud Map private DNS namespace + per-service entries. | `CreatePrivateDnsNamespace`, `DeleteNamespace`, `GetNamespace`, `ListNamespaces`, `CreateService`, `DeleteService`, `GetService`, `ListServices`, `GetOperation`, `TagResource`, `UntagResource`, `ListTagsForResource` |

## What the deployer does **not** need

- Anything under `ec2:*Vpc*`, `ec2:*Subnet*`, `ec2:*RouteTable*`,
  `ec2:*InternetGateway*`, `ec2:*NatGateway*` — VPC is bring-your-own.
- `kms:*` — no customer-managed keys are provisioned.
- `bedrock:*` — Bedrock is runtime-only on the maestro task role.
- `acm:*` — required only by the optional cert-import Lambda's own role,
  not by the deployer.
- Any `*:*` or account-wide read.
- Any cross-account permissions.

## Suggested grouping into managed policies

If you can't grant a single broad role, three managed policies cover the
install cleanly:

1. **`cardinal-deploy-stack`** — `cloudformation:*` plus all `*:*Tag*` and
   `*:Describe*`/`*:List*` actions across the services above.
2. **`cardinal-deploy-iam`** — the `iam:*` actions, scoped to
   `arn:aws:iam::${account}:role/cardinal-*`.
3. **`cardinal-deploy-resources`** — the resource-creating actions on
   ecs/rds/ec2-sg/elbv2/s3/sqs/secretsmanager/ssm/logs/lambda/servicediscovery,
   scoped to `cardinal-*` ARNs where the AWS API supports prefixes.

Splitting along those lines lets a security team approve the IAM grants
separately from the resource grants if their policy requires it.
