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
        "LambdaCodeS3Bucket",
        "LambdaCodeS3Key",
    }
    assert required <= set(template_dict["Parameters"])


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


def test_default_lambda_code_path_uses_published_bucket(template_dict):
    bucket_default = template_dict["Parameters"]["LambdaCodeS3Bucket"]["Default"]
    key_default = template_dict["Parameters"]["LambdaCodeS3Key"]["Default"]
    assert bucket_default == "cardinal-cfn"
    assert key_default.startswith("lakerunner/") and key_default.endswith("/cardinal-data-setup-lambda.zip")
