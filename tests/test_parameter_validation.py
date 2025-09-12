import json

from lakerunner_ecs import t as ecs_template
from lakerunner_rds import t as rds_template
from lakerunner_s3 import t as storage_template


def test_ecs_vpc_parameter_type():
    params = json.loads(ecs_template.to_json())["Parameters"]
    assert params["VpcId"]["Type"] == "AWS::EC2::VPC::Id"


def test_rds_parameters_exist():
    params = json.loads(rds_template.to_json())["Parameters"]
    assert "PrivateSubnets" in params
    assert "TaskSecurityGroupId" in params


def test_storage_override_defaults():
    params = json.loads(storage_template.to_json())["Parameters"]
    assert params["ApiKeysOverride"]["Default"] == ""
    assert params["StorageProfilesOverride"]["Default"] == ""
