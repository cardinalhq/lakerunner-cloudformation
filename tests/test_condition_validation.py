import json

from lakerunner_storage import t as storage_template
from lakerunner_root import t as root_template


def test_storage_conditions_exist():
    conds = json.loads(storage_template.to_json())["Conditions"]
    assert "HasApiKeysOverride" in conds
    assert "HasStorageProfilesOverride" in conds


def test_root_deploy_conditions():
    conds = json.loads(root_template.to_json())["Conditions"]
    expected = {"DeployVpc", "DeployEcs", "DeployRds", "DeployStorage", "DeployMigration", "DeployServices", "DeployGrafanaService", "DeployOtelCollector"}
    assert expected.issubset(set(conds.keys()))
