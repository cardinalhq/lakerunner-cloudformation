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


def test_pem_parameters_not_no_echo(template_dict):
    for n in ("CertificateBody", "CertificatePrivateKey", "CertificateChain"):
        assert "NoEcho" not in template_dict["Parameters"][n], (
            f"{n} must be a plain text parameter, not NoEcho"
        )


def test_cert_arn_default_empty(template_dict):
    assert template_dict["Parameters"]["CertificateArn"].get("Default") == ""


def test_import_cert_condition(template_dict):
    cond = template_dict["Conditions"]["ImportCert"]
    # And(Equals(CertificateArn, ""), Not(Equals(CertificateBody, "")))
    assert "Fn::And" in cond


def test_lambda_resources_gated_on_import_cert(template_dict):
    for logical in ("CertLambdaLogGroup", "CertLambdaRole",
                    "CertLambda", "ImportedCertificate"):
        res = template_dict["Resources"][logical]
        assert res.get("Condition") == "ImportCert", (
            f"{logical} must be conditioned on ImportCert"
        )


def test_lambda_role_grants_acm_import(template_dict):
    role = template_dict["Resources"]["CertLambdaRole"]
    statements = role["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
    actions = [a for s in statements for a in s["Action"]]
    assert "acm:ImportCertificate" in actions
    assert "acm:DeleteCertificate" in actions


def test_lambda_role_acm_import_unrestricted(template_dict):
    """ACM ImportCertificate has no ARN at create time, so policy must use '*' for it."""
    role = template_dict["Resources"]["CertLambdaRole"]
    statements = role["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
    import_stmts = [s for s in statements if "acm:ImportCertificate" in s["Action"]]
    assert import_stmts, "no statement granting acm:ImportCertificate"
    assert all(s["Resource"] == "*" for s in import_stmts), (
        "acm:ImportCertificate must be granted on Resource: '*'"
    )


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
