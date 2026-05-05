# Permissions — lakerunner + maestro (runtime)

What the **running application** has access to. Every role here is
created and assumed inside the customer account at install time and
deleted on stack teardown. ARN scopes use `${InstallIdLong}` (12 hex
chars derived from the root stack ID) so multiple installs in one
account stay isolated.

This is the "what does the running software actually do?" half of the
permissions story. The deployer / install-time permissions live in
`permissions-infrastructure.md`.

Source of truth: `src/cardinal_cfn/children/*.py`.

## IAM roles

### Service-launch role (shared)

| Role | Trust | Why | Permissions |
|---|---|---|---|
| `ExecutionRole` | `ecs-tasks.amazonaws.com` | ECS uses this at task launch on every Fargate task to pull the image, write the bootstrap log stream, and resolve `secrets:` blocks on container definitions. | AWS-managed `AmazonECSTaskExecutionRolePolicy` (ECR pull, base CW Logs) + `secretsmanager:GetSecretValue` on `cardinal/${InstallIdLong}/*`, `DbMasterSecret-*`, `MaestroDbSecret-*`, `InternalServiceKeysSecret-*` + `ssm:GetParameter*` on `parameter/cardinal/${InstallIdLong}/*`. |

### ECS task roles (application identities)

One per service. All trust `ecs-tasks.amazonaws.com`. **Base** = S3 RW
on the ingest bucket, SQS consume+send on the ingest queue, SSM read on
the two config params, Secrets Manager read on `DbMasterSecret`,
`LicenseSecret`, `InternalServiceKeysSecret`, and CW Logs writes to its
own log group.

| Role | Why | Deviation from base |
|---|---|---|
| `QueryWorkerTaskRole` | Runs query workers. | Base only. |
| `QueryApiTaskRole` | Runs the query API; discovers live workers via the ECS API. | Base + `ecs:DescribeServices/ListTasks/DescribeTasks` (resource `*`, condition `ecs:cluster = ${ClusterArn}` — these actions don't accept ARN scopes). |
| `ProcessLogsTaskRole`, `ProcessMetricsTaskRole`, `ProcessTracesTaskRole`, `PubsubSqsTaskRole` | Log/metric/trace ingest workers. | Base only. |
| `SweeperTaskRole`, `AlertEvaluatorTaskRole` | Background control-plane jobs. | Base only. |
| `MonitoringTaskRole` | Autoscales the three `process-*` services. | Base + `ecs:DescribeServices`, `ecs:UpdateService` scoped to the three process service ARNs. |
| `AdminApiTaskRole` | Admin API; needs to seed first admin key on first boot. | Base + extra ARN (`AdminApiKeySecretArn`) added to the existing Secrets Manager statement. |
| `OtelTaskRole` | OTel collector; writes raw bytes to S3, no queue work. | S3 R/W/list + SSM + `LicenseSecret`/`InternalServiceKeysSecret` + own log group. **No SQS, no DB.** |
| `MaestroTaskRole` | Maestro UI + bundled DEX OIDC; talks only to Postgres and Bedrock. | DB master + maestro DB + license + internal-keys secrets, two SSM params, four maestro log groups, plus `bedrock:InvokeModel(WithResponseStream)` on `arn:aws:bedrock:${AWS::Region}::foundation-model/*`. **No S3, no SQS.** |
| `MigratorTaskRole` | One-shot DB migrator container (runs at install/upgrade only). | `secretsmanager:GetSecretValue` on `DbMasterSecret` + `logs:*` on `*` (self-creates its log group). **No S3, no SQS, no app secrets.** |

### Lambda roles (CloudFormation custom resources)

Both trust `lambda.amazonaws.com`. Logs are inline (not via the
AWS-managed basic-execution policy) so scopes stay deliberate.

| Role | Why | Permissions |
|---|---|---|
| `MigrationLambdaRole` | Drives one-shot `ecs:RunTask` of the migrator on stack create/update. | `logs:*` on `*`, `ecs:RunTask` on the migrator task definition (cond. `ecs:cluster = ${ClusterArn}`), `ecs:DescribeTasks` (same cond.), `iam:PassRole` on `ExecutionRoleArn` and `MigratorTaskRoleArn` only. |
| `CertLambdaRole` (optional) | Imports a customer-supplied PEM into ACM when `CertificateArn` is not provided. | `logs:*` on `*`, `acm:ImportCertificate` on `*` (no ARN exists at create time), `acm:DeleteCertificate`/`AddTagsToCertificate`/`RemoveTagsFromCertificate` scoped to `arn:aws:acm:${AWS::Region}:${AWS::AccountId}:certificate/*`. |

## Resource policies

| Policy | Why |
|---|---|
| `IngestQueuePolicy` (SQS) | S3 cannot deliver `s3:ObjectCreated:*` to SQS without an explicit grant. Allows `sqs:GetQueueAttributes`/`GetQueueUrl`/`SendMessage` from `s3.amazonaws.com` only when `aws:SourceAccount = ${AWS::AccountId}` (blocks cross-account spam). |

## Security groups

All three live in the customer's VPC. Tasks run in private subnets with
`AssignPublicIp: DISABLED`; the ALB is `Scheme: internal`.

| SG | Used by | Ingress | Egress | Why |
|---|---|---|---|---|
| `TaskSG` | Every ECS task in the install. | TCP 0-65535 from self (task-to-task via Cloud Map); TCP 0-65535 from `AlbSG` (ALB → targets). | All. | Egress is open because tasks need AWS APIs, ECR, Bedrock, etc. through customer NAT/VPC endpoints. Self-ingress enables internal service-to-service routing without going through the ALB. |
| `AlbSG` | The ALB only. | TCP 443 and TCP 9443 from `0.0.0.0/0` (= the customer's VPC, since the ALB is internal). | Default (all). | 443 carries query/maestro/otel; 9443 is a dedicated admin-api listener (admin-api's UI serves at `/`, which would clash with path-pattern rules on 443). |
| `DbSecurityGroup` | The RDS instance. | TCP 5432 from `TaskSG` only. | Default (all). | Defense-in-depth on top of `PubliclyAccessible: false` and private-subnet placement. |

## Network and data posture

- All ECS services: `AssignPublicIp: DISABLED`, private subnets.
- DB: `StorageEncrypted: true`, `LRDB_SSLMODE: require`, master password
  generated by CloudFormation into Secrets Manager (never a parameter or
  env var).
- Ingest bucket: customer-data-bearing, `DeletionPolicy: Retain`.
- License/admin secrets: `DeletionPolicy: Retain`.
- DB instance: `DeletionPolicy: Snapshot`.
- Service-to-service traffic stays inside `TaskSG` via Cloud Map private DNS.
- ECS rolling deploys run with the deployment circuit breaker enabled, so a
  bad image rolls back automatically.

## What is **not** granted

Common questions, answered up front:

- No role grants `s3:*` or `sqs:*` on resources outside the install.
- No role grants `iam:*` except `iam:PassRole` on the migration Lambda,
  scoped to two ARNs.
- No role grants `ec2:*`, `rds:*`, `kms:*`, or any account-wide read.
- No role grants Bedrock access except `MaestroTaskRole`, and only on
  `foundation-model/*` (not on customer-owned models or agents).
- No role uses `*` as both action and resource.
- No security group exposes anything to the public internet — the ALB is
  internal-scheme, so its `0.0.0.0/0` rules are VPC-local.
