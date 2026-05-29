"""Loader for cardinal-defaults.yaml."""

import os

import yaml


_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_DEFAULTS_PATH = os.path.join(_REPO_ROOT, "cardinal-defaults.yaml")
_OTEL_CONFIG_PATH = os.path.join(_REPO_ROOT, "cardinal-otel-config.yaml")
_REMOTE_OTEL_CONFIG_PATH = os.path.join(_REPO_ROOT, "cardinal-remote-otel-config.yaml")


def load_defaults() -> dict:
    """Load the consolidated defaults YAML and return it as a dict.

    Raises ValueError when the file is empty, malformed, or does not parse
    to a mapping — surfacing build-time misconfigurations loudly.
    """
    with open(_DEFAULTS_PATH, "r") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(
            f"{_DEFAULTS_PATH}: expected a YAML mapping at the top level, "
            f"got {type(data).__name__}"
        )
    return data


def load_otel_default_config() -> str:
    """Return the cardinal-otel-config.yaml file as a YAML string.

    The cardinalhq-otel-collector image uses a run-with-env-config wrapper
    that reads the config from the CHQ_COLLECTOR_CONFIG_YAML env var. The
    otel child stack passes this string in by default; customers can
    override it via the OtelConfigYaml root parameter.
    """
    with open(_OTEL_CONFIG_PATH, "r") as f:
        return f.read()


def load_remote_otel_default_config() -> str:
    """Return cardinal-remote-otel-config.yaml as a string.

    Same shape as load_otel_default_config but with role_arn on each awss3
    exporter so the remote collector assumes the cross-account writer role.
    """
    with open(_REMOTE_OTEL_CONFIG_PATH, "r") as f:
        return f.read()
