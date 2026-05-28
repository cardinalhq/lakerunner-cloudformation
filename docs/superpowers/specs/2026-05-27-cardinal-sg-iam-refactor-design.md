# Cardinal CFN -- stack-owned security groups and IAM roles

Today, the customer pre-creates and threads four identifiers into the
Cardinal templates: `TaskRoleArn`, `ExecutionRoleArn`, `TaskSgId`, `AlbSgId`
to `cardinal-lakerunner`, plus `DBSecurityGroupId` to `cardinal-infrastructure`.
The two roles are usually a single admin-equivalent role; the two SGs
typically permit all task-to-task ingress on every port and all egress.
The model works but it is the opposite of least privilege and pushes the
hardest-to-reason-about decisions (which task needs which AWS API) onto
the operator who has the least context.

This refactor moves SGs and IAM roles into CloudFormation, splits the
single shared role into per-tier roles, and splits the single shared
task SG into per-tier task SGs. The customer continues to supply: the
ECS cluster, the VPC + private subnets, and the cleanup task role (used
once at teardown time only). Everything else -- including the ALB SG,
the RDS SG, six task SGs, six task roles, and one shared ECS execution
role -- is created by the Cardinal templates.

## Goals

- One CFN child stack creates every lakerunner-tier SG and role; the
  infra stack creates the one infra-tier SG (RDS). No template ever
  asks the customer for a role ARN or SG ID again, except for the
  cleanup task role used at teardown.
- Each task role scopes its permissions to exactly the AWS APIs the
  tier's services actually call. Same for SGs: ingress is allowed only
  on the ports actually consumed.
- Resource and child-stack names are `cardinal-` prefixed and short
  enough that the auto-generated nested-stack physical IDs (parent +
  `-LogicalId-` + 13-char hash) stay readable.
- The legacy shell driver (`scripts/data-setup.sh`) and the standalone
  ALB SG helper (`src/cardinal_cfn/cardinal_alb_sg.py`) are deleted.
  `cardinal-infrastructure.yaml` is the single supported infra path.

## Non-goals

- Multi-install isolation. Still one Cardinal install per AWS
  account+region; physical names stay `cardinal-*` without an install
  suffix.
- Egress restriction on task SGs. Tasks need outbound 443 to a long
  tail of AWS endpoints (ECR, STS, SecretsManager, SSM, CW Logs, plus
  the data plane). Allow-all-egress is the default; ingress + IAM are
  the scoping mechanisms.
- An in-place migration path for existing Cardinal installs. This
  branch assumes a fresh deploy. Test target tears down before redeploy.
- Customer-supplied KMS keys for any of the stack-owned resources.

## Customer-supplied surface, before and after

| Parameter | Before | After |
|---|---|---|
| `VpcId` | lakerunner | lakerunner + infra |
| `PrivateSubnets` | lakerunner + infra | lakerunner + infra |
| `ClusterName`, `ClusterArn` | lakerunner | lakerunner |
| `ServiceNamespaceId`, `ServiceNamespaceName` | lakerunner | (removed -- stack creates the Cloud Map namespace) |
| `TaskRoleArn`, `ExecutionRoleArn` | lakerunner | (removed) |
| `TaskSgId`, `AlbSgId` | lakerunner | (removed) |
| `DBSecurityGroupId` | infra | (removed) |
| `AlbAllowedCidrs` (new) | -- | lakerunner; default `10.0.0.0/8,172.16.0.0/12,192.168.0.0/16` |
| `CleanupTaskRoleArn`, `CleanupExecutionRoleArn` | cleanup | cleanup (unchanged) |

The customer-facing surface shrinks by four parameters on the
lakerunner root and one on the infra root, and gains one new parameter
on the lakerunner root (`AlbAllowedCidrs`).

## Stack layout

Two customer-facing CloudFormation roots, unchanged:

- `cardinal-vpc.yaml` -- optional VPC
- `cardinal-infrastructure.yaml` -- data plane (RDS, S3, SQS, secrets,
  SSM params); now also owns `cardinal-rds-sg`
- `cardinal-lakerunner.yaml` -- application root, **nine** nested
  children (one more than before; `Security` is new)

Plus the existing `cardinal-cleanup.yaml` (unchanged) and
`cardinal-vpc.yaml` (unchanged).

### New child: `security.yaml`

A new nested child, logical ID `Security`, owns every lakerunner-tier
SG and role. It is the *first* child the root instantiates; every
other child takes its outputs (SG IDs, role ARNs) as parameters.

Resources:

- **ALB SG** (`cardinal-alb-sg`): ingress on 443, 9443, 4318 from
  `AlbAllowedCidrs`; egress all.
- **Migration task SG** (`cardinal-svc-migration-sg`): egress all;
  no ingress (one-shot migrator never gets ingress).
- **Query task SG** (`cardinal-svc-query-sg`): ingress from ALB SG
  on the query-api container port (8080) and the query-api
  health-check port (8090); ingress from self on query-worker port
  (8081, so query-api can reach query-worker on the same SG);
  egress all.
- **Process task SG** (`cardinal-svc-process-sg`): egress all;
  no inbound (process services and pubsub-sqs are pull-only).
- **Control task SG** (`cardinal-svc-control-sg`): ingress from ALB SG
  on admin-api container port (9091); egress all.
- **OTel task SG** (`cardinal-svc-otel-sg`): ingress from ALB SG on
  4318; ingress from query/process/control/maestro SGs on 4318
  (in-cluster telemetry); egress all.
- **Maestro task SG** (`cardinal-svc-maestro-sg`): ingress from ALB SG
  on the maestro UI port (4200), MCP gateway port (8080), and DEX
  port (5556); ingress from self on the same ports (sidecar DEX);
  egress all.

Cross-stack SG ingress (resources on a sibling-owned SG):

- Six `AWS::EC2::SecurityGroupIngress` resources adding 5432 from
  each tier SG (that needs DB access) into the infra-owned
  `cardinal-rds-sg` (passed in via the new `RdsSecurityGroupId`
  parameter from the infra root). The migration, query, process,
  control, and maestro tiers all need DB access; otel does not.

Roles:

- **Shared execution role** (`cardinal-task-exec-role`,
  `ecs-tasks.amazonaws.com` trust): AWS-managed
  `AmazonECSTaskExecutionRolePolicy` (ECR pull, base CW Logs) +
  `secretsmanager:GetSecretValue` on `cardinal-*` +
  `ssm:GetParameter*` on `/cardinal/*`. Used by every task definition.
- **Migration task role** (`cardinal-svc-migration-role`):
  `secretsmanager:GetSecretValue` on the DB master secret;
  `ssm:GetParameter*` on the storage-profiles + api-keys SSM
  params; CW Logs writes to `/cardinal/lakerunner-migrator*`.
- **Query task role** (`cardinal-svc-query-role`): DB-master secret
  read; license secret read; storage-profiles + api-keys SSM read;
  `s3:GetObject` + `s3:ListBucket` on ingest bucket;
  `ecs:DescribeServices`, `ecs:ListTasks`, `ecs:DescribeTasks` on
  the cluster (query-api's ECS-based worker discovery);
  CW Logs writes to `/cardinal/query-*`.
- **Process task role** (`cardinal-svc-process-role`): DB-master +
  license secret read; storage-profiles SSM read; `s3:GetObject`,
  `s3:PutObject`, `s3:DeleteObject`, `s3:ListBucket` on ingest
  bucket; `sqs:ReceiveMessage`, `sqs:DeleteMessage`,
  `sqs:GetQueueAttributes` on ingest queue;
  `bedrock:InvokeModel{,WithResponseStream}` on
  `arn:aws:bedrock:*::foundation-model/*`; CW Logs writes.
- **Control task role** (`cardinal-svc-control-role`): DB-master +
  license + admin-key secret read; storage-profiles + api-keys SSM
  read; `s3:DeleteObject`, `s3:ListBucket` on ingest bucket
  (sweeper); `ecs:UpdateService`, `ecs:DescribeServices` on the
  cluster (monitoring's autoscaler) with an `ecs:cluster` IAM
  condition pinning the cluster ARN; CW Logs writes.
- **OTel task role** (`cardinal-svc-otel-role`): license secret
  read (OTel reads the license header for the CHQ pipeline);
  CW Logs writes to `/cardinal/otel-*`.
- **Maestro task role** (`cardinal-svc-maestro-role`): DB-master +
  license + admin-key secret read; storage-profiles SSM read;
  CW Logs writes.

Outputs:

- `AlbSgId`
- `MigrationSgId`, `QuerySgId`, `ProcessSgId`, `ControlSgId`,
  `OtelSgId`, `MaestroSgId`
- `ExecutionRoleArn`
- `MigrationRoleArn`, `QueryRoleArn`, `ProcessRoleArn`,
  `ControlRoleArn`, `OtelRoleArn`, `MaestroRoleArn`

### Cloud Map namespace

Moves into the lakerunner root (created directly, not in a nested
child -- it's a single `AWS::ServiceDiscovery::PrivateDnsNamespace`
resource and lives next to the install-id derivation). Defaults to
namespace name `cardinal.local`; customer can override via a new
`ServiceNamespaceName` parameter (kept under the Advanced group).
The root passes both the namespace ID and name to the children that
need them (services_query, services_control, otel, maestro).

### Shortened child logical IDs

| Old | New |
|---|---|
| `AlbStack` | `Alb` |
| `CertStack` | `Cert` |
| `MigrationStack` | `Migration` |
| `ServicesQueryStack` | `Query` |
| `ServicesProcessStack` | `Process` |
| `ServicesControlStack` | `Control` |
| `OtelStack` | `Otel` |
| `MaestroStack` | `Maestro` |
| -- | `Security` (new) |

Saves up to 7 characters on the auto-generated nested-stack physical
name (e.g. `cardinal-lr-ServicesQueryStack-X9F3...` ->
`cardinal-lr-Query-X9F3...`).

## Cross-stack wiring

Deployment order: VPC root (optional) -> infra root -> lakerunner root.

`cardinal-infrastructure.yaml`:

- Drops `DBSecurityGroupId` parameter.
- Adds `VpcId` parameter (needed to create the RDS SG).
- Creates `RdsSecurityGroup` (no ingress rules; description: "Cardinal
  RDS; ingress added by lakerunner stack."). Attaches it to the RDS
  instance.
- Adds output `RdsSecurityGroupId`.

`cardinal-lakerunner.yaml`:

- Drops `TaskRoleArn`, `ExecutionRoleArn`, `TaskSgId`, `AlbSgId`,
  `ServiceNamespaceId`, `ServiceNamespaceName` parameters.
- Adds `RdsSecurityGroupId` parameter (from the infra output).
- Adds `AlbAllowedCidrs` parameter (CSV; default
  `10.0.0.0/8,172.16.0.0/12,192.168.0.0/16`).
- Adds optional `ServiceNamespaceName` parameter (default
  `cardinal.local`).
- Creates the Cloud Map namespace inline.
- Instantiates `Security` first; passes all its outputs into the other
  children.

Each existing child trades the old shared params for tier-specific
ones:

- `alb.py`: `AlbSgId` -> still consumes one SG id, now from `Security`.
- `migration.py`: `TaskSecurityGroupId` -> `MigrationSgId`;
  `TaskRoleArn` -> `MigrationRoleArn`; `ExecutionRoleArn` unchanged
  shape.
- `services_query.py`: `TaskSecurityGroupId` -> `QuerySgId`;
  `TaskRoleArn` -> `QueryRoleArn`.
- `services_process.py`: -> `ProcessSgId` / `ProcessRoleArn`.
- `services_control.py`: -> `ControlSgId` / `ControlRoleArn`.
- `otel.py`: -> `OtelSgId` / `OtelRoleArn`.
- `maestro.py`: -> `MaestroSgId` / `MaestroRoleArn`.

The wider parameter surface inside each child stack is otherwise
unchanged.

## Deletion / lifecycle

- `cardinal-rds-sg` carries `DeletionPolicy: Retain` (matches the RDS
  instance; deleting the SG while a Retain'd RDS still references it
  fails anyway).
- Task SGs, ALB SG, IAM roles, Cloud Map namespace: `DeletionPolicy:
  Delete` (default). Stateless; recreated on next stack create.
- The legacy `scripts/data-setup.sh` and `src/cardinal_cfn/cardinal_alb_sg.py`,
  along with `tests/templates/test_cardinal_alb_sg.py`, are deleted.
  `cardinal-alb-sg.yaml` is removed from the generated-templates
  output and from the build/release wiring (`Makefile`, `build.sh`,
  `release.yml`).

## Documentation updates

- `docs/operations/install-infrastructure.md` -- drop shell-path
  section; update parameter list (add `VpcId`, drop
  `DBSecurityGroupId`); document new `RdsSecurityGroupId` output.
- `docs/operations/install-lakerunner.md` -- drop
  `TaskRoleArn`/`ExecutionRoleArn`/`TaskSgId`/`AlbSgId` from required
  inputs; add `RdsSecurityGroupId` and `AlbAllowedCidrs`; drop
  `ServiceNamespaceId`/`ServiceNamespaceName` (or document them as
  optional advanced overrides).
- `docs/operations/permissions-infrastructure.md` -- update deployer
  policy: must hold `iam:CreateRole`/`PutRolePolicy`/`DeleteRole` and
  `ec2:CreateSecurityGroup`/`AuthorizeSecurityGroupIngress`/etc.
- `docs/operations/permissions-lakerunner.md` -- replace the
  "customer-supplied roles + SGs" cookbook with a "deployer principal"
  cookbook (iam + ec2 SG write actions, plus the existing ECS/ELB/etc.).
  Document the new role + SG names so operators can grep for them.
- `docs/operations/tearing-down.md` -- drop the `data-setup.sh` step;
  the cleanup stack already handles the data-layer wipe.
- `docs/operations/deploying.md` -- update the two-step flow
  documentation if it references the old parameter set.

## Tests

New:

- `tests/templates/test_security.py` -- assertions over the security
  stack's generated YAML. Cover: SG count (1 ALB + 6 task SGs), role
  count (1 exec + 6 task roles), each role's policy scopes to the
  expected actions+resources, each SG's ingress matches the expected
  source+port table, six `AWS::EC2::SecurityGroupIngress` resources
  added to the supplied RDS SG.

Updated (parameter surface changes):

- `tests/templates/test_root.py`, `test_root_wiring.py`,
  `test_cardinal_infrastructure.py`, `test_alb.py`, `test_migration.py`,
  `test_services_query.py`, `test_services_process.py`,
  `test_services_control.py`, `test_otel.py`, `test_maestro.py`,
  `test_no_lambda.py` -- all updated to the new parameter shapes.
  Existing assertions that probe "no IAM resources" in each
  per-service-tier child stay green (IAM moved to Security, not back
  into the per-tier stacks).

Removed:

- `tests/templates/test_cardinal_alb_sg.py`.

## Open questions

None blocking. Implementation can start.
