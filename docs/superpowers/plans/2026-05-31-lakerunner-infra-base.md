# cardinal-lakerunner-infra-base Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development / executing-plans. Checkbox steps.

**Goal:** Standalone `cardinal-lakerunner-infra-base` template — the IT/security-owned stack: ALB SG + per-tier task SGs + inter-tier ingress, the shared ECS execution role + six per-tier task roles, the cooked-only write bucket, the license/admin secrets, and the operator-managed SSM config params. It owns no RDS, no ingest queue.

**Architecture:** New generator `src/cardinal_cfn/lakerunner_infra_base.py` (`build()`/`__main__`), standalone. It is the union of two existing modules, minus RDS:
- From `src/cardinal_cfn/children/security.py`: ALB SG, 6 task SGs, inter-tier ingress rules, the `ExecutionRole`, and the 6 tier task roles (`Migration/Query/Process/Control/Otel/Maestro`). DROP the `Rds5432From*` ingress rules and the `RdsSecurityGroupId` param (RDS ingress now lives in `lakerunner-infra-rds`).
- From `src/cardinal_cfn/cardinal_infrastructure.py`: the cooked bucket (replaces the ingest bucket; NO SQS, NO notification, `Retain`), the license + admin-key secrets, and the storage-profiles + api-keys SSM params.

Deploy order is `base → rds → services`, so `base` cannot reference rds/services outputs. The roles therefore scope by NAME PATTERN (decided below), not by threaded ARNs.

## Decisions (deliberate; document in code comments)

1. **Name-pattern IAM** (decouples base from rds/services):
   - Exec role + task-role secret access → `arn:${AWS::Partition}:secretsmanager:${AWS::Region}:${AWS::AccountId}:secret:cardinal-*` (was: threaded `DbMasterSecretArn`/`LicenseSecretArn`/`AdminKeySecretArn`). This REQUIRES the rds DB master secret to be named `cardinal-db-master` (see Amendment A) and base's license/admin secrets named `cardinal-license` / `cardinal-admin-key`.
   - SSM access stays `/cardinal/*` (already name-pattern in security.py — unchanged).
   - S3 statements (`_stmt_s3_read`/`_stmt_s3_readwrite`) target the **cooked bucket** base creates (reference its name directly).
   - Process tier: REPLACE the local `_stmt_sqs_consume()` with a cross-account `sts:AssumeRole` on `arn:${AWS::Partition}:iam::*:role/cardinal-satellite-access*` (the lakerunner poller assumes each satellite's access role; that role carries the actual S3/SQS perms). This REQUIRES the satellite access role to be named `cardinal-satellite-access` (see Amendment B).
2. **Cooked bucket**: name param `CookedBucketName` default `cardinal-cooked-${AWS::AccountId}-${AWS::Region}`, `Retain`, encryption + public-access-block (like satellite raw bucket), NO lifecycle expiry (cooked is durable), NO SQS, NO notification.
3. Keep creating the storage-profiles/api-keys SSM params (the migrator still imports them; seed storage-profiles with the cooked bucket + region, api-keys with the initial key or `[]`) — mirror `cardinal_infrastructure.py`'s SSM section.

## Amendments to already-built stacks (do these in THIS work, small)

- **Amendment A** — `src/cardinal_cfn/lakerunner_infra_rds.py`: give `DBMasterSecret` an explicit `Name="cardinal-db-master"` (matches CLAUDE.md's documented `cardinal-*-db-master` secret and lets base's exec role scope to `cardinal-*`). Update/extend `tests/templates/test_lakerunner_infra_rds.py` to assert the name. Commit separately: `fix(rds): name DB master secret cardinal-db-master`.
- **Amendment B** — `src/cardinal_cfn/satellite_infra_base.py`: give `LakerunnerAccessRole` an explicit `RoleName="cardinal-satellite-access"` so the lakerunner poller can grant `sts:AssumeRole` on `cardinal-satellite-access*`. Update `tests/templates/test_satellite_infra_base.py` to assert the RoleName. (One install per account, so a fixed role name is safe per CLAUDE.md.) Commit separately: `feat(satellite): name access role cardinal-satellite-access for cross-account assume`.

## Reference (READ fully before implementing)

- `src/cardinal_cfn/children/security.py` — the WHOLE file. Reproduce: `_ecs_tasks_trust`, the IAM statement helpers (`_stmt_secrets_read`, `_stmt_ssm_read`, `_stmt_s3_read`, `_stmt_s3_readwrite`, `_stmt_sqs_consume`, `_stmt_cw_logs`), the ALB SG + 6 task SGs + inter-tier ingress, the exec role, the 6 task roles, and the outputs (`_emit`). Apply the Decisions above to the secret/s3/sqs statements.
- `src/cardinal_cfn/cardinal_infrastructure.py` — the cooked-bucket analog (use the IngestBucket minus the NotificationConfiguration and minus the lifecycle-expiry, `Retain`), the LicenseSecret/AdminKeySecret blocks (give them names `cardinal-license`/`cardinal-admin-key`), and the StorageProfilesParam/ApiKeysParam blocks.
- `src/cardinal_cfn/satellite_infra_base.py` — house style for a standalone generator.

## Parameters

Keep from security.py: `VpcId`, `ClusterArn` (for the ecs:* Condition `ArnEquals ecs:cluster`), `AlbAllowedCidrs` (ALB SG ingress). Drop `RdsSecurityGroupId` and all the threaded `*Arn`/`*ParamName` inputs (now base-created or name-pattern). Add from infra: `CookedBucketName` (default blank → `cardinal-cooked-...`), `LicenseData` (NoEcho), `InitialIngestApiKey` (NoEcho, default ""), `OrganizationId` (default the canonical org), and the SSM/secret name params with `cardinal-*` defaults. Group with `add_parameter_group_metadata`.

## Outputs (consumed by rds + services via the Jenkins driver)

- All 7 SG ids: `AlbSecurityGroupId`, `MigrationSecurityGroupId`, `QuerySecurityGroupId`, `ProcessSecurityGroupId`, `ControlSecurityGroupId`, `OtelSecurityGroupId`, `MaestroSecurityGroupId`.
- All 7 role arns: `ExecutionRoleArn`, `MigrationRoleArn`, `QueryRoleArn`, `ProcessRoleArn`, `ControlRoleArn`, `OtelRoleArn`, `MaestroRoleArn`.
- `CookedBucketName`, `LicenseSecretArn`, `AdminKeySecretArn`, `StorageProfilesParamName`, `ApiKeysParamName`.

## Tasks (TDD; commit per task)

1. Amendment A (rds secret name) + Amendment B (satellite role name) with their tests. Two commits.
2. Scaffold base generator + parameters + build wiring (build.sh/Makefile, `cardinal-lakerunner-infra-base.yaml`). Test required params; cooked-bucket default name. Commit.
3. ALB SG + 6 task SGs + inter-tier ingress (lift from security.py, drop RDS ingress). Tests: 7 SGs present; ALB SG ingress on the HTTPS/OTel ports from AlbAllowedCidrs; a sibling ingress rule exists. Commit.
4. Exec role + 6 task roles with name-pattern IAM. Tests: exec role secrets scoped to `cardinal-*` (NOT a threaded Ref); ssm `/cardinal/*`; process role has `sts:AssumeRole` on `cardinal-satellite-access*` and NO local sqs queue ARN; query role keeps the ecs:Describe* condition; s3 statements target the cooked bucket. Commit.
5. Cooked bucket (Retain, encrypted, PAB, no SQS/notification/lifecycle) + license/admin secrets (Retain, named) + storage-profiles/api-keys SSM params. Tests: cooked bucket Retain + no NotificationConfiguration; secrets named cardinal-license/cardinal-admin-key + Retain; SSM params seeded. Commit.
6. Outputs (all SG ids + role arns + bucket + secret arns + ssm names). Test all present. Commit.
7. Build + cfn-lint + full suite green. Commit fixups.

## Self-review notes

- The name-pattern IAM is the load-bearing decision; comment it where the secret/sqs statements diverge from security.py.
- Verify cfn-lint: a wildcard secret ARN resource and an `iam::*:role/...` assume-role resource both lint clean (IAM resource wildcards are allowed). The cooked bucket without a notification config lints clean.
- This stack is large (~700 lines, union of two modules). Use `# ---` section headers. If any single section becomes unwieldy, a module-level helper (e.g. `_task_sgs(t, vpc)`) is acceptable, but keep parity with security.py's structure for reviewability.
