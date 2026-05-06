"""Tests for the data-setup script generator."""

from cardinal_cfn.data_setup.render import render_data_setup_script


def test_render_emits_posix_shell():
    out = render_data_setup_script()
    assert out.startswith("#!/bin/sh\n")
    assert "set -eu" in out


def test_render_contains_required_ensure_functions():
    out = render_data_setup_script()
    for fn in [
        "ensure_db_subnet_group",
        "ensure_db_instance",
        "wait_db_available",
        "ensure_db_master_secret_value",
        "ensure_s3_bucket",
        "ensure_s3_lifecycle",
        "ensure_s3_block_public_access",
        "ensure_sqs_queue",
        "ensure_sqs_policy",
        "ensure_s3_notification",
        "ensure_secret_with_value",
        "ensure_ssm_parameter",
    ]:
        assert f"{fn}()" in out, f"missing helper {fn}"


def test_render_orders_queue_before_policy_before_notification():
    out = render_data_setup_script()
    q = out.find('QUEUE_URL=$(ensure_sqs_queue "$QUEUE_NAME")')
    p = out.find('ensure_sqs_policy "$QUEUE_URL"')
    n = out.find('ensure_s3_notification "$BUCKET_NAME"')
    assert 0 <= q < p < n


def test_render_orders_db_before_master_secret_value():
    out = render_data_setup_script()
    d = out.find("ensure_db_instance cardinal-db")
    w = out.find("wait_db_available cardinal-db")
    s = out.find("ensure_db_master_secret_value cardinal-db-master cardinal-db")
    assert 0 <= d < w < s


def test_render_uses_deterministic_resource_names():
    out = render_data_setup_script()
    assert 'BUCKET_NAME="cardinal-ingest-${ACCOUNT_ID}-${REGION}"' in out
    assert 'QUEUE_NAME="cardinal-ingest"' in out
    assert 'DB_IDENTIFIER="cardinal-db"' in out
    for purpose in [
        "cardinal-db-master",
        "cardinal-license",
        "cardinal-internal-keys",
        "cardinal-admin-key",
        "cardinal-maestro-db",
    ]:
        assert purpose in out


def test_render_writes_output_json_with_required_keys():
    out = render_data_setup_script()
    for key in [
        "DbEndpoint",
        "DbPort",
        "DbName",
        "DbMasterSecretArn",
        "MaestroDbSecretArn",
        "IngestBucketName",
        "IngestQueueUrl",
        "IngestQueueArn",
        "LicenseSecretArn",
        "InternalKeysSecretArn",
        "AdminKeySecretArn",
        "StorageProfilesParamName",
        "ApiKeysParamName",
    ]:
        assert key in out, f"missing output key {key}"


def test_render_emits_no_install_id_references():
    out = render_data_setup_script()
    assert "InstallId" not in out


def test_render_validates_required_args():
    out = render_data_setup_script()
    for arg in [
        "REGION",
        "VPC_ID",
        "PRIVATE_SUBNETS",
        "DB_SG_ID",
        "LICENSE_DATA_FILE",
        "DEX_ADMIN_EMAIL",
        "DEX_ADMIN_PASSWORD_HASH_FILE",
    ]:
        assert arg in out


def test_render_applies_required_tags():
    out = render_data_setup_script()
    for key in ["Application", "Component", "ManagedBy", "Name"]:
        assert key in out


def test_render_managed_by_is_data_setup_script():
    out = render_data_setup_script()
    assert 'MANAGED_BY="cardinal-data-setup-script"' in out
