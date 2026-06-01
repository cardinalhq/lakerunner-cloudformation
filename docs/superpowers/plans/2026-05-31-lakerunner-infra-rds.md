# cardinal-lakerunner-infra-rds Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development / executing-plans. Checkbox steps.

**Goal:** Standalone `cardinal-lakerunner-infra-rds` template — the RDS Postgres instance, its security group + per-tier 5432 ingress rules, subnet group, master-credentials secret, in the lakerunner account.

**Architecture:** New generator `src/cardinal_cfn/lakerunner_infra_rds.py` (`build()`/`__main__`), standalone. It lifts the RDS section of `src/cardinal_cfn/children/../cardinal_infrastructure.py` (RdsSecurityGroup, DBSubnetGroup, DBMasterSecret, DBInstance, DBMasterSecretAttachment) and adds the `Rds5432From<tier>` ingress rules that currently live in `src/cardinal_cfn/children/security.py` (lines ~428-446). Deploy order is `base → rds`, so the task security groups are inputs (outputs of `lakerunner-infra-base`); this stack creates the RDS SG and the ingress rules pointing the task SGs at it.

**Tech Stack:** Python 3, troposphere, pytest, cfn-lint.

## Design anchors

- DB lifecycle = `Snapshot` on delete (customer data preserved); `DeletionProtection=False` so the stack can actually be torn down during trials (snapshot still protects data). This is a deliberate change from `cardinal_infrastructure.py`'s `DeletionProtection=True`, aligning with the spec's "deleting the stack cleans up; RDS is the snapshot exception."
- RDS SG and subnet group = `Delete` on stack delete. Master secret = `Retain` (needed to restore from snapshot).
- DB-client tiers needing 5432: migration, query, process, control, maestro. **otel does NOT** (no DB dependency — matches `security.py`). Five discrete task-SG-id parameters, five ingress rules.

## Reference (READ before implementing)

- `cardinal_infrastructure.py` (no children path; it's `src/cardinal_cfn/cardinal_infrastructure.py`) — the RDS resources block (~lines 430-525) and its `_tags`/`_retain`/`_snapshot` helpers + tag constants. Mirror its DBInstance config exactly EXCEPT `DeletionProtection`.
- `src/cardinal_cfn/children/security.py` ~lines 428-446 — the `Rds5432From<Tier>` SecurityGroupIngress shape (GroupId = RDS SG, IpProtocol tcp, FromPort/ToPort 5432, SourceSecurityGroupId = the tier task SG, Description).
- `src/cardinal_cfn/satellite_infra_base.py` — house style for a standalone generator.

## Parameters

| Name | Type | Default | Notes |
|---|---|---|---|
| `VpcId` | AWS::EC2::VPC::Id | — | |
| `PrivateSubnetsCsv` | String | — | subnet group; `Split(",", ...)` |
| `DBEngineVersion` | String | `18.4` | |
| `DBInstanceClass` | String | `db.r7g.large` | |
| `DBAllocatedStorage` | Number | `100` | |
| `MigrationSecurityGroupId` | AWS::EC2::SecurityGroup::Id | — | from base |
| `QuerySecurityGroupId` | AWS::EC2::SecurityGroup::Id | — | from base |
| `ProcessSecurityGroupId` | AWS::EC2::SecurityGroup::Id | — | from base |
| `ControlSecurityGroupId` | AWS::EC2::SecurityGroup::Id | — | from base |
| `MaestroSecurityGroupId` | AWS::EC2::SecurityGroup::Id | — | from base |

Group params with `add_parameter_group_metadata` (DB sizing / DB clients).

## Resources

1. `RdsSecurityGroup` (EC2 SG): `GroupDescription` ("Cardinal lakerunner RDS; ingress added per DB-client tier"), `VpcId=Ref(VpcId)`, egress all (mirror infra's). Policy: Delete.
2. Five `SecurityGroupIngress` — `Rds5432FromMigration/Query/Process/Control/Maestro`: `GroupId=Ref(RdsSecurityGroup)`, `IpProtocol="tcp"`, `FromPort=5432`, `ToPort=5432`, `SourceSecurityGroupId=Ref(<TierSecurityGroupId>)`, `Description`. (Use a Python loop over a list of (title, param) pairs.)
3. `DBSubnetGroup`: `SubnetIds=Split(",", Ref("PrivateSubnetsCsv"))`. Policy: Delete.
4. `DBMasterSecret` (SecretsManager Secret): `GenerateSecretString` username lakerunner / password 40 / ExcludePunctuation. Policy: Retain.
5. `DBInstance`: mirror infra exactly — Engine postgres, EngineVersion Ref, DBInstanceClass Ref, AllocatedStorage Ref, StorageType gp3, StorageEncrypted True, DBName "lakerunner", Port 5432, MasterUsername lakerunner, MasterUserPassword `{{resolve:secretsmanager:...}}`, DBSubnetGroupName Ref, VPCSecurityGroups [Ref(RdsSecurityGroup)], PubliclyAccessible False, MultiAZ False, BackupRetentionPeriod 7, **DeletionProtection=False**, Tags. Policy: Snapshot.
6. `DBMasterSecretAttachment` (SecretTargetAttachment): SecretId Ref(secret), TargetId Ref(db), TargetType AWS::RDS::DBInstance.

## Outputs

- `DbEndpoint` = `GetAtt(db, "Endpoint.Address")`
- `DbPort` = `GetAtt(db, "Endpoint.Port")`
- `DbName` = `"lakerunner"`
- `DbMasterSecretArn` = `Ref(secret)`
- `RdsSecurityGroupId` = `Ref(rds_sg)` (so anything else that needs to reach RDS can be wired)

## Tasks (TDD; commit after each)

1. Scaffold + params + build wiring (build.sh generation + cfn-lint list; Makefile lint). Tests: `test_required_parameters`, `test_db_defaults` (EngineVersion 18.4, InstanceClass db.r7g.large, AllocatedStorage 100). Commit.
2. RDS SG + 5 ingress rules. Tests: `test_rds_sg_present`, `test_five_db_client_ingress_rules` (5 SecurityGroupIngress on 5432 GroupId=RDS SG, sources are the 5 tier SG refs), `test_otel_has_no_db_ingress` (no rule sources an OtelSecurityGroupId — there is no such param). Commit.
3. Subnet group + master secret + DB instance + attachment. Tests: `test_db_is_snapshot_policy` (DeletionPolicy Snapshot), `test_db_deletion_protection_disabled` (Properties.DeletionProtection is False), `test_db_encrypted_and_private` (StorageEncrypted True, PubliclyAccessible False), `test_master_secret_retained`. Commit.
4. Outputs. Test `test_outputs_present` (DbEndpoint, DbPort, DbName, DbMasterSecretArn, RdsSecurityGroupId). Commit.
5. Build + cfn-lint + full suite green. Commit fixups if any.

## Self-review notes

- `DeletionProtection=False` is intentional (trial teardown); the Snapshot policy preserves data. Call this out in a code comment.
- Confirm cfn-lint is clean; an RDS instance with a resolve-secret password and gp3 storage lints clean in `cardinal_infrastructure.py`, so parity should too.
