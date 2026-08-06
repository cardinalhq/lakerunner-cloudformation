"""Tests for the cardinal-satellite-cwmetrics standalone template."""

import json

import pytest

from cardinal_cfn import satellite_cwmetrics, satellite_infra_base


@pytest.fixture
def td():
    return json.loads(satellite_cwmetrics.build().to_json())


def test_required_parameters(td):
    for n in (
        "RawBucketName",
        "OrganizationId",
        "ExcludeNamespace",
        "FirehoseBufferSeconds",
        "FirehoseBufferSizeMB",
        "NameSuffix",
    ):
        assert n in td["Parameters"], f"missing parameter: {n}"


def test_description_mentions_pull_model(td):
    desc = td["Description"].lower()
    assert "pull" in desc
    assert "nothing pushes" in desc


def test_creates_no_bucket_queue_or_access_role(td):
    """The whole point of this stack: it reuses satellite-infra-base's bucket,
    queue and cross-account role rather than standing up its own."""
    types = {r["Type"] for r in td["Resources"].values()}
    assert "AWS::S3::Bucket" not in types
    assert "AWS::SQS::Queue" not in types
    roles = [
        name
        for name, r in td["Resources"].items()
        if r["Type"] == "AWS::IAM::Role"
    ]
    # Only the two service roles Firehose and CloudWatch need; no role
    # trusting a Lakerunner principal.
    assert set(roles) == {"FirehoseDeliveryRole", "MetricStreamRole"}
    for name in roles:
        trust = td["Resources"][name]["Properties"]["AssumeRolePolicyDocument"]
        assert "Service" in trust["Statement"][0]["Principal"]


def test_output_format_is_plain_cloudwatch_json(td):
    """Not opentelemetry1.0: that double conversion emits OTLP summary form,
    which loses fidelity. Lakerunner ingests the plain CW metric JSON."""
    assert td["Resources"]["CwmetricsStream"]["Properties"]["OutputFormat"] == "json"
    # And it must not be operator-selectable.
    assert "MetricStreamOutputFormat" not in td["Parameters"]
    assert "OutputFormat" not in td["Parameters"]


def test_delivery_prefix_is_a_notified_ingest_prefix(td):
    """Objects delivered outside a notified prefix never reach the poller."""
    prefix = td["Resources"]["CwmetricsDeliveryStream"]["Properties"][
        "ExtendedS3DestinationConfiguration"
    ]["Prefix"]["Fn::Sub"]
    assert any(
        prefix.startswith(p) for p in satellite_infra_base.INGEST_PREFIXES
    )
    assert prefix.startswith("cwmetrics-raw/")


def test_delivery_prefix_leads_with_org(td):
    """Lakerunner resolves the org from the first path segment, matching the
    collector's otel-raw/${ORG}/... layout."""
    prefix = td["Resources"]["CwmetricsDeliveryStream"]["Properties"][
        "ExtendedS3DestinationConfiguration"
    ]["Prefix"]["Fn::Sub"]
    assert prefix.startswith("cwmetrics-raw/${OrganizationId}/")
    assert "!{timestamp:yyyy/MM/dd/HH}" in prefix


def test_error_prefix_is_outside_every_notified_prefix(td):
    """Failed-record payloads must not be handed to the poller."""
    err = td["Resources"]["CwmetricsDeliveryStream"]["Properties"][
        "ExtendedS3DestinationConfiguration"
    ]["ErrorOutputPrefix"]
    for p in satellite_infra_base.INGEST_PREFIXES:
        assert not err.startswith(p)


def test_delivery_is_gzip_and_buffered_from_parameters(td):
    cfg = td["Resources"]["CwmetricsDeliveryStream"]["Properties"][
        "ExtendedS3DestinationConfiguration"
    ]
    assert cfg["CompressionFormat"] == "GZIP"
    assert cfg["BufferingHints"]["IntervalInSeconds"] == {
        "Ref": "FirehoseBufferSeconds"
    }
    assert cfg["BufferingHints"]["SizeInMBs"] == {"Ref": "FirehoseBufferSizeMB"}


def test_firehose_logging_enabled(td):
    """Without it a delivery failure is silent and metrics just stop."""
    logging = td["Resources"]["CwmetricsDeliveryStream"]["Properties"][
        "ExtendedS3DestinationConfiguration"
    ]["CloudWatchLoggingOptions"]
    assert logging["Enabled"] is True
    assert logging["LogGroupName"] == {"Ref": "FirehoseLogGroup"}


def test_delivery_role_writes_only_its_own_prefixes(td):
    """Firehose has no business touching otel-raw/."""
    stmts = td["Resources"]["FirehoseDeliveryRole"]["Properties"]["Policies"][0][
        "PolicyDocument"
    ]["Statement"]
    write = next(s for s in stmts if s["Sid"] == "CwmetricsPrefixWrite")
    resources = [r["Fn::Sub"] for r in write["Resource"]]
    assert all("cwmetrics-" in r for r in resources)
    assert not any("otel-raw" in r for r in resources)
    assert "s3:PutObject" in write["Action"]
    assert "s3:DeleteObject" not in write["Action"]


def test_delivery_role_is_unnamed(td):
    """No cross-account bucket policy grants it by name -- unlike the SaaS
    terraform, the bucket is in this same account."""
    assert "RoleName" not in td["Resources"]["FirehoseDeliveryRole"]["Properties"]


def test_delivery_role_trust_has_external_id(td):
    trust = td["Resources"]["FirehoseDeliveryRole"]["Properties"][
        "AssumeRolePolicyDocument"
    ]["Statement"][0]
    assert trust["Principal"] == {"Service": "firehose.amazonaws.com"}
    assert trust["Condition"]["StringEquals"]["sts:ExternalId"] == {
        "Ref": "AWS::AccountId"
    }


def test_metric_stream_role_scoped_to_this_stream(td):
    stmts = td["Resources"]["MetricStreamRole"]["Properties"]["Policies"][0][
        "PolicyDocument"
    ]["Statement"]
    put = next(s for s in stmts if s["Sid"] == "FirehosePut")
    assert put["Resource"] == {
        "Fn::GetAtt": ["CwmetricsDeliveryStream", "Arn"]
    }
    assert set(put["Action"]) == {"firehose:PutRecord", "firehose:PutRecordBatch"}


def test_metric_stream_trusts_cloudwatch_service(td):
    trust = td["Resources"]["MetricStreamRole"]["Properties"][
        "AssumeRolePolicyDocument"
    ]["Statement"][0]
    assert trust["Principal"] == {
        "Service": "streams.metrics.cloudwatch.amazonaws.com"
    }
    assert trust["Condition"]["StringEquals"]["aws:SourceAccount"] == {
        "Ref": "AWS::AccountId"
    }


def test_exclude_namespace_is_conditional(td):
    ex = td["Resources"]["CwmetricsStream"]["Properties"]["ExcludeFilters"]["Fn::If"]
    assert ex[0] == "HasExcludeNamespace"
    assert ex[1] == [{"Namespace": {"Ref": "ExcludeNamespace"}}]
    assert ex[2] == {"Ref": "AWS::NoValue"}
    assert td["Parameters"]["ExcludeNamespace"]["Default"] == "AWS/Usage"


def test_log_group_is_delete_policy(td):
    lg = td["Resources"]["FirehoseLogGroup"]
    assert lg["DeletionPolicy"] == "Delete"
    assert lg["Properties"]["RetentionInDays"] == 7


def test_log_group_name_gets_suffix(td):
    assert td["Resources"]["FirehoseLogGroup"]["Properties"]["LogGroupName"] == {
        "Fn::If": [
            "HasNameSuffix",
            {"Fn::Sub": "/cardinal/cwmetrics-firehose-${NameSuffix}"},
            "/cardinal/cwmetrics-firehose",
        ]
    }


def test_outputs_present(td):
    for o in ("DeliveryStreamArn", "RawPrefix", "ErrorPrefix", "FirehoseLogGroupName"):
        assert o in td["Outputs"], f"missing output: {o}"


def test_pull_model_no_cross_account_push(td):
    """Nothing here references a remote account: the delivery destination is
    a bucket named by parameter, resolved in this account."""
    assert "LakerunnerPrincipal" not in json.dumps(td)
    # No SNS/Lambda fan-out, no second delivery destination.
    types = {r["Type"] for r in td["Resources"].values()}
    assert "AWS::SNS::Topic" not in types
    assert "AWS::Lambda::Function" not in types
