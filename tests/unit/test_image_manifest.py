"""Tests for the image manifest + image-reference helpers."""

import pytest

from cardinal_cfn import image_manifest
from cardinal_cfn.defaults import load_defaults


def test_satellite_manifest_is_otel_image():
    assert image_manifest.manifest_lines("satellite") == [
        load_defaults()["images"]["otel"]
    ]


def test_unknown_stack_raises():
    with pytest.raises(ValueError):
        image_manifest.manifest_lines("nope")


def test_image_ref_returns_pinned_default():
    assert image_manifest.image_ref("otel") == load_defaults()["images"]["otel"]


def test_image_ref_unknown_key_raises():
    with pytest.raises(ValueError):
        image_manifest.image_ref("nope")


def test_registry_relative_strips_registry_host():
    ref = "public.ecr.aws/cardinalhq.io/cardinalhq-otel-collector:v1.8.0@sha256:abc"
    assert image_manifest.registry_relative(ref) == (
        "cardinalhq.io/cardinalhq-otel-collector:v1.8.0@sha256:abc"
    )


def test_registry_relative_requires_registry():
    with pytest.raises(ValueError):
        image_manifest.registry_relative("busybox:1.37")


def test_otel_suffix_matches_default_minus_registry():
    otel = load_defaults()["images"]["otel"]
    assert image_manifest.registry_relative(otel) == otel.split("/", 1)[1]
