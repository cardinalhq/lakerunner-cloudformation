"""Tests for the naming and tag conventions module."""

from cardinal_cfn.naming import (
    APPLICATION,
    MANAGED_BY_TAG,
    PROJECT,
    LakerunnerComponent,
    cardinal_tags,
    log_group_name,
    name_tag,
    secret_name,
    ssm_param_name,
)

COMMON_TAG_KEYS = {"Name", "Project", "Application", "Component", "ManagedBy"}


def _tag_dict(tags) -> dict[str, str]:
    return {item["Key"]: item["Value"] for item in tags.to_dict()}


def test_constants():
    assert PROJECT == "cardinal"
    assert APPLICATION == "cardinal-lakerunner"


def test_tags_carry_the_common_set():
    tags = _tag_dict(cardinal_tags(component="ingest-bucket", managed_by="cardinal-cfn-satellite"))
    assert set(tags) == COMMON_TAG_KEYS
    assert tags["Project"] == PROJECT
    assert tags["Application"] == APPLICATION
    assert tags["Component"] == "ingest-bucket"
    assert tags["ManagedBy"] == "cardinal-cfn-satellite"
    assert tags["Name"] == "cardinal-ingest-bucket"


def test_managed_by_defaults_to_the_children_value():
    tags = _tag_dict(cardinal_tags(component="compute"))
    assert tags["ManagedBy"] == MANAGED_BY_TAG


def test_role_puts_install_id_in_name_and_keeps_the_common_set():
    tags = _tag_dict(cardinal_tags(component="compute", role="query-api"))
    assert set(tags) == COMMON_TAG_KEYS
    assert tags["Name"] == {"Fn::Sub": "cardinal-query-api-${InstallIdShort}"}
    assert tags["Component"] == "compute"


def test_name_tag_emits_plain_string_no_install_id():
    assert name_tag(role="ingest-bucket") == "cardinal-ingest-bucket"


def test_secret_name_uses_dash_prefix_no_install_id():
    assert secret_name(purpose="db-master") == "cardinal-db-master"


def test_ssm_param_name_uses_slash_prefix_no_install_id():
    assert ssm_param_name(key="storage-profiles") == "/cardinal/storage-profiles"


def test_log_group_name_uses_slash_prefix():
    assert log_group_name(service="query-api") == "/cardinal/query-api"


def test_lakerunner_components_are_known():
    assert LakerunnerComponent.QUERY_API.value == "query-api"
    assert LakerunnerComponent.MIGRATOR.value == "migrator"
    assert LakerunnerComponent.MAESTRO.value == "maestro"


def test_lakerunner_components_complete_coverage():
    # Lock in the full set so removing one becomes a deliberate choice.
    assert {c.value for c in LakerunnerComponent} == {
        "query-api",
        "query-worker",
        "process-logs",
        "process-metrics",
        "process-traces",
        "pubsub-sqs",
        "sweeper",
        "monitoring",
        "admin-api",
        "alert-evaluator",
        "otel-collector",
        "maestro",
        "dex",
        "migrator",
    }
