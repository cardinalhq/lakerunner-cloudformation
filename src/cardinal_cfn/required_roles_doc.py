"""Generates ``docs/operations/required-roles.md`` from the IAM policy
builders, so the customer's IT can see exactly what each role they
provide must contain.

Run via ``python3 -m cardinal_cfn.required_roles_doc``.

The output is deterministic in account/region (uses placeholder
``${AccountId}`` / ``${Region}`` strings the customer substitutes).
"""

from __future__ import annotations

import json

from cardinal_cfn import iam_policies


_ACCOUNT = "${AccountId}"
_REGION = "${Region}"
_CLUSTER_ARN = f"arn:aws:ecs:{_REGION}:{_ACCOUNT}:cluster/cardinal"
_BUCKET = f"cardinal-ingest-{_ACCOUNT}-{_REGION}"
_TASK_ROLE_ARN = f"arn:aws:iam::{_ACCOUNT}:role/cardinal-task-role"
_EXEC_ROLE_ARN = f"arn:aws:iam::{_ACCOUNT}:role/cardinal-execution-role"


_TASK_ROLE_TRUST = {
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "ecs-tasks.amazonaws.com"},
        "Action": "sts:AssumeRole",
    }],
}
_LAMBDA_TRUST = {
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "lambda.amazonaws.com"},
        "Action": "sts:AssumeRole",
    }],
}


def _task_role_policy() -> dict:
    return {
        "Version": "2012-10-17",
        "Statement": [
            *iam_policies.s3_rw_policy_doc(bucket_name=_BUCKET)["Statement"],
            *iam_policies.sqs_rw_policy_doc(account_id=_ACCOUNT, region=_REGION, queue_name="cardinal-ingest")["Statement"],
            *iam_policies.ssm_read_policy_doc(account_id=_ACCOUNT, region=_REGION)["Statement"],
            *iam_policies.secrets_read_policy_doc(account_id=_ACCOUNT, region=_REGION)["Statement"],
            *iam_policies.logs_write_policy_doc(account_id=_ACCOUNT, region=_REGION)["Statement"],
            *iam_policies.ecs_describe_policy_doc(cluster_arn=_CLUSTER_ARN)["Statement"],
            *iam_policies.bedrock_invoke_policy_doc(region=_REGION)["Statement"],
        ],
    }


def _execution_role_policy() -> dict:
    return {
        "Version": "2012-10-17",
        "Statement": [
            *iam_policies.secrets_read_policy_doc(account_id=_ACCOUNT, region=_REGION)["Statement"],
            *iam_policies.ssm_read_policy_doc(account_id=_ACCOUNT, region=_REGION)["Statement"],
        ],
    }


def _migration_lambda_role_policy() -> dict:
    return {
        "Version": "2012-10-17",
        "Statement": [
            *iam_policies.ecs_run_task_policy_doc(
                cluster_arn=_CLUSTER_ARN,
                task_definition_family="cardinal-migrator",
                account_id=_ACCOUNT,
                region=_REGION,
            )["Statement"],
            *iam_policies.pass_role_policy_doc(role_arns=[_TASK_ROLE_ARN, _EXEC_ROLE_ARN])["Statement"],
            {
                "Effect": "Allow",
                "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
                "Resource": "*",
            },
        ],
    }


def _data_setup_lambda_role_policy() -> dict:
    """Lambda needs full create+update+delete on the data resources it manages.

    Customer's IT accepts the broad scope on this role because the Lambda
    code is auditable in the repo and runs only what cardinal-data-setup.yaml
    invokes it for.
    """
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
                "Resource": "*",
            },
            {
                "Effect": "Allow",
                "Action": ["sts:GetCallerIdentity"],
                "Resource": "*",
            },
            {
                "Effect": "Allow",
                "Action": [
                    "rds:CreateDBInstance", "rds:DeleteDBInstance", "rds:ModifyDBInstance",
                    "rds:DescribeDBInstances", "rds:CreateDBSubnetGroup", "rds:DeleteDBSubnetGroup",
                    "rds:DescribeDBSubnetGroups", "rds:AddTagsToResource",
                    "rds:RemoveTagsFromResource", "rds:ListTagsForResource",
                ],
                "Resource": "*",
            },
            {
                "Effect": "Allow",
                "Action": [
                    "s3:CreateBucket", "s3:DeleteBucket", "s3:HeadBucket",
                    "s3:GetBucketLocation", "s3:ListBucket",
                    "s3:PutBucketTagging", "s3:GetBucketTagging",
                    "s3:PutBucketLifecycleConfiguration", "s3:GetBucketLifecycleConfiguration",
                    "s3:PutBucketNotification", "s3:GetBucketNotification",
                    "s3:PutBucketNotificationConfiguration", "s3:GetBucketNotificationConfiguration",
                    "s3:PutPublicAccessBlock", "s3:GetPublicAccessBlock",
                ],
                "Resource": [f"arn:aws:s3:::{_BUCKET}"],
            },
            {
                "Effect": "Allow",
                "Action": [
                    "sqs:CreateQueue", "sqs:DeleteQueue", "sqs:GetQueueUrl",
                    "sqs:GetQueueAttributes", "sqs:SetQueueAttributes",
                    "sqs:TagQueue", "sqs:ListQueueTags",
                ],
                "Resource": [f"arn:aws:sqs:{_REGION}:{_ACCOUNT}:cardinal-ingest"],
            },
            {
                "Effect": "Allow",
                "Action": [
                    "secretsmanager:CreateSecret", "secretsmanager:DeleteSecret",
                    "secretsmanager:UpdateSecret", "secretsmanager:DescribeSecret",
                    "secretsmanager:GetSecretValue", "secretsmanager:PutSecretValue",
                    "secretsmanager:GetRandomPassword",
                    "secretsmanager:TagResource", "secretsmanager:UntagResource",
                ],
                "Resource": [f"arn:aws:secretsmanager:{_REGION}:{_ACCOUNT}:secret:cardinal-*"],
            },
            {
                "Effect": "Allow",
                "Action": [
                    "ssm:PutParameter", "ssm:GetParameter", "ssm:GetParameters",
                    "ssm:DeleteParameter", "ssm:DescribeParameters",
                    "ssm:AddTagsToResource", "ssm:RemoveTagsFromResource",
                ],
                "Resource": [f"arn:aws:ssm:{_REGION}:{_ACCOUNT}:parameter/cardinal/*"],
            },
            {
                "Effect": "Allow",
                "Action": ["ec2:DescribeSubnets", "ec2:DescribeVpcs", "ec2:DescribeSecurityGroups"],
                "Resource": "*",
            },
        ],
    }


def _format_policy_block(doc: dict) -> str:
    return "```json\n" + json.dumps(doc, indent=2, sort_keys=True) + "\n```"


def render_doc() -> str:
    sections: list[str] = []
    sections.append("# Cardinal lakerunner -- required IAM roles\n")
    sections.append(
        "All IAM roles Cardinal needs are pre-created by the customer's IT and "
        "passed into the lakerunner CFN stack as parameters. This document "
        "is the authoritative reference for what each role's trust + inline "
        "policy must contain. It is generated from "
        "`src/cardinal_cfn/required_roles_doc.py` so it never drifts from the "
        "actual permissions Cardinal expects.\n"
    )
    sections.append(
        "Substitute `${AccountId}` and `${Region}` with the install's account "
        "and region. The single shared model -- pass the same ARN for every "
        "role parameter -- works as long as the role contains the union of "
        "every policy below.\n"
    )

    table = [
        ("`cardinal-task-role`", "ECS tasks (`ecs-tasks.amazonaws.com`)", "Used as the `TaskRoleArn` parameter on every ECS task definition in the lakerunner stack."),
        ("`cardinal-execution-role`", "ECS tasks (`ecs-tasks.amazonaws.com`)", "Used as the `ExecutionRoleArn` parameter; ECS uses it at task launch to pull images and resolve `secrets:` blocks."),
        ("`cardinal-migration-lambda-role`", "Lambda (`lambda.amazonaws.com`)", "Used by the migration custom resource Lambda to run the one-shot migrator ECS task."),
        ("`cardinal-data-setup-lambda-role`", "Lambda (`lambda.amazonaws.com`)", "Used by the cardinal-data-setup Lambda; full create+update+delete on the data resources it manages."),
    ]
    sections.append("## Roles overview\n")
    sections.append("| Role name (suggested) | Trust principal | Used by |")
    sections.append("|---|---|---|")
    for row in table:
        sections.append(f"| {row[0]} | {row[1]} | {row[2]} |")
    sections.append("")

    role_specs = [
        ("cardinal-task-role", _TASK_ROLE_TRUST, _task_role_policy(), [], "ECS task role for every Cardinal lakerunner service. Single shared role across all 12 services; any task can read any `cardinal-*` secret."),
        ("cardinal-execution-role", _TASK_ROLE_TRUST, _execution_role_policy(), ["arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"], "ECS task execution role. Attach the AWS-managed `AmazonECSTaskExecutionRolePolicy` managed policy AND the inline policy below."),
        ("cardinal-migration-lambda-role", _LAMBDA_TRUST, _migration_lambda_role_policy(), [], "Migration Lambda. Triggers a one-shot RunTask of the `cardinal-migrator` task definition on stack create + on `LakerunnerImage` parameter change."),
        ("cardinal-data-setup-lambda-role", _LAMBDA_TRUST, _data_setup_lambda_role_policy(), [], "Data-setup Lambda. Executes inside `cardinal-data-setup.yaml` (or via direct `aws lambda invoke`) to create RDS, S3, SQS, secrets, and SSM params. Has full create+update+delete on its scope so it can recover from partial failures by re-running."),
    ]

    for name, trust, inline, managed, blurb in role_specs:
        sections.append(f"## `{name}`\n")
        sections.append(blurb + "\n")
        sections.append("**Trust policy:**")
        sections.append(_format_policy_block(trust))
        sections.append("")
        if managed:
            sections.append("**Managed policies to attach:**")
            for arn in managed:
                sections.append(f"- `{arn}`")
            sections.append("")
        sections.append("**Inline policy:**")
        sections.append(_format_policy_block(inline))
        sections.append("")

    sections.append("## Single-role shortcut\n")
    sections.append(
        "A customer who wants to grant one powerful role and skip the per-role "
        "scoping can build a role with the union of every inline policy above "
        "(both ECS-tasks and Lambda trust principals are needed -- the role's "
        "trust must allow both `ecs-tasks.amazonaws.com` and "
        "`lambda.amazonaws.com`). Pass that single ARN for every role "
        "parameter on both the data-setup stack and the lakerunner stack."
    )

    return "\n".join(sections) + "\n"


if __name__ == "__main__":
    import sys
    sys.stdout.write(render_doc())
