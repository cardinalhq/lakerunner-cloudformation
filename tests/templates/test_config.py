"""Tests for the config nested-stack template."""

import json

import pytest

from cardinal_cfn.children import config


@pytest.fixture
def template_dict():
    return json.loads(config.build().to_json())


def test_no_secure_string_ssm(template_dict):
    """Spec: AWS::SSM::Parameter cannot be SecureString in CFN."""
    for r in template_dict["Resources"].values():
        if r["Type"] == "AWS::SSM::Parameter":
            assert r["Properties"]["Type"] == "String"


def test_sensitive_params_use_no_echo(template_dict):
    for name in ("LicenseData", "ApiKeysOverride", "StorageProfilesOverride"):
        assert template_dict["Parameters"][name]["NoEcho"] is True


def test_creates_three_secrets(template_dict):
    secrets = [r for r in template_dict["Resources"].values()
               if r["Type"] == "AWS::SecretsManager::Secret"]
    assert len(secrets) >= 3


def test_admin_api_key_retained(template_dict):
    by_id = template_dict["Resources"]
    admin = next((v for k, v in by_id.items() if "AdminApiKey" in k), None)
    assert admin is not None
    assert admin.get("DeletionPolicy") == "Retain"


def test_internal_service_keys_deleted(template_dict):
    by_id = template_dict["Resources"]
    isk = next((v for k, v in by_id.items() if "InternalServiceKeys" in k), None)
    assert isk is not None
    assert isk.get("DeletionPolicy") == "Delete"


def test_outputs_required(template_dict):
    for n in ("LicenseSecretArn", "InternalServiceKeysSecretArn",
              "AdminApiKeySecretArn", "StorageProfilesParamName", "ApiKeysParamName"):
        assert n in template_dict["Outputs"]
