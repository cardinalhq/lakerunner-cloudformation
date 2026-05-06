"""Tests for the naming and tag conventions module."""

import pytest

from cardinal_cfn.naming import (
    APPLICATION,
    PROJECT,
    LakerunnerComponent,
    cardinal_tags,
    log_group_name,
    name_tag,
    secret_name,
    ssm_param_name,
)


def _tag_dict(tags) -> dict[str, str]:
    return {item["Key"]: item["Value"] for item in tags.to_dict()}


def test_constants():
    assert PROJECT == "cardinal"
    assert APPLICATION == "cardinal-lakerunner"


def test_tags_carry_required_keys():
    tags = cardinal_tags(component="task-role", managed_by="cardinal-prereqs-script")
    keys = set(_tag_dict(tags).keys())
    assert {"Application", "Component", "ManagedBy", "Name"} <= keys


def test_tags_values_match_inputs():
    tags = _tag_dict(cardinal_tags(component="ingest-bucket", managed_by="cardinal-data-setup-script"))
    assert tags["Application"] == APPLICATION
    assert tags["Component"] == "ingest-bucket"
    assert tags["ManagedBy"] == "cardinal-data-setup-script"
    assert tags["Name"] == "cardinal-ingest-bucket"


def test_tags_install_version_optional():
    tags_without = _tag_dict(cardinal_tags(component="x", managed_by="m"))
    assert "cardinal:install-version" not in tags_without

    tags_with = _tag_dict(cardinal_tags(component="x", managed_by="m", install_version="v1.2.3"))
    assert tags_with["cardinal:install-version"] == "v1.2.3"


def test_managed_by_required():
    with pytest.raises(ValueError):
        cardinal_tags(component="x", managed_by="")


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
