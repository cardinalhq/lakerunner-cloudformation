"""Tests for the shared IAM policy-document builders."""

from cardinal_cfn.iam_policies import (
    bedrock_invoke_policy_doc,
    ecs_describe_policy_doc,
    ecs_run_task_policy_doc,
    logs_write_policy_doc,
    pass_role_policy_doc,
    s3_rw_policy_doc,
    secrets_read_policy_doc,
    sqs_rw_policy_doc,
    ssm_read_policy_doc,
)


def test_secrets_read_doc_scopes_to_cardinal_prefix():
    doc = secrets_read_policy_doc(account_id="123", region="us-east-2")
    assert doc["Version"] == "2012-10-17"
    [stmt] = doc["Statement"]
    assert stmt["Effect"] == "Allow"
    assert stmt["Action"] == ["secretsmanager:GetSecretValue"]
    assert stmt["Resource"] == [
        "arn:aws:secretsmanager:us-east-2:123:secret:cardinal-*"
    ]


def test_ssm_read_doc_scopes_to_cardinal_prefix():
    doc = ssm_read_policy_doc(account_id="123", region="us-east-2")
    [stmt] = doc["Statement"]
    assert "ssm:GetParameter" in stmt["Action"]
    assert stmt["Resource"] == [
        "arn:aws:ssm:us-east-2:123:parameter/cardinal/*"
    ]


def test_s3_rw_doc_scopes_to_named_bucket():
    doc = s3_rw_policy_doc(bucket_name="cardinal-ingest-123-us-east-2")
    actions = {a for stmt in doc["Statement"] for a in stmt["Action"]}
    assert "s3:GetObject" in actions
    assert "s3:PutObject" in actions
    resources = {r for stmt in doc["Statement"] for r in stmt["Resource"]}
    assert "arn:aws:s3:::cardinal-ingest-123-us-east-2" in resources
    assert "arn:aws:s3:::cardinal-ingest-123-us-east-2/*" in resources


def test_sqs_rw_doc_scopes_to_named_queue():
    doc = sqs_rw_policy_doc(account_id="123", region="us-east-2", queue_name="cardinal-ingest")
    [stmt] = doc["Statement"]
    assert stmt["Resource"] == [
        "arn:aws:sqs:us-east-2:123:cardinal-ingest"
    ]


def test_logs_write_doc_scopes_to_cardinal_prefix():
    doc = logs_write_policy_doc(account_id="123", region="us-east-2")
    [stmt] = doc["Statement"]
    assert stmt["Resource"] == [
        "arn:aws:logs:us-east-2:123:log-group:/cardinal/*",
        "arn:aws:logs:us-east-2:123:log-group:/cardinal/*:*",
    ]


def test_ecs_describe_doc_uses_cluster_condition():
    doc = ecs_describe_policy_doc(cluster_arn="arn:aws:ecs:us-east-2:123:cluster/cardinal")
    [stmt] = doc["Statement"]
    assert "ecs:DescribeServices" in stmt["Action"]
    assert "ecs:UpdateService" in stmt["Action"]
    assert stmt["Resource"] == "*"
    assert stmt["Condition"]["ArnEquals"]["ecs:cluster"] == "arn:aws:ecs:us-east-2:123:cluster/cardinal"


def test_bedrock_invoke_doc_scoped_to_foundation_models():
    doc = bedrock_invoke_policy_doc(region="us-east-2")
    [stmt] = doc["Statement"]
    assert stmt["Resource"] == ["arn:aws:bedrock:us-east-2::foundation-model/*"]


def test_pass_role_doc_lists_specific_arns():
    doc = pass_role_policy_doc(role_arns=["arn:aws:iam::123:role/cardinal-task-role"])
    [stmt] = doc["Statement"]
    assert stmt["Action"] == ["iam:PassRole"]
    assert stmt["Resource"] == ["arn:aws:iam::123:role/cardinal-task-role"]


def test_run_task_doc_uses_cluster_condition():
    doc = ecs_run_task_policy_doc(
        cluster_arn="arn:aws:ecs:us-east-2:123:cluster/cardinal",
        task_definition_family="cardinal-migrator",
        account_id="123",
        region="us-east-2",
    )
    actions = {a for stmt in doc["Statement"] for a in stmt["Action"]}
    assert {"ecs:RunTask", "ecs:DescribeTasks"} <= actions
    run_stmt = next(s for s in doc["Statement"] if "ecs:RunTask" in s["Action"])
    assert run_stmt["Resource"] == [
        "arn:aws:ecs:us-east-2:123:task-definition/cardinal-migrator:*"
    ]
    assert run_stmt["Condition"]["ArnEquals"]["ecs:cluster"] == "arn:aws:ecs:us-east-2:123:cluster/cardinal"
