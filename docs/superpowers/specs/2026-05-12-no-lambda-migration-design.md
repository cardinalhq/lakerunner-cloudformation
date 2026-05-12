# No-Lambda DB migration — design

## Problem

The `cardinal-lakerunner` stack runs DB migrations via a Lambda-backed custom
resource (`migration.yaml`: `AWS::Lambda::Function` + `Custom::` resource that
`ecs.run_task`s the migrator task and polls for exit 0). Some target
environments cannot run Lambda at all, and only have IAM roles whose trust
policy lists `ecs-tasks.amazonaws.com` (not `lambda.amazonaws.com`). Migrations
are not optional — every service-tier stack gates on them — so the stack as
published cannot deploy there.

## Approach

Replace the migration custom resource with a long-running **ECS Service** whose
task runs the migrator and then sleeps. CloudFormation already blocks the
service-tier stacks on `MigrationStack` reaching `CREATE_COMPLETE` (existing
`DependsOn`), and a nested `AWS::CloudFormation::Stack` is only `CREATE_COMPLETE`
when all its resources are — so if the migration ECS Service only reaches steady
state *after a successful migration*, that gate keeps its meaning with no
Lambda, no custom resource, and no change to how the parent orders things.

"Reaches steady state only after a successful migration" is enforced with ECS
container ordering inside the migrator task definition:

| Container | Essential | Image | dependsOn |
|---|---|---|---|
| `configdb-init` | false | `DbInitImage` | — (runs first; `psql CREATE DATABASE configdb` if absent) |
| `migrator` | **false** (was true) | `LakerunnerImage` | `configdb-init: COMPLETE` |
| `keepalive` | **true** (new) | `DbInitImage` | `migrator: SUCCESS` |

`keepalive` runs a sleep loop (`sh -c 'while sleep 3600; do :; done'`). Because
it is the only `essential` container and ECS will not start it until `migrator`
exits 0, the *task* is not `RUNNING` until migrations succeed, so the *service*
does not reach steady state until then, so `MigrationStack` does not reach
`CREATE_COMPLETE` until then. A non-zero `migrator` exit → `keepalive` never
starts → task fails → with the ECS deployment circuit breaker enabled, the
service deployment fails → `MigrationStack` fails → the parent stack
create/update fails and rolls back. The migration error is in the
`MigratorLogGroup` CloudWatch logs.

On a `LakerunnerImage` change, the migration service's task definition changes,
ECS does a rolling redeploy (the new task reruns `configdb-init` → `migrator`
→ `keepalive`), the service reaches steady state only after the new migrations
succeed, *then* the service-tier stacks update with the new image — same
drift-prevention property the Lambda trigger gave us (the migrator and the
service tasks always run the same image). The migration ECS task uses
`TaskRoleArn` / `ExecutionRoleArn` — both `ecs-tasks.amazonaws.com`-assumable —
so the environment's ECS-trust-only role works.

Cost: one `256` CPU / `512` MiB Fargate task running a sleep loop permanently
(~$3/month). `DesiredCount` is hardcoded to `1` (not a parameter): an operator
who wants to reclaim the slot can `aws ecs update-service --desired-count 0`
manually — that is harmless CloudFormation drift, and the next `LakerunnerImage`
bump re-applies `DesiredCount: 1` from the template (so the migration reruns
exactly when it should). A parameter at `0` would instead *suppress* the
migration on the next image bump, which is the more dangerous failure mode.

## Changes

### `src/cardinal_cfn/children/migration.py`

- Remove the `MigrationLambdaRoleArn` parameter.
- Remove the `LambdaLogGroup` log group.
- `migrator` container: `Essential=False` (keep its `configdb-init: COMPLETE`
  dependency, command, env, secrets, logs).
- Add `keepalive` container: `Essential=True`, `Image=Ref("DbInitImage")`,
  `EntryPoint=["sh", "-c"]`, `Command=["echo migrations complete; while sleep
  3600; do :; done"]`, `DependsOn=[ContainerDependency("migrator", "SUCCESS")]`,
  same `awslogs` group (stream prefix `keepalive`). No env/secrets.
- Remove `MigrationLambda` (`AWS::Lambda::Function`), `MigrationRunner`
  (`Custom::` resource), and the `from cardinal_cfn.children import
  migration_lambda` import.
- Add `MigratorService` (`AWS::ECS::Service`): `Cluster=Ref("ClusterArn")`,
  `TaskDefinition=Ref(MigratorTaskDef)`, `LaunchType="FARGATE"`,
  `DesiredCount=1`, `NetworkConfiguration` →
  `AwsvpcConfiguration(Subnets=Split(",", Ref("PrivateSubnetsCsv")),
  SecurityGroups=[Ref("TaskSecurityGroupId")], AssignPublicIp="DISABLED")`,
  `DeploymentConfiguration(MinimumHealthyPercent=0, MaximumPercent=100,
  DeploymentCircuitBreaker(Enable=True, Rollback=True))`. No `ServiceName`
  (CFN-generated + `Name` tag), no Cloud Map registration. Tags
  `cardinal_tags(component="migration", role="migrator-service")`.
- Rename output `MigrationCustomResourceRef` → `MigrationServiceArn`
  (`Value=Ref(MigratorService)`).

### `src/cardinal_cfn/root.py`

- Remove the `MigrationLambdaRoleArn` parameter (from the parameter list and
  the "IAM roles + security groups" parameter group).
- Remove `"MigrationLambdaRoleArn": Ref("MigrationLambdaRoleArn")` from the
  `MigrationStack` child parameters.
- `migration_complete = GetAtt(migration_stack, "Outputs.MigrationServiceArn")`
  (was `Outputs.MigrationCustomResourceRef`). Everything downstream
  (`MigrationComplete` threaded into the service / maestro children, the
  explicit `depends_on=["MigrationStack"]`) is unchanged.

### Deletions

- `src/cardinal_cfn/children/migration_lambda.py`
- `tests/unit/test_migration_lambda.py`

### Tests

- Rewrite `tests/templates/test_migration.py`: assert the three-container
  ordering chain, `migrator` non-essential / `keepalive` essential, the ECS
  Service (FARGATE, desired 1, private subnets, no public IP, circuit breaker),
  no `AWS::Lambda::Function` / `Custom::*` resources, no `MigrationLambdaRoleArn`
  parameter, and the `MigrationServiceArn` output.
- Update `tests/templates/test_root.py` / `test_root_wiring.py`: drop
  `MigrationLambdaRoleArn` from expected parameters; `MigrationComplete` is
  still threaded into the four DB-dependent children.
- Add a guard test that no template under `generated-templates/` (vpc, infra,
  lakerunner root, all children) contains `AWS::Lambda::Function`, except
  `cardinal-lakerunner/cert.yaml` (the optional PEM-import path — see Known
  limitations).

### Docs / scripts

- `CLAUDE.md`: rewrite the "Migration custom resource" section to describe the
  ECS-service mechanism.
- `docs/operations/*`: drop `MigrationLambdaRoleArn` from the lakerunner
  parameter reference and permissions docs; note that the migration service is
  a permanent (tiny) Fargate task and how to scale it to 0.
- `do-not-commit-lakerunner.sh` (gitignored scratch): drop the
  `MigrationLambdaRoleArn` parameter.

## Known limitations / out of scope

- The cert-import path in `cert.yaml` is still Lambda-backed, but it is only
  created when `CertificateArn` is empty. No-Lambda environments must supply an
  ACM `CertificateArn` (importing a self-signed cert into ACM with
  `aws acm import-certificate` needs no Lambda). Making the PEM path Lambda-free
  is out of scope here.
- The migrator must remain idempotent (`lakerunner migrate` = apply pending
  up-migrations). A stray Fargate task recycle reruns `configdb-init` (no-op)
  and `migrator` (no-op) and goes back to sleeping; that is fine. Backward
  schema moves are not part of this design.

## Test plan

1. `make build` (cfn-lint clean) and `make test` green.
2. Deploy `cardinal-lakerunner` into the test account (493746473138, us-east-1)
   against the `cardinal-infrastructure` outputs, a wide-open SG, an
   ECS-trust-only admin role, a fresh ECS cluster + Cloud Map namespace, and a
   self-signed ACM cert. Confirm: `MigrationStack` reaches `CREATE_COMPLETE`
   only after the `migrator` container logs success; the service-tier stacks
   then come up; the migration service settles at 1 running task; no Lambda
   functions in the account from this stack.
