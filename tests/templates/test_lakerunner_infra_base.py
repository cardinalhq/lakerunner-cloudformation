"""Tests for the cardinal-lakerunner-infra-base standalone template."""

import json

import pytest

from cardinal_cfn import lakerunner_infra_base


@pytest.fixture
def td():
    return json.loads(lakerunner_infra_base.build().to_json())


# ---------------------------------------------------------------------------
# Task 2: parameters + cooked-bucket default name
# ---------------------------------------------------------------------------


def test_required_parameters(td):
    for n in (
        "VpcId",
        "AlbAllowedCidr1",
        "AlbAllowedCidr2",
        "AlbAllowedCidr3",
        "AlbScheme",
        "ClusterArn",
        "CookedBucketName",
        "LicenseSecretName",
        "AdminKeySecretName",
        "LicenseData",
    ):
        assert n in td["Parameters"], f"missing parameter: {n}"


def test_no_org_content_params_or_resources(td):
    """infra-base seeds NO org content; Maestro owns it via /api/v1/provision.

    Lakerunner installs admin-key-only (ADMIN_INITIAL_API_KEY, already wired on
    admin-api); the org, its storage line, and its ingest key are provisioned by
    Maestro, not seeded here.
    """
    params = td["Parameters"]
    for gone in ("OrganizationId", "InitialIngestApiKey",
                 "StorageProfilesParamName", "ApiKeysParamName"):
        assert gone not in params, f"{gone} should be removed from infra-base"
    resources = td["Resources"]
    assert "StorageProfilesParam" not in resources
    assert "ApiKeysParam" not in resources
    outputs = td.get("Outputs", {})
    assert "StorageProfilesParamName" not in outputs
    assert "ApiKeysParamName" not in outputs


def test_dropped_rds_param(td):
    """security.py's RdsSecurityGroupId param is gone (RDS ingress lives in rds)."""
    assert "RdsSecurityGroupId" not in td["Parameters"]


def test_no_threaded_arn_params(td):
    """Name-pattern IAM means no threaded secret/queue ARN params."""
    for n in ("DbMasterSecretArn", "LicenseSecretArn", "AdminKeySecretArn",
              "QueueArn", "BucketName"):
        assert n not in td["Parameters"], f"unexpected threaded param: {n}"


def test_license_data_is_no_echo(td):
    assert td["Parameters"]["LicenseData"].get("NoEcho") is True


def test_cooked_bucket_default_name(td):
    assert td["Parameters"]["CookedBucketName"]["Default"] == ""
    # Default name is derived via the UseDefaultCookedBucketName condition.
    val = td["Resources"]["CookedBucket"]["Properties"]["BucketName"]
    assert val["Fn::If"][0] == "UseDefaultCookedBucketName"
    assert val["Fn::If"][1] == {
        "Fn::Sub": "cardinal-cooked-${AWS::AccountId}-${AWS::Region}"
    }


# ---------------------------------------------------------------------------
# Task 3: ALB SG + 6 task SGs + inter-tier ingress
# ---------------------------------------------------------------------------


def test_six_security_groups(td):
    sgs = {n for n, r in td["Resources"].items()
           if r["Type"] == "AWS::EC2::SecurityGroup"}
    assert sgs == {
        "AlbSecurityGroup",
        "MigrationSecurityGroup",
        "QuerySecurityGroup",
        "ProcessSecurityGroup",
        "ControlSecurityGroup",
        "MaestroSecurityGroup",
    }


def test_alb_ingress_on_https_and_otel_ports_from_cidrs(td):
    """ALB ingress on 443/9443/4318 sourced from AlbAllowedCidr params."""
    ingress = {
        n: r for n, r in td["Resources"].items()
        if r["Type"] == "AWS::EC2::SecurityGroupIngress"
        and r["Properties"].get("CidrIp", {}) != "0.0.0.0/0"
        and "CidrIp" in r["Properties"]
    }
    seen = {}
    for r in ingress.values():
        p = r["Properties"]
        if p["GroupId"] == {"Ref": "AlbSecurityGroup"}:
            seen.setdefault(p["FromPort"], set()).add(
                json.dumps(p["CidrIp"], sort_keys=True)
            )
    for port in (443, 9443, 4318):
        assert port in seen, f"no ALB CIDR ingress on {port}"
    # All three CIDR slots are wired: port 443 has three distinct sources,
    # one per AlbAllowedCidr1/2/3 param.
    assert len(seen[443]) == 3, f"expected 3 CIDR sources on 443, got {seen[443]}"
    for idx in (1, 2, 3):
        assert (
            json.dumps({"Ref": f"AlbAllowedCidr{idx}"}, sort_keys=True)
            in seen[443]
        ), f"AlbAllowedCidr{idx} not wired to ALB 443 ingress"


def test_internet_facing_alb_ingress_is_condition_gated(td):
    """The 0.0.0.0/0 ALB ingress rules only apply under internet-facing scheme,
    so internal-scheme deploys never expose 0.0.0.0/0."""
    for port in (443, 9443, 4318):
        rid = f"AlbIngress{port}FromInternet"
        r = td["Resources"][rid]
        assert r["Properties"]["CidrIp"] == "0.0.0.0/0"
        assert r["Condition"] == "AlbIsInternetFacing", (
            f"{rid} is not gated by AlbIsInternetFacing"
        )


def test_no_rds_ingress_rules(td):
    """security.py's Rds5432From* rules are dropped here."""
    for n, r in td["Resources"].items():
        if r["Type"] == "AWS::EC2::SecurityGroupIngress":
            assert r["Properties"].get("FromPort") != 5432, (
                f"{n}: unexpected 5432 ingress rule in base"
            )
            assert not n.startswith("Rds5432From")


def test_sibling_ingress_rule_exists(td):
    """A cross-tier SG-to-SG ingress rule (Maestro -> query-api) exists."""
    r = td["Resources"]["QueryFromMaestro"]["Properties"]
    assert r["GroupId"] == {"Ref": "QuerySecurityGroup"}
    assert r["SourceSecurityGroupId"] == {"Ref": "MaestroSecurityGroup"}
    assert r["FromPort"] == 8080


# ---------------------------------------------------------------------------
# Task 4: exec role + 5 task roles with name-pattern IAM
# ---------------------------------------------------------------------------


def _exec_statements(td):
    return td["Resources"]["ExecutionRole"]["Properties"]["Policies"][0][
        "PolicyDocument"
    ]["Statement"]


def _role_statements(td, role):
    return td["Resources"][role]["Properties"]["Policies"][0][
        "PolicyDocument"
    ]["Statement"]


def test_exec_role_secrets_scoped_to_cardinal_pattern(td):
    s = next(s for s in _exec_statements(td) if s["Sid"] == "ResolveCardinalSecrets")
    # Resource is a name-pattern Sub, not a threaded ARN Ref.
    res = s["Resource"]
    assert isinstance(res, dict) and "Fn::Sub" in res
    assert res["Fn::Sub"].endswith(":secret:cardinal-*")


def test_no_ssm_read_iam(td):
    """No SSM parameters remain, so no role grants ssm:GetParameter*."""
    assert not any(
        s.get("Sid") == "ResolveCardinalSsm" for s in _exec_statements(td)
    )
    for role in ("MigrationRole", "QueryRole", "ProcessRole", "ControlRole",
                 "MaestroRole"):
        assert not any(
            s.get("Sid") == "ReadSsmParams" for s in _role_statements(td, role)
        ), f"{role} still grants ssm:GetParameter*"


def test_all_task_roles_use_name_pattern_secrets(td):
    """No task role references a threaded secret ARN param."""
    for role in ("MigrationRole", "QueryRole", "ProcessRole", "ControlRole",
                 "MaestroRole"):
        stmts = _role_statements(td, role)
        secrets = next(s for s in stmts if s["Sid"] == "ReadSecrets")
        res = secrets["Resource"]
        assert isinstance(res, dict) and "Fn::Sub" in res
        assert res["Fn::Sub"].endswith(":secret:cardinal-*")
        blob = json.dumps(stmts)
        for threaded in ("DbMasterSecretArn", "LicenseSecretArn",
                         "AdminKeySecretArn"):
            assert threaded not in blob, f"{role} threads {threaded}"


def test_process_role_assumes_satellite_access_and_has_no_local_queue(td):
    stmts = _role_statements(td, "ProcessRole")
    assume = next(s for s in stmts if s["Sid"] == "AssumeSatelliteAccess")
    assert assume["Action"] == "sts:AssumeRole"
    assert assume["Resource"]["Fn::Sub"].endswith(
        "role/cardinal-satellite-access*"
    )
    blob = json.dumps(stmts)
    assert "QueueArn" not in blob
    assert "sqs:" not in blob, "process role should carry no local SQS perms"


def test_query_role_keeps_ecs_describe_condition(td):
    stmts = _role_statements(td, "QueryRole")
    desc = next(s for s in stmts if s["Sid"] == "DescribeWorkerTasks")
    assert desc["Condition"]["ArnEquals"]["ecs:cluster"] == {"Ref": "ClusterArn"}
    assert "ecs:DescribeServices" in desc["Action"]


def test_control_role_can_scale_with_cluster_condition(td):
    """The monitoring autoscaler's ecs:UpdateService must stay cluster-scoped."""
    stmts = _role_statements(td, "ControlRole")
    scale = next(
        s for s in stmts if "ecs:UpdateService" in s.get("Action", [])
    )
    assert scale["Condition"]["ArnEquals"]["ecs:cluster"] == {"Ref": "ClusterArn"}


def test_query_role_s3_targets_cooked_bucket(td):
    stmts = _role_statements(td, "QueryRole")
    s3 = next(s for s in stmts if s["Sid"] == "CookedBucketRead")
    blob = json.dumps(s3["Resource"])
    assert "cardinal-cooked-" in blob


def test_process_role_s3_readwrite_targets_cooked_bucket(td):
    stmts = _role_statements(td, "ProcessRole")
    s3 = next(s for s in stmts if s["Sid"] == "CookedBucketReadWrite")
    assert "s3:PutObject" in s3["Action"]
    assert "cardinal-cooked-" in json.dumps(s3["Resource"])


def test_exec_role_has_managed_execution_policy(td):
    arns = td["Resources"]["ExecutionRole"]["Properties"]["ManagedPolicyArns"]
    assert any("AmazonECSTaskExecutionRolePolicy" in a for a in arns)


# ---------------------------------------------------------------------------
# Task 5: cooked bucket + secrets + SSM params
# ---------------------------------------------------------------------------


def test_cooked_bucket_retain(td):
    b = td["Resources"]["CookedBucket"]
    assert b["DeletionPolicy"] == "Retain"
    assert b["UpdateReplacePolicy"] == "Retain"


def test_cooked_bucket_no_notification_or_lifecycle(td):
    props = td["Resources"]["CookedBucket"]["Properties"]
    assert "NotificationConfiguration" not in props
    assert "LifecycleConfiguration" not in props


def test_cooked_bucket_encrypted_and_pab(td):
    props = td["Resources"]["CookedBucket"]["Properties"]
    enc = props["BucketEncryption"]["ServerSideEncryptionConfiguration"][0]
    assert enc["ServerSideEncryptionByDefault"]["SSEAlgorithm"] == "AES256"
    # PublicAccessBlock is opt-in (default off): the property is wrapped in an
    # Fn::If keyed on AddCookedBucketPublicAccessBlock, falling back to NoValue.
    pab = props["PublicAccessBlockConfiguration"]["Fn::If"]
    assert pab[0] == "AddCookedBucketPublicAccessBlock"
    assert pab[1] == {
        "BlockPublicAcls": True,
        "BlockPublicPolicy": True,
        "IgnorePublicAcls": True,
        "RestrictPublicBuckets": True,
    }
    assert pab[2] == {"Ref": "AWS::NoValue"}


def test_public_access_block_param_defaults_off(td):
    p = td["Parameters"]["ConfigureBucketPublicAccessBlock"]
    assert p["Default"] == "false"
    assert set(p["AllowedValues"]) == {"false", "true"}


def test_no_sqs_resources(td):
    for n, r in td["Resources"].items():
        assert not r["Type"].startswith("AWS::SQS::"), f"{n} is an SQS resource"


def test_license_secret_named_and_retained(td):
    s = td["Resources"]["LicenseSecret"]
    assert s["DeletionPolicy"] == "Retain"
    assert s["Properties"]["Name"] == {"Ref": "LicenseSecretName"}
    assert td["Parameters"]["LicenseSecretName"]["Default"] == "cardinal-license"


def test_admin_key_secret_named_and_retained(td):
    s = td["Resources"]["AdminKeySecret"]
    assert s["DeletionPolicy"] == "Retain"
    assert s["Properties"]["Name"] == {"Ref": "AdminKeySecretName"}
    assert td["Parameters"]["AdminKeySecretName"]["Default"] == "cardinal-admin-key"


# ---------------------------------------------------------------------------
# Task 6: outputs
# ---------------------------------------------------------------------------


def test_all_outputs_present(td):
    for o in (
        "AlbSecurityGroupId",
        "MigrationSecurityGroupId",
        "QuerySecurityGroupId",
        "ProcessSecurityGroupId",
        "ControlSecurityGroupId",
        "MaestroSecurityGroupId",
        "ExecutionRoleArn",
        "MigrationRoleArn",
        "QueryRoleArn",
        "ProcessRoleArn",
        "ControlRoleArn",
        "MaestroRoleArn",
        "CookedBucketName",
        "LicenseSecretArn",
        "AdminKeySecretArn",
    ):
        assert o in td["Outputs"], f"missing output: {o}"
