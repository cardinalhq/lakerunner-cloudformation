"""Generates ``cardinal-prereqs.sh`` -- POSIX shell, idempotent ensure_* steps.

The script is run once per AWS account+region with a privileged identity to
create the IAM roles and security groups Cardinal needs. After the deployer
takes over, those resources are immutable from its perspective.
"""

from __future__ import annotations

_HEADER = """\
#!/bin/sh
# cardinal-prereqs.sh -- create IAM roles + security groups Cardinal needs.
#
# Run once per AWS account+region with a privileged identity. Idempotent:
# matching resource is a no-op, drifted resource exits 2 with a diff.
#
# After successful run, hand the printed Key=Value block (or --output-file
# JSON) to whoever runs cardinal-data-setup.sh and then the two CFN stacks.

set -eu

PROJECT="cardinal"
APPLICATION="cardinal-lakerunner"
MANAGED_BY="cardinal-prereqs-script"

REGION=""
VPC_ID=""
OUTPUT_FILE=""

usage() {
    cat <<'EOF'
Usage: cardinal-prereqs.sh --region REGION --vpc-id VPC [--output-file PATH]

Required:
  --region        AWS region.
  --vpc-id        VPC ID where the SGs live.

Optional:
  --output-file   Write a JSON object of {ParameterKey: Value} to this path.

Exit codes:
  0  success or no-op
  1  AWS / unexpected failure
  2  drift detected or input/preflight failure
EOF
}

log() { printf '[%s] %s\\n' "cardinal-prereqs" "$*" >&2; }
fail() { code="$1"; shift; printf '[cardinal-prereqs] ERROR: %s\\n' "$*" >&2; exit "$code"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --region) REGION="$2"; shift 2 ;;
        --vpc-id) VPC_ID="$2"; shift 2 ;;
        --output-file) OUTPUT_FILE="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; fail 2 "unknown argument: $1" ;;
    esac
done

[ -n "$REGION" ] || { usage >&2; fail 2 "--region required"; }
[ -n "$VPC_ID" ] || { usage >&2; fail 2 "--vpc-id required"; }

for tool in aws jq; do
    command -v "$tool" >/dev/null 2>&1 || fail 2 "required tool not found: $tool"
done

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
CLUSTER_ARN="arn:aws:ecs:${REGION}:${ACCOUNT_ID}:cluster/cardinal"
TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT INT TERM HUP

# ---------------------------------------------------------------------------
# Per-config-item idempotency helpers
# ---------------------------------------------------------------------------
ensure_role() {
    role_name="$1"
    trust_file="$2"
    description="$3"
    existing=$(aws iam get-role --role-name "$role_name" \\
        --query 'Role.AssumeRolePolicyDocument' --output json 2>/dev/null || echo "")
    if [ -z "$existing" ]; then
        log "creating role $role_name"
        aws iam create-role \\
            --role-name "$role_name" \\
            --assume-role-policy-document "file://$trust_file" \\
            --description "$description" \\
            --tags "Key=Application,Value=$APPLICATION" \\
                   "Key=Project,Value=$PROJECT" \\
                   "Key=ManagedBy,Value=$MANAGED_BY" \\
                   "Key=Component,Value=$role_name" \\
                   "Key=Name,Value=$role_name" >/dev/null
    else
        actual=$(printf '%s' "$existing" | jq -S .)
        wanted=$(jq -S . "$trust_file")
        if [ "$actual" != "$wanted" ]; then
            fail 2 "role $role_name exists with drifted trust policy"
        fi
        log "role $role_name exists, trust policy matches"
    fi
}

ensure_inline_policy() {
    role_name="$1"
    policy_name="$2"
    policy_file="$3"
    existing=$(aws iam get-role-policy --role-name "$role_name" \\
        --policy-name "$policy_name" --query 'PolicyDocument' --output json 2>/dev/null || echo "")
    wanted=$(jq -S . "$policy_file")
    if [ -z "$existing" ]; then
        log "putting inline policy $policy_name on $role_name"
        aws iam put-role-policy --role-name "$role_name" \\
            --policy-name "$policy_name" \\
            --policy-document "file://$policy_file" >/dev/null
    else
        actual=$(printf '%s' "$existing" | jq -S .)
        if [ "$actual" != "$wanted" ]; then
            fail 2 "inline policy $policy_name on $role_name has drifted"
        fi
        log "inline policy $policy_name on $role_name matches"
    fi
}

ensure_managed_policy_attached() {
    role_name="$1"
    policy_arn="$2"
    attached=$(aws iam list-attached-role-policies --role-name "$role_name" \\
        --query 'AttachedPolicies[].PolicyArn' --output text 2>/dev/null || echo "")
    case " $attached " in
        *" $policy_arn "*)
            log "managed policy $policy_arn already attached to $role_name"
            ;;
        *)
            log "attaching managed policy $policy_arn to $role_name"
            aws iam attach-role-policy --role-name "$role_name" --policy-arn "$policy_arn" >/dev/null
            ;;
    esac
}

ensure_sg() {
    sg_name="$1"
    description="$2"
    sg_id=$(aws ec2 describe-security-groups \\
        --filters "Name=vpc-id,Values=$VPC_ID" "Name=group-name,Values=$sg_name" \\
        --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || echo "")
    if [ -z "$sg_id" ] || [ "$sg_id" = "None" ]; then
        log "creating security group $sg_name" >&2
        sg_id=$(aws ec2 create-security-group \\
            --group-name "$sg_name" \\
            --description "$description" \\
            --vpc-id "$VPC_ID" \\
            --tag-specifications "ResourceType=security-group,Tags=[{Key=Application,Value=$APPLICATION},{Key=Project,Value=$PROJECT},{Key=ManagedBy,Value=$MANAGED_BY},{Key=Component,Value=$sg_name},{Key=Name,Value=$sg_name}]" \\
            --query 'GroupId' --output text)
    else
        log "security group $sg_name exists ($sg_id)" >&2
    fi
    printf '%s\\n' "$sg_id"
}

ensure_ingress_self() {
    sg_id="$1"; protocol="$2"; from="$3"; to="$4"
    existing=$(aws ec2 describe-security-groups --group-ids "$sg_id" \\
        --query "SecurityGroups[0].IpPermissions[?IpProtocol=='$protocol' && FromPort==\\`$from\\` && ToPort==\\`$to\\`].UserIdGroupPairs[?GroupId=='$sg_id'].GroupId" \\
        --output text 2>/dev/null || echo "")
    if [ -n "$existing" ] && [ "$existing" != "None" ]; then
        return 0
    fi
    log "authorize self ingress $protocol $from-$to on $sg_id"
    aws ec2 authorize-security-group-ingress --group-id "$sg_id" \\
        --ip-permissions "IpProtocol=$protocol,FromPort=$from,ToPort=$to,UserIdGroupPairs=[{GroupId=$sg_id}]" >/dev/null
}

ensure_ingress_sg() {
    sg_id="$1"; src_sg_id="$2"; protocol="$3"; from="$4"; to="$5"
    existing=$(aws ec2 describe-security-groups --group-ids "$sg_id" \\
        --query "SecurityGroups[0].IpPermissions[?IpProtocol=='$protocol' && FromPort==\\`$from\\` && ToPort==\\`$to\\`].UserIdGroupPairs[?GroupId=='$src_sg_id'].GroupId" \\
        --output text 2>/dev/null || echo "")
    if [ -n "$existing" ] && [ "$existing" != "None" ]; then
        return 0
    fi
    log "authorize sg ingress $protocol $from-$to from $src_sg_id on $sg_id"
    aws ec2 authorize-security-group-ingress --group-id "$sg_id" \\
        --ip-permissions "IpProtocol=$protocol,FromPort=$from,ToPort=$to,UserIdGroupPairs=[{GroupId=$src_sg_id}]" >/dev/null
}

ensure_ingress_cidr() {
    sg_id="$1"; cidr="$2"; protocol="$3"; from="$4"; to="$5"
    existing=$(aws ec2 describe-security-groups --group-ids "$sg_id" \\
        --query "SecurityGroups[0].IpPermissions[?IpProtocol=='$protocol' && FromPort==\\`$from\\` && ToPort==\\`$to\\`].IpRanges[?CidrIp=='$cidr'].CidrIp" \\
        --output text 2>/dev/null || echo "")
    if [ -n "$existing" ] && [ "$existing" != "None" ]; then
        return 0
    fi
    log "authorize cidr ingress $protocol $from-$to from $cidr on $sg_id"
    aws ec2 authorize-security-group-ingress --group-id "$sg_id" \\
        --ip-permissions "IpProtocol=$protocol,FromPort=$from,ToPort=$to,IpRanges=[{CidrIp=$cidr}]" >/dev/null
}
"""


_POLICY_DOCS = """\

# ---------------------------------------------------------------------------
# Render trust + inline policy JSON files
# ---------------------------------------------------------------------------
cat >"$TMP_DIR/trust-ecs-tasks.json" <<'JSON'
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ecs-tasks.amazonaws.com"},"Action":"sts:AssumeRole"}]}
JSON
cat >"$TMP_DIR/trust-lambda.json" <<'JSON'
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}
JSON

BUCKET_NAME="cardinal-ingest-${ACCOUNT_ID}-${REGION}"
QUEUE_ARN="arn:aws:sqs:${REGION}:${ACCOUNT_ID}:cardinal-ingest"
TASK_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/cardinal-task-role"
EXECUTION_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/cardinal-execution-role"

jq -n \\
    --arg bucket "$BUCKET_NAME" \\
    --arg queue_arn "$QUEUE_ARN" \\
    --arg secrets_arn "arn:aws:secretsmanager:${REGION}:${ACCOUNT_ID}:secret:cardinal-*" \\
    --arg ssm_arn "arn:aws:ssm:${REGION}:${ACCOUNT_ID}:parameter/cardinal/*" \\
    --arg log_group_arn "arn:aws:logs:${REGION}:${ACCOUNT_ID}:log-group:/cardinal/*" \\
    --arg log_group_streams "arn:aws:logs:${REGION}:${ACCOUNT_ID}:log-group:/cardinal/*:*" \\
    --arg cluster_arn "$CLUSTER_ARN" \\
    --arg bedrock_arn "arn:aws:bedrock:${REGION}::foundation-model/*" \\
    '{Version:"2012-10-17",Statement:[
      {Effect:"Allow",Action:["s3:GetBucketLocation","s3:ListBucket","s3:GetBucketNotification"],Resource:["arn:aws:s3:::"+$bucket]},
      {Effect:"Allow",Action:["s3:GetObject","s3:PutObject","s3:DeleteObject","s3:AbortMultipartUpload"],Resource:["arn:aws:s3:::"+$bucket+"/*"]},
      {Effect:"Allow",Action:["sqs:ReceiveMessage","sqs:DeleteMessage","sqs:SendMessage","sqs:GetQueueAttributes","sqs:GetQueueUrl","sqs:ChangeMessageVisibility"],Resource:[$queue_arn]},
      {Effect:"Allow",Action:["ssm:GetParameter","ssm:GetParameters","ssm:GetParametersByPath"],Resource:[$ssm_arn]},
      {Effect:"Allow",Action:["secretsmanager:GetSecretValue"],Resource:[$secrets_arn]},
      {Effect:"Allow",Action:["logs:CreateLogStream","logs:PutLogEvents","logs:DescribeLogStreams"],Resource:[$log_group_arn,$log_group_streams]},
      {Effect:"Allow",Action:["ecs:DescribeServices","ecs:DescribeTasks","ecs:ListTasks","ecs:UpdateService"],Resource:"*",Condition:{ArnEquals:{"ecs:cluster":$cluster_arn}}},
      {Effect:"Allow",Action:["bedrock:InvokeModel","bedrock:InvokeModelWithResponseStream"],Resource:[$bedrock_arn]}
    ]}' >"$TMP_DIR/cardinal-task-role-policy.json"

jq -n \\
    --arg secrets_arn "arn:aws:secretsmanager:${REGION}:${ACCOUNT_ID}:secret:cardinal-*" \\
    --arg ssm_arn "arn:aws:ssm:${REGION}:${ACCOUNT_ID}:parameter/cardinal/*" \\
    '{Version:"2012-10-17",Statement:[
      {Effect:"Allow",Action:["secretsmanager:GetSecretValue"],Resource:[$secrets_arn]},
      {Effect:"Allow",Action:["ssm:GetParameter","ssm:GetParameters","ssm:GetParametersByPath"],Resource:[$ssm_arn]}
    ]}' >"$TMP_DIR/cardinal-execution-role-policy.json"

jq -n \\
    --arg cluster_arn "$CLUSTER_ARN" \\
    --arg taskdef_arn "arn:aws:ecs:${REGION}:${ACCOUNT_ID}:task-definition/cardinal-migrator:*" \\
    --arg task_role_arn "$TASK_ROLE_ARN" \\
    --arg exec_role_arn "$EXECUTION_ROLE_ARN" \\
    '{Version:"2012-10-17",Statement:[
      {Effect:"Allow",Action:["ecs:RunTask"],Resource:[$taskdef_arn],Condition:{ArnEquals:{"ecs:cluster":$cluster_arn}}},
      {Effect:"Allow",Action:["ecs:DescribeTasks"],Resource:"*",Condition:{ArnEquals:{"ecs:cluster":$cluster_arn}}},
      {Effect:"Allow",Action:["iam:PassRole"],Resource:[$task_role_arn,$exec_role_arn]},
      {Effect:"Allow",Action:["logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents"],Resource:"*"}
    ]}' >"$TMP_DIR/cardinal-migration-lambda-role-policy.json"
"""


_ROLE_CREATION = """\

# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------
ensure_role cardinal-task-role "$TMP_DIR/trust-ecs-tasks.json" "Shared task role for every Cardinal ECS task"
ensure_inline_policy cardinal-task-role cardinal-task-role-policy "$TMP_DIR/cardinal-task-role-policy.json"

ensure_role cardinal-execution-role "$TMP_DIR/trust-ecs-tasks.json" "ECS task execution role"
ensure_managed_policy_attached cardinal-execution-role arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy
ensure_inline_policy cardinal-execution-role cardinal-execution-role-policy "$TMP_DIR/cardinal-execution-role-policy.json"

ensure_role cardinal-migration-lambda-role "$TMP_DIR/trust-lambda.json" "Migration Lambda execution role"
ensure_inline_policy cardinal-migration-lambda-role cardinal-migration-lambda-role-policy "$TMP_DIR/cardinal-migration-lambda-role-policy.json"

TASK_ROLE_ARN_OUT=$(aws iam get-role --role-name cardinal-task-role --query 'Role.Arn' --output text)
EXECUTION_ROLE_ARN_OUT=$(aws iam get-role --role-name cardinal-execution-role --query 'Role.Arn' --output text)
MIGRATION_LAMBDA_ROLE_ARN_OUT=$(aws iam get-role --role-name cardinal-migration-lambda-role --query 'Role.Arn' --output text)
"""


_SG_CREATION = """\

# ---------------------------------------------------------------------------
# Security groups (create all first, THEN ingress rules)
# ---------------------------------------------------------------------------
TASK_SG_ID=$(ensure_sg cardinal-task-sg "Cardinal ECS tasks; intra-cluster + ALB ingress")
ALB_SG_ID=$(ensure_sg cardinal-alb-sg "Cardinal internal ALB")
DB_SG_ID=$(ensure_sg cardinal-db-sg "Cardinal RDS Postgres")

ensure_ingress_self "$TASK_SG_ID" tcp 0 65535
ensure_ingress_sg   "$TASK_SG_ID" "$ALB_SG_ID" tcp 0 65535
ensure_ingress_cidr "$ALB_SG_ID" 0.0.0.0/0 tcp 443 443
ensure_ingress_cidr "$ALB_SG_ID" 0.0.0.0/0 tcp 9443 9443
ensure_ingress_sg   "$DB_SG_ID" "$TASK_SG_ID" tcp 5432 5432
"""


_OUTPUT = """\

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
printf 'Key=TaskRoleArn,Value=%s\\n' "$TASK_ROLE_ARN_OUT"
printf 'Key=ExecutionRoleArn,Value=%s\\n' "$EXECUTION_ROLE_ARN_OUT"
printf 'Key=MigrationLambdaRoleArn,Value=%s\\n' "$MIGRATION_LAMBDA_ROLE_ARN_OUT"
printf 'Key=TaskSgId,Value=%s\\n' "$TASK_SG_ID"
printf 'Key=AlbSgId,Value=%s\\n' "$ALB_SG_ID"
printf 'Key=DbSgId,Value=%s\\n' "$DB_SG_ID"

if [ -n "$OUTPUT_FILE" ]; then
    jq -n \\
        --arg task_role "$TASK_ROLE_ARN_OUT" \\
        --arg exec_role "$EXECUTION_ROLE_ARN_OUT" \\
        --arg mig_role "$MIGRATION_LAMBDA_ROLE_ARN_OUT" \\
        --arg task_sg "$TASK_SG_ID" \\
        --arg alb_sg "$ALB_SG_ID" \\
        --arg db_sg "$DB_SG_ID" \\
        '{TaskRoleArn:$task_role, ExecutionRoleArn:$exec_role, MigrationLambdaRoleArn:$mig_role, TaskSgId:$task_sg, AlbSgId:$alb_sg, DbSgId:$db_sg}' \\
        >"$OUTPUT_FILE"
    log "wrote $OUTPUT_FILE"
fi

log "done"
"""


def render_prereqs_script() -> str:
    return _HEADER + _POLICY_DOCS + _ROLE_CREATION + _SG_CREATION + _OUTPUT


if __name__ == "__main__":
    import sys

    sys.stdout.write(render_prereqs_script())
