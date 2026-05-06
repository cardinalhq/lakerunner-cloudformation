# Cardinal lakerunner -- required IAM roles

All IAM roles Cardinal needs are pre-created by the customer's IT and passed into the lakerunner CFN stack as parameters. This document is the authoritative reference for what each role's trust + inline policy must contain. It is generated from `src/cardinal_cfn/required_roles_doc.py` so it never drifts from the actual permissions Cardinal expects.

Substitute `${AccountId}` and `${Region}` with the install's account and region. The single shared model -- pass the same ARN for every role parameter -- works as long as the role contains the union of every policy below.

## Roles overview

| Role name (suggested) | Trust principal | Used by |
|---|---|---|
| `cardinal-task-role` | ECS tasks (`ecs-tasks.amazonaws.com`) | Used as the `TaskRoleArn` parameter on every ECS task definition in the lakerunner stack. |
| `cardinal-execution-role` | ECS tasks (`ecs-tasks.amazonaws.com`) | Used as the `ExecutionRoleArn` parameter; ECS uses it at task launch to pull images and resolve `secrets:` blocks. |
| `cardinal-migration-lambda-role` | Lambda (`lambda.amazonaws.com`) | Used by the migration custom resource Lambda to run the one-shot migrator ECS task. |
| `cardinal-data-setup-lambda-role` | Lambda (`lambda.amazonaws.com`) | Used by the cardinal-data-setup Lambda; full create+update+delete on the data resources it manages. |

## `cardinal-task-role`

ECS task role for every Cardinal lakerunner service. Single shared role across all 12 services; any task can read any `cardinal-*` secret.

**Trust policy:**
```json
{
  "Statement": [
    {
      "Action": "sts:AssumeRole",
      "Effect": "Allow",
      "Principal": {
        "Service": "ecs-tasks.amazonaws.com"
      }
    }
  ],
  "Version": "2012-10-17"
}
```

**Inline policy:**
```json
{
  "Statement": [
    {
      "Action": [
        "s3:GetBucketLocation",
        "s3:ListBucket",
        "s3:GetBucketNotification"
      ],
      "Effect": "Allow",
      "Resource": [
        "arn:aws:s3:::cardinal-ingest-${AccountId}-${Region}"
      ]
    },
    {
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:AbortMultipartUpload"
      ],
      "Effect": "Allow",
      "Resource": [
        "arn:aws:s3:::cardinal-ingest-${AccountId}-${Region}/*"
      ]
    },
    {
      "Action": [
        "sqs:ReceiveMessage",
        "sqs:DeleteMessage",
        "sqs:SendMessage",
        "sqs:GetQueueAttributes",
        "sqs:GetQueueUrl",
        "sqs:ChangeMessageVisibility"
      ],
      "Effect": "Allow",
      "Resource": [
        "arn:aws:sqs:${Region}:${AccountId}:cardinal-ingest"
      ]
    },
    {
      "Action": [
        "ssm:GetParameter",
        "ssm:GetParameters",
        "ssm:GetParametersByPath"
      ],
      "Effect": "Allow",
      "Resource": [
        "arn:aws:ssm:${Region}:${AccountId}:parameter/cardinal/*"
      ]
    },
    {
      "Action": [
        "secretsmanager:GetSecretValue"
      ],
      "Effect": "Allow",
      "Resource": [
        "arn:aws:secretsmanager:${Region}:${AccountId}:secret:cardinal-*"
      ]
    },
    {
      "Action": [
        "logs:CreateLogStream",
        "logs:PutLogEvents",
        "logs:DescribeLogStreams"
      ],
      "Effect": "Allow",
      "Resource": [
        "arn:aws:logs:${Region}:${AccountId}:log-group:/cardinal/*",
        "arn:aws:logs:${Region}:${AccountId}:log-group:/cardinal/*:*"
      ]
    },
    {
      "Action": [
        "ecs:DescribeServices",
        "ecs:DescribeTasks",
        "ecs:ListTasks",
        "ecs:UpdateService"
      ],
      "Condition": {
        "ArnEquals": {
          "ecs:cluster": "arn:aws:ecs:${Region}:${AccountId}:cluster/cardinal"
        }
      },
      "Effect": "Allow",
      "Resource": "*"
    },
    {
      "Action": [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream"
      ],
      "Effect": "Allow",
      "Resource": [
        "arn:aws:bedrock:${Region}::foundation-model/*"
      ]
    }
  ],
  "Version": "2012-10-17"
}
```

## `cardinal-execution-role`

ECS task execution role. Attach the AWS-managed `AmazonECSTaskExecutionRolePolicy` managed policy AND the inline policy below.

**Trust policy:**
```json
{
  "Statement": [
    {
      "Action": "sts:AssumeRole",
      "Effect": "Allow",
      "Principal": {
        "Service": "ecs-tasks.amazonaws.com"
      }
    }
  ],
  "Version": "2012-10-17"
}
```

**Managed policies to attach:**
- `arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy`

**Inline policy:**
```json
{
  "Statement": [
    {
      "Action": [
        "secretsmanager:GetSecretValue"
      ],
      "Effect": "Allow",
      "Resource": [
        "arn:aws:secretsmanager:${Region}:${AccountId}:secret:cardinal-*"
      ]
    },
    {
      "Action": [
        "ssm:GetParameter",
        "ssm:GetParameters",
        "ssm:GetParametersByPath"
      ],
      "Effect": "Allow",
      "Resource": [
        "arn:aws:ssm:${Region}:${AccountId}:parameter/cardinal/*"
      ]
    }
  ],
  "Version": "2012-10-17"
}
```

## `cardinal-migration-lambda-role`

Migration Lambda. Triggers a one-shot RunTask of the `cardinal-migrator` task definition on stack create + on `LakerunnerImage` parameter change.

**Trust policy:**
```json
{
  "Statement": [
    {
      "Action": "sts:AssumeRole",
      "Effect": "Allow",
      "Principal": {
        "Service": "lambda.amazonaws.com"
      }
    }
  ],
  "Version": "2012-10-17"
}
```

**Inline policy:**
```json
{
  "Statement": [
    {
      "Action": [
        "ecs:RunTask"
      ],
      "Condition": {
        "ArnEquals": {
          "ecs:cluster": "arn:aws:ecs:${Region}:${AccountId}:cluster/cardinal"
        }
      },
      "Effect": "Allow",
      "Resource": [
        "arn:aws:ecs:${Region}:${AccountId}:task-definition/cardinal-migrator:*"
      ]
    },
    {
      "Action": [
        "ecs:DescribeTasks"
      ],
      "Condition": {
        "ArnEquals": {
          "ecs:cluster": "arn:aws:ecs:${Region}:${AccountId}:cluster/cardinal"
        }
      },
      "Effect": "Allow",
      "Resource": "*"
    },
    {
      "Action": [
        "iam:PassRole"
      ],
      "Effect": "Allow",
      "Resource": [
        "arn:aws:iam::${AccountId}:role/cardinal-task-role",
        "arn:aws:iam::${AccountId}:role/cardinal-execution-role"
      ]
    },
    {
      "Action": [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ],
      "Effect": "Allow",
      "Resource": "*"
    }
  ],
  "Version": "2012-10-17"
}
```

## `cardinal-data-setup-lambda-role`

Data-setup Lambda. Executes inside `cardinal-data-setup.yaml` (or via direct `aws lambda invoke`) to create RDS, S3, SQS, secrets, and SSM params. Has full create+update+delete on its scope so it can recover from partial failures by re-running.

**Trust policy:**
```json
{
  "Statement": [
    {
      "Action": "sts:AssumeRole",
      "Effect": "Allow",
      "Principal": {
        "Service": "lambda.amazonaws.com"
      }
    }
  ],
  "Version": "2012-10-17"
}
```

**Inline policy:**
```json
{
  "Statement": [
    {
      "Action": [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ],
      "Effect": "Allow",
      "Resource": "*"
    },
    {
      "Action": [
        "sts:GetCallerIdentity"
      ],
      "Effect": "Allow",
      "Resource": "*"
    },
    {
      "Action": [
        "rds:CreateDBInstance",
        "rds:DeleteDBInstance",
        "rds:ModifyDBInstance",
        "rds:DescribeDBInstances",
        "rds:CreateDBSubnetGroup",
        "rds:DeleteDBSubnetGroup",
        "rds:DescribeDBSubnetGroups",
        "rds:AddTagsToResource",
        "rds:RemoveTagsFromResource",
        "rds:ListTagsForResource"
      ],
      "Effect": "Allow",
      "Resource": "*"
    },
    {
      "Action": [
        "s3:CreateBucket",
        "s3:DeleteBucket",
        "s3:HeadBucket",
        "s3:GetBucketLocation",
        "s3:ListBucket",
        "s3:PutBucketTagging",
        "s3:GetBucketTagging",
        "s3:PutBucketLifecycleConfiguration",
        "s3:GetBucketLifecycleConfiguration",
        "s3:PutBucketNotification",
        "s3:GetBucketNotification",
        "s3:PutBucketNotificationConfiguration",
        "s3:GetBucketNotificationConfiguration",
        "s3:PutPublicAccessBlock",
        "s3:GetPublicAccessBlock"
      ],
      "Effect": "Allow",
      "Resource": [
        "arn:aws:s3:::cardinal-ingest-${AccountId}-${Region}"
      ]
    },
    {
      "Action": [
        "sqs:CreateQueue",
        "sqs:DeleteQueue",
        "sqs:GetQueueUrl",
        "sqs:GetQueueAttributes",
        "sqs:SetQueueAttributes",
        "sqs:TagQueue",
        "sqs:ListQueueTags"
      ],
      "Effect": "Allow",
      "Resource": [
        "arn:aws:sqs:${Region}:${AccountId}:cardinal-ingest"
      ]
    },
    {
      "Action": [
        "secretsmanager:CreateSecret",
        "secretsmanager:DeleteSecret",
        "secretsmanager:UpdateSecret",
        "secretsmanager:DescribeSecret",
        "secretsmanager:GetSecretValue",
        "secretsmanager:PutSecretValue",
        "secretsmanager:GetRandomPassword",
        "secretsmanager:TagResource",
        "secretsmanager:UntagResource"
      ],
      "Effect": "Allow",
      "Resource": [
        "arn:aws:secretsmanager:${Region}:${AccountId}:secret:cardinal-*"
      ]
    },
    {
      "Action": [
        "ssm:PutParameter",
        "ssm:GetParameter",
        "ssm:GetParameters",
        "ssm:DeleteParameter",
        "ssm:DescribeParameters",
        "ssm:AddTagsToResource",
        "ssm:RemoveTagsFromResource"
      ],
      "Effect": "Allow",
      "Resource": [
        "arn:aws:ssm:${Region}:${AccountId}:parameter/cardinal/*"
      ]
    },
    {
      "Action": [
        "ec2:DescribeSubnets",
        "ec2:DescribeVpcs",
        "ec2:DescribeSecurityGroups"
      ],
      "Effect": "Allow",
      "Resource": "*"
    }
  ],
  "Version": "2012-10-17"
}
```

## Single-role shortcut

A customer who wants to grant one powerful role and skip the per-role scoping can build a role with the union of every inline policy above (both ECS-tasks and Lambda trust principals are needed -- the role's trust must allow both `ecs-tasks.amazonaws.com` and `lambda.amazonaws.com`). Pass that single ARN for every role parameter on both the data-setup stack and the lakerunner stack.
