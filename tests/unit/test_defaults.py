"""Tests for cardinal-defaults.yaml loader."""

from unittest import mock

import pytest

from cardinal_cfn import defaults
from cardinal_cfn.defaults import load_defaults


def test_load_defaults_returns_dict_with_expected_top_level_keys():
    d = load_defaults()
    expected = {"services", "images", "api_keys", "storage_profiles", "maestro", "otel"}
    assert expected.issubset(d.keys())


def test_load_defaults_services_has_query_worker():
    d = load_defaults()
    assert "lakerunner-query-worker" in d["services"]


def test_load_defaults_images_has_lakerunner():
    d = load_defaults()
    assert "lakerunner" in d["images"]


def test_load_defaults_raises_on_empty_file(tmp_path):
    """An empty defaults file would silently surface as TypeError downstream."""
    empty = tmp_path / "empty.yaml"
    empty.write_text("")
    with mock.patch.object(defaults, "_DEFAULTS_PATH", str(empty)):
        with pytest.raises(ValueError, match="expected a YAML mapping"):
            load_defaults()


def test_load_defaults_raises_on_non_mapping_yaml(tmp_path):
    """A scalar or list at the top level is not a usable defaults file."""
    bad = tmp_path / "bad.yaml"
    bad.write_text("- one\n- two\n")
    with mock.patch.object(defaults, "_DEFAULTS_PATH", str(bad)):
        with pytest.raises(ValueError, match="expected a YAML mapping"):
            load_defaults()
