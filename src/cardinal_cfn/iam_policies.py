"""Pure-data IAM policy-document builders.

Used by the shell-script generators (which inline the JSON into
``aws iam put-role-policy --policy-document file://...`` calls) and
by the CFN template generators (which embed the same dicts as
``Policies=[Policy(PolicyDocument=...)]`` on consuming resources).

Every builder returns a plain ``dict`` shaped as a valid AWS IAM
policy document. Pass concrete account/region/ARN values; do not
parameterize via Sub strings -- the shell-script generator can't
emit Sub.
"""

from __future__ import annotations


def _doc(*statements: dict) -> dict:
    return {"Version": "2012-10-17", "Statement": list(statements)}


def secrets_read_policy_doc(*, account_id: str, region: str) -> dict:
    return _doc({
        "Effect": "Allow",
        "Action": ["secretsmanager:GetSecretValue"],
        "Resource": [
            f"arn:aws:secretsmanager:{region}:{account_id}:secret:cardinal-*"
        ],
    })


def ssm_read_policy_doc(*, account_id: str, region: str) -> dict:
    return _doc({
        "Effect": "Allow",
        "Action": [
            "ssm:GetParameter",
            "ssm:GetParameters",
            "ssm:GetParametersByPath",
        ],
        "Resource": [f"arn:aws:ssm:{region}:{account_id}:parameter/cardinal/*"],
    })


def s3_rw_policy_doc(*, bucket_name: str) -> dict:
    return _doc(
        {
            "Effect": "Allow",
            "Action": [
                "s3:GetBucketLocation",
                "s3:ListBucket",
                "s3:GetBucketNotification",
            ],
            "Resource": [f"arn:aws:s3:::{bucket_name}"],
        },
        {
            "Effect": "Allow",
            "Action": [
                "s3:GetObject",
                "s3:PutObject",
                "s3:DeleteObject",
                "s3:AbortMultipartUpload",
            ],
            "Resource": [f"arn:aws:s3:::{bucket_name}/*"],
        },
    )


def sqs_rw_policy_doc(*, account_id: str, region: str, queue_name: str) -> dict:
    return _doc({
        "Effect": "Allow",
        "Action": [
            "sqs:ReceiveMessage",
            "sqs:DeleteMessage",
            "sqs:SendMessage",
            "sqs:GetQueueAttributes",
            "sqs:GetQueueUrl",
            "sqs:ChangeMessageVisibility",
        ],
        "Resource": [f"arn:aws:sqs:{region}:{account_id}:{queue_name}"],
    })


def logs_write_policy_doc(*, account_id: str, region: str) -> dict:
    return _doc({
        "Effect": "Allow",
        "Action": [
            "logs:CreateLogStream",
            "logs:PutLogEvents",
            "logs:DescribeLogStreams",
        ],
        "Resource": [
            f"arn:aws:logs:{region}:{account_id}:log-group:/cardinal/*",
            f"arn:aws:logs:{region}:{account_id}:log-group:/cardinal/*:*",
        ],
    })


def ecs_describe_policy_doc(*, cluster_arn: str) -> dict:
    return _doc({
        "Effect": "Allow",
        "Action": [
            "ecs:DescribeServices",
            "ecs:DescribeTasks",
            "ecs:ListTasks",
            "ecs:UpdateService",
        ],
        "Resource": "*",
        "Condition": {"ArnEquals": {"ecs:cluster": cluster_arn}},
    })


def bedrock_invoke_policy_doc(*, region: str) -> dict:
    return _doc({
        "Effect": "Allow",
        "Action": [
            "bedrock:InvokeModel",
            "bedrock:InvokeModelWithResponseStream",
        ],
        "Resource": [f"arn:aws:bedrock:{region}::foundation-model/*"],
    })


def pass_role_policy_doc(*, role_arns: list[str]) -> dict:
    return _doc({
        "Effect": "Allow",
        "Action": ["iam:PassRole"],
        "Resource": list(role_arns),
    })


def ecs_run_task_policy_doc(*, cluster_arn: str, task_definition_family: str, account_id: str, region: str) -> dict:
    return _doc(
        {
            "Effect": "Allow",
            "Action": ["ecs:RunTask"],
            "Resource": [
                f"arn:aws:ecs:{region}:{account_id}:task-definition/{task_definition_family}:*"
            ],
            "Condition": {"ArnEquals": {"ecs:cluster": cluster_arn}},
        },
        {
            "Effect": "Allow",
            "Action": ["ecs:DescribeTasks"],
            "Resource": "*",
            "Condition": {"ArnEquals": {"ecs:cluster": cluster_arn}},
        },
    )
