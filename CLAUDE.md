# Cardinal CloudFormation Development Instructions

This file contains instructions for Claude on how to work with this CloudFormation repository.

## Repository Status

This branch (`cardinal-cfn-refactor`) is mid-refactor. The old 10-template flat layout has been deleted and is being replaced by a thin-root + nested-children architecture. See:

- `docs/superpowers/specs/2026-04-28-cardinal-cfn-refactor-design.md` — design spec (source of truth)
- `docs/superpowers/plans/2026-04-28-cardinal-cfn-refactor.md` — phase-by-phase implementation plan

When in doubt, the design spec wins. The plan tells you the order of operations.

## Target architecture

Two customer-facing CloudFormation root templates:

- `cardinal-vpc.yaml` — optional VPC (skipped by customers using their own VPC)
- `cardinal-lakerunner.yaml` — application root, composed of eleven nested children

The lakerunner root nests these children:

| # | Template | Owns |
|---|---|---|
| 1 | `cluster.yaml` | ECS cluster, base TaskSG, shared execution role, base log group |
| 2 | `database.yaml` | RDS subnet group, DB instance, DB master secret |
| 3 | `storage.yaml` | S3 ingest bucket + lifecycle, SQS queue + policy, S3 → SQS notifications |
| 4 | `alb.yaml` | ALB, default 443 listener, ALB security group |
| 5 | `config.yaml` | SSM params, license/admin/internal-keys secrets |
| 6 | `migration.yaml` | One-shot DB migration custom resource |
| 7 | `services-query.yaml` | query-api, query-worker |
| 8 | `services-process.yaml` | process-{logs,metrics,traces}, pubsub-sqs |
| 9 | `services-control.yaml` | sweeper, monitoring, admin-api, alert-evaluator |
| 10 | `otel.yaml` | otel-collector |
| 11 | `maestro.yaml` | Maestro + bundled DEX OIDC |

Cross-stack wiring goes through the root via `Fn::GetAtt childStack.Outputs.X` → child parameter. Sibling children never reference each other directly.

## Repo / generator code layout (target)

```
src/
  cardinal_cfn/
    __init__.py
    install_id.py            # InstallIdShort/InstallIdLong derivation (root only)
    naming.py                # Tag conventions, name-tag helpers
    parameters.py            # Shared parameter / NoEcho / parameter-group helpers
    images.py                # Image-override parameter machinery
    policies.py              # DeletionPolicy / UpdateReplacePolicy table
    listener_priorities.py   # Pre-allocated ListenerRule priorities (B → C safe)
    defaults.py              # cardinal-defaults.yaml loader
    children/                # one module per nested-stack child
    root.py                  # parent template generator
  cardinal_vpc.py            # standalone VPC generator
cardinal-defaults.yaml       # consolidated defaults (services, images, maestro, otel)
tests/
  unit/                      # helper-level tests
  templates/                 # per-template assertions via cloud-radar
.github/workflows/
  test.yml                   # PR/main test runner
  release.yml                # publish to S3 on tag
```

Generated templates go to `generated-templates/`, mirroring the S3 key layout:

```
generated-templates/
  cardinal-vpc.yaml
  cardinal-lakerunner.yaml             # the root
  cardinal-lakerunner/                 # the children, mirrors S3 prefix
    cluster.yaml
    database.yaml
    ...
```

## Key design rules

### Naming and tags

- Default to **CloudFormation-generated physical names** with a `Name` tag.
- Prefix is `cardinal-`. Use `chq-` only when an AWS resource name length cap forces it.
- Explicit physical names *only* where externally referenced — SSM Parameter names (AWS-required), the S3 ingest bucket name (predictability), license/admin secrets (referenced from outside the stack).
- Never name RDS, ECS clusters/services, listener rules, log groups, target groups — explicit names block in-place updates.

### Multi-install isolation

`InstallIdShort` (8 hex) and `InstallIdLong` (12 hex) are derived from the root stack's `AWS::StackId` and propagated as parameters to every nested child. **Children never compute these themselves** — `Ref(AWS::StackId)` in a child returns the child's id, not the root's.

```
UUID            = Fn::Select(2, Fn::Split("/", Ref(AWS::StackId)))
InstallIdShort  = Fn::Select(0, Fn::Split("-", UUID))
InstallIdLong   = Fn::Join("", [first two segments of UUID])
```

### Sensitive values

- Sensitive values **always** go to Secrets Manager. `AWS::SSM::Parameter` cannot be `SecureString` in CloudFormation.
- Parameters carrying secrets (`LicenseData`, `ApiKeysOverride`, `StorageProfilesOverride`) declare `NoEcho: true`.

### List parameters into nested stacks

CloudFormation passes nested-stack parameters as strings. Lists like `PrivateSubnets` cannot be reliably forwarded as `List<...>`. The convention: every child declares such parameters as `String` (CSV) and uses `Fn::Split(",", ...)` internally. The root joins with `Fn::Join(",", ...)` before passing.

### Lifecycle policies

Customer-data-bearing resources get `DeletionPolicy: Snapshot` (RDS) or `Retain` (S3 ingest, license/admin secrets). Stateless resources are `Delete`. The exact table lives in `src/cardinal_cfn/policies.py` and is enforced by `apply_policy(resource, kind)`.

### ListenerRule priorities

Pre-allocated, registered in `src/cardinal_cfn/listener_priorities.py`. Each service's priority stays the same when the service is later moved into its own per-service stack — preventing collisions during the future B → C split.

| Service | Priority |
|---|---|
| query-api | 100 |
| admin-api | 110 |
| maestro-https | 200 |
| maestro-dex | 210 |
| otel-grpc | 300 |
| (reserved) | 400-999 |

### Migration custom resource

Lambda-backed, runs the lakerunner migrator as a one-shot ECS task. Behavior:

- Stable `PhysicalResourceId`: `cardinal-migration-<InstallIdLong>`.
- Trigger: `MigrationVersion` property = lakerunner image **digest** (`sha256:...`). Tags are rejected.
- Create runs the migrator. Update reruns only if `MigrationVersion` changed. Delete is a no-op.

### Service tier rule (B → C safe)

The three service tier stacks (`services-query`, `services-process`, `services-control`) own *only* per-service resources: ECS Service, TaskDefinition, TargetGroup, ListenerRule, per-service log group, per-service task role. Anything shared across services lives in `cluster`, `alb`, or `config`. This rule keeps the door open to a future per-service-stack split with minimal disruption.

## Build and testing

The build script and Makefile are being rewritten in Phase 8. Until then, generators are run individually via:

```sh
python3 -m cardinal_cfn.children.<child>      # emits YAML to stdout
python3 -m cardinal_cfn.root                  # emits root YAML
python3 src/cardinal_vpc.py                   # emits VPC YAML
```

Tests:

```sh
pytest tests/unit/        # helper unit tests
pytest tests/templates/   # per-template assertions
pytest tests/             # everything
```

Linting:

```sh
cfn-lint generated-templates/cardinal-vpc.yaml \
         generated-templates/cardinal-lakerunner.yaml \
         generated-templates/cardinal-lakerunner/*.yaml
```

All templates must pass cfn-lint with no errors. Warnings are tolerable when explainable.

## Publishing (Phase 8)

GitHub Actions on tag push (`v*`) builds, lints, tests, and publishes to a vendor-managed public S3 bucket (`cardinal-cfn`, provisioned in `terraform-deployments/aws/production/cloudformation-distribution.tf`). Customers paste the regional S3 URL into the CloudFormation console:

```
https://cardinal-cfn.s3.<region>.amazonaws.com/lakerunner/<version>/cardinal-lakerunner.yaml
```

Air-gapped customers override the `TemplateBaseUrl` parameter on the root stack to point at a customer-owned mirror.

## Security considerations

- Never hardcode secrets — Secrets Manager only.
- All ECS tasks run with `AssignPublicIp: DISABLED`.
- Database connections require SSL (`LRDB_SSLMODE: require`).
- DB credentials are auto-generated into Secrets Manager.
- ECS task roles follow least privilege — each service has its own task role scoped to exactly the resources it needs.
- All tasks run in private subnets with no public IP assignment.
- ECS rolling deployments use `MinimumHealthyPercent: 50`, `MaximumPercent: 200`, and the deployment circuit breaker enabled, so a bad image bump rolls back automatically.

## Coding style

- Follow existing coding style as much as practical.
- No trailing whitespace, no extra blank lines.
- All code formatted properly.
- All text-like files end with a final newline.
- Useful comments are welcome; verbosity should be minimal; document non-obvious code only.
- "Section" style comments are OK.
- Markdown unordered lists use `-` not `*`.
- Markdown ordered lists repeat `1.` for each item.
- Blank lines between markdown headers, code blocks, and other items.
- Never add advertisements for Claude or Anthropic to docs or commit messages.
- No emoji.
- If my coworker (user) asks me to change ECS containers to non-root, remind them that bind mounts will require root.
