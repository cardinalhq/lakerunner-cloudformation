"""Tests for the root cardinal-lakerunner template."""

import json

import pytest

from cardinal_cfn import root


@pytest.fixture
def td():
    return json.loads(root.build().to_json())


def test_required_parameters(td):
    for n in (
        "VpcId",
        "PrivateSubnets",
        "CertificateArn",
        "TemplateBaseUrl",
        # ALB inbound CIDRs (up to 3, all default to RFC1918)
        "AlbAllowedCidr1",
        "AlbAllowedCidr2",
        "AlbAllowedCidr3",
        # Cloud Map namespace name (stack creates the namespace)
        "ServiceNamespaceName",
        # Infrastructure-stack outputs threaded in
        "DbEndpoint",
        "DbPort",
        "DbName",
        "DbMasterSecretArn",
        "RdsSecurityGroupId",
        "IngestBucketName",
        "IngestQueueUrl",
        "IngestQueueArn",
        "LicenseSecretArn",
        "AdminKeySecretArn",
        "StorageProfilesParamName",
        "ApiKeysParamName",
        "ClusterName",
        "ClusterArn",
    ):
        assert n in td["Parameters"], f"missing parameter: {n}"


def test_no_phase1_data_or_secret_input_parameters(td):
    """license/api/storage payloads are owned by the infrastructure stack."""
    for n in ("LicenseData", "ApiKeysOverride", "StorageProfilesOverride"):
        assert n not in td["Parameters"], f"unexpected legacy parameter: {n}"


def test_no_customer_supplied_iam_or_sg_parameters(td):
    """SGs and IAM roles are stack-owned -- customers no longer supply them."""
    for n in ("TaskRoleArn", "ExecutionRoleArn", "TaskSgId", "AlbSgId",
              "ServiceNamespaceId"):
        assert n not in td["Parameters"], (
            f"parameter {n} should have been removed (stack-owned now)"
        )


def test_alb_scheme_defaults_to_internal(td):
    p = td["Parameters"]["AlbScheme"]
    assert p["Default"] == "internal"
    assert set(p["AllowedValues"]) == {"internal", "internet-facing"}


def test_public_subnets_parameter_defaults_to_empty(td):
    """PublicSubnets is optional -- only required when AlbScheme=internet-facing,
    ignored otherwise. Default empty so internal-scheme installs don't have to
    fabricate placeholder subnets."""
    p = td["Parameters"]["PublicSubnets"]
    assert p["Type"] == "CommaDelimitedList"
    assert p["Default"] == ""


def test_alb_child_receives_scheme_and_subnets_conditional(td):
    params = td["Resources"]["Alb"]["Properties"]["Parameters"]
    assert params["Scheme"] == {"Ref": "AlbScheme"}
    # AlbSubnetsCsv picks between PublicSubnets and PrivateSubnets via an If.
    sub_param = params["AlbSubnetsCsv"]
    assert "Fn::If" in sub_param
    if_args = sub_param["Fn::If"]
    assert if_args[0] == "AlbIsInternetFacing"


def test_security_child_receives_alb_scheme(td):
    sec_params = td["Resources"]["Security"]["Properties"]["Parameters"]
    assert sec_params["AlbScheme"] == {"Ref": "AlbScheme"}


def test_template_base_url_default_pattern(td):
    default = td["Parameters"]["TemplateBaseUrl"]["Default"]
    assert default.startswith("https://")
    assert ".s3." in default
    assert ".amazonaws.com/" in default


def test_console_parameter_groups(td):
    groups = td["Metadata"]["AWS::CloudFormation::Interface"]["ParameterGroups"]
    labels = [g["Label"]["default"] for g in groups]
    assert labels == [
        "Networking",
        "Infrastructure-stack outputs",
        "Sizing",
        "Images",
        "Advanced",
    ]


def test_nested_stack_count(td):
    """Security child added -> 9 nested children total."""
    nested = [r for r in td["Resources"].values() if r["Type"] == "AWS::CloudFormation::Stack"]
    assert len(nested) == 9


def test_nested_stack_logical_ids(td):
    nested = {k for k, v in td["Resources"].items()
              if v["Type"] == "AWS::CloudFormation::Stack"}
    expected = {
        "Security",
        "Alb",
        "Cert",
        "Migration",
        "Query",
        "Process",
        "Control",
        "Otel",
        "Maestro",
    }
    assert nested == expected


def test_no_legacy_data_stacks(td):
    """No data-tier stacks live in the lakerunner root."""
    nested = {k for k, v in td["Resources"].items()
              if v["Type"] == "AWS::CloudFormation::Stack"}
    for legacy in ("DatabaseStack", "StorageStack", "ConfigStack", "ClusterStack"):
        assert legacy not in nested, f"legacy nested stack still present: {legacy}"


def test_service_tier_stacks_depend_on_migration(td):
    for logical_id in ("Query", "Process", "Control"):
        deps = td["Resources"][logical_id].get("DependsOn", [])
        if isinstance(deps, str):
            deps = [deps]
        assert "Migration" in deps, f"{logical_id} missing Migration DependsOn"


def test_services_control_depends_on_services_process(td):
    """The monitoring service in Control consumes Process outputs
    (process-* service names) for its ECS autoscaler wiring."""
    deps = td["Resources"]["Control"].get("DependsOn", [])
    if isinstance(deps, str):
        deps = [deps]
    assert "Process" in deps


def test_services_control_receives_monitoring_autoscaler_inputs(td):
    """Root must thread the cluster name + 3 process service names + 3 replica
    parameters into Control so the monitoring autoscaler can scale."""
    params = td["Resources"]["Control"]["Properties"]["Parameters"]
    for n in (
        "ClusterName",
        "ProcessLogsServiceName",
        "ProcessMetricsServiceName",
        "ProcessTracesServiceName",
        "ProcessLogsReplicas",
        "ProcessMetricsReplicas",
        "ProcessTracesReplicas",
    ):
        assert n in params, f"Control missing parameter: {n}"
    for n, src_output in (
        ("ProcessLogsServiceName", "ProcessLogsServiceName"),
        ("ProcessMetricsServiceName", "ProcessMetricsServiceName"),
        ("ProcessTracesServiceName", "ProcessTracesServiceName"),
    ):
        getatt = params[n].get("Fn::GetAtt")
        assert getatt == ["Process", f"Outputs.{src_output}"], (
            f"{n} must come from Process output; got {params[n]!r}"
        )


def test_maestro_stack_depends_on_migration(td):
    deps = td["Resources"]["Maestro"].get("DependsOn", [])
    if isinstance(deps, str):
        deps = [deps]
    assert "Migration" in deps


def test_maestro_stack_receives_bucket_name(td):
    """Root must forward the ingest bucket name to the Maestro child so its
    MAESTRO_BOOTSTRAP_BUCKET_* env vars can drive the post-fix maestro
    image to recreate the organization_buckets join row."""
    params = td["Resources"]["Maestro"]["Properties"]["Parameters"]
    assert params["BucketName"] == {"Ref": "IngestBucketName"}


def test_template_url_uses_kebab_case_filenames(td):
    """Module names like services_query map to services-query.yaml."""
    migration_url = td["Resources"]["Migration"]["Properties"]["TemplateURL"]
    assert "migration.yaml" in json.dumps(migration_url)
    security_url = td["Resources"]["Security"]["Properties"]["TemplateURL"]
    assert "security.yaml" in json.dumps(security_url)
    sq_url = td["Resources"]["Query"]["Properties"]["TemplateURL"]
    assert "services-query.yaml" in json.dumps(sq_url)
    sp_url = td["Resources"]["Process"]["Properties"]["TemplateURL"]
    assert "services-process.yaml" in json.dumps(sp_url)
    sc_url = td["Resources"]["Control"]["Properties"]["TemplateURL"]
    assert "services-control.yaml" in json.dumps(sc_url)


def test_outputs(td):
    for n in ("AlbDnsName", "QueryApiUrl", "MaestroUrl", "InstallIdShort", "InstallIdLong"):
        assert n in td["Outputs"], f"missing output: {n}"


def test_alb_is_internet_facing_condition(td):
    """The condition controls which subnet list the ALB attaches to. Root
    declares it; the Security child has its own equivalent condition for
    its 0.0.0.0/0 ingress rules."""
    conditions = td.get("Conditions", {})
    assert "AlbIsInternetFacing" in conditions
    assert conditions["AlbIsInternetFacing"] == {
        "Fn::Equals": [{"Ref": "AlbScheme"}, "internet-facing"]
    }


def test_service_namespace_created_in_root(td):
    """Cloud Map private DNS namespace is created in the root, not customer-supplied."""
    ns = td["Resources"].get("ServiceNamespace")
    assert ns is not None, "root must create the Cloud Map namespace inline"
    assert ns["Type"] == "AWS::ServiceDiscovery::PrivateDnsNamespace"


def test_security_child_runs_before_others(td):
    """Security must not DependsOn anything; Alb consumes its outputs."""
    sec = td["Resources"]["Security"]
    assert "DependsOn" not in sec, "Security should be the first child to instantiate"
    # Alb gets AlbSgId from Security's output.
    alb_params = td["Resources"]["Alb"]["Properties"]["Parameters"]
    alb_sg = alb_params.get("AlbSgId")
    assert alb_sg == {"Fn::GetAtt": ["Security", "Outputs.AlbSecurityGroupId"]}, (
        f"Alb.AlbSgId must come from Security output; got {alb_sg!r}"
    )
