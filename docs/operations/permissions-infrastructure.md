# Permissions — infrastructure (install-time)

What the **deployer principal** (CI role, human, or service account that
runs `cloudformation deploy` against the lakerunner templates) needs in
order to create, update, and tear down the AWS resources the templates
declare.

This is the "what does your platform team need to provision?" half of the
permissions story. Runtime permissions — the IAM roles assumed by the
running tasks themselves — live in `permissions-lakerunner.md`. The
trust + inline policy contents for **every** customer-supplied IAM role
are in `required-roles.md`.

VPC is bring-your-own and intentionally excluded.

The lakerunner stack creates neither IAM roles nor security groups nor
data-bearing resources. The deployer therefore does not need `iam:*`,
`rds:*`, `s3:*`, `sqs:*`, `secretsmanager:*`, `ssm:*`, or
`ec2:*SecurityGroup*` permissions for the lakerunner stack itself. Those
permissions are required only by:

1. The IT principal that pre-creates the IAM roles and security groups
   (one-time, scoped to the cookbook contents).
2. The data-setup Lambda's execution role (auditable, granted
   create+update+delete on its own scope).

## Scope assumptions

- Resource ARNs match `cardinal-*` and `/cardinal/*` (S3 bucket, SQS
  queue, secrets, SSM params, log groups) where the AWS API supports
  name-prefix scoping.
- `iam:PassRole` is scoped to `arn:aws:iam::${account}:role/cardinal-*`.
- No `kms:*` is required — the templates rely on AWS-managed keys for
  RDS, Secrets Manager, and S3.
- The deployer never touches running data planes — it only creates,
  updates, and deletes infrastructure.

## API actions by AWS service

| Service | Why the deployer needs it | Minimum API actions |
|---|---|---|
| `cloudformation` | Create/update/delete the root stack and the nested children; CFN reads the published S3 templates. | `CreateStack`, `UpdateStack`, `DeleteStack`, `DescribeStacks`, `DescribeStackEvents`, `DescribeStackResources`, `DescribeStackResource`, `GetTemplate`, `ListStacks`, `CreateChangeSet`, `DescribeChangeSet`, `ExecuteChangeSet`, `DeleteChangeSet` |
| `iam` | `iam:PassRole` only; pass the customer-supplied `TaskRoleArn`, `ExecutionRoleArn`, `MigrationLambdaRoleArn`, and (optionally) `CertLambdaRoleArn` to ECS and Lambda. No `iam:CreateRole` or other write actions. | `PassRole` |
| `ecs` | Cluster, task definitions, services. | `CreateCluster`, `DeleteCluster`, `DescribeClusters`, `RegisterTaskDefinition`, `DeregisterTaskDefinition`, `DescribeTaskDefinition`, `CreateService`, `UpdateService`, `DeleteService`, `DescribeServices`, `ListServices`, `ListTasks`, `DescribeTasks`, `TagResource` |
| `elasticloadbalancingv2` | ALB, two listeners, target groups, listener rules. | `CreateLoadBalancer`, `DeleteLoadBalancer`, `DescribeLoadBalancers`, `ModifyLoadBalancerAttributes`, `CreateListener`, `DeleteListener`, `ModifyListener`, `DescribeListeners`, `CreateTargetGroup`, `DeleteTargetGroup`, `DescribeTargetGroups`, `ModifyTargetGroup`, `CreateRule`, `DeleteRule`, `ModifyRule`, `DescribeRules`, `AddTags`, `RemoveTags`, `DescribeTags` |
| `logs` | Per-service log groups + retention. | `CreateLogGroup`, `DeleteLogGroup`, `PutRetentionPolicy`, `DescribeLogGroups`, `TagResource`, `UntagResource`, `ListTagsForResource` |
| `lambda` | Migration Lambda (always); cert-import Lambda (only when importing PEM certs). | `CreateFunction`, `DeleteFunction`, `UpdateFunctionCode`, `UpdateFunctionConfiguration`, `GetFunction`, `GetFunctionConfiguration`, `InvokeFunction`, `TagResource`, `UntagResource`, `ListTags` |
| `servicediscovery` | Cloud Map private DNS namespace + per-service entries. | `CreatePrivateDnsNamespace`, `DeleteNamespace`, `GetNamespace`, `ListNamespaces`, `CreateService`, `DeleteService`, `GetService`, `ListServices`, `GetOperation`, `TagResource`, `UntagResource`, `ListTagsForResource` |

## What the deployer does **not** need

- `iam:*` write actions (`CreateRole`, `PutRolePolicy`, etc.). Roles
  are pre-created by the customer's IT.
- `ec2:*SecurityGroup*` write actions. SGs are pre-created by the
  customer's IT.
- `rds:*`, `s3:*`, `sqs:*`, `secretsmanager:*`, `ssm:*`. The
  data-bearing resources are owned by the data-setup Lambda's role
  (auditable, scoped to its own resources).
- Anything under `ec2:*Vpc*`, `ec2:*Subnet*`, `ec2:*RouteTable*`,
  `ec2:*InternetGateway*`, `ec2:*NatGateway*` — VPC is bring-your-own.
- `kms:*` — no customer-managed keys are provisioned.
- `bedrock:*` — Bedrock is runtime-only on the customer-supplied task role.
- `acm:*` — required only by the optional cert-import Lambda's own role,
  not by the deployer.
- Any `*:*` or account-wide read.
- Any cross-account permissions.
