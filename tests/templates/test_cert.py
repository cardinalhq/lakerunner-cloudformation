"""Tests for the cert nested-stack template."""

import json

import pytest

from cardinal_cfn.children import cert


@pytest.fixture
def template_dict():
    return json.loads(cert.build().to_json())


def test_required_parameters(template_dict):
    for n in ("InstallIdShort", "InstallIdLong",
              "CertificateArn", "CertificateBody",
              "CertificatePrivateKey", "CertificateChain"):
        assert n in template_dict["Parameters"], f"missing parameter: {n}"


def test_pem_parameters_are_no_echo(template_dict):
    for n in ("CertificateBody", "CertificatePrivateKey", "CertificateChain"):
        assert template_dict["Parameters"][n].get("NoEcho") is True, (
            f"{n} must be NoEcho=true"
        )


def test_cert_arn_default_empty(template_dict):
    assert template_dict["Parameters"]["CertificateArn"].get("Default") == ""


def test_import_cert_condition(template_dict):
    cond = template_dict["Conditions"]["ImportCert"]
    # And(Equals(CertificateArn, ""), Not(Equals(CertificateBody, "")))
    assert "Fn::And" in cond


def test_lambda_resources_gated_on_import_cert(template_dict):
    for logical in ("CertLambdaLogGroup", "CertLambda", "ImportedCertificate"):
        res = template_dict["Resources"][logical]
        assert res.get("Condition") == "ImportCert", (
            f"{logical} must be conditioned on ImportCert"
        )


def test_no_internally_managed_iam_role(template_dict):
    """Phase 2: the cert-import Lambda's role is supplied as a parameter."""
    roles = [r for r in template_dict["Resources"].values()
             if r["Type"] == "AWS::IAM::Role"]
    assert len(roles) == 0


def test_cert_lambda_role_arn_parameter(template_dict):
    assert "CertLambdaRoleArn" in template_dict["Parameters"]
    assert template_dict["Parameters"]["CertLambdaRoleArn"]["Default"] == ""


def test_lambda_role_uses_parameter(template_dict):
    fn = template_dict["Resources"]["CertLambda"]
    assert fn["Properties"]["Role"] == {"Ref": "CertLambdaRoleArn"}


def test_custom_resource_passes_pem_props(template_dict):
    cr = template_dict["Resources"]["ImportedCertificate"]
    props = cr["Properties"]
    for n in ("CertificateBody", "CertificatePrivateKey", "CertificateChain",
              "InstallIdLong"):
        assert n in props, f"custom resource missing prop {n}"


def test_effective_arn_output_is_conditional(template_dict):
    out = template_dict["Outputs"]["EffectiveCertificateArn"]
    value = out["Value"]
    assert "Fn::If" in value
    branches = value["Fn::If"]
    assert branches[0] == "ImportCert"
    # Then-branch reads from custom resource; else-branch passes through.
    assert "Fn::GetAtt" in branches[1]
    assert branches[1]["Fn::GetAtt"] == ["ImportedCertificate", "CertificateArn"]
    assert branches[2] == {"Ref": "CertificateArn"}


def test_lambda_runtime_pinned(template_dict):
    fn = template_dict["Resources"]["CertLambda"]
    assert fn["Properties"]["Runtime"].startswith("python3."), (
        "Lambda runtime must be a python3.x"
    )
