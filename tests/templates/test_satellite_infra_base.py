"""Tests for the cardinal-satellite-infra-base standalone template."""

import json

import pytest

from cardinal_cfn import satellite_infra_base


@pytest.fixture
def td():
    return json.loads(satellite_infra_base.build().to_json())


def test_required_parameters(td):
    for n in (
        "LakerunnerPrincipal",
        "ExternalId",
        "RawBucketName",
        "RawBucketLifecycleDays",
    ):
        assert n in td["Parameters"], f"missing parameter: {n}"


def test_description_mentions_pull_model(td):
    assert "pull" in td["Description"].lower()
