"""Tests for the standalone cardinal-lakerunner-services root template."""

import json

import pytest

from cardinal_cfn import lakerunner_services


@pytest.fixture
def td():
    return json.loads(lakerunner_services.build().to_json())


def _nested_logical_ids(td):
    return {k for k, v in td["Resources"].items()
            if v["Type"] == "AWS::CloudFormation::Stack"}


def test_no_security_child(td):
    """No nested stack should be the Security child."""
    assert "Security" not in _nested_logical_ids(td)
    for r in td["Resources"].values():
        if r["Type"] != "AWS::CloudFormation::Stack":
            continue
        url = json.dumps(r["Properties"]["TemplateURL"])
        assert "security.yaml" not in url, "security child must be removed"


def test_creates_no_iam_or_sg(td):
    """The template's own Resources create no IAM roles and no security groups.

    Everything role/SG-shaped arrives as a parameter; the only non-nested-stack
    resource is the Cloud Map private DNS namespace.
    """
    for k, r in td["Resources"].items():
        assert r["Type"] != "AWS::IAM::Role", f"{k} is an IAM role"
        assert r["Type"] != "AWS::EC2::SecurityGroup", f"{k} is a security group"


def test_role_and_sg_params_present(td):
    sg_params = [
        "AlbSecurityGroupId",
        "MigrationSecurityGroupId",
        "QuerySecurityGroupId",
        "ProcessSecurityGroupId",
        "ControlSecurityGroupId",
        "MaestroSecurityGroupId",
    ]
    role_params = [
        "ExecutionRoleArn",
        "MigrationRoleArn",
        "QueryRoleArn",
        "ProcessRoleArn",
        "ControlRoleArn",
        "MaestroRoleArn",
    ]
    for n in sg_params:
        assert n in td["Parameters"], f"missing SG param: {n}"
        assert td["Parameters"][n]["Type"] == "AWS::EC2::SecurityGroup::Id"
    for n in role_params:
        assert n in td["Parameters"], f"missing role param: {n}"
        assert td["Parameters"][n]["Type"] == "String"


def test_data_plane_params(td):
    params = td["Parameters"]
    for n in (
        "CookedBucketName",
        "DbEndpoint",
        "DbPort",
        "DbName",
        "DbMasterSecretArn",
        "LicenseSecretArn",
        "AdminKeySecretArn",
    ):
        assert n in params, f"missing data-plane param: {n}"
    # Removed/renamed sources
    assert "IngestBucketName" not in params
    assert "RdsSecurityGroupId" not in params
    # Org content is Maestro-owned: the SSM-seed param names are gone.
    assert "StorageProfilesParamName" not in params
    assert "ApiKeysParamName" not in params
    # QueueArn was never plumbed; QueueUrl/QueueRoleArn are the group-0 inputs.
    assert "QueueArn" not in params, "vestigial queue param present: QueueArn"


def test_organization_id_required_no_default(td):
    """OrganizationId is required (no default) on the services root, matching
    infra-base so the bootstrap org is operator-chosen and consistent."""
    p = td["Parameters"]["OrganizationId"]
    assert "Default" not in p, "OrganizationId must have no default"
    assert p["AllowedPattern"] == (
        r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
    )


def test_pubsub_sqs_queue_wired_to_process_child(td):
    """The group-0 SQS inputs (QueueUrl/QueueRoleArn) flow into the Process
    child, where the pubsub-sqs container sets them as plain SQS_* env vars."""
    assert "PubsubSqsEnv" not in td["Parameters"], "old shell-blob param still present"
    for n in ("QueueUrl", "QueueRoleArn"):
        assert n in td["Parameters"], f"missing queue param: {n}"
        assert td["Parameters"][n]["Default"] == "", n
    process = td["Resources"]["Process"]["Properties"]["Parameters"]
    assert process["QueueUrl"] == {"Ref": "QueueUrl"}
    assert process["QueueRoleArn"] == {"Ref": "QueueRoleArn"}
    assert "PubsubSqsEnv" not in process, "Process child should not receive PubsubSqsEnv"


def test_process_child_params_match_declared(td):
    """Every parameter declared by services-process.yaml is supplied by the
    root, and no extra parameters are passed that the child doesn't declare.
    missing [] extra [] means the invariant holds."""
    from cardinal_cfn.children import services_process
    child_td = json.loads(services_process.build().to_json())
    declared = set(child_td["Parameters"].keys())
    passed = set(td["Resources"]["Process"]["Properties"]["Parameters"].keys())
    missing = sorted(declared - passed)
    extra = sorted(passed - declared)
    assert missing == [] and extra == [], (
        f"Process child param mismatch: missing {missing}  extra {extra}"
    )


def test_children_present(td):
    nested = _nested_logical_ids(td)
    expected = {
        "Cert",
        "Alb",
        "Migration",
        "Query",
        "Process",
        "Control",
        "Maestro",
    }
    assert nested == expected


def test_cooked_bucket_wired_to_children(td):
    """At least one child receives Ref CookedBucketName for its bucket param."""
    ref = {"Ref": "CookedBucketName"}
    query = td["Resources"]["Query"]["Properties"]["Parameters"]
    assert query["BucketName"] == ref


def test_migration_child_gets_no_org_content_params(td):
    """Org content is Maestro-owned: the Migration child no longer receives the
    SSM-seed param names, the org id, or the ingest bucket."""
    migration = td["Resources"]["Migration"]["Properties"]["Parameters"]
    for gone in ("StorageProfilesParamName", "ApiKeysParamName",
                 "OrgId", "IngestBucketName"):
        assert gone not in migration, f"Migration child still gets {gone}"


def test_dex_extra_users_param_and_forwarded_to_maestro(td):
    """Additional DEX accounts: NoEcho param on the root, forwarded verbatim to
    the Maestro child (which sets it as the dex DEX_EXTRA_USERS env var)."""
    p = td["Parameters"]["DexExtraUsers"]
    assert p["Type"] == "String"
    assert p["NoEcho"] is True
    assert p["Default"] == ""
    maestro = td["Resources"]["Maestro"]["Properties"]["Parameters"]
    assert maestro["DexExtraUsers"] == {"Ref": "DexExtraUsers"}


def test_self_telemetry_endpoint_param(td):
    """The locked SelfTelemetry=[No] toggle is gone; a real configurable
    SelfTelemetryEndpoint (String, default "") takes its place."""
    params = td["Parameters"]
    assert "SelfTelemetry" not in params, "vestigial SelfTelemetry toggle still present"
    assert "SelfTelemetryEndpoint" in params
    p = params["SelfTelemetryEndpoint"]
    assert p["Type"] == "String"
    assert p["Default"] == ""


def test_self_telemetry_on_condition(td):
    """SelfTelemetryOn is true exactly when the endpoint is non-empty."""
    cond = td["Conditions"]["SelfTelemetryOn"]
    assert cond == {
        "Fn::Not": [{"Fn::Equals": [{"Ref": "SelfTelemetryEndpoint"}, ""]}]
    }


def test_pubsub_autoregister_params_present_with_defaults(td):
    """PubsubAutoRegister and PubsubAutoRegisterWritesToInstance exist on the
    root with the correct defaults."""
    params = td["Parameters"]
    assert "PubsubAutoRegister" in params
    assert params["PubsubAutoRegister"]["Default"] == "true"
    assert params["PubsubAutoRegister"]["AllowedValues"] == ["true", "false"]
    assert "PubsubAutoRegisterWritesToInstance" in params
    assert params["PubsubAutoRegisterWritesToInstance"]["Default"] == "1"


def test_additional_queue_groups_forwarded_to_process_child(td):
    """The root declares numbered queue params and forwards them to the process
    child so pubsub-sqs can consume multiple satellite queues."""
    params = td["Parameters"]
    for n in (1, 10):
        for p in (f"QueueUrl{n}", f"QueueRegion{n}", f"QueueRoleArn{n}"):
            assert p in params, f"root missing {p}"
            assert params[p]["Default"] == ""
    process = td["Resources"]["Process"]["Properties"]["Parameters"]
    assert process["QueueUrl1"] == {"Ref": "QueueUrl1"}
    assert process["QueueRoleArn10"] == {"Ref": "QueueRoleArn10"}


def test_pubsub_autoregister_wired_to_process_child(td):
    """The two auto-registration params are forwarded as Refs to the Process
    child and are absent from all other children."""
    process = td["Resources"]["Process"]["Properties"]["Parameters"]
    assert process.get("PubsubAutoRegister") == {"Ref": "PubsubAutoRegister"}
    assert process.get("PubsubAutoRegisterWritesToInstance") == (
        {"Ref": "PubsubAutoRegisterWritesToInstance"}
    )
    for child in ("Query", "Control", "Migration", "Maestro", "Alb", "Cert"):
        p = td["Resources"][child]["Properties"]["Parameters"]
        assert "PubsubAutoRegister" not in p, (
            f"{child} unexpectedly receives PubsubAutoRegister"
        )
        assert "PubsubAutoRegisterWritesToInstance" not in p, (
            f"{child} unexpectedly receives PubsubAutoRegisterWritesToInstance"
        )


def test_self_telemetry_wired_to_tiers_only(td):
    """Query/Process/Control receive the endpoint Ref and the If-derived enabled
    flag; Maestro and the other children do not."""
    endpoint_ref = {"Ref": "SelfTelemetryEndpoint"}
    enabled_if = {"Fn::If": ["SelfTelemetryOn", "true", "false"]}
    for tier in ("Query", "Process", "Control"):
        p = td["Resources"][tier]["Properties"]["Parameters"]
        assert p["SelfTelemetryEndpoint"] == endpoint_ref, tier
        assert p["SelfTelemetryEnabled"] == enabled_if, tier
    for other in ("Maestro", "Migration", "Alb", "Cert"):
        p = td["Resources"][other]["Properties"]["Parameters"]
        assert "SelfTelemetryEndpoint" not in p, other
        assert "SelfTelemetryEnabled" not in p, other


def test_public_dns_name_overrides_hostname(td):
    """PublicDnsName (optional, default "") replaces the ALB DNS name in every
    externally visible URL: the Maestro child's AlbDnsName (issuer/redirect
    URLs) and the QueryApiUrl output. The AlbDnsName output stays the raw ALB
    name so the operator knows the CNAME target."""
    p = td["Parameters"]["PublicDnsName"]
    assert p["Type"] == "String"
    assert p["Default"] == ""
    assert td["Conditions"]["PublicDnsNameSet"] == {
        "Fn::Not": [{"Fn::Equals": [{"Ref": "PublicDnsName"}, ""]}]
    }
    effective = {"Fn::If": [
        "PublicDnsNameSet",
        {"Ref": "PublicDnsName"},
        {"Fn::GetAtt": ["Alb", "Outputs.AlbDnsName"]},
    ]}
    maestro = td["Resources"]["Maestro"]["Properties"]["Parameters"]
    assert maestro["AlbDnsName"] == effective
    query_url = td["Outputs"]["QueryApiUrl"]["Value"]
    assert query_url["Fn::Sub"][1]["AlbDns"] == effective
    assert td["Outputs"]["AlbDnsName"]["Value"] == {
        "Fn::GetAtt": ["Alb", "Outputs.AlbDnsName"]
    }
