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
        "PublicSubnets",
        "AlbScheme",
        "CertificateArn",
        "LicenseData",
        "ApiKeysOverride",
        "StorageProfilesOverride",
        "TemplateBaseUrl",
    ):
        assert n in td["Parameters"], f"missing parameter: {n}"


def test_template_base_url_default_pattern(td):
    default = td["Parameters"]["TemplateBaseUrl"]["Default"]
    assert default.startswith("https://")
    assert ".s3." in default
    assert ".amazonaws.com/" in default


def test_console_parameter_groups(td):
    groups = td["Metadata"]["AWS::CloudFormation::Interface"]["ParameterGroups"]
    labels = [g["Label"]["default"] for g in groups]
    assert labels == ["Networking", "Sizing", "Images", "Advanced"]


def test_license_data_no_echo(td):
    assert td["Parameters"]["LicenseData"]["NoEcho"] is True


def test_api_keys_override_no_echo(td):
    assert td["Parameters"]["ApiKeysOverride"]["NoEcho"] is True


def test_storage_profiles_override_no_echo(td):
    assert td["Parameters"]["StorageProfilesOverride"]["NoEcho"] is True


def test_nested_stack_count(td):
    nested = [r for r in td["Resources"].values() if r["Type"] == "AWS::CloudFormation::Stack"]
    assert len(nested) == 12


def test_nested_stack_logical_ids(td):
    nested = {k for k, v in td["Resources"].items()
              if v["Type"] == "AWS::CloudFormation::Stack"}
    expected = {
        "ClusterStack",
        "DatabaseStack",
        "StorageStack",
        "AlbStack",
        "ConfigStack",
        "CertStack",
        "MigrationStack",
        "ServicesQueryStack",
        "ServicesProcessStack",
        "ServicesControlStack",
        "OtelStack",
        "MaestroStack",
    }
    assert nested == expected


def test_alb_depends_on_cluster(td):
    """alb owns the TaskSG ingress, must wait on cluster."""
    alb = td["Resources"]["AlbStack"]
    deps = alb.get("DependsOn", [])
    if isinstance(deps, str):
        deps = [deps]
    assert "ClusterStack" in deps


def test_service_tier_stacks_depend_on_migration(td):
    for logical_id in ("ServicesQueryStack", "ServicesProcessStack", "ServicesControlStack"):
        deps = td["Resources"][logical_id].get("DependsOn", [])
        if isinstance(deps, str):
            deps = [deps]
        assert "MigrationStack" in deps, f"{logical_id} missing MigrationStack DependsOn"


def test_services_control_depends_on_services_process(td):
    """The monitoring service in services-control consumes services-process
    outputs (process-* service names) for its ECS autoscaler wiring."""
    deps = td["Resources"]["ServicesControlStack"].get("DependsOn", [])
    if isinstance(deps, str):
        deps = [deps]
    assert "ServicesProcessStack" in deps


def test_services_control_receives_monitoring_autoscaler_inputs(td):
    """Root must thread the cluster name + 3 process service names + 3 replica
    parameters into services-control so the monitoring autoscaler can scale."""
    params = td["Resources"]["ServicesControlStack"]["Properties"]["Parameters"]
    for n in (
        "ClusterName",
        "ProcessLogsServiceName",
        "ProcessMetricsServiceName",
        "ProcessTracesServiceName",
        "ProcessLogsReplicas",
        "ProcessMetricsReplicas",
        "ProcessTracesReplicas",
    ):
        assert n in params, f"ServicesControlStack missing parameter: {n}"
    for n, src_output in (
        ("ProcessLogsServiceName", "ProcessLogsServiceName"),
        ("ProcessMetricsServiceName", "ProcessMetricsServiceName"),
        ("ProcessTracesServiceName", "ProcessTracesServiceName"),
    ):
        getatt = params[n].get("Fn::GetAtt")
        assert getatt == ["ServicesProcessStack", f"Outputs.{src_output}"], (
            f"{n} must come from ServicesProcessStack output; got {params[n]!r}"
        )


def test_maestro_stack_depends_on_migration(td):
    deps = td["Resources"]["MaestroStack"].get("DependsOn", [])
    if isinstance(deps, str):
        deps = [deps]
    assert "MigrationStack" in deps


def test_template_url_uses_kebab_case_filenames(td):
    """Module names like services_query map to services-query.yaml."""
    # MigrationStack's TemplateURL should reference migration.yaml.
    migration_url = td["Resources"]["MigrationStack"]["Properties"]["TemplateURL"]
    # Sub renders to {"Fn::Sub": "${TemplateBaseUrl}migration.yaml"}
    assert "migration.yaml" in json.dumps(migration_url)
    # ServicesQueryStack -> services-query.yaml
    sq_url = td["Resources"]["ServicesQueryStack"]["Properties"]["TemplateURL"]
    assert "services-query.yaml" in json.dumps(sq_url)
    # ServicesProcessStack -> services-process.yaml
    sp_url = td["Resources"]["ServicesProcessStack"]["Properties"]["TemplateURL"]
    assert "services-process.yaml" in json.dumps(sp_url)
    # ServicesControlStack -> services-control.yaml
    sc_url = td["Resources"]["ServicesControlStack"]["Properties"]["TemplateURL"]
    assert "services-control.yaml" in json.dumps(sc_url)


def test_outputs(td):
    for n in ("AlbDnsName", "QueryApiUrl", "MaestroUrl", "InstallIdShort", "InstallIdLong"):
        assert n in td["Outputs"], f"missing output: {n}"


def test_has_public_subnets_condition(td):
    assert "HasPublicSubnets" in td.get("Conditions", {})
