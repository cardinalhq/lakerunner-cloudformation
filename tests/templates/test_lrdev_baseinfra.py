"""Tests for the lrdev-baseinfra standalone template."""

import json

import pytest

from cardinal_cfn import lrdev_baseinfra


@pytest.fixture
def td():
    return json.loads(lrdev_baseinfra.build().to_json())


def test_environment_parameter_defaults_to_lrdev(td):
    assert "EnvironmentName" in td["Parameters"]
    assert td["Parameters"]["EnvironmentName"]["Default"] == "lrdev"


def test_creates_a_single_ecs_cluster(td):
    clusters = [r for r in td["Resources"].values() if r["Type"] == "AWS::ECS::Cluster"]
    assert len(clusters) == 1


def test_no_explicit_cluster_name(td):
    """ECS cluster names block in-place updates; rely on CFN-generated name."""
    cluster = next(r for r in td["Resources"].values() if r["Type"] == "AWS::ECS::Cluster")
    assert "ClusterName" not in cluster.get("Properties", {})


def test_container_insights_enabled(td):
    cluster = next(r for r in td["Resources"].values() if r["Type"] == "AWS::ECS::Cluster")
    settings = cluster["Properties"]["ClusterSettings"]
    assert any(s["Name"] == "containerInsights" and s["Value"] == "enabled" for s in settings)


def test_outputs(td):
    for n in ("ClusterName", "ClusterArn"):
        assert n in td["Outputs"], f"missing output: {n}"


def test_outputs_have_no_export(td):
    for name, out in td["Outputs"].items():
        assert "Export" not in out, f"output {name} should not have an Export"


def test_cluster_tagged_for_lrdev(td):
    cluster = next(r for r in td["Resources"].values() if r["Type"] == "AWS::ECS::Cluster")
    tags = {t["Key"]: t["Value"] for t in cluster["Properties"]["Tags"]}
    assert tags["Project"] == "lrdev"
    assert tags["ManagedBy"] == "lrdev-cfn"
    assert tags["Role"] == "cluster"
