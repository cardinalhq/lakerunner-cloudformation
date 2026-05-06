"""Tests for the cardinal data-setup Lambda handler.

These exercise the pure-data and naming-contract pieces. Behavioral
tests against actual AWS APIs would require ``moto`` and are out of
scope for the build-time test suite (the handler is exercised in CI
against a live account before publish).
"""

import json

import pytest
from botocore.exceptions import ClientError

from cardinal_cfn.data_setup_lambda import handler as h


def test_naming_contract_constants():
    assert h.DB_IDENTIFIER == "cardinal-db"
    assert h.DB_SUBNET_GROUP_NAME == "cardinal-db-subnet-group"
    assert h.DB_NAME == "lakerunner"
    assert h.DB_PORT == 5432
    assert h.SQS_QUEUE_NAME == "cardinal-ingest"
    assert h.SECRET_NAMES == {
        "db_master": "cardinal-db-master",
        "license": "cardinal-license",
        "internal_keys": "cardinal-internal-keys",
        "admin_key": "cardinal-admin-key",
        "maestro_db": "cardinal-maestro-db",
    }
    assert h.SSM_PARAM_NAMES == {
        "storage_profiles": "/cardinal/storage-profiles",
        "api_keys": "/cardinal/api-keys",
    }


def test_bucket_name_uses_account_and_region():
    assert h._bucket_name("123456789012", "us-east-2") == "cardinal-ingest-123456789012-us-east-2"


def test_queue_arn_is_well_formed():
    assert h._queue_arn("123456789012", "us-east-2") == "arn:aws:sqs:us-east-2:123456789012:cardinal-ingest"


def test_common_tags_carry_required_keys():
    tags = {t["Key"]: t["Value"] for t in h._common_tags("ingest-bucket")}
    assert {"Application", "Project", "ManagedBy", "Component", "Name"} <= set(tags)
    assert tags["Application"] == "cardinal-lakerunner"
    assert tags["ManagedBy"] == "cardinal-data-setup-lambda"
    assert tags["Component"] == "ingest-bucket"
    assert tags["Name"] == "cardinal-ingest-bucket"


def test_props_extracts_resource_properties_for_cfn_event():
    event = {"ResourceProperties": {"VpcId": "vpc-x", "RequestType": "Create"}, "RequestType": "Create"}
    assert h._props(event) == {"VpcId": "vpc-x", "RequestType": "Create"}


def test_props_falls_back_to_event_for_direct_invoke():
    event = {"VpcId": "vpc-x"}
    assert h._props(event) == event


def test_str_required_raises():
    with pytest.raises(ValueError, match="missing required property: VpcId"):
        h._str({}, "VpcId")


def test_str_default_used_when_missing():
    assert h._str({}, "DbInstanceClass", "db.t3.medium") == "db.t3.medium"


def test_int_coerces_string_input():
    assert h._int({"DbAllocatedStorage": "100"}, "DbAllocatedStorage") == 100


def test_list_accepts_string_csv():
    assert h._list({"PrivateSubnets": "subnet-a, subnet-b , subnet-c"}, "PrivateSubnets") == ["subnet-a", "subnet-b", "subnet-c"]


def test_list_accepts_actual_list():
    assert h._list({"PrivateSubnets": ["subnet-a", "subnet-b"]}, "PrivateSubnets") == ["subnet-a", "subnet-b"]


def test_list_required_raises():
    with pytest.raises(ValueError, match="missing required property: PrivateSubnets"):
        h._list({}, "PrivateSubnets")


def test_generate_password_length_and_no_punctuation():
    pw = h._generate_password()
    assert len(pw) == 40
    # excludes shell-breaking and ambiguous chars
    assert all(c.isalnum() for c in pw)


def test_random_hex_length():
    assert len(h._random_hex(32)) == 64  # 32 bytes -> 64 hex chars


def test_handler_delete_event_is_noop(monkeypatch):
    """Delete events MUST NOT touch any AWS APIs in the default policy."""
    called = []
    monkeypatch.setattr(h, "_send_cfn_response", lambda *a, **kw: called.append("response"))
    monkeypatch.setattr(h, "run", lambda *_a, **_kw: pytest.fail("run() should not be called on Delete"))
    event = {
        "RequestType": "Delete",
        "ResponseURL": "https://example.invalid",
        "StackId": "arn:...",
        "RequestId": "rid",
        "LogicalResourceId": "Custom",
        "PhysicalResourceId": "cardinal-data-setup",
    }
    result = h.handler(event, None)
    assert result == {"status": "noop-on-delete"}
    assert called == ["response"]


def test_handler_failure_sends_failed_to_cfn(monkeypatch):
    captured = {}

    def fake_send(event, status, data, reason=""):
        captured["status"] = status
        captured["reason"] = reason

    monkeypatch.setattr(h, "_send_cfn_response", fake_send)
    monkeypatch.setattr(h, "run", lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("boom")))
    event = {
        "RequestType": "Create",
        "ResponseURL": "https://example.invalid",
        "StackId": "arn:...",
        "RequestId": "rid",
        "LogicalResourceId": "Custom",
        "ResourceProperties": {},
    }
    with pytest.raises(RuntimeError):
        h.handler(event, None)
    assert captured["status"] == "FAILED"
    assert "boom" in captured["reason"]


def test_handler_direct_invoke_returns_run_result(monkeypatch):
    monkeypatch.setattr(h, "run", lambda props, region=None: {"DbEndpoint": "x"})
    event = {"VpcId": "vpc-x", "Region": "us-east-2"}  # no RequestType, no ResponseURL
    out = h.handler(event, None)
    assert out == {"DbEndpoint": "x"}


# ---------------------------------------------------------------------------
# Password convergence -- the secret is the source of truth across re-runs.
# ---------------------------------------------------------------------------
class _FakeSecretsManager:
    """Minimal in-memory stand-in for boto3 secretsmanager client."""

    def __init__(self, *, exists: dict | None = None):
        self._secrets: dict = {}
        self._next_arn = 0
        if exists:
            for name, value in exists.items():
                self._next_arn += 1
                self._secrets[name] = {
                    "ARN": f"arn:aws:secretsmanager:us-east-2:111:secret:{name}-AABBCC",
                    "SecretString": value,
                }

    def describe_secret(self, *, SecretId):  # noqa: N803 -- boto3 kw shape
        if SecretId not in self._secrets:
            raise ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "not found"}}, "DescribeSecret")
        return {"ARN": self._secrets[SecretId]["ARN"]}

    def get_secret_value(self, *, SecretId):  # noqa: N803
        if SecretId not in self._secrets:
            raise ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "not found"}}, "GetSecretValue")
        return {"SecretString": self._secrets[SecretId]["SecretString"]}

    def create_secret(self, *, Name, Description, SecretString, Tags):  # noqa: N803
        if Name in self._secrets:
            raise ClientError({"Error": {"Code": "ResourceExistsException", "Message": "exists"}}, "CreateSecret")
        self._next_arn += 1
        arn = f"arn:aws:secretsmanager:us-east-2:111:secret:{Name}-NEW{self._next_arn:03d}"
        self._secrets[Name] = {"ARN": arn, "SecretString": SecretString}
        return {"ARN": arn}


def test_db_master_secret_first_create_generates_password_and_writes_secret():
    sm = _FakeSecretsManager()
    arn, password = h.ensure_db_master_secret(sm, name="cardinal-db-master", username="lakerunner")
    assert arn.startswith("arn:aws:secretsmanager:")
    assert len(password) == 40
    stored = json.loads(sm._secrets["cardinal-db-master"]["SecretString"])
    assert stored["username"] == "lakerunner"
    assert stored["password"] == password


def test_db_master_secret_rerun_returns_existing_password():
    """Convergence: secret is the source of truth. The DB instance must be
    created with the password already in the secret -- not a fresh one --
    on re-run, otherwise password drift breaks DB authentication."""
    existing = json.dumps({"username": "lakerunner", "password": "EXISTING-PASSWORD-FROM-PRIOR-RUN"})
    sm = _FakeSecretsManager(exists={"cardinal-db-master": existing})

    arn, password = h.ensure_db_master_secret(sm, name="cardinal-db-master", username="lakerunner")
    assert password == "EXISTING-PASSWORD-FROM-PRIOR-RUN", \
        "ensure_db_master_secret must return the existing secret's password on re-run"
    # Secret is unmodified (still has the original value).
    assert sm._secrets["cardinal-db-master"]["SecretString"] == existing


def test_db_master_secret_rejects_secret_missing_password_key():
    """Refuse to silently degrade: a secret that exists but lacks a password
    key is corrupted state, not something to overwrite."""
    sm = _FakeSecretsManager(exists={"cardinal-db-master": json.dumps({"username": "lakerunner"})})
    with pytest.raises(ValueError, match="missing 'password' key"):
        h.ensure_db_master_secret(sm, name="cardinal-db-master", username="lakerunner")


# ---------------------------------------------------------------------------
# wait_db_available -- timeout sized to fit Lambda execution cap
# ---------------------------------------------------------------------------
def test_wait_db_available_default_timeout_below_lambda_cap():
    """The Lambda function's Timeout in the wrapper template is 900s. The
    poll's default deadline must leave headroom for the handler to send a
    cfn-response on FAILED, otherwise the custom resource hangs."""
    import inspect
    sig = inspect.signature(h.wait_db_available)
    default_timeout = sig.parameters["timeout_seconds"].default
    assert default_timeout <= 880, \
        f"wait_db_available timeout ({default_timeout}s) must leave room for cfn-response under the 900s Lambda cap"
