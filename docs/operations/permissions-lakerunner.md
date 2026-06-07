# Permissions — lakerunner + maestro (runtime)

What the **running application** has access to.

Every IAM role and security group is created by the `Security` nested
stack in `cardinal-lakerunner`. The customer no longer supplies any of
them. (The one exception is the `cardinal-cleanup` stack, which uses a
customer-supplied teardown role -- documented in
[`dev-environment.md`](dev-environment.md).)

This doc is the "what does the running software actually do?" half of
the permissions story. Install-time permissions for the deployer
principal live in [`permissions-infrastructure.md`](permissions-infrastructure.md).

## IAM roles (all stack-created in the `Security` child)

### Shared execution role

`ExecutionRole` (logical id) -- trust `ecs-tasks.amazonaws.com`. ECS
uses it at task launch on every Fargate task to pull the image, write
the bootstrap log stream, and resolve `secrets:` blocks on container
definitions.

Permissions: AWS-managed `AmazonECSTaskExecutionRolePolicy` (ECR pull,
base CW Logs) + `secretsmanager:GetSecretValue` on `cardinal-*` +
`ssm:GetParameter*` on `parameter/cardinal/*`.

### Per-tier task roles

One per child stack. Trust is always `ecs-tasks.amazonaws.com`.

| Role | Used by | Permissions (over and above bare-minimum CW logs) |
|---|---|---|
| `MigrationRole` | `Migration` child's migrator task. | `secretsmanager:GetSecretValue` on the db-master secret; `ssm:GetParameter*` on `/cardinal/storage-profiles` and `/cardinal/api-keys`. |
| `QueryRole` | query-api + query-worker. | db-master + license secret read; storage-profiles + api-keys SSM read; `s3:GetObject` / `s3:ListBucket` on the ingest bucket; `ecs:DescribeServices`, `ecs:ListTasks`, `ecs:DescribeTasks` scoped to the cluster (query-api's ECS-based worker discovery). |
| `ProcessRole` | process-{logs,metrics,traces} + pubsub-sqs. | db-master + license secret read; storage-profiles SSM read; `s3:GetObject` / `s3:PutObject` / `s3:DeleteObject` / `s3:ListBucket` on the ingest bucket; `sqs:ReceiveMessage` / `sqs:DeleteMessage` / `sqs:GetQueueAttributes` on the ingest queue; `bedrock:InvokeModel{,WithResponseStream}` on `foundation-model/*`. |
| `ControlRole` | sweeper + monitoring + admin-api + alert-evaluator. | db-master + license + admin-key secret read; storage-profiles + api-keys SSM read; `s3:GetObject` / `s3:DeleteObject` / `s3:ListBucket` on the ingest bucket (sweeper); `ecs:UpdateService` / `ecs:DescribeServices` scoped to the cluster (monitoring's autoscaler). |
| `OtelRole` | otel-gateway collector. | License secret read; CW Logs writes only. |
| `MaestroRole` | maestro + dex sidecar. | db-master + license + admin-key secret read; storage-profiles + api-keys SSM read. |

The migrator runs as an ECS service (`migration.yaml`: Fargate task
that runs `lakerunner migrate`, then a `keepalive` container that
idles). No Lambda anywhere in the product. The TLS cert path either
forwards a supplied `CertificateArn` or creates an
`AWS::IAM::ServerCertificate` from PEM parameters.

## Resource policies

| Policy | Owner | Why |
|---|---|---|
| `IngestQueuePolicy` (SQS) | `cardinal-infrastructure` stack | S3 cannot deliver `s3:ObjectCreated:*` to SQS without an explicit grant. Allows `sqs:SendMessage` from `s3.amazonaws.com` only when `aws:SourceAccount = ${AccountId}` (blocks cross-account spam). |

## Security groups (all stack-created)

All live in the customer's VPC. Tasks run in private subnets with
`AssignPublicIp: DISABLED`; the ALB is `Scheme: internal`.

| SG (logical id) | Used by | Inbound | Outbound | Why |
|---|---|---|---|---|
| `AlbSecurityGroup` (`cardinal-alb-sg`) | The ALB. | TCP 443 / 9443 / 4318 from `AlbAllowedCidr1..3` (default: all RFC1918). | All. | 443 carries query/maestro/otel; 9443 is the dedicated admin-api listener; 4318 is OTLP/HTTP. |
| `MigrationSecurityGroup` (`cardinal-svc-migration-sg`) | One-shot migrator task. | None. | All. | Migrator initiates DB + ECR + Secrets connections; never receives traffic. |
| `QuerySecurityGroup` (`cardinal-svc-query-sg`) | query-api + query-worker. | TCP 8080 from `AlbSecurityGroup`; TCP 8081 from self (query-api → query-worker). | All. | ALB hits 8080 on query-api; query-api hits the worker on 8081 over the shared tier SG. |
| `ProcessSecurityGroup` (`cardinal-svc-process-sg`) | process-{logs,metrics,traces} + pubsub-sqs. | None. | All. | Process tier is pull-only (SQS + S3 + DB). |
| `ControlSecurityGroup` (`cardinal-svc-control-sg`) | sweeper + monitoring + admin-api + alert-evaluator. | TCP 9091 from `AlbSecurityGroup` (admin-api). | All. | Only admin-api is ALB-attached; the rest are pull-only. |
| `OtelSecurityGroup` (`cardinal-svc-otel-sg`) | otel-gateway collector. | TCP 4318 from `AlbSecurityGroup`; TCP 4318 from each of `Query` / `Process` / `Control` / `Maestro` SGs (self-telemetry). | All. | OTLP/HTTP ingestion. |
| `MaestroSecurityGroup` (`cardinal-svc-maestro-sg`) | maestro + dex sidecar. | TCP 4200 (maestro UI) and TCP 5556 (dex) from `AlbSecurityGroup`. | All. | Maestro's UI catch-all + dex's `/dex/*`. |
| `RdsSecurityGroup` (`cardinal-rds-sg`, owned by the infra stack) | The RDS instance. | TCP 5432 from `Migration` / `Query` / `Process` / `Control` / `Maestro` task SGs (added by the lakerunner Security child). | All. | OTel does not need DB; no rule. |

## Network and data posture

- All ECS services: `AssignPublicIp: DISABLED`, private subnets.
- DB: `StorageEncrypted: true`, `LRDB_SSLMODE: require`, master password
  generated by `cardinal-infrastructure` into Secrets Manager.
- Ingest bucket: customer-data-bearing, `Retain` on stack delete.
- License/admin secrets: `Retain` on stack delete.
- Service-to-service traffic stays inside the tier SGs (and the
  query-tier self-ingress for api → worker) via Cloud Map private DNS.
- ECS rolling deploys run with the deployment circuit breaker enabled,
  so a bad image rolls back automatically.

## What is **not** granted

- No role grants `s3:*` or `sqs:*` on resources outside the install
  (all ARN scopes are `cardinal-ingest-*` and `cardinal-ingest`).
- No running service has `iam:*`. The deployer needs `iam:PassRole` on
  the stack-created roles to register ECS task definitions -- see the
  install-time permissions doc.
- No role grants `ec2:*`, `rds:*`, or `kms:*` to the running services.
- No security group exposes anything to the public internet -- the ALB
  is internal-scheme.
