# cardinal-lakerunner-services Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development / executing-plans. Checkbox steps.

**Goal:** Standalone `cardinal-lakerunner-services` root template — the application tier (ALB, cert, migration, query/process/control services, otel, maestro nested children), **parameter-driven**: it creates NO IAM roles, NO security groups, NO RDS, NO buckets, NO secrets. Every such value arrives as a parameter (driver-wired from `lakerunner-infra-base` and `lakerunner-infra-rds` outputs).

**Architecture:** New generator `src/cardinal_cfn/lakerunner_services.py`, produced by transforming `src/cardinal_cfn/root.py`. It reuses the EXISTING nested children under `generated-templates/cardinal-lakerunner/` (alb, cert, migration, services-query/process/control, otel, maestro) UNCHANGED — those children already accept `TaskSecurityGroupId`, `ExecutionRoleArn`, `TaskRoleArn`, `BucketName`, `DbEndpoint`, etc. as parameters. The only structural change vs `root.py` is: the `Security` child is removed, and the values it used to supply via `GetAtt(security_stack, ...)` now come from `Ref(<new parameter>)`.

This is ADDITIVE: leave `root.py`, `security.py`, `cardinal_infrastructure.py` and their tests in place (the current `cardinal-lakerunner.yaml` keeps working and the 529-test suite stays green). They are retired in a later cleanup pass once the new topology is validated in trials.

## Transform rules (apply to a copy of root.py)

Start from `src/cardinal_cfn/root.py`. Produce `lakerunner_services.py` with these changes:

1. **Remove the `Security` child** block entirely (the `_add_child(t, "Security", ...)` call and the `sec_*`/`*_role` `GetAtt` locals at root.py:509-541).

2. **Add parameters** for everything Security used to output, sourced from `lakerunner-infra-base`:
   - SG ids: `AlbSecurityGroupId`, `MigrationSecurityGroupId`, `QuerySecurityGroupId`, `ProcessSecurityGroupId`, `ControlSecurityGroupId`, `OtelSecurityGroupId`, `MaestroSecurityGroupId` (Type `AWS::EC2::SecurityGroup::Id`).
   - Role arns: `ExecutionRoleArn`, `MigrationRoleArn`, `QueryRoleArn`, `ProcessRoleArn`, `ControlRoleArn`, `OtelRoleArn`, `MaestroRoleArn` (Type `String`).
   Rebind the locals: `sec_alb_sg = Ref("AlbSecurityGroupId")`, ..., `exec_role = Ref("ExecutionRoleArn")`, `migration_role = Ref("MigrationRoleArn")`, etc. (Keeping the local names means the rest of the wiring below is untouched.)

3. **Rework the data-plane parameters** (currently "infra-stack outputs" at root.py:142-340):
   - REMOVE: `RdsSecurityGroupId` (rds owns its SG + ingress now; services don't need it), `IngestQueueUrl`, `IngestQueueArn` as REQUIRED — see below.
   - RENAME usage: replace every `Ref("IngestBucketName")` with `Ref("CookedBucketName")` (the cooked bucket from base now backs both otel-raw writes and cooked, single-account interim).
   - KEEP from rds: `DbEndpoint`, `DbPort`, `DbName`, `DbMasterSecretArn`.
   - KEEP from base: `LicenseSecretArn`, `AdminKeySecretArn`, `StorageProfilesParamName`, `ApiKeysParamName`, plus new `CookedBucketName`.
   - SQS interim: add `QueueUrl` and `QueueArn` parameters with `Default=""` (the pull-model queues are registered later; for v1 the process tier may run with no queue). Pass them through to the children exactly where `IngestQueueUrl`/`IngestQueueArn` were passed. Do NOT block on a queue.

4. **Keep everything else** identical: Cloud Map namespace creation, install-id derivation, the cert/alb/migration/query/process/control/otel/maestro `_add_child` calls and their wiring, sizing params, image overrides, dex params, `TemplateBaseUrl`, the `MaestroEnabled` condition, parameter-group metadata. Only the role/SG/bucket/queue SOURCES change (Ref new params instead of GetAtt security / IngestBucket).

5. **ALB minimization is OUT OF SCOPE for v1** (it requires child edits in services-query/control/alb). Keep the existing ALB wiring (query-api/admin-api still attach to the ALB). Add a `## Deferred` note in the module docstring.

## Build wiring

`build.sh`: add a generation line `python3 -m cardinal_cfn.lakerunner_services > generated-templates/cardinal-lakerunner-services.yaml` and add it to the cfn-lint list. The CHILDREN are already generated under `generated-templates/cardinal-lakerunner/` by the existing loop — the new root's `TemplateBaseUrl` + child keys must match those existing child template names (alb.yaml, cert.yaml, migration.yaml, services-query.yaml, services-process.yaml, services-control.yaml, otel.yaml, maestro.yaml). `Makefile` lint: add the new template.

## Tests (create tests/templates/test_lakerunner_services.py)

- `test_no_security_child`: the template has NO nested stack whose TemplateURL/key is `security.yaml` (assert no resource is the Security child).
- `test_creates_no_iam_or_sg`: the template's own Resources contain NO `AWS::IAM::Role` and NO `AWS::EC2::SecurityGroup` (everything is params/nested stacks). (Cloud Map namespace + nested `AWS::CloudFormation::Stack` resources are fine.)
- `test_role_and_sg_params_present`: all 7 SG-id params + all 7 role-arn params declared.
- `test_data_plane_params`: `CookedBucketName`, `DbEndpoint`, `DbPort`, `DbName`, `DbMasterSecretArn`, `LicenseSecretArn`, `AdminKeySecretArn`, `StorageProfilesParamName`, `ApiKeysParamName` present; `QueueUrl`/`QueueArn` present with Default "".
- `test_children_present`: nested stacks for cert, alb, migration, query, process, control, otel, maestro exist (8 children, no security).
- `test_cooked_bucket_wired_to_children`: at least one child receives `Ref("CookedBucketName")` for its bucket parameter (assert the namespace/wiring by inspecting the Migration or Otel child params in the generated JSON).

## Tasks (TDD; commit per task)

1. Scaffold `lakerunner_services.py` by copying root.py; remove Security child; add the 14 role/SG params + rebind locals; build.sh/Makefile wiring. Tests: `test_no_security_child`, `test_role_and_sg_params_present`, `test_children_present`. Commit.
2. Data-plane params: CookedBucketName (replace IngestBucketName usages), optional QueueUrl/QueueArn, drop RdsSecurityGroupId. Tests: `test_data_plane_params`, `test_cooked_bucket_wired_to_children`, `test_creates_no_iam_or_sg`. Commit.
3. Build + cfn-lint + full suite green. The generated `cardinal-lakerunner-services.yaml` must cfn-lint clean (it references the same children; cfn-lint validates the root in isolation — nested TemplateURLs are not resolved by cfn-lint, so this is fine). Commit fixups.

## Self-review notes

- The children are REUSED UNCHANGED. Verify the new root passes EXACTLY the parameter names each child declares (compare against root.py's existing `_add_child` dicts — they are correct; only the sources change). A wrong/missing child param name will not fail cfn-lint (nested params aren't resolved) but WILL fail at deploy — so the `_add_child` param dicts must stay byte-for-byte except for the role/SG/bucket/queue source swaps.
- Interim limitations to call out in the docstring: (a) ALB still carries query-api/admin-api (minimization deferred); (b) process-tier SQS is optional/empty pending the registration mechanism; (c) the cooked bucket backs both otel-raw and cooked in the single-account interim.
- Do NOT modify root.py/security.py/cardinal_infrastructure.py or any child. Purely additive.
