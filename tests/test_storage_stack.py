import json
from lakerunner_storage import t as template


def test_template_is_valid_json():
    json.loads(template.to_json())


def test_bucket_and_queue_exist():
    template_dict = json.loads(template.to_json())
    resources = template_dict["Resources"]
    assert any(r["Type"] == "AWS::S3::Bucket" for r in resources.values())
    assert any(r["Type"] == "AWS::SQS::Queue" for r in resources.values())


def test_parameters_exist():
    template_dict = json.loads(template.to_json())
    params = template_dict["Parameters"]
    assert "ApiKeysOverride" in params
    assert "StorageProfilesOverride" in params
