"""Tests for the security child stack: SGs, IAM roles, RDS ingress rules."""

import json

import pytest

from cardinal_cfn.children import security


@pytest.fixture
def td():
    return json.loads(security.build().to_json())


def _types(td):
    return [r["Type"] for r in td["Resources"].values()]


def test_required_parameters(td):
    for n in (
        "VpcId",
        "AlbAllowedCidr1",
        "AlbAllowedCidr2",
        "AlbAllowedCidr3",
        "RdsSecurityGroupId",
        "ClusterArn",
        "BucketName",
        "QueueArn",
        "DbMasterSecretArn",
        "LicenseSecretArn",
        "AdminKeySecretArn",
        "StorageProfilesParamName",
        "ApiKeysParamName",
    ):
        assert n in td["Parameters"], f"missing parameter: {n}"


def test_creates_seven_security_groups(td):
    """1 ALB SG + 6 task SGs (migration, query, process, control, otel, maestro)."""
    types = _types(td)
    assert types.count("AWS::EC2::SecurityGroup") == 7


def test_named_security_groups_present(td):
    for logical_id in (
        "AlbSecurityGroup",
        "MigrationSecurityGroup",
        "QuerySecurityGroup",
        "ProcessSecurityGroup",
        "ControlSecurityGroup",
        "OtelSecurityGroup",
        "MaestroSecurityGroup",
    ):
        assert logical_id in td["Resources"], f"missing SG: {logical_id}"
        assert td["Resources"][logical_id]["Type"] == "AWS::EC2::SecurityGroup"


def test_creates_seven_iam_roles(td):
    """1 shared execution role + 6 per-tier task roles."""
    types = _types(td)
    assert types.count("AWS::IAM::Role") == 7


def test_named_iam_roles_present(td):
    for logical_id in (
        "ExecutionRole",
        "MigrationRole",
        "QueryRole",
        "ProcessRole",
        "ControlRole",
        "OtelRole",
        "MaestroRole",
    ):
        assert logical_id in td["Resources"], f"missing role: {logical_id}"
        assert td["Resources"][logical_id]["Type"] == "AWS::IAM::Role"


def test_execution_role_uses_managed_ecs_policy(td):
    role = td["Resources"]["ExecutionRole"]["Properties"]
    managed = role.get("ManagedPolicyArns", [])
    assert (
        "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
        in managed
    )


def test_execution_role_can_read_each_infra_secret_arn(td):
    """The execution role pulls task `secrets:` env vars at container start.

    DBMasterSecret is CFN-generated and does NOT match a `cardinal-*` prefix,
    so its ARN must be granted explicitly via Ref(DbMasterSecretArn). Same
    for License and AdminKey (they happen to have cardinal-* names but the
    template should rely on the parameter ARNs, not a wildcard).
    """
    role = td["Resources"]["ExecutionRole"]["Properties"]
    policies = role.get("Policies", [])
    assert policies, "ExecutionRole must declare inline policies"

    secret_stmts = []
    for p in policies:
        for s in p.get("PolicyDocument", {}).get("Statement", []):
            if s.get("Sid") == "ResolveCardinalSecrets":
                secret_stmts.append(s)
    assert len(secret_stmts) == 1, "expected exactly one ResolveCardinalSecrets statement"

    stmt = secret_stmts[0]
    resources = stmt["Resource"]
    assert isinstance(resources, list), (
        "ResolveCardinalSecrets.Resource must be a list of specific secret ARNs, "
        "not a wildcard -- DBMasterSecret has a CFN-generated name and the "
        "cardinal-* wildcard does not match it"
    )
    refs = {tuple(sorted(r.items())) if isinstance(r, dict) else r for r in resources}
    for param in ("DbMasterSecretArn", "LicenseSecretArn", "AdminKeySecretArn"):
        assert (("Ref", param),) in refs, (
            f"ExecutionRole must grant secrets:GetSecretValue on Ref({param}); "
            f"got resources={resources}"
        )


def test_all_task_roles_trust_ecs_tasks(td):
    for role_id in (
        "MigrationRole", "QueryRole", "ProcessRole",
        "ControlRole", "OtelRole", "MaestroRole",
        "ExecutionRole",
    ):
        trust = td["Resources"][role_id]["Properties"]["AssumeRolePolicyDocument"]
        statements = trust["Statement"]
        assert any(
            s.get("Principal", {}).get("Service") == "ecs-tasks.amazonaws.com"
            for s in statements
        ), f"{role_id} must trust ecs-tasks.amazonaws.com"


def test_rds_ingress_rules_for_each_db_tier(td):
    """Five tiers (Migration, Query, Process, Control, Maestro) talk to RDS.

    OTel does not. Each gets a SecurityGroupIngress resource adding port
    5432 to the customer's RDS SG.
    """
    rds_ingresses = [
        (lid, r) for lid, r in td["Resources"].items()
        if r["Type"] == "AWS::EC2::SecurityGroupIngress"
        and lid.startswith("Rds5432From")
    ]
    titles = {lid for lid, _ in rds_ingresses}
    assert titles == {
        "Rds5432FromMigration",
        "Rds5432FromQuery",
        "Rds5432FromProcess",
        "Rds5432FromControl",
        "Rds5432FromMaestro",
    }
    for lid, r in rds_ingresses:
        props = r["Properties"]
        assert props["FromPort"] == 5432
        assert props["ToPort"] == 5432
        assert props["IpProtocol"] == "tcp"
        assert props["GroupId"] == {"Ref": "RdsSecurityGroupId"}, (
            f"{lid} must target the customer's RDS SG, got {props['GroupId']!r}"
        )


def test_otel_does_not_get_rds_ingress(td):
    """OTel collector does not need DB access."""
    assert "Rds5432FromOtel" not in td["Resources"]


def test_alb_ingress_only_when_cidr_set(td):
    """Each ALB ingress rule is conditional on its CIDR slot being non-empty."""
    for lid, r in td["Resources"].items():
        if r["Type"] != "AWS::EC2::SecurityGroupIngress":
            continue
        if not lid.startswith("AlbIngress"):
            continue
        assert "Condition" in r, f"{lid} must be guarded by a HasAlbCidr* condition"


def test_alb_listener_ports_covered(td):
    """One ingress rule per (port, cidr-slot): 3 ports x 3 slots = 9 rules."""
    alb_ports = sorted({
        r["Properties"]["FromPort"]
        for lid, r in td["Resources"].items()
        if lid.startswith("AlbIngress")
    })
    assert alb_ports == [443, 4318, 9443]


def test_alb_to_query_ingress(td):
    rule = td["Resources"]["QueryFromAlb"]["Properties"]
    assert rule["FromPort"] == 8080
    assert rule["SourceSecurityGroupId"] == {"Ref": "AlbSecurityGroup"}


def test_query_self_ingress_to_worker(td):
    rule = td["Resources"]["QueryWorkerFromQuery"]["Properties"]
    assert rule["FromPort"] == 8081
    assert rule["SourceSecurityGroupId"] == {"Ref": "QuerySecurityGroup"}


def test_alb_to_admin_api(td):
    rule = td["Resources"]["ControlAdminApiFromAlb"]["Properties"]
    assert rule["FromPort"] == 9091
    assert rule["SourceSecurityGroupId"] == {"Ref": "AlbSecurityGroup"}


def test_alb_to_otel_4318(td):
    rule = td["Resources"]["OtelFromAlb"]["Properties"]
    assert rule["FromPort"] == 4318
    assert rule["SourceSecurityGroupId"] == {"Ref": "AlbSecurityGroup"}


def test_self_telemetry_otel_ingress(td):
    """Query / process / control / maestro all reach the otel collector on 4318."""
    for source in ("Query", "Process", "Control", "Maestro"):
        rule = td["Resources"][f"OtelFrom{source}"]["Properties"]
        assert rule["FromPort"] == 4318
        assert rule["SourceSecurityGroupId"] == {"Ref": f"{source}SecurityGroup"}


def test_alb_to_maestro_ports(td):
    ui = td["Resources"]["MaestroUiFromAlb"]["Properties"]
    assert ui["FromPort"] == 4200
    dex = td["Resources"]["MaestroDexFromAlb"]["Properties"]
    assert dex["FromPort"] == 5556


def test_outputs(td):
    expected = {
        "AlbSecurityGroupId",
        "MigrationSecurityGroupId",
        "QuerySecurityGroupId",
        "ProcessSecurityGroupId",
        "ControlSecurityGroupId",
        "OtelSecurityGroupId",
        "MaestroSecurityGroupId",
        "ExecutionRoleArn",
        "MigrationRoleArn",
        "QueryRoleArn",
        "ProcessRoleArn",
        "ControlRoleArn",
        "OtelRoleArn",
        "MaestroRoleArn",
    }
    assert set(td["Outputs"].keys()) == expected


def test_process_role_has_bedrock_and_sqs_consume(td):
    """Process tier must invoke Bedrock and consume the ingest SQS queue."""
    role = td["Resources"]["ProcessRole"]["Properties"]
    statements = role["Policies"][0]["PolicyDocument"]["Statement"]
    actions = []
    for s in statements:
        a = s["Action"]
        actions.extend(a if isinstance(a, list) else [a])
    assert "bedrock:InvokeModel" in actions
    assert "sqs:ReceiveMessage" in actions
    assert "s3:PutObject" in actions
    assert "s3:DeleteObject" in actions


def test_control_role_has_ecs_update_service(td):
    """Control tier (monitoring autoscaler) needs ecs:UpdateService."""
    role = td["Resources"]["ControlRole"]["Properties"]
    statements = role["Policies"][0]["PolicyDocument"]["Statement"]
    actions = []
    for s in statements:
        a = s["Action"]
        actions.extend(a if isinstance(a, list) else [a])
    assert "ecs:UpdateService" in actions


def test_query_role_has_ecs_describe_tasks(td):
    """Query tier (query-api worker discovery) needs ecs:DescribeTasks."""
    role = td["Resources"]["QueryRole"]["Properties"]
    statements = role["Policies"][0]["PolicyDocument"]["Statement"]
    actions = []
    for s in statements:
        a = s["Action"]
        actions.extend(a if isinstance(a, list) else [a])
    assert "ecs:DescribeTasks" in actions
    assert "ecs:ListTasks" in actions


def test_otel_role_is_minimal(td):
    """OTel role only reads the license + writes CW logs."""
    role = td["Resources"]["OtelRole"]["Properties"]
    statements = role["Policies"][0]["PolicyDocument"]["Statement"]
    actions = []
    for s in statements:
        a = s["Action"]
        actions.extend(a if isinstance(a, list) else [a])
    # No S3, no SQS, no Bedrock, no ECS.
    forbidden = {"s3:GetObject", "sqs:ReceiveMessage", "bedrock:InvokeModel",
                 "ecs:UpdateService", "ecs:DescribeTasks"}
    assert not (forbidden & set(actions)), (
        f"otel role overreaches: {forbidden & set(actions)!r}"
    )
