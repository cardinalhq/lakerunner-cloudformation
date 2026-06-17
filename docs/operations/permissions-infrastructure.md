# Permissions — infrastructure (install-time)

What the **deployer principal** (CI role, human, or service account that
runs `cloudformation deploy` against the Cardinal templates) needs in
order to create, update, and tear down the AWS resources the templates
declare.

This is the "what does your platform team need to provision?" half of the
permissions story. Runtime permissions — the IAM roles assumed by the
running tasks themselves — live in `permissions-lakerunner.md` (those
roles are now stack-created; the customer no longer supplies them).

VPC and ECS cluster are bring-your-own and intentionally excluded.

## Scope assumptions

- Resource ARNs match `cardinal-*` and `/cardinal/*` (S3 bucket, SQS
  queue, secrets, SSM params, log groups, IAM roles, security groups
  via Name tag) where the AWS API supports name-prefix scoping.
- `iam:PassRole` is scoped to `arn:aws:iam::${account}:role/*` because
  the lakerunner stack creates per-tier roles with CFN-generated
  physical names. Tighten to your install prefix if needed.
- No `kms:*` is required — the templates rely on AWS-managed keys for
  RDS, Secrets Manager, and S3.
- The process-tier autoscaling uses the
  `AWSServiceRoleForApplicationAutoScaling_ECSService` service-linked
  role. AWS creates it automatically the first time a scalable target is
  registered; if your account does not already have it, the deployer
  also needs `iam:CreateServiceLinkedRole` for the
  `ecs.application-autoscaling.amazonaws.com` service.

## API actions by AWS service

| Service | Why the deployer needs it | Minimum API actions |
|---|---|---|
| `cloudformation` | Create/update/delete the root stack and the nested children; CFN reads the published S3 templates. | `CreateStack`, `UpdateStack`, `DeleteStack`, `DescribeStacks`, `DescribeStackEvents`, `DescribeStackResources`, `DescribeStackResource`, `GetTemplate`, `ListStacks`, `CreateChangeSet`, `DescribeChangeSet`, `ExecuteChangeSet`, `DeleteChangeSet` |
| `iam` | Stack-created task roles + execution role; `iam:PassRole` to register ECS task definitions; server-certificate actions for the optional PEM cert path. | `CreateRole`, `DeleteRole`, `GetRole`, `UpdateRole`, `PutRolePolicy`, `DeleteRolePolicy`, `GetRolePolicy`, `ListRolePolicies`, `AttachRolePolicy`, `DetachRolePolicy`, `ListAttachedRolePolicies`, `TagRole`, `UntagRole`, `ListRoleTags`, `PassRole`, `UploadServerCertificate`, `DeleteServerCertificate`, `GetServerCertificate`, `ListServerCertificates`, `TagServerCertificate`, `UntagServerCertificate` |
| `ec2` (security groups only) | Stack-created SGs: 1 ALB SG, 6 task SGs, 1 RDS SG (infra stack); cross-stack ingress rules. | `CreateSecurityGroup`, `DeleteSecurityGroup`, `DescribeSecurityGroups`, `DescribeSecurityGroupRules`, `AuthorizeSecurityGroupIngress`, `RevokeSecurityGroupIngress`, `AuthorizeSecurityGroupEgress`, `RevokeSecurityGroupEgress`, `ModifySecurityGroupRules`, `UpdateSecurityGroupRuleDescriptionsIngress`, `UpdateSecurityGroupRuleDescriptionsEgress`, `CreateTags`, `DeleteTags`, `DescribeTags` |
| `rds` | RDS instance + DB subnet group (infrastructure stack). | `CreateDBInstance`, `DeleteDBInstance`, `ModifyDBInstance`, `DescribeDBInstances`, `CreateDBSubnetGroup`, `DeleteDBSubnetGroup`, `DescribeDBSubnetGroups`, `AddTagsToResource`, `RemoveTagsFromResource`, `ListTagsForResource` |
| `s3` | Ingest bucket + lifecycle + notification config (infrastructure stack). | `CreateBucket`, `DeleteBucket`, `PutBucketTagging`, `GetBucketTagging`, `PutBucketLifecycleConfiguration`, `GetBucketLifecycleConfiguration`, `PutBucketNotificationConfiguration`, `GetBucketNotificationConfiguration`, `PutBucketPolicy`, `GetBucketPolicy`, `DeleteBucketPolicy`, `PutBucketPublicAccessBlock`, `GetBucketPublicAccessBlock`, `GetBucketLocation`, `ListBucket` |
| `sqs` | Ingest queue + queue policy. | `CreateQueue`, `DeleteQueue`, `GetQueueAttributes`, `SetQueueAttributes`, `ListQueueTags`, `TagQueue`, `UntagQueue` |
| `secretsmanager` | License, admin-key, db-master secrets + the secret-target attachment. | `CreateSecret`, `DeleteSecret`, `DescribeSecret`, `UpdateSecret`, `TagResource`, `UntagResource`, `ListSecrets`, `PutSecretValue`, `GetSecretValue` (for the SecretTargetAttachment lookup) |
| `ssm` | Storage-profiles + api-keys parameters. | `PutParameter`, `DeleteParameter`, `GetParameter`, `GetParameters`, `AddTagsToResource`, `RemoveTagsFromResource`, `ListTagsForResource` |
| `ecs` | Task definitions, services (the lakerunner stack does NOT create the cluster). | `RegisterTaskDefinition`, `DeregisterTaskDefinition`, `DescribeTaskDefinition`, `CreateService`, `UpdateService`, `DeleteService`, `DescribeServices`, `ListServices`, `ListTasks`, `DescribeTasks`, `TagResource`, `UntagResource`, `ListTagsForResource` |
| `application-autoscaling` | CPU target-tracking autoscaling for the process-{logs,metrics,traces} services. | `RegisterScalableTarget`, `DeregisterScalableTarget`, `DescribeScalableTargets`, `PutScalingPolicy`, `DeleteScalingPolicy`, `DescribeScalingPolicies` |
| `cloudwatch` | Target-tracking policies create the alarms that drive scale in/out. | `PutMetricAlarm`, `DeleteAlarms`, `DescribeAlarms` |
| `elasticloadbalancingv2` | ALB, three listeners (443, 9443, 4318), target groups, listener rules. | `CreateLoadBalancer`, `DeleteLoadBalancer`, `DescribeLoadBalancers`, `ModifyLoadBalancerAttributes`, `CreateListener`, `DeleteListener`, `ModifyListener`, `DescribeListeners`, `CreateTargetGroup`, `DeleteTargetGroup`, `DescribeTargetGroups`, `ModifyTargetGroup`, `CreateRule`, `DeleteRule`, `ModifyRule`, `DescribeRules`, `AddTags`, `RemoveTags`, `DescribeTags` |
| `logs` | Per-service log groups + retention. | `CreateLogGroup`, `DeleteLogGroup`, `PutRetentionPolicy`, `DescribeLogGroups`, `TagResource`, `UntagResource`, `ListTagsForResource` |
| `servicediscovery` | Cloud Map private DNS namespace (created by the lakerunner root) + per-service registrations. | `CreatePrivateDnsNamespace`, `DeleteNamespace`, `GetNamespace`, `ListNamespaces`, `CreateService`, `DeleteService`, `GetService`, `ListServices`, `GetOperation`, `TagResource`, `UntagResource`, `ListTagsForResource` |

The deploy must be invoked with `--capabilities CAPABILITY_IAM` so
CloudFormation can manage the IAM roles created by the `Security`
child stack.

## What the deployer does **not** need

- VPC / subnet / route table / IGW / NAT / TGW write actions. VPC is
  bring-your-own.
- ECS cluster create/delete. Cluster is bring-your-own.
- `kms:*` — no customer-managed keys are provisioned.
- `bedrock:*` — Bedrock is runtime-only on the process-tier task role.
- Any `*:*` or account-wide read.
- Any cross-account permissions.

## Tearing down

Same actions as create; plus the `Retain` / `Snapshot` policies on the
infra-tier data resources mean a `delete-stack` leaves orphans behind
on purpose. Wiping the data is a deliberate operator step covered in
[`dev-environment.md`](dev-environment.md) (the `cardinal-cleanup` stack uses a
separate, narrowly-scoped customer-supplied `CleanupTaskRoleArn` — see
that doc for the policy).
