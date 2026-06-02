# Tearing down a Cardinal install (cleanup-lakerunner.sh)

This document is for operators (Jenkins, or an authorized human) tearing down a full Cardinal install end-to-end. The procedure deletes the `cardinal-lakerunner` and `cardinal-infrastructure` CloudFormation stacks and the `cardinal-*` data resources retained by the infrastructure stack, but **only those**: the customer's ECS cluster, subnets, and VPC are not touched.

For partial cleanup (e.g. leaving the lakerunner stack in place, or only wiping the data layer), this is the wrong tool. Use the AWS CLI directly.

## When to use this

- Full uninstall of a Cardinal sandbox / pre-prod environment.
- After a botched install, when the operator role can deploy stacks but can't delete data-bearing resources.

## When NOT to use this

- Production. There is no undo. Snapshots are explicitly disabled.
- Single-resource cleanup (e.g. just the RDS). Use `aws rds delete-db-instance` directly.

## Prerequisites

1. The `cardinal-cfn-deployer` CFN service role (or equivalent) — same role used to deploy `cardinal-lakerunner` and `cardinal-infrastructure`.
1. A privileged ECS task role (and execution role) that the cleanup task will assume. See "Required IAM policies" below for the exact statement.
1. The operator's own IAM role with the policies in the same section.
1. The customer's ECS cluster name, two private subnets, and a security group the task can use.
1. AWS CLI v2 and `jq` on the runner.

## Running

```sh
dev-scripts/cleanup-lakerunner.sh \
    --region us-east-1 \
    --version v0.0.83 \
    --cluster-name <CLUSTER> \
    --private-subnets subnet-aaa,subnet-bbb \
    --task-sg-id sg-ccc \
    --cleanup-task-role-arn      arn:aws:iam::<ACCT>:role/cardinal-cleanup \
    --cleanup-execution-role-arn arn:aws:iam::<ACCT>:role/cardinal-cleanup-exec \
    --deployer-role-arn          arn:aws:iam::<ACCT>:role/cardinal-cfn-deployer \
    --infra-stack-name           cardinal-infrastructure \
    --yes
```

Without `--yes` the script prints the plan and exits with code 2 so Jenkins can show the operator the blast radius before confirming.

### Deleting the ALB security group

`cardinal-alb-sg` is customer-supplied and owned by neither the `cardinal-lakerunner` nor the `cardinal-infrastructure` stack, so neither stack-delete reaches it. Pass `--alb-sg-id sg-...` to have the step-4 sweep delete it too (omit to leave it in place):

```sh
    --alb-sg-id sg-0ffe4f191fee82022 \
```

The ALB itself lives in the lakerunner stack and is gone by the time the sweep runs (step 1), so the delete is safe — **unless** another surviving security group still references `cardinal-alb-sg` as an ingress source (e.g. the v1.39 ALB-to-query/control health-port rules on port 8090). In that case AWS returns `DependencyViolation`; clear those ingress rules first, then re-run (every step is idempotent). The cleanup task role already holds `ec2:DeleteSecurityGroup` for the RDS SG, so no IAM change is needed.

## What happens

1. Driver creates `cardinal-cleanup` (`--role-arn cardinal-cfn-deployer`) from the published `cardinal-cleanup.yaml`. The stack provisions a `cardinal-cleanup` task definition and a log group.
1. Driver launches the task in the customer's cluster.
1. Inside the task, the four-step teardown runs:
    1. **Drain + delete cardinal-lakerunner.** Walk the stack tree via `cloudformation:ListStackResources` to find ECS services; set `DesiredCount=0`; stop running and pending tasks; wait up to 5 minutes for `runningCount=0 AND pendingCount=0`. Then `cloudformation:DeleteStack cardinal-lakerunner --role-arn $DEPLOYER_ROLE_ARN`, wait for delete-complete.
    1. **Empty the ingest S3 bucket.** All versions + in-flight multipart uploads.
    1. **Delete cardinal-infrastructure.** CFN handles the DBInstance via its `DeletionPolicy: Snapshot` (final snapshot + delete). Every other data-layer resource has `DeletionPolicy: Retain` and survives this step. Crucially this means the secret/RDS ordering is CFN's problem, not the task's; if the task tried to delete the master secret first the in-flight snapshot would fail with "secret can't be found".
    1. **Sweep the Retain'd resources.** Using the physical IDs discovered from cardinal-infrastructure *before* step 3 (once the stack is gone, the CFN-generated names are unrecoverable): delete the bucket, force-delete every secret (DBMaster + license + admin-key), delete the SSM parameters, delete the SQS queue, delete the RDS subnet group, delete every RDS snapshot the step-3 final-snapshot left behind.
1. Fire `cloudformation:DeleteStack cardinal-cleanup --role-arn $DEPLOYER_ROLE_ARN` asynchronously and exit. CFN handles the cleanup-stack teardown after the task ends.
1. Driver tails logs to its stdout; exits with the task's exit code.

The cleanup task SG must **not** be a `cardinal-lakerunner`-owned SG -- if it is, the SG cannot be deleted (the cleanup task's ENI is using it) and step 1's `delete-stack cardinal-lakerunner` deadlocks. Use a customer-supplied or VPC-level SG.

## Exit codes

- `0` — task succeeded; cleanup stack self-delete in progress.
- `1` — task ran but failed (non-zero exit code), or self-delete wait failed.
- `2` — pre-flight failure: missing argument, missing `--yes`, etc.

## Recovery from a failed run

- `cardinal-cleanup` is left in CREATE_COMPLETE state if create succeeded but the task failed. Re-running the driver auto-deletes the stranded stack before creating a fresh one (no manual intervention required).
- If the cleanup task partially completed before failing, re-run the driver. Every data-layer step is idempotent (already-absent treated as success).
- If the ownership-tag check refused a delete, inspect the resource's actual tags with `aws <service> list-tags-for-resource ...`. If the resource is legitimately Cardinal-owned, manually apply the expected tags and re-run. If it isn't, leave it alone.

## Required IAM policies

### Operator role (Jenkins)

```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "cloudformation:CreateStack",
                "cloudformation:DescribeStacks",
                "cloudformation:DescribeStackEvents",
                "cloudformation:DeleteStack"
            ],
            "Resource": "arn:aws:cloudformation:*:*:stack/cardinal-cleanup/*",
            "Condition": {
                "StringEquals": {
                    "cloudformation:RoleArn": "<DeployerRoleArn>"
                },
                "StringLike": {
                    "cloudformation:TemplateUrl": [
                        "https://cardinal-cfn.s3.us-east-2.amazonaws.com/lakerunner/*/cardinal-cleanup.yaml",
                        "https://cardinal-cfn-us-east-1.s3.us-east-1.amazonaws.com/lakerunner/*/cardinal-cleanup.yaml"
                    ]
                }
            }
        },
        {
            "Effect": "Allow",
            "Action": "ecs:RunTask",
            "Resource": "arn:aws:ecs:<REGION>:<ACCOUNT>:task-definition/cardinal-cleanup:*",
            "Condition": {
                "ArnEquals":    { "ecs:cluster": "arn:aws:ecs:<REGION>:<ACCOUNT>:cluster/<CLUSTER>" },
                "StringEquals": { "ecs:enable-execute-command": "false" }
            }
        },
        {
            "Effect": "Allow",
            "Action": "ecs:DescribeTasks",
            "Resource": "arn:aws:ecs:<REGION>:<ACCOUNT>:task/<CLUSTER>/*",
            "Condition": {
                "ArnEquals": { "ecs:cluster": "arn:aws:ecs:<REGION>:<ACCOUNT>:cluster/<CLUSTER>" }
            }
        },
        {
            "Effect": "Allow",
            "Action": "iam:PassRole",
            "Resource": [
                "<CleanupTaskRoleArn>",
                "<CleanupExecutionRoleArn>"
            ],
            "Condition": {
                "StringEquals": { "iam:PassedToService": "ecs-tasks.amazonaws.com" }
            }
        },
        {
            "Effect": "Allow",
            "Action": "iam:PassRole",
            "Resource": "<DeployerRoleArn>",
            "Condition": {
                "StringEquals": { "iam:PassedToService": "cloudformation.amazonaws.com" }
            }
        },
        {
            "Effect": "Allow",
            "Action": ["logs:GetLogEvents"],
            "Resource": "arn:aws:logs:*:*:log-group:/aws/ecs/cardinal-cleanup/*:log-stream:*"
        },
        {
            "Effect": "Allow",
            "Action": "logs:DescribeLogStreams",
            "Resource": "arn:aws:logs:*:*:log-group:/aws/ecs/cardinal-cleanup/*"
        }
    ]
}
```

Air-gapped customers replace the two `TemplateUrl` patterns with their exact mirror URL. **Do not wildcard the bucket host** — any bucket matching `cardinal-cfn-*` could be created by anyone.

### Cleanup task role

Trust policy must allow `ecs-tasks.amazonaws.com:AssumeRole`. The permissions policy is documented in `docs/superpowers/specs/2026-05-27-cleanup-stack-design.md` under "Customer-supplied `CleanupTaskRoleArn` — required policy". All ARNs in that policy are **account+region pinned** (no `*:*` wildcards) so an operator who tries to redirect the task at another regional install via environment overrides is blocked at IAM.

### Cleanup execution role

`AmazonECSTaskExecutionRolePolicy` plus:

```json
{
    "Effect": "Allow",
    "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
    "Resource": "arn:aws:logs:*:*:log-group:/aws/ecs/cardinal-cleanup/*:log-stream:*"
}
```

## Network requirements

The cleanup task runs in private subnets without public IPs. It must reach the public ECR image (`public.ecr.aws/aws-cli/aws-cli:latest`) at launch and several AWS APIs during the run.

- **Image pull from `public.ecr.aws`:** requires either NAT egress or a customer mirror of the image into private ECR. The standard `ecr-api` / `ecr-dkr` interface endpoints do not serve `public.ecr.aws`. Without NAT, the customer mirrors the image, rebuilds `cardinal-cleanup.yaml` with the mirrored URI (the image is intentionally not a stack parameter — see the spec), and points the operator role's `TemplateUrl` condition at the mirrored template.
- **AWS API endpoints** (via NAT or VPC interface endpoints): cloudformation, ecs, s3, rds, sqs, secretsmanager, ssm, logs, sts.
