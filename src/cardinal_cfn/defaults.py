"""Loader for cardinal-defaults.yaml."""

import os

import yaml


_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_DEFAULTS_PATH = os.path.join(_REPO_ROOT, "cardinal-defaults.yaml")


def load_defaults() -> dict:
    """Load the consolidated defaults YAML and return it as a dict."""
    with open(_DEFAULTS_PATH, "r") as f:
        return yaml.safe_load(f)
