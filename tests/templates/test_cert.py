"""Tests for the cert nested-stack template (no-Lambda IAM server cert form)."""

import json

import pytest

from cardinal_cfn.children import cert


@pytest.fixture
def template_dict():
    return json.loads(cert.build().to_json())


def _types(td):
    return [r["Type"] for r in td["Resources"].values()]


def test_required_parameters(template_dict):
    for n in ("InstallIdShort", "InstallIdLong", "CertificateArn",
              "CertificateBody", "CertificatePrivateKey", "CertificateChain"):
        assert n in template_dict["Parameters"], f"missing parameter: {n}"


def test_pem_parameters_are_no_echo(template_dict):
    for n in ("CertificateBody", "CertificatePrivateKey", "CertificateChain"):
        assert template_dict["Parameters"][n].get("NoEcho") is True, f"{n} must be NoEcho"


def test_cert_arn_default_empty(template_dict):
    assert template_dict["Parameters"]["CertificateArn"].get("Default") == ""


def test_no_cert_lambda_role_parameter(template_dict):
    """The cert-import Lambda is gone, so its role parameter must be too."""
    assert "CertLambdaRoleArn" not in template_dict["Parameters"]


def test_no_lambda_or_custom_resource(template_dict):
    types = _types(template_dict)
    assert "AWS::Lambda::Function" not in types, "cert must not create a Lambda"
    assert not any(t == "AWS::CloudFormation::CustomResource" or t.startswith("Custom::")
                   for t in types), "cert must not use a custom resource"


def test_no_internally_managed_iam_role(template_dict):
    roles = [r for r in template_dict["Resources"].values() if r["Type"] == "AWS::IAM::Role"]
    assert roles == []


def test_create_server_cert_condition(template_dict):
    # CertificateArn empty AND a PEM body provided.
    cond = template_dict["Conditions"]["CreateServerCert"]
    assert "Fn::And" in cond
    assert "HasCertChain" in template_dict["Conditions"]


def test_server_certificate_built_from_pems(template_dict):
    sc = template_dict["Resources"].get("ServerCertificate")
    assert sc is not None and sc["Type"] == "AWS::IAM::ServerCertificate"
    assert sc.get("Condition") == "CreateServerCert"
    props = sc["Properties"]
    assert props["CertificateBody"] == {"Ref": "CertificateBody"}
    assert props["PrivateKey"] == {"Ref": "CertificatePrivateKey"}
    assert props["CertificateChain"] == {
        "Fn::If": ["HasCertChain", {"Ref": "CertificateChain"}, {"Ref": "AWS::NoValue"}]
    }
    # CFN-generated physical name (the cert is only referenced by ARN).
    assert "ServerCertificateName" not in props


def test_effective_arn_output_is_conditional(template_dict):
    value = template_dict["Outputs"]["EffectiveCertificateArn"]["Value"]
    assert value["Fn::If"][0] == "CreateServerCert"
    assert value["Fn::If"][1] == {"Fn::GetAtt": ["ServerCertificate", "Arn"]}
    assert value["Fn::If"][2] == {"Ref": "CertificateArn"}
