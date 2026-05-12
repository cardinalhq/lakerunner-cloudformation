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


def test_creates_four_managed_secrets_plus_master(td):
    """db-master + license + internal-keys + admin-key + maestro-db = 5."""
    secrets = [
        r for r in td["Resources"].values()
        if r["Type"] == "AWS::SecretsManager::Secret"
    ]
    assert len(secrets) == 5


def test_creates_two_ssm_parameters(td):
    types = _types(td)
    assert types.count("AWS::SSM::Parameter") == 2


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


def test_internal_keys_secret_is_plain_string(td):
    sec = _by_logical_id(td, "InternalKeysSecret")["Properties"]
    gen = sec["GenerateSecretString"]
    assert "SecretStringTemplate" not in gen
    assert "GenerateStringKey" not in gen
    assert gen["PasswordLength"] == 64


def test_maestro_db_secret_generates_password_with_username(td):
    sec = _by_logical_id(td, "MaestroDBSecret")["Properties"]
    gen = sec["GenerateSecretString"]
    assert gen["GenerateStringKey"] == "password"
    assert "maestro" in gen["SecretStringTemplate"]


def test_ssm_parameters_use_overridable_names(td):
    sp = _by_logical_id(td, "StorageProfilesParam")["Properties"]
    ak = _by_logical_id(td, "ApiKeysParam")["Properties"]
    assert sp["Name"] == {"Ref": "StorageProfilesParamName"}
    assert ak["Name"] == {"Ref": "ApiKeysParamName"}
    assert sp["Type"] == "String"
    assert ak["Type"] == "String"
    assert sp["Value"] == "{}"
    assert ak["Value"] == "{}"


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
    IngestQueuePolicy. A plain DependsOn would dangle when ImportMode=Yes
    excludes the policy, so on the create path the ordering rides in an
    otherwise-unused Fn::Sub variable referencing the policy; in import mode the
    value is just the bare queue ARN (Fn::Sub context values must be strings, so
    Fn::If -> AWS::NoValue cannot be smuggled into the Sub itself)."""
    bucket = _by_logical_id(td, "IngestBucket")
    assert "DependsOn" not in bucket
    queue_val = bucket["Properties"]["NotificationConfiguration"][
        "QueueConfigurations"
    ][0]["Queue"]
    branches = queue_val["Fn::If"]
    assert branches[0] == "CreateCfnOnlyResources"
    sub_vars = branches[1]["Fn::Sub"][1]
    assert sub_vars["QueueArn"] == {"Fn::GetAtt": ["IngestQueue", "Arn"]}
    assert sub_vars["PolicyDependency"] == {"Ref": "IngestQueuePolicy"}
    assert branches[2] == {"Fn::GetAtt": ["IngestQueue", "Arn"]}


def test_no_dependson_references_a_conditional_resource(td):
    """A DependsOn pointing at a Condition-gated resource dangles whenever that
    condition is false (notably ImportMode=Yes), which fails change-set
    creation with 'Unresolved resource dependencies'."""
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
    branches = queue_cfg["Queue"]["Fn::If"]
    assert branches[0] == "CreateCfnOnlyResources"
    # create path: Fn::Sub renders to the queue ARN, carrying the policy dep
    sub = branches[1]["Fn::Sub"]
    assert sub[0] == "${QueueArn}"
    assert sub[1]["QueueArn"] == {"Fn::GetAtt": ["IngestQueue", "Arn"]}
    # import path: bare ARN, no dependency on the (absent) queue policy
    assert branches[2] == {"Fn::GetAtt": ["IngestQueue", "Arn"]}


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
    "MaestroDbSecretArn",
    "IngestBucketName",
    "IngestQueueUrl",
    "IngestQueueArn",
    "LicenseSecretArn",
    "InternalKeysSecretArn",
    "AdminKeySecretArn",
    "StorageProfilesParamName",
    "ApiKeysParamName",
}


def test_outputs_match_script_contract(td):
    assert set(td["Outputs"].keys()) == _EXPECTED_OUTPUT_KEYS


def test_outputs_have_no_export(td):
    for name, out in td["Outputs"].items():
        assert "Export" not in out, f"output {name} should not have an Export"


def test_outputs_gated_so_import_change_set_adds_none(td):
    """An IMPORT change set rejects adding/modifying Outputs, so every output is
    conditioned on CreateCfnOnlyResources -- false when ImportMode=Yes, so the
    import template has no Outputs section at all."""
    for name, out in td["Outputs"].items():
        assert out.get("Condition") == "CreateCfnOnlyResources", (
            f"output {name} must be gated on CreateCfnOnlyResources"
        )


# ---------------------------------------------------------------------------
# Optional name parameters (used at import time)
# ---------------------------------------------------------------------------


_IMPORT_NAME_PARAMS = (
    "DBInstanceIdentifier",
    "DBSubnetGroupName",
    "IngestQueueName",
    "DBMasterSecretName",
    "InternalKeysSecretName",
    "MaestroDBSecretName",
)


def test_import_name_parameters_exist_with_blank_defaults(td):
    for n in _IMPORT_NAME_PARAMS:
        assert n in td["Parameters"], f"missing import-name parameter: {n}"
        assert td["Parameters"][n]["Default"] == "", (
            f"{n} default must be blank so fresh installs auto-name"
        )


_AUTO_NAME_CONDITIONS = {
    "AutoNameDBInstance",
    "AutoNameDBSubnetGroup",
    "AutoNameIngestQueue",
    "AutoNameDBMasterSecret",
    "AutoNameInternalKeysSecret",
    "AutoNameMaestroDBSecret",
}


def test_auto_name_conditions_present(td):
    assert _AUTO_NAME_CONDITIONS <= set(td["Conditions"].keys())


def _name_property(td, logical_id, prop):
    return td["Resources"][logical_id]["Properties"][prop]


def test_auto_name_use_site_pattern(td):
    """Each import-eligible resource uses Fn::If(AutoNameX, NoValue, Ref(X))."""
    cases = [
        ("DBInstance", "DBInstanceIdentifier", "AutoNameDBInstance"),
        ("DBSubnetGroup", "DBSubnetGroupName", "AutoNameDBSubnetGroup"),
        ("IngestQueue", "QueueName", "AutoNameIngestQueue"),
        ("DBMasterSecret", "Name", "AutoNameDBMasterSecret"),
        ("InternalKeysSecret", "Name", "AutoNameInternalKeysSecret"),
        ("MaestroDBSecret", "Name", "AutoNameMaestroDBSecret"),
    ]
    for logical_id, prop, condition in cases:
        value = _name_property(td, logical_id, prop)
        if_branches = value["Fn::If"]
        assert if_branches[0] == condition
        assert if_branches[1] == {"Ref": "AWS::NoValue"}
        # third branch is Ref to the corresponding parameter
        ref_param = if_branches[2]["Ref"]
        assert ref_param in _IMPORT_NAME_PARAMS, ref_param
