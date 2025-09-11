import json
from lakerunner_rds import t as template


def test_required_parameters_exist():
    template_dict = json.loads(template.to_json())
    params = template_dict["Parameters"]
    assert "PrivateSubnets" in params
    assert "TaskSecurityGroupId" in params


def test_database_resource_exists():
    template_dict = json.loads(template.to_json())
    resources = template_dict["Resources"]
    assert any(r["Type"] == "AWS::RDS::DBInstance" for r in resources.values())
