"""Tests for the cardinal-data-setup CFN wrapper template."""

import json

import pytest

from cardinal_cfn.data_setup_lambda.template import build_template


@pytest.fixture
def template_dict():
    return json.loads(build_template().to_json())


def test_template_renders(template_dict):
    assert "Resources" in template_dict
    assert "Parameters" in template_dict
    assert "Outputs" in template_dict


def test_required_parameters_declared(template_dict):
    required = {
        "DataSetupLambdaRoleArn",
        "VpcId",
        "PrivateSubnets",
        "DbSgId",
        "LicenseData",
        "DbInstanceClass",
        "DbAllocatedStorage",
        "BucketLifecycleDays",
        "LambdaCodeS3Url",
    }
    assert required <= set(template_dict["Parameters"])


def test_split_lambda_code_params_are_gone(template_dict):
    """The previous LambdaCodeS3Bucket / LambdaCodeS3Key pair was replaced
    by a single full-URL parameter; make sure nothing reintroduces them."""
    params = set(template_dict["Parameters"])
    assert "LambdaCodeS3Bucket" not in params
    assert "LambdaCodeS3Key" not in params


def test_secret_parameters_marked_no_echo(template_dict):
    assert template_dict["Parameters"]["LicenseData"]["NoEcho"] is True


def test_oidc_parameters_not_passed_through(template_dict):
    """OIDC config is consumed by the lakerunner stack, not by data-setup."""
    params = set(template_dict["Parameters"])
    assert "DexAdminEmail" not in params
    assert "DexAdminPasswordHash" not in params
    assert "OidcSuperadminEmails" not in params


def test_lambda_function_uses_customer_supplied_role(template_dict):
    fn = template_dict["Resources"]["DataSetupFunction"]["Properties"]
    assert fn["Role"] == {"Ref": "DataSetupLambdaRoleArn"}
    assert fn["FunctionName"] == "cardinal-data-setup"
    assert fn["Runtime"] == "python3.11"
    assert fn["Handler"] == "handler.handler"
    assert fn["Timeout"] == 900


def test_custom_resource_invokes_the_lambda(template_dict):
    cr = template_dict["Resources"]["DataSetup"]
    assert cr["Type"] == "AWS::CloudFormation::CustomResource"
    assert cr["Properties"]["ServiceToken"] == {"Fn::GetAtt": ["DataSetupFunction", "Arn"]}


def test_outputs_cover_lambda_response_keys(template_dict):
    expected_outputs = {
        "DbEndpoint", "DbPort", "DbName",
        "DbMasterSecretArn", "MaestroDbSecretArn",
        "IngestBucketName", "IngestQueueUrl", "IngestQueueArn",
        "LicenseSecretArn", "InternalKeysSecretArn", "AdminKeySecretArn",
        "StorageProfilesParamName", "ApiKeysParamName",
    }
    assert expected_outputs == set(template_dict["Outputs"])


def test_outputs_use_get_att_on_custom_resource(template_dict):
    db_endpoint = template_dict["Outputs"]["DbEndpoint"]
    assert db_endpoint["Value"] == {"Fn::GetAtt": ["DataSetup", "DbEndpoint"]}


def test_naming_contract_lambda_function_name_is_stable(template_dict):
    """The Lambda function name is the contract; Lambda role inline policy ARNs depend on it."""
    fn = template_dict["Resources"]["DataSetupFunction"]["Properties"]
    assert fn["FunctionName"] == "cardinal-data-setup"


def test_default_lambda_code_url_targets_published_bucket(template_dict):
    """Default LambdaCodeS3Url points at the published regional bucket; pattern
    must match the AllowedPattern regex so the default is always self-validating."""
    import re

    p = template_dict["Parameters"]["LambdaCodeS3Url"]
    assert re.match(p["AllowedPattern"], p["Default"]), p["Default"]
    assert p["Default"].startswith("s3://cardinal-cfn")
    assert p["Default"].endswith("/cardinal-data-setup-lambda.zip")
    # Three slash-separated key segments (prefix/version/file).
    key = p["Default"].split("/", 3)[3]
    assert key.count("/") == 2


def test_lambda_code_property_parses_url_into_bucket_and_key(template_dict):
    """The Code.S3Bucket / Code.S3Key pair is derived from LambdaCodeS3Url
    via Fn::Split + Fn::Select + Fn::Join. Element indices encode the
    three-segment key shape that AllowedPattern enforces."""
    code = template_dict["Resources"]["DataSetupFunction"]["Properties"]["Code"]
    expected_split = {"Fn::Split": ["/", {"Ref": "LambdaCodeS3Url"}]}
    assert code["S3Bucket"] == {"Fn::Select": [2, expected_split]}
    assert code["S3Key"] == {"Fn::Join": ["/", [
        {"Fn::Select": [3, expected_split]},
        {"Fn::Select": [4, expected_split]},
        {"Fn::Select": [5, expected_split]},
    ]]}
