"""Tests for the image manifest generator."""

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
