"""Behavioral tests for the cert-import custom-resource Lambda."""

import builtins
import json
import sys
import types
from unittest import mock

import pytest

from cardinal_cfn.children import cert_lambda


SAMPLE_CERT = "-----BEGIN CERTIFICATE-----\nMIIBkTCB+...AAA=\n-----END CERTIFICATE-----\n"
SAMPLE_KEY = "-----BEGIN PRIVATE KEY-----\nMIIBOgIBAAJBA...AAA=\n-----END PRIVATE KEY-----\n"
SAMPLE_ARN = "arn:aws:acm:us-east-1:111122223333:certificate/abc-123"


class _ClientError(Exception):
    """Stand-in for botocore.exceptions.ClientError used by the lambda."""

    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


@pytest.fixture
def lambda_module():
    fake_boto3 = types.SimpleNamespace(
        client=lambda name: mock.MagicMock(name=f"boto3.{name}")
    )
    fake_botocore_exceptions = types.ModuleType("botocore.exceptions")
    fake_botocore_exceptions.ClientError = _ClientError
    fake_botocore = types.ModuleType("botocore")
    fake_botocore.exceptions = fake_botocore_exceptions

    saved = {k: sys.modules.get(k) for k in ("boto3", "botocore", "botocore.exceptions")}
    sys.modules["boto3"] = fake_boto3
    sys.modules["botocore"] = fake_botocore
    sys.modules["botocore.exceptions"] = fake_botocore_exceptions
    try:
        ns = {"__name__": "cert_lambda_under_test"}
        compiled = compile(cert_lambda.SOURCE, "<cert_lambda>", "exec")
        builtins.exec(compiled, ns)
        ns["urllib"].request.urlopen = mock.MagicMock(
            name="urlopen", return_value=mock.MagicMock(read=lambda: b"")
        )
        ns["time"].sleep = lambda *_a, **_kw: None
        yield ns
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def _event(request_type, *, body=SAMPLE_CERT, key=SAMPLE_KEY, chain="",
           install_id_long="abcdef012345", physical_id=None, old=None):
    event = {
        "RequestType": request_type,
        "StackId": "arn:aws:cloudformation:us-east-1:1234567890:stack/test/abc",
        "RequestId": "rid-1",
        "LogicalResourceId": "ImportedCertificate",
        "ResponseURL": "https://cfn.example.com/response",
        "ResourceProperties": {
            "InstallIdLong": install_id_long,
            "CertificateBody": body,
            "CertificatePrivateKey": key,
            "CertificateChain": chain,
        },
    }
    if physical_id is not None:
        event["PhysicalResourceId"] = physical_id
    if old is not None:
        event["OldResourceProperties"] = old
    return event


def _context():
    ctx = mock.MagicMock()
    ctx.log_stream_name = "log-stream"
    return ctx


def _last_response_payload(lambda_module):
    urlopen = lambda_module["urllib"].request.urlopen
    request = urlopen.call_args.args[0]
    return json.loads(request.data.decode("utf-8"))


def test_create_imports_and_returns_arn(lambda_module):
    acm = lambda_module["acm"]
    acm.import_certificate.return_value = {"CertificateArn": SAMPLE_ARN}
    lambda_module["lambda_handler"](_event("Create"), _context())
    body = _last_response_payload(lambda_module)
    assert body["Status"] == "SUCCESS"
    assert body["PhysicalResourceId"] == SAMPLE_ARN
    assert body["Data"]["CertificateArn"] == SAMPLE_ARN
    kwargs = acm.import_certificate.call_args.kwargs
    assert "CertificateArn" not in kwargs
    assert kwargs["Certificate"] == SAMPLE_CERT.encode("utf-8")
    assert kwargs["PrivateKey"] == SAMPLE_KEY.encode("utf-8")


def test_create_with_chain_passes_chain(lambda_module):
    acm = lambda_module["acm"]
    acm.import_certificate.return_value = {"CertificateArn": SAMPLE_ARN}
    chain = "-----BEGIN CERTIFICATE-----\nintermediate\n-----END CERTIFICATE-----\n"
    lambda_module["lambda_handler"](_event("Create", chain=chain), _context())
    kwargs = acm.import_certificate.call_args.kwargs
    assert kwargs["CertificateChain"] == chain.encode("utf-8")


def test_create_tags_certificate(lambda_module):
    acm = lambda_module["acm"]
    acm.import_certificate.return_value = {"CertificateArn": SAMPLE_ARN}
    lambda_module["lambda_handler"](_event("Create", install_id_long="deadbeef0001"), _context())
    acm.add_tags_to_certificate.assert_called_once()
    tags = acm.add_tags_to_certificate.call_args.kwargs["Tags"]
    keys = {t["Key"]: t["Value"] for t in tags}
    assert keys["cardinal:component"] == "cert"
    assert keys["cardinal:install-id-long"] == "deadbeef0001"
    assert keys["Name"] == "cardinal-cert-deadbeef0001"


def test_update_unchanged_skips_import(lambda_module):
    acm = lambda_module["acm"]
    old_props = {
        "CertificateBody": SAMPLE_CERT,
        "CertificatePrivateKey": SAMPLE_KEY,
        "CertificateChain": "",
    }
    lambda_module["lambda_handler"](
        _event("Update", physical_id=SAMPLE_ARN, old=old_props),
        _context(),
    )
    body = _last_response_payload(lambda_module)
    assert body["Status"] == "SUCCESS"
    assert body["PhysicalResourceId"] == SAMPLE_ARN
    assert not acm.import_certificate.called


def test_update_changed_reimports_into_existing_arn(lambda_module):
    acm = lambda_module["acm"]
    acm.import_certificate.return_value = {"CertificateArn": SAMPLE_ARN}
    old_props = {
        "CertificateBody": "OLD CERT",
        "CertificatePrivateKey": SAMPLE_KEY,
        "CertificateChain": "",
    }
    lambda_module["lambda_handler"](
        _event("Update", physical_id=SAMPLE_ARN, old=old_props),
        _context(),
    )
    body = _last_response_payload(lambda_module)
    assert body["Status"] == "SUCCESS"
    assert body["PhysicalResourceId"] == SAMPLE_ARN
    kwargs = acm.import_certificate.call_args.kwargs
    assert kwargs["CertificateArn"] == SAMPLE_ARN


def test_delete_removes_cert(lambda_module):
    acm = lambda_module["acm"]
    lambda_module["lambda_handler"](
        _event("Delete", physical_id=SAMPLE_ARN), _context()
    )
    body = _last_response_payload(lambda_module)
    assert body["Status"] == "SUCCESS"
    acm.delete_certificate.assert_called_once_with(CertificateArn=SAMPLE_ARN)


def test_delete_with_no_arn_is_noop(lambda_module):
    acm = lambda_module["acm"]
    lambda_module["lambda_handler"](
        _event("Delete", physical_id="cardinal-cert-noop"), _context()
    )
    body = _last_response_payload(lambda_module)
    assert body["Status"] == "SUCCESS"
    assert not acm.delete_certificate.called


def test_delete_retries_on_in_use(lambda_module):
    acm = lambda_module["acm"]
    acm.delete_certificate.side_effect = [
        _ClientError("ResourceInUseException"),
        _ClientError("ResourceInUseException"),
        None,
    ]
    lambda_module["lambda_handler"](
        _event("Delete", physical_id=SAMPLE_ARN), _context()
    )
    body = _last_response_payload(lambda_module)
    assert body["Status"] == "SUCCESS"
    assert acm.delete_certificate.call_count == 3


def test_delete_swallows_already_gone(lambda_module):
    acm = lambda_module["acm"]
    acm.delete_certificate.side_effect = _ClientError("ResourceNotFoundException")
    lambda_module["lambda_handler"](
        _event("Delete", physical_id=SAMPLE_ARN), _context()
    )
    body = _last_response_payload(lambda_module)
    assert body["Status"] == "SUCCESS"


def test_create_missing_pem_fails(lambda_module):
    lambda_module["lambda_handler"](
        _event("Create", body="", key=""), _context()
    )
    body = _last_response_payload(lambda_module)
    assert body["Status"] == "FAILED"
    assert "required" in body["Reason"].lower()


def test_create_failure_reports_failed_to_cfn(lambda_module):
    acm = lambda_module["acm"]
    acm.import_certificate.side_effect = RuntimeError("invalid PEM")
    lambda_module["lambda_handler"](_event("Create"), _context())
    body = _last_response_payload(lambda_module)
    assert body["Status"] == "FAILED"
    assert "invalid PEM" in body["Reason"]
