"""Tests for the cardinal-infrastructure standalone template."""

import json

import pytest

from cardinal_cfn import cardinal_infrastructure


@pytest.fixture
def td():
    return json.loads(cardinal_infrastructure.build().to_json())


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------


def test_required_parameters(td):
    for n in (
        "PrivateSubnets",
        "DBSecurityGroupId",
        "DBEngineVersion",
        "DBInstanceClass",
        "DBAllocatedStorage",
        "IngestBucketLifecycleDays",
        "IngestBucketName",
        "LicenseSecretName",
        "AdminKeySecretName",
        "StorageProfilesParamName",
        "ApiKeysParamName",
        "LicenseData",
    ):
        assert n in td["Parameters"], f"missing parameter: {n}"


def test_license_data_is_no_echo(td):
    assert td["Parameters"]["LicenseData"].get("NoEcho") is True


def test_db_engine_version_default_matches_test_account(td):
    """The script's test deploys land on Postgres 18.3 -- pin same default."""
    assert td["Parameters"]["DBEngineVersion"]["Default"] == "18.3"


def test_db_instance_class_default(td):
    assert td["Parameters"]["DBInstanceClass"]["Default"] == "db.t3.medium"


def test_db_allocated_storage_default(td):
    assert td["Parameters"]["DBAllocatedStorage"]["Default"] == 100


def test_lifecycle_days_default(td):
    assert td["Parameters"]["IngestBucketLifecycleDays"]["Default"] == 7


def test_recovery_override_defaults(td):
    """Defaults match the names data-setup.sh creates today."""
    assert td["Parameters"]["LicenseSecretName"]["Default"] == "cardinal-license"
    assert td["Parameters"]["AdminKeySecretName"]["Default"] == "cardinal-admin-key"
    assert (
        td["Parameters"]["StorageProfilesParamName"]["Default"]
        == "/cardinal/storage-profiles"
    )
    assert td["Parameters"]["ApiKeysParamName"]["Default"] == "/cardinal/api-keys"


def test_ingest_bucket_name_default_is_blank(td):
    """Blank default => template synthesizes cardinal-ingest-<acct>-<region>."""
    assert td["Parameters"]["IngestBucketName"]["Default"] == ""


def test_no_install_id_parameters(td):
    for n in ("InstallIdShort", "InstallIdLong"):
        assert n not in td["Parameters"], f"{n} should not be a parameter"


# ---------------------------------------------------------------------------
# Resources -- presence
# ---------------------------------------------------------------------------


def _types(td):
    return [r["Type"] for r in td["Resources"].values()]


def test_creates_rds_instance_and_subnet_group(td):
    types = _types(td)
    assert types.count("AWS::RDS::DBInstance") == 1
    assert types.count("AWS::RDS::DBSubnetGroup") == 1


def test_creates_secret_target_attachment(td):
    types = _types(td)
    assert types.count("AWS::SecretsManager::SecretTargetAttachment") == 1


def test_creates_managed_secrets(td):
    """db-master + license + admin-key = 3 (maestro reuses the master)."""
    secrets = [
        r for r in td["Resources"].values()
        if r["Type"] == "AWS::SecretsManager::Secret"
    ]
    assert len(secrets) == 3


def test_creates_two_ssm_parameters(td):
    types = _types(td)
    assert types.count("AWS::SSM::Parameter") == 2


def test_ssm_seeds_render_nonempty_yaml_lists(td):
    # Regression: the migrator imports these into configdb and needs YAML
    # *lists*. A top-level map ("{}") fails to unmarshal into
    # []initialize.StorageProfile and aborts the migration -- the bug that hit
    # CFN-infra installs because #110 fixed only data-setup.sh, not this path.
    sp = td["Resources"]["StorageProfilesParam"]["Properties"]["Value"]
    body = sp["Fn::Sub"][0] if isinstance(sp, dict) else sp
    assert body.lstrip().startswith("- "), f"storage profiles must be a YAML list: {body!r}"
    assert "{}" not in body
    for field in ("organization_id", "collector_name", "cloud_provider", "region", "bucket"):
        assert field in body, f"storage profile missing {field}"

    ak = td["Resources"]["ApiKeysParam"]["Properties"]["Value"]
    assert ak == "[]", f"api keys must seed an empty list, got: {ak!r}"


def test_creates_ingest_bucket_and_queue(td):
    types = _types(td)
    assert types.count("AWS::S3::Bucket") == 1
    assert types.count("AWS::SQS::Queue") == 1
    assert types.count("AWS::SQS::QueuePolicy") == 1


# ---------------------------------------------------------------------------
# Resources -- shape
# ---------------------------------------------------------------------------


def _by_logical_id(td, logical_id):
    return td["Resources"][logical_id]


def test_rds_engine_settings(td):
    rds = _by_logical_id(td, "DBInstance")["Properties"]
    assert rds["Engine"] == "postgres"
    assert rds["StorageEncrypted"] is True
    assert rds["StorageType"] == "gp3"
    assert rds["PubliclyAccessible"] is False
    assert rds["MultiAZ"] is False
    assert rds["DeletionProtection"] is True
    assert rds["BackupRetentionPeriod"] == 7
    assert rds["Port"] == 5432
    assert rds["DBName"] == "lakerunner"
    assert rds["MasterUsername"] == "lakerunner"


def test_rds_master_password_resolves_from_secret(td):
    rds = _by_logical_id(td, "DBInstance")["Properties"]
    sub = rds["MasterUserPassword"]["Fn::Sub"]
    # ['{{resolve:secretsmanager:${SecretArn}::password}}', {'SecretArn': {...}}]
    assert "resolve:secretsmanager" in sub[0]
    assert sub[1]["SecretArn"] == {"Ref": "DBMasterSecret"}


def test_rds_uses_snapshot_policy(td):
    rds = _by_logical_id(td, "DBInstance")
    assert rds["DeletionPolicy"] == "Snapshot"
    assert rds["UpdateReplacePolicy"] == "Snapshot"


def test_db_subnet_group_takes_private_subnets(td):
    sg = _by_logical_id(td, "DBSubnetGroup")["Properties"]
    assert sg["SubnetIds"] == {"Ref": "PrivateSubnets"}


def test_db_master_secret_generates_password(td):
    secret = _by_logical_id(td, "DBMasterSecret")["Properties"]
    gen = secret["GenerateSecretString"]
    assert gen["GenerateStringKey"] == "password"
    assert gen["PasswordLength"] == 40
    assert gen["ExcludePunctuation"] is True
    assert "lakerunner" in gen["SecretStringTemplate"]


def test_target_attachment_links_secret_and_db(td):
    att = _by_logical_id(td, "DBMasterSecretAttachment")["Properties"]
    assert att["SecretId"] == {"Ref": "DBMasterSecret"}
    assert att["TargetId"] == {"Ref": "DBInstance"}
    assert att["TargetType"] == "AWS::RDS::DBInstance"


def test_license_secret_uses_parameter_value(td):
    sec = _by_logical_id(td, "LicenseSecret")["Properties"]
    assert sec["SecretString"] == {"Ref": "LicenseData"}
    assert sec["Name"] == {"Ref": "LicenseSecretName"}


def test_admin_key_secret_generates_keyed_json(td):
    sec = _by_logical_id(td, "AdminKeySecret")["Properties"]
    gen = sec["GenerateSecretString"]
    assert gen["GenerateStringKey"] == "key"
    assert gen["PasswordLength"] == 64
    assert gen["SecretStringTemplate"] == "{}"
    assert sec["Name"] == {"Ref": "AdminKeySecretName"}


def test_no_maestro_db_secret(td):
    """maestro connects with the master DB secret, so no separate secret."""
    assert "MaestroDBSecret" not in td["Resources"]


def test_ssm_parameters_use_overridable_names(td):
    sp = _by_logical_id(td, "StorageProfilesParam")["Properties"]
    ak = _by_logical_id(td, "ApiKeysParam")["Properties"]
    assert sp["Name"] == {"Ref": "StorageProfilesParamName"}
    assert ak["Name"] == {"Ref": "ApiKeysParamName"}
    assert sp["Type"] == "String"
    assert ak["Type"] == "String"
    # Seed values are asserted in test_ssm_seeds_render_nonempty_yaml_lists.


def test_ingest_queue_policy_allows_s3_with_source_conditions(td):
    pol = _by_logical_id(td, "IngestQueuePolicy")["Properties"]["PolicyDocument"]
    stmt = pol["Statement"][0]
    assert stmt["Effect"] == "Allow"
    assert stmt["Principal"]["Service"] == "s3.amazonaws.com"
    assert "sqs:SendMessage" in stmt["Action"]
    assert "aws:SourceAccount" in stmt["Condition"]["StringEquals"]
    assert "aws:SourceArn" in stmt["Condition"]["ArnLike"]


def test_ingest_bucket_orders_after_queue_policy(td):
    """S3 validates the SQS notification destination when the bucket's
    notification config is applied, so the bucket must be created after
    IngestQueuePolicy -- expressed as a plain DependsOn."""
    bucket = _by_logical_id(td, "IngestBucket")
    dep = bucket["DependsOn"]
    deps = dep if isinstance(dep, list) else [dep]
    assert "IngestQueuePolicy" in deps
    queue_val = bucket["Properties"]["NotificationConfiguration"][
        "QueueConfigurations"
    ][0]["Queue"]
    assert queue_val == {"Fn::GetAtt": ["IngestQueue", "Arn"]}


def test_no_dependson_references_a_conditional_resource(td):
    """A DependsOn pointing at a Condition-gated resource dangles whenever that
    condition is false, which fails change-set creation with 'Unresolved
    resource dependencies'."""
    conditional_ids = {
        lid for lid, res in td["Resources"].items() if "Condition" in res
    }
    for lid, res in td["Resources"].items():
        dep = res.get("DependsOn")
        if dep is None:
            continue
        deps = dep if isinstance(dep, list) else [dep]
        bad = conditional_ids.intersection(deps)
        assert not bad, f"{lid} DependsOn condition-gated resource(s): {sorted(bad)}"


def _walk(node):
    """Yield ``node`` and every dict/list nested inside it."""
    yield node
    if isinstance(node, dict):
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


_NO_VALUE = {"Ref": "AWS::NoValue"}


def test_no_fn_sub_context_resolves_to_no_value(td):
    """CreateChangeSet rejects an Fn::Sub whose context object holds anything
    that is not a string (or a string-returning function) -- AWS::NoValue, even
    behind an Fn::If, fails template parsing."""
    for node in _walk(td):
        if not isinstance(node, dict) or "Fn::Sub" not in node:
            continue
        body = node["Fn::Sub"]
        if not isinstance(body, list) or len(body) < 2:
            continue
        for name, value in body[1].items():
            assert value != _NO_VALUE, f"Fn::Sub var {name!r} is AWS::NoValue"
            if isinstance(value, dict) and "Fn::If" in value:
                _, t_branch, f_branch = value["Fn::If"]
                assert _NO_VALUE not in (t_branch, f_branch), (
                    f"Fn::Sub var {name!r} has an Fn::If branch of AWS::NoValue"
                )


def test_ingest_bucket_lifecycle_uses_parameter(td):
    rules = _by_logical_id(td, "IngestBucket")["Properties"]["LifecycleConfiguration"]["Rules"]
    rule = rules[0]
    assert rule["Status"] == "Enabled"
    assert rule["ExpirationInDays"] == {"Ref": "IngestBucketLifecycleDays"}
    assert rule["AbortIncompleteMultipartUpload"]["DaysAfterInitiation"] == 1


def test_ingest_bucket_notification_targets_queue(td):
    notif = _by_logical_id(td, "IngestBucket")["Properties"][
        "NotificationConfiguration"
    ]
    queue_cfg = notif["QueueConfigurations"][0]
    assert queue_cfg["Event"] == "s3:ObjectCreated:*"
    assert queue_cfg["Queue"] == {"Fn::GetAtt": ["IngestQueue", "Arn"]}


def test_ingest_bucket_name_uses_default_or_override(td):
    name = _by_logical_id(td, "IngestBucket")["Properties"]["BucketName"]
    if_value = name["Fn::If"]
    assert if_value[0] == "UseDefaultBucketName"
    # default branch produces cardinal-ingest-<acct>-<region>
    default_sub = if_value[1]["Fn::Sub"]
    assert "cardinal-ingest-" in default_sub
    assert "AWS::AccountId" in default_sub
    assert "AWS::Region" in default_sub
    assert if_value[2] == {"Ref": "IngestBucketName"}


# ---------------------------------------------------------------------------
# Deletion / replace policies
# ---------------------------------------------------------------------------


_RETAIN_TYPES = {
    "AWS::SecretsManager::Secret",
    "AWS::S3::Bucket",
    "AWS::SQS::Queue",
    "AWS::RDS::DBSubnetGroup",
    "AWS::SSM::Parameter",
}


def test_data_resources_are_retained(td):
    """No data-bearing resource is allowed to vanish on stack delete."""
    for logical_id, res in td["Resources"].items():
        if res["Type"] in _RETAIN_TYPES:
            assert res.get("DeletionPolicy") == "Retain", (
                f"{logical_id} ({res['Type']}) must have DeletionPolicy: Retain"
            )
            assert res.get("UpdateReplacePolicy") == "Retain", (
                f"{logical_id} ({res['Type']}) must have UpdateReplacePolicy: Retain"
            )


def test_rds_uses_snapshot(td):
    rds = _by_logical_id(td, "DBInstance")
    assert rds["DeletionPolicy"] == "Snapshot"
    assert rds["UpdateReplacePolicy"] == "Snapshot"


# ---------------------------------------------------------------------------
# Outputs (must match data-setup.sh JSON keys 1:1)
# ---------------------------------------------------------------------------


_EXPECTED_OUTPUT_KEYS = {
    "DbEndpoint",
    "DbPort",
    "DbName",
    "DbMasterSecretArn",
    "IngestBucketName",
    "IngestQueueUrl",
    "IngestQueueArn",
    "LicenseSecretArn",
    "AdminKeySecretArn",
    "StorageProfilesParamName",
    "ApiKeysParamName",
}


def test_outputs_match_script_contract(td):
    assert set(td["Outputs"].keys()) == _EXPECTED_OUTPUT_KEYS


def test_outputs_have_no_export(td):
    for name, out in td["Outputs"].items():
        assert "Export" not in out, f"output {name} should not have an Export"


def test_outputs_are_unconditional(td):
    """The stack always creates its resources, so outputs carry no Condition."""
    for name, out in td["Outputs"].items():
        assert "Condition" not in out, f"output {name} should be unconditional"


# ---------------------------------------------------------------------------
# Import mode removed: no ImportMode parameter, no auto-name conditions, and
# the import-only explicit-name parameters are gone. RDS, the subnet group,
# the SQS queue, and the db-master secret are CFN-auto-named.
# ---------------------------------------------------------------------------


def test_no_import_mode_parameter(td):
    for n in (
        "ImportMode",
        "DBInstanceIdentifier",
        "DBSubnetGroupName",
        "IngestQueueName",
        "DBMasterSecretName",
        "MaestroDBSecretName",
    ):
        assert n not in td["Parameters"], f"{n} should be removed with import mode"


def test_no_import_mode_conditions(td):
    conditions = set(td.get("Conditions", {}).keys())
    for c in (
        "CreateCfnOnlyResources",
        "AutoNameDBInstance",
        "AutoNameDBSubnetGroup",
        "AutoNameIngestQueue",
        "AutoNameDBMasterSecret",
        "AutoNameMaestroDBSecret",
    ):
        assert c not in conditions, f"{c} should be removed with import mode"


def test_auto_named_resources_omit_explicit_names(td):
    """Resources that were import-overridable are now plain CFN-auto-named."""
    assert "DBInstanceIdentifier" not in _by_logical_id(td, "DBInstance")["Properties"]
    assert "DBSubnetGroupName" not in _by_logical_id(td, "DBSubnetGroup")["Properties"]
    assert "QueueName" not in _by_logical_id(td, "IngestQueue")["Properties"]
    assert "Name" not in _by_logical_id(td, "DBMasterSecret")["Properties"]


def test_no_resource_is_conditional(td):
    """Import mode was the only source of resource-level conditions."""
    for lid, res in td["Resources"].items():
        assert "Condition" not in res, f"{lid} should not be conditional"
