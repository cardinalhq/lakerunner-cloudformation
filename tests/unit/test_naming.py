"""Tests for naming and tag helpers."""

import json

from cardinal_cfn.naming import cardinal_tags, name_tag, ssm_param_name, secret_name


def _render(obj):
    return json.loads(json.dumps(obj, default=lambda o: o.to_dict()))


def test_cardinal_tags_includes_required_keys():
    rendered = _render(cardinal_tags(component="storage", role="ingest-bucket"))
    keys = {tag["Key"] for tag in rendered}
    assert keys == {"Name", "Project", "Component", "ManagedBy"}


def test_cardinal_tags_name_includes_install_id_and_role():
    rendered = _render(cardinal_tags(component="storage", role="ingest-bucket"))
    name_tag_value = next(t["Value"] for t in rendered if t["Key"] == "Name")
    assert name_tag_value == {"Fn::Sub": "cardinal-ingest-bucket-${InstallIdShort}"}


def test_cardinal_tags_project_constant():
    rendered = _render(cardinal_tags(component="storage", role="ingest-bucket"))
    project = next(t["Value"] for t in rendered if t["Key"] == "Project")
    assert project == "cardinal"


def test_name_tag_returns_just_the_name_dict():
    """name_tag() is for resources that take a single Name attribute (not Tags=)."""
    rendered = _render(name_tag(role="ecs-cluster"))
    assert rendered == {"Fn::Sub": "cardinal-ecs-cluster-${InstallIdShort}"}


def test_ssm_param_name_uses_install_id_long():
    rendered = _render(ssm_param_name(key="storage-profiles"))
    assert rendered == {"Fn::Sub": "cardinal/${InstallIdLong}/storage-profiles"}


def test_secret_name_uses_install_id_long():
    rendered = _render(secret_name(purpose="admin-api-key"))
    assert rendered == {"Fn::Sub": "cardinal/${InstallIdLong}/admin-api-key"}
