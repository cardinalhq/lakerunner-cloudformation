"""Tests for the cardinal-satellite-services standalone template."""

import json

import pytest

from cardinal_cfn import satellite_services


@pytest.fixture
def td():
    return json.loads(satellite_services.build().to_json())


# ---------------------------------------------------------------------------
# Task 1: Parameters + conditions
# ---------------------------------------------------------------------------


def test_required_parameters(td):
    for n in (
        "RawBucketName",
        "OrganizationId",
        "VpcId",
        "AlbSubnetsCsv",
        "TaskSubnetsCsv",
        "EcsClusterArn",
        "AlbScheme",
    ):
        assert n in td["Parameters"], f"missing parameter: {n}"


def test_alb_scheme_allowed_values(td):
    p = td["Parameters"]["AlbScheme"]
    assert p["AllowedValues"] == ["internal", "internet-facing"]
    assert p["Default"] == "internal"


def test_organization_id_required_uuid(td):
    """OrganizationId is required (no Default) and constrained to a UUID."""
    p = td["Parameters"]["OrganizationId"]
    assert p["Type"] == "String"
    assert "Default" not in p, "OrganizationId must be required (no Default)"
    assert p["AllowedPattern"] == (
        r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
    )


# ---------------------------------------------------------------------------
# Task 2: IAM roles
# ---------------------------------------------------------------------------


def test_task_role_writes_only_raw_bucket(td):
    """Collector task role allows writes to the raw bucket, but NOT s3:DeleteObject."""
    roles = {
        k: v for k, v in td["Resources"].items()
        if v["Type"] == "AWS::IAM::Role"
    }
    task_role = roles["CollectorTaskRole"]
    stmts = task_role["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
    all_actions = []
    for stmt in stmts:
        actions = stmt.get("Action", [])
        if isinstance(actions, str):
            actions = [actions]
        all_actions.extend(actions)
    assert "s3:PutObject" in all_actions
    assert "s3:ListBucket" in all_actions
    assert "s3:DeleteObject" not in all_actions


def test_no_license_anywhere(td):
    """The collector image needs no license: no LicenseSecretArn param, no
    secretsmanager:GetSecretValue on the exec role, and no LICENSE_DATA secret
    on the container. A satellite may live in a different account than the
    central license secret, so nothing here may depend on it."""
    assert "LicenseSecretArn" not in td["Parameters"]

    exec_role = td["Resources"]["CollectorExecutionRole"]
    stmts = exec_role["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
    for s in stmts:
        actions = s["Action"] if isinstance(s["Action"], list) else [s["Action"]]
        assert "secretsmanager:GetSecretValue" not in actions

    task_def = next(
        r for r in td["Resources"].values() if r["Type"] == "AWS::ECS::TaskDefinition"
    )
    container = task_def["Properties"]["ContainerDefinitions"][0]
    secret_names = [s["Name"] for s in container.get("Secrets", [])]
    assert "LICENSE_DATA" not in secret_names
    assert "LicenseSecretArn" not in json.dumps(td)


# ---------------------------------------------------------------------------
# Task 3: Security groups
# ---------------------------------------------------------------------------


def test_alb_sg_ingress_on_4318(td):
    """ALB SG has ingress on port 4318 from IngestSourceCidr."""
    alb_sg = td["Resources"]["AlbSecurityGroup"]
    ingress = alb_sg["Properties"].get("SecurityGroupIngress", [])
    rules_4318 = [r for r in ingress if r.get("FromPort") == 4318]
    assert rules_4318, "ALB SG must have ingress on 4318"
    for rule in rules_4318:
        assert rule.get("CidrIp") == {"Ref": "IngestSourceCidr"}


def test_task_sg_ingress_from_alb_sg(td):
    """Task SG has ingress on port 4318 sourced from the AlbSecurityGroup."""
    task_sg = td["Resources"]["TaskSecurityGroup"]
    ingress = task_sg["Properties"].get("SecurityGroupIngress", [])
    rules_4318 = [
        r for r in ingress
        if r.get("FromPort") == 4318
        and r.get("SourceSecurityGroupId") == {"Ref": "AlbSecurityGroup"}
    ]
    assert rules_4318, "Task SG must allow 4318 from AlbSecurityGroup"


# ---------------------------------------------------------------------------
# Task 4: ALB + listener + target group + listener rule
# ---------------------------------------------------------------------------


def test_alb_uses_scheme_param(td):
    alb = td["Resources"]["Alb"]
    assert alb["Properties"]["Scheme"] == {"Ref": "AlbScheme"}


def test_otel_listener_is_plain_http_4318(td):
    listeners = [
        r for r in td["Resources"].values()
        if r["Type"] == "AWS::ElasticLoadBalancingV2::Listener"
    ]
    assert listeners, "must have at least one listener"
    otel = next(
        (r for r in listeners if r["Properties"].get("Port") == 4318),
        None,
    )
    assert otel is not None, "must have a listener on port 4318"
    assert otel["Properties"]["Protocol"] == "HTTP"


def test_listener_rule_v1_path(td):
    rules = [
        r for r in td["Resources"].values()
        if r["Type"] == "AWS::ElasticLoadBalancingV2::ListenerRule"
    ]
    assert rules, "must have a listener rule"
    conditions = rules[0]["Properties"]["Conditions"]
    path_conds = [c for c in conditions if c.get("Field") == "path-pattern"]
    assert path_conds
    values = path_conds[0]["PathPatternConfig"]["Values"]
    assert "/v1/*" in values


def test_target_group_health_on_13133(td):
    tgs = [
        r for r in td["Resources"].values()
        if r["Type"] == "AWS::ElasticLoadBalancingV2::TargetGroup"
    ]
    assert tgs
    assert tgs[0]["Properties"]["HealthCheckPort"] == "13133"


# ---------------------------------------------------------------------------
# Task 5: Log group, task definition, ECS service
# ---------------------------------------------------------------------------


def test_collector_service_is_pure_on_demand(td):
    # The collector is the deploy-critical ingest path: pure on-demand FARGATE
    # so every new task places during a rolling deploy. FARGATE_SPOT can't
    # guarantee placement, so no spot tier anywhere.
    svc = next(
        r for r in td["Resources"].values() if r["Type"] == "AWS::ECS::Service"
    )
    items = svc["Properties"]["CapacityProviderStrategy"]
    assert items == [{"CapacityProvider": "FARGATE", "Weight": 1}]
    assert all("Base" not in i for i in items)


def test_service_no_cloud_map(td):
    """Satellite services collector has no Cloud Map (no ServiceRegistries)."""
    svc = next(
        r for r in td["Resources"].values() if r["Type"] == "AWS::ECS::Service"
    )
    assert "ServiceRegistries" not in svc["Properties"]


def test_service_loadbalancer_wires_target_group(td):
    svc = next(
        r for r in td["Resources"].values() if r["Type"] == "AWS::ECS::Service"
    )
    lbs = svc["Properties"].get("LoadBalancers", [])
    assert lbs, "service must have LoadBalancers"
    assert lbs[0]["ContainerPort"] == 4318


def test_taskdef_writes_bucket_env(td):
    task_def = next(
        r for r in td["Resources"].values() if r["Type"] == "AWS::ECS::TaskDefinition"
    )
    container = task_def["Properties"]["ContainerDefinitions"][0]
    env = {e["Name"]: e["Value"] for e in container.get("Environment", [])}
    assert env.get("LRDB_S3_BUCKET") == {"Ref": "RawBucketName"}


def test_org_env_is_organization_id_param(td):
    """ORG comes from the operator-supplied OrganizationId param, not storage_profiles."""
    task_def = next(
        r for r in td["Resources"].values() if r["Type"] == "AWS::ECS::TaskDefinition"
    )
    container = task_def["Properties"]["ContainerDefinitions"][0]
    env = {e["Name"]: e["Value"] for e in container.get("Environment", [])}
    assert env.get("ORG") == {"Ref": "OrganizationId"}


def test_collector_env_is_stackid_derived_sub(td):
    """COLLECTOR is an auto-generated a${...} Sub tied to AWS::StackId, never 'lakerunner'."""
    task_def = next(
        r for r in td["Resources"].values() if r["Type"] == "AWS::ECS::TaskDefinition"
    )
    container = task_def["Properties"]["ContainerDefinitions"][0]
    env = {e["Name"]: e["Value"] for e in container.get("Environment", [])}
    collector = env.get("COLLECTOR")
    assert isinstance(collector, dict) and "Fn::Sub" in collector, (
        f"COLLECTOR must be a Fn::Sub, got: {collector!r}"
    )
    sub = collector["Fn::Sub"]
    # Sub with named substitution: [template, {Short: <stackid expr>}]
    template, subs = sub
    assert template == "a${Short}"
    assert collector != "lakerunner"
    # The substitution must derive from AWS::StackId.
    assert "AWS::StackId" in json.dumps(subs)


def test_service_assigns_no_public_ip(td):
    awsvpc = td["Resources"]["CollectorService"]["Properties"][
        "NetworkConfiguration"
    ]["AwsvpcConfiguration"]
    assert awsvpc["AssignPublicIp"] == "DISABLED"


def test_taskdef_runs_on_arm64(td):
    rp = td["Resources"]["OtelGrpcTaskDef"]["Properties"]["RuntimePlatform"]
    assert rp["CpuArchitecture"] == "ARM64"
    assert rp["OperatingSystemFamily"] == "LINUX"


# ---------------------------------------------------------------------------
# Task 6: Outputs
# ---------------------------------------------------------------------------


def test_outputs_present(td):
    for o in (
        "CollectorAlbDnsName",
        "CollectorEndpoint",
        "CollectorServiceName",
        "CollectorTaskRoleArn",
    ):
        assert o in td["Outputs"], f"missing output: {o}"
