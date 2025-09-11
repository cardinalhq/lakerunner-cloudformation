import json
from lakerunner_ecs import t as template


def test_vpc_parameter_exists():
    template_dict = json.loads(template.to_json())
    assert "VpcId" in template_dict["Parameters"]


def test_resources_exist():
    template_dict = json.loads(template.to_json())
    resources = template_dict["Resources"]
    assert any(r["Type"] == "AWS::EC2::SecurityGroup" for r in resources.values())
    assert any(r["Type"] == "AWS::ECS::Cluster" for r in resources.values())
