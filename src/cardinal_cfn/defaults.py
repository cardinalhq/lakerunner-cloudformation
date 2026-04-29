"""Loader for cardinal-defaults.yaml."""

import os

import yaml


_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_DEFAULTS_PATH = os.path.join(_REPO_ROOT, "cardinal-defaults.yaml")


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
