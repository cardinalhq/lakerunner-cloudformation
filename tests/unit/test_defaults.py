"""Tests for cardinal-defaults.yaml loader."""

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
