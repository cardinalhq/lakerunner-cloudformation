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
        "QueueUrl",
        "QueueRoleArn",
    ):
        assert n in params, f"missing data-plane param: {n}"
    # Removed/renamed sources
    assert "IngestBucketName" not in params
    assert "RdsSecurityGroupId" not in params
    # Org content is Maestro-owned: the SSM-seed param names are gone.
    assert "StorageProfilesParamName" not in params
    assert "ApiKeysParamName" not in params
    # Vestigial queue param (ARN form) gone.
    assert "QueueArn" not in params, "vestigial queue param present: QueueArn"
    # Satellite SSM param gone: no parameter whose name contains "Satellite" or "satellites".
    satellite_params = [k for k in params if "Satellite" in k or "satellite" in k]
    assert not satellite_params, f"unexpected satellite param(s): {satellite_params}"


def test_organization_id_required_no_default(td):
    """OrganizationId is required (no default) on the services root, matching
    infra-base so the bootstrap org is operator-chosen and consistent."""
    p = td["Parameters"]["OrganizationId"]
    assert "Default" not in p, "OrganizationId must have no default"
    assert p["AllowedPattern"] == (
        r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
    )


def test_pubsub_sqs_queue_params_present_and_forwarded(td):
    """QueueUrl/QueueRoleArn are present and forwarded to the Process child
    for the pubsub-sqs container's SQS_QUEUE_URL / SQS_ROLE_ARN env vars."""
    assert "PubsubSqsEnv" not in td["Parameters"], "old shell-blob param still present"
    for n in ("QueueUrl", "QueueRoleArn"):
        assert n in td["Parameters"], f"{n} must be present"
    process = td["Resources"]["Process"]["Properties"]["Parameters"]
    assert process.get("QueueUrl") == {"Ref": "QueueUrl"}, "Process child must receive QueueUrl"
    assert process.get("QueueRoleArn") == {"Ref": "QueueRoleArn"}, "Process child must receive QueueRoleArn"


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


def test_maestro_child_gets_bucket_name_not_satellites_param(td):
    """Maestro receives the cooked bucket name (for MAESTRO_BOOTSTRAP_BUCKET_*);
    the root must NOT forward any satellite-named parameter."""
    maestro = td["Resources"]["Maestro"]["Properties"]["Parameters"]
    assert maestro.get("BucketName") == {"Ref": "CookedBucketName"}, (
        "BucketName (from CookedBucketName) must be forwarded to Maestro child"
    )
    satellite_keys = [k for k in maestro if "Satellite" in k or "satellite" in k]
    assert not satellite_keys, f"Maestro child unexpectedly receives: {satellite_keys}"


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


def test_pubsub_autoregister_params_absent(td):
    """Autoregister params are removed (since v1.5.0, stays removed);
    pubsub-sqs reads registration config from configdb."""
    params = td["Parameters"]
    autoregister_params = [k for k in params if "AutoRegister" in k or "Autoregister" in k]
    assert not autoregister_params, f"unexpected autoregister param(s): {autoregister_params}"


def test_additional_queue_groups_absent(td):
    """Numbered satellite queue params are removed; pubsub-sqs reads its queue
    list from configdb (removed in v1.5.0, stays removed)."""
    params = td["Parameters"]
    numbered_queue_params = [k for k in params
                             if k.startswith(("QueueUrl", "QueueRegion", "QueueRoleArn"))
                             and len(k) > len("QueueRoleArn") or (
                                 k.startswith("QueueUrl") and k[-1:].isdigit()
                             )]
    # Only QueueUrl and QueueRoleArn (un-numbered) are allowed.
    allowed = {"QueueUrl", "QueueRoleArn"}
    bad = [k for k in params
           if (k.startswith("QueueUrl") or k.startswith("QueueRegion") or
               k.startswith("QueueRoleArn")) and k not in allowed]
    assert not bad, f"unexpected numbered queue param(s): {bad}"
    # Process child: only the two plain params forwarded, no numbered ones.
    process = td["Resources"]["Process"]["Properties"]["Parameters"]
    numbered_in_child = [k for k in process
                         if (k.startswith("QueueUrl") or k.startswith("QueueRegion") or
                             k.startswith("QueueRoleArn")) and k not in allowed]
    assert not numbered_in_child, f"Process child gets numbered queue param(s): {numbered_in_child}"


def test_pubsub_autoregister_absent_from_all_children(td):
    """Autoregister params are gone from root; no child should receive them."""
    for child in ("Query", "Process", "Control", "Migration", "Maestro", "Alb", "Cert"):
        p = td["Resources"][child]["Properties"]["Parameters"]
        bad = [k for k in p if "AutoRegister" in k or "Autoregister" in k]
        assert not bad, f"{child} unexpectedly receives autoregister param(s): {bad}"


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
