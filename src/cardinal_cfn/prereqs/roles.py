"""Role specifications -- pure data, used by the shell-script renderer
and consumed by tests that verify the naming contract."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from cardinal_cfn import iam_policies


_ECS_TASKS_TRUST = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"Service": "ecs-tasks.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }
    ],
}

_LAMBDA_TRUST = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"Service": "lambda.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }
    ],
}


@dataclass(frozen=True)
class RoleSpec:
    name: str
    description: str
    trust_policy_json: str
    inline_policy_name: str
    inline_policy_json: str
    managed_policy_arns: tuple[str, ...] = field(default_factory=tuple)


def _task_role_inline_policy(*, account_id: str, region: str, cluster_arn: str) -> dict:
    bucket = f"cardinal-ingest-{account_id}-{region}"
    return {
        "Version": "2012-10-17",
        "Statement": [
            *iam_policies.s3_rw_policy_doc(bucket_name=bucket)["Statement"],
            *iam_policies.sqs_rw_policy_doc(
                account_id=account_id, region=region, queue_name="cardinal-ingest"
            )["Statement"],
            *iam_policies.ssm_read_policy_doc(account_id=account_id, region=region)["Statement"],
            *iam_policies.secrets_read_policy_doc(account_id=account_id, region=region)["Statement"],
            *iam_policies.logs_write_policy_doc(account_id=account_id, region=region)["Statement"],
            *iam_policies.ecs_describe_policy_doc(cluster_arn=cluster_arn)["Statement"],
            *iam_policies.bedrock_invoke_policy_doc(region=region)["Statement"],
        ],
    }


def _execution_role_inline_policy(*, account_id: str, region: str) -> dict:
    return {
        "Version": "2012-10-17",
        "Statement": [
            *iam_policies.secrets_read_policy_doc(account_id=account_id, region=region)["Statement"],
            *iam_policies.ssm_read_policy_doc(account_id=account_id, region=region)["Statement"],
        ],
    }


def _migration_lambda_role_inline_policy(*, account_id: str, region: str, cluster_arn: str) -> dict:
    task_role_arn = f"arn:aws:iam::{account_id}:role/cardinal-task-role"
    execution_role_arn = f"arn:aws:iam::{account_id}:role/cardinal-execution-role"
    return {
        "Version": "2012-10-17",
        "Statement": [
            *iam_policies.ecs_run_task_policy_doc(
                cluster_arn=cluster_arn,
                task_definition_family="cardinal-migrator",
                account_id=account_id,
                region=region,
            )["Statement"],
            *iam_policies.pass_role_policy_doc(role_arns=[task_role_arn, execution_role_arn])["Statement"],
            {
                "Effect": "Allow",
                "Action": [
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                ],
                "Resource": "*",
            },
        ],
    }


def expected_role_specs(*, account_id: str, region: str, cluster_arn: str) -> list[RoleSpec]:
    return [
        RoleSpec(
            name="cardinal-task-role",
            description="Shared task role for every Cardinal ECS task",
            trust_policy_json=json.dumps(_ECS_TASKS_TRUST, sort_keys=True),
            inline_policy_name="cardinal-task-role-policy",
            inline_policy_json=json.dumps(
                _task_role_inline_policy(account_id=account_id, region=region, cluster_arn=cluster_arn),
                sort_keys=True,
            ),
        ),
        RoleSpec(
            name="cardinal-execution-role",
            description="ECS task execution role (image pull + secrets/ssm resolve)",
            trust_policy_json=json.dumps(_ECS_TASKS_TRUST, sort_keys=True),
            inline_policy_name="cardinal-execution-role-policy",
            inline_policy_json=json.dumps(
                _execution_role_inline_policy(account_id=account_id, region=region),
                sort_keys=True,
            ),
            managed_policy_arns=(
                "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy",
            ),
        ),
        RoleSpec(
            name="cardinal-migration-lambda-role",
            description="Migration Lambda execution role (one-shot RunTask of the migrator)",
            trust_policy_json=json.dumps(_LAMBDA_TRUST, sort_keys=True),
            inline_policy_name="cardinal-migration-lambda-role-policy",
            inline_policy_json=json.dumps(
                _migration_lambda_role_inline_policy(
                    account_id=account_id, region=region, cluster_arn=cluster_arn
                ),
                sort_keys=True,
            ),
        ),
    ]
