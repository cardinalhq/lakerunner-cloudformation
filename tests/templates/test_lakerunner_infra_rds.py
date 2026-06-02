"""Tests for the cardinal-lakerunner-infra-rds standalone template."""

import json

import pytest

from cardinal_cfn import lakerunner_infra_rds


@pytest.fixture
def td():
    return json.loads(lakerunner_infra_rds.build().to_json())


# ---------------------------------------------------------------------------
# Task 1: parameters + defaults
# ---------------------------------------------------------------------------


def test_required_parameters(td):
    for n in (
        "VpcId",
        "PrivateSubnetsCsv",
        "DBEngineVersion",
        "DBInstanceClass",
        "MigrationSecurityGroupId",
        "QuerySecurityGroupId",
        "ProcessSecurityGroupId",
        "ControlSecurityGroupId",
        "MaestroSecurityGroupId",
    ):
        assert n in td["Parameters"], f"missing parameter: {n}"


def test_storage_param_removed(td):
    """Aurora manages storage; the AllocatedStorage knob is gone."""
    assert "DBAllocatedStorage" not in td["Parameters"]


def test_db_defaults(td):
    assert td["Parameters"]["DBEngineVersion"]["Default"] == "17.9"
    assert td["Parameters"]["DBInstanceClass"]["Default"] == "db.r8g.large"


# ---------------------------------------------------------------------------
# Task 2: RDS SG + 5 ingress rules
# ---------------------------------------------------------------------------


def test_rds_sg_present(td):
    assert "RdsSecurityGroup" in td["Resources"]
    sg = td["Resources"]["RdsSecurityGroup"]
    assert sg["Type"] == "AWS::EC2::SecurityGroup"
    props = sg["Properties"]
    assert props["VpcId"] == {"Ref": "VpcId"}


def test_five_db_client_ingress_rules(td):
    """Five SecurityGroupIngress resources on port 5432; GroupId = RDS SG."""
    ingress_resources = {
        name: r
        for name, r in td["Resources"].items()
        if r["Type"] == "AWS::EC2::SecurityGroupIngress"
        and r["Properties"].get("FromPort") == 5432
    }
    assert len(ingress_resources) == 5, (
        f"expected 5 ingress rules on 5432, got {len(ingress_resources)}: "
        f"{list(ingress_resources)}"
    )
    for name, r in ingress_resources.items():
        props = r["Properties"]
        assert props["GroupId"] == {"Ref": "RdsSecurityGroup"}, (
            f"{name}: GroupId should reference RdsSecurityGroup"
        )
        assert props["ToPort"] == 5432
        assert props["IpProtocol"] == "tcp"

    # verify each tier SG is a source
    sources = [
        r["Properties"]["SourceSecurityGroupId"]
        for r in ingress_resources.values()
    ]
    for param in (
        "MigrationSecurityGroupId",
        "QuerySecurityGroupId",
        "ProcessSecurityGroupId",
        "ControlSecurityGroupId",
        "MaestroSecurityGroupId",
    ):
        assert {"Ref": param} in sources, (
            f"no ingress rule sources {param}"
        )


def test_otel_has_no_db_ingress(td):
    """otel has no DB dependency; no OtelSecurityGroupId param or ingress rule."""
    assert "OtelSecurityGroupId" not in td.get("Parameters", {})
    for name, r in td["Resources"].items():
        if r["Type"] == "AWS::EC2::SecurityGroupIngress":
            src = r["Properties"].get("SourceSecurityGroupId", {})
            assert src != {"Ref": "OtelSecurityGroupId"}, (
                f"{name} unexpectedly references OtelSecurityGroupId"
            )


# ---------------------------------------------------------------------------
# Task 3: DB resources lifecycle/settings
# ---------------------------------------------------------------------------


def test_aurora_cluster_engine(td):
    """The data-bearing resource is an Aurora PostgreSQL cluster."""
    cluster = td["Resources"]["DBCluster"]
    assert cluster["Type"] == "AWS::RDS::DBCluster"
    assert cluster["Properties"]["Engine"] == "aurora-postgresql"


def test_writer_instance_is_aurora_and_joins_cluster(td):
    """The writer is a stateless Aurora instance pointed at the cluster, with a
    Delete policy (the cluster, not the instance, owns the data)."""
    inst = td["Resources"]["DBInstance"]
    assert inst["Type"] == "AWS::RDS::DBInstance"
    props = inst["Properties"]
    assert props["Engine"] == "aurora-postgresql"
    assert props["DBClusterIdentifier"] == {"Ref": "DBCluster"}
    assert props["DBInstanceClass"] == {"Ref": "DBInstanceClass"}
    assert inst["DeletionPolicy"] == "Delete"
    assert inst["UpdateReplacePolicy"] == "Delete"


def test_db_is_snapshot_policy(td):
    """The cluster holds the data, so it carries the Snapshot policy."""
    db = td["Resources"]["DBCluster"]
    assert db["DeletionPolicy"] == "Snapshot"
    assert db["UpdateReplacePolicy"] == "Snapshot"


def test_db_deletion_protection_disabled(td):
    """Trial teardown relies on DeletionProtection=False; Snapshot preserves data."""
    assert td["Resources"]["DBCluster"]["Properties"]["DeletionProtection"] is False


def test_db_encrypted_and_private(td):
    """StorageEncrypted is a cluster setting; the writer stays non-public."""
    assert td["Resources"]["DBCluster"]["Properties"]["StorageEncrypted"] is True
    assert td["Resources"]["DBInstance"]["Properties"]["PubliclyAccessible"] is False


def test_secret_attaches_to_cluster(td):
    """The secret target is the cluster, so host resolves to the writer endpoint."""
    att = td["Resources"]["DBMasterSecretAttachment"]["Properties"]
    assert att["TargetType"] == "AWS::RDS::DBCluster"
    assert att["TargetId"] == {"Ref": "DBCluster"}


def test_master_secret_retained(td):
    secret = td["Resources"]["DBMasterSecret"]
    assert secret["DeletionPolicy"] == "Retain"
    assert secret["UpdateReplacePolicy"] == "Retain"


def test_master_secret_named_cardinal_db_master(td):
    """Explicit name lets lakerunner-infra-base scope to the cardinal-* pattern."""
    assert td["Resources"]["DBMasterSecret"]["Properties"]["Name"] == "cardinal-db-master"


def test_rds_sg_and_subnet_group_are_delete_policy(td):
    for r in ("RdsSecurityGroup", "DBSubnetGroup"):
        assert td["Resources"][r]["DeletionPolicy"] == "Delete"
        assert td["Resources"][r]["UpdateReplacePolicy"] == "Delete"


def test_master_password_resolves_from_secret(td):
    pw = td["Resources"]["DBCluster"]["Properties"]["MasterUserPassword"]
    sub = pw["Fn::Sub"]
    template_str = sub[0] if isinstance(sub, list) else sub
    assert "resolve:secretsmanager:" in template_str
    assert "::password}}" in template_str


# ---------------------------------------------------------------------------
# Task 4: outputs
# ---------------------------------------------------------------------------


def test_outputs_present(td):
    for o in (
        "DbEndpoint",
        "DbPort",
        "DbName",
        "DbMasterSecretArn",
        "RdsSecurityGroupId",
    ):
        assert o in td["Outputs"], f"missing output: {o}"
