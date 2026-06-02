# Convert lakerunner-infra-rds to Aurora PostgreSQL (provisioned, r8g)

## Goal

Replace the single `AWS::RDS::DBInstance` in the `cardinal-lakerunner-infra-rds`
stack with a provisioned **Aurora PostgreSQL** cluster + one writer instance, and
default the writer to `db.r8g.large`. The stack/module/template/driver names are
kept (`infra-rds`) so all downstream wiring is untouched.

This is a resource-type **replacement**, not an in-place update. New installs get
Aurora fresh; migrating an existing RDS install's data (snapshot/dump → Aurora) is
out of scope.

## Generator changes (`src/cardinal_cfn/lakerunner_infra_rds.py`)

Unchanged: RDS security group + five per-tier 5432 ingress rules, `DBSubnetGroup`,
`DBMasterSecret` (Retain, named `cardinal-db-master`).

Replace `DBInstance` with two resources:

- `DBCluster` (`AWS::RDS::DBCluster`) — holds the data and all cluster-level
  settings: `Engine="aurora-postgresql"`, `EngineVersion=Ref(DBEngineVersion)`,
  `DatabaseName="lakerunner"`, `MasterUsername="lakerunner"`, `MasterUserPassword`
  resolved from the secret, `Port=5432`, `DBSubnetGroupName=Ref(DBSubnetGroup)`,
  `VpcSecurityGroupIds=[Ref(RdsSecurityGroup)]`, `StorageEncrypted=True`,
  `BackupRetentionPeriod=7`, `DeletionProtection=False`. Policy: **Snapshot**.
- `DBInstance` (writer) — stateless compute: `Engine="aurora-postgresql"`,
  `DBInstanceClass=Ref(DBInstanceClass)`, `DBClusterIdentifier=Ref(DBCluster)`,
  `PubliclyAccessible=False`. Policy: **Delete**. (Aurora instances inherit the
  subnet group and security groups from the cluster, so those are not set here.)

`SecretTargetAttachment` retargets to the cluster: `TargetId=Ref(DBCluster)`,
`TargetType="AWS::RDS::DBCluster"`. It fills the secret's host/port/dbname from the
cluster **writer** endpoint — the lakerunner containers consume those unchanged.

Outputs `DbEndpoint` / `DbPort` become `GetAtt(DBCluster, "Endpoint.Address")` /
`GetAtt(DBCluster, "Endpoint.Port")` (writer endpoint). `DbName`,
`DbMasterSecretArn`, `RdsSecurityGroupId` are unchanged — downstream parameter
wiring (`FROM_STACKS` into the services stack) is unaffected.

### Parameter changes

| Parameter | Old default | New default |
|---|---|---|
| `DBInstanceClass` | `db.r7g.large` | `db.r8g.large` |
| `DBEngineVersion` | `18.4` | `17.9` (latest GA Aurora PG major; `db.r8g.large` verified orderable in us-east-1) |
| `DBAllocatedStorage` | `100` | removed (Aurora manages storage) |

Drop `DBAllocatedStorage` from the parameter list and the "DB Sizing" parameter
group. Module docstring/description strings change "RDS Postgres instance" to
"Aurora PostgreSQL cluster".

## Driver part (`scripts-src/parts/deploy-lakerunner-infra-rds.sh`)

Remove the `DB_ALLOCATED_STORAGE` → `DBAllocatedStorage` passthrough and its usage
line; refresh the `DB_INSTANCE_CLASS` / `DB_ENGINE_VERSION` default hints
(`db.r8g.large` / `17.9`). Regenerate `scripts/deploy-lakerunner-infra-rds.sh` with
`make scripts`; the drift test enforces the result.

## Tests (`tests/templates/test_lakerunner_infra_rds.py`)

- `test_required_parameters`: drop `DBAllocatedStorage`.
- `test_db_defaults`: `DBEngineVersion == "17.9"`, `DBInstanceClass == "db.r8g.large"`,
  remove the AllocatedStorage assertion.
- Snapshot/DeletionProtection/encryption/master-password tests retarget to the
  cluster; `PubliclyAccessible` asserts on the writer instance.
- Add: cluster `Engine == "aurora-postgresql"`; a writer `DBInstance` with
  `Engine == "aurora-postgresql"`, `DBClusterIdentifier == {"Ref": "DBCluster"}`,
  and `DeletionPolicy == "Delete"`.

## Docs

`docs/operations/jenkins-chained-deploy.md`: drop the `DB_ALLOCATED_STORAGE` row and
update the `DB_INSTANCE_CLASS` / `DB_ENGINE_VERSION` template-default hints.

## Out of scope

Reader replicas, Serverless v2, Global database, cluster parameter groups; existing
RDS → Aurora data migration; Aurora-awareness in `cleanup_script.py` (the chained
stack is torn down by CFN `delete-stack`, which honors the cluster's Snapshot
policy). Legacy `cardinal_infrastructure.py` monolith and its docs are untouched.
