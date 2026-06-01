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
        "DBAllocatedStorage",
        "MigrationSecurityGroupId",
        "QuerySecurityGroupId",
        "ProcessSecurityGroupId",
        "ControlSecurityGroupId",
        "MaestroSecurityGroupId",
    ):
        assert n in td["Parameters"], f"missing parameter: {n}"


def test_db_defaults(td):
    assert td["Parameters"]["DBEngineVersion"]["Default"] == "18.4"
    assert td["Parameters"]["DBInstanceClass"]["Default"] == "db.r7g.large"
    assert td["Parameters"]["DBAllocatedStorage"]["Default"] == 100


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


def test_db_is_snapshot_policy(td):
    db = td["Resources"]["DBInstance"]
    assert db["DeletionPolicy"] == "Snapshot"
    assert db["UpdateReplacePolicy"] == "Snapshot"


def test_db_deletion_protection_disabled(td):
    """Trial teardown relies on DeletionProtection=False; Snapshot preserves data."""
    assert td["Resources"]["DBInstance"]["Properties"]["DeletionProtection"] is False


def test_db_encrypted_and_private(td):
    props = td["Resources"]["DBInstance"]["Properties"]
    assert props["StorageEncrypted"] is True
    assert props["PubliclyAccessible"] is False


def test_master_secret_retained(td):
    secret = td["Resources"]["DBMasterSecret"]
    assert secret["DeletionPolicy"] == "Retain"
    assert secret["UpdateReplacePolicy"] == "Retain"


def test_rds_sg_and_subnet_group_are_delete_policy(td):
    for r in ("RdsSecurityGroup", "DBSubnetGroup"):
        assert td["Resources"][r]["DeletionPolicy"] == "Delete"
        assert td["Resources"][r]["UpdateReplacePolicy"] == "Delete"


def test_master_password_resolves_from_secret(td):
    pw = td["Resources"]["DBInstance"]["Properties"]["MasterUserPassword"]
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
