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


def test_internet_facing_ingress_rules_per_alb_port(td):
    """When AlbScheme=internet-facing, the Security child adds a 0.0.0.0/0
    ingress rule on every ALB port (443, 9443, 4318) gated by the
    AlbIsInternetFacing condition. Operators who leave AlbScheme=internal
    don't get these rules (the resource is conditional)."""
    expected_ports = {443, 9443, 4318}
    rules = {}
    for r in td["Resources"].values():
        if r["Type"] == "AWS::EC2::SecurityGroupIngress" and \
           r.get("Condition") == "AlbIsInternetFacing":
            rules[r["Properties"]["FromPort"]] = r["Properties"]
    assert set(rules.keys()) == expected_ports, (
        f"missing internet-facing ALB ingress rule on ports "
        f"{expected_ports - set(rules.keys())!r}"
    )
    for port, props in rules.items():
        assert props["CidrIp"] == "0.0.0.0/0", (
            f"internet-facing ingress on {port} must be 0.0.0.0/0; "
            f"got {props['CidrIp']!r}"
        )
        assert props["GroupId"] == {"Ref": "AlbSecurityGroup"}


def test_alb_scheme_condition_present(td):
    assert "AlbIsInternetFacing" in td.get("Conditions", {})


def test_alb_to_otel_health_13133(td):
    """The OTel target group health-checks on port 13133, not 4318. Without
    an ALB-SG -> OTel-SG rule on 13133 the ECS task is marked unhealthy by
    the ALB and the deployment circuit breaker rolls the stack back.

    OTel is the only tier that uses HealthCheckPort != traffic-port, so the
    other tiers do not need a separate health-port ingress rule (the
    data-plane rule already covers them)."""
    rule = td["Resources"]["OtelHealthFromAlb"]["Properties"]
    assert rule["FromPort"] == 13133
    assert rule["ToPort"] == 13133
    assert rule["SourceSecurityGroupId"] == {"Ref": "AlbSecurityGroup"}
    assert rule["GroupId"] == {"Ref": "OtelSecurityGroup"}


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


# ---------------------------------------------------------------------------
# Per-tier role contract -- the source-of-truth audit lives in
# docs/operations/iam-roles.md. Each tier role is asserted both *positively*
# (must have these actions, scoped where AWS permits) and *negatively* (must
# NOT have these actions, to keep blast radius tight). If you add a new
# permission to a tier role, update the doc + this table + the matching
# helper below in the same change.
# ---------------------------------------------------------------------------

# Set of forbidden actions used by `test_otel_role_is_minimal` below.
_FORBIDDEN_FOR_OTEL = {
    "s3:GetObject", "s3:ListBucket", "s3:DeleteObject",
    "sqs:ReceiveMessage", "bedrock:InvokeModel",
    "ecs:UpdateService", "ecs:DescribeTasks",
}


def _role_actions(td: dict, role_id: str) -> set[str]:
    role = td["Resources"][role_id]["Properties"]
    actions: list[str] = []
    for p in role.get("Policies", []) or []:
        for s in p.get("PolicyDocument", {}).get("Statement", []):
            a = s.get("Action", [])
            actions.extend(a if isinstance(a, list) else [a])
    return set(actions)


# Per-role (required-actions, forbidden-actions) contract derived from the
# IAM-roles doc. Adjust both this table and the doc when permissions change.
_TIER_ROLE_CONTRACT = {
    "MigrationRole": (
        {  # required
            "secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret",
            "ssm:GetParameter", "ssm:GetParameters",
            "logs:CreateLogStream", "logs:PutLogEvents",
        },
        {  # forbidden
            "s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket",
            "sqs:ReceiveMessage", "bedrock:InvokeModel",
            "ecs:UpdateService", "ecs:DescribeTasks",
        },
    ),
    "QueryRole": (
        {
            "secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret",
            "ssm:GetParameter", "ssm:GetParameters",
            "s3:GetObject", "s3:ListBucket", "s3:GetBucketLocation",
            "logs:CreateLogStream", "logs:PutLogEvents",
            "ecs:DescribeTasks", "ecs:ListTasks", "ecs:DescribeServices",
        },
        {
            "s3:PutObject", "s3:DeleteObject",
            "sqs:ReceiveMessage", "bedrock:InvokeModel",
            "ecs:UpdateService",
        },
    ),
    "ProcessRole": (
        {
            "secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret",
            "ssm:GetParameter", "ssm:GetParameters",
            "s3:GetObject", "s3:PutObject", "s3:DeleteObject",
            "s3:ListBucket", "s3:GetBucketLocation",
            "sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes",
            "bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream",
            "logs:CreateLogStream", "logs:PutLogEvents",
        },
        {
            "ecs:UpdateService", "ecs:DescribeTasks",
        },
    ),
    "ControlRole": (
        {
            "secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret",
            "ssm:GetParameter", "ssm:GetParameters",
            "s3:GetObject", "s3:ListBucket", "s3:DeleteObject",
            "ecs:UpdateService", "ecs:DescribeServices",
            "logs:CreateLogStream", "logs:PutLogEvents",
        },
        {
            "s3:PutObject",  # sweeper/alert-evaluator never write to S3
            "sqs:ReceiveMessage", "bedrock:InvokeModel",
        },
    ),
    "OtelRole": (
        {
            "secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret",
            "s3:PutObject",
            "logs:CreateLogStream", "logs:PutLogEvents",
        },
        _FORBIDDEN_FOR_OTEL,
    ),
    "MaestroRole": (
        {
            "secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret",
            "ssm:GetParameter", "ssm:GetParameters",
            "logs:CreateLogStream", "logs:PutLogEvents",
        },
        {
            "s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket",
            "sqs:ReceiveMessage", "bedrock:InvokeModel",
            "ecs:UpdateService", "ecs:DescribeTasks",
        },
    ),
}


@pytest.mark.parametrize("role_id", sorted(_TIER_ROLE_CONTRACT.keys()))
def test_tier_role_required_actions_present(td, role_id):
    required, _ = _TIER_ROLE_CONTRACT[role_id]
    actions = _role_actions(td, role_id)
    missing = required - actions
    assert not missing, (
        f"{role_id} is missing required actions: {sorted(missing)!r}. "
        f"See docs/operations/iam-roles.md for the contract."
    )


@pytest.mark.parametrize("role_id", sorted(_TIER_ROLE_CONTRACT.keys()))
def test_tier_role_forbidden_actions_absent(td, role_id):
    _, forbidden = _TIER_ROLE_CONTRACT[role_id]
    actions = _role_actions(td, role_id)
    overreach = forbidden & actions
    assert not overreach, (
        f"{role_id} overreaches with actions it should not have: "
        f"{sorted(overreach)!r}. See docs/operations/iam-roles.md."
    )


def test_otel_role_is_minimal(td):
    """OTel role only reads the license, writes CW logs, and puts raw OTLP
    signals into the ``otel-raw/`` prefix of the ingest bucket. It must NOT
    have read/list/delete on the bucket, SQS consume, Bedrock, or ECS API."""
    role = td["Resources"]["OtelRole"]["Properties"]
    statements = role["Policies"][0]["PolicyDocument"]["Statement"]
    actions = []
    for s in statements:
        a = s["Action"]
        actions.extend(a if isinstance(a, list) else [a])
    assert not (_FORBIDDEN_FOR_OTEL & set(actions)), (
        f"otel role overreaches: {_FORBIDDEN_FOR_OTEL & set(actions)!r}"
    )
    # Must be able to PutObject into otel-raw/ (the awss3 exporter target).
    put_stmts = [s for s in statements if "s3:PutObject" in (
        s["Action"] if isinstance(s["Action"], list) else [s["Action"]]
    )]
    assert put_stmts, "OtelRole must grant s3:PutObject (for awss3 exporter)"
    for s in put_stmts:
        res = s["Resource"]
        res_strs = res if isinstance(res, list) else [res]
        for r in res_strs:
            if isinstance(r, dict) and "Fn::Sub" in r:
                template = r["Fn::Sub"]
                assert template.endswith("/otel-raw/*"), (
                    f"OtelRole PutObject resource must be scoped to otel-raw/* "
                    f"prefix; got: {template}"
                )
