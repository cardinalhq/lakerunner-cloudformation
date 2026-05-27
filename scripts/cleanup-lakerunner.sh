#!/bin/sh
# Tear down a cardinal-lakerunner install end-to-end via a privileged ECS
# Fargate task. Companion to scripts/deploy-lakerunner.sh.
#
# Self-contained: POSIX shell + AWS CLI v2 + jq. No Python.
#
# The script creates a cardinal-cleanup CFN stack (which delivers a vetted
# task definition), launches the cleanup task into the customer's ECS
# cluster, tails the task's logs, and exits with the task's exit code.
# The task itself does the heavy lifting: drain ECS services, delete the
# cardinal-lakerunner stack, wipe the cardinal-* data layer (S3, RDS, SQS,
# secrets, SSM) with ownership-tag enforcement, and self-delete the cleanup
# stack.
#
# See docs/operations/cleanup.md for the runbook and
# docs/superpowers/specs/2026-05-27-cleanup-stack-design.md for the design.

set -eu

DEFAULT_TEMPLATE_BASE_URL="https://cardinal-cfn-us-east-1.s3.us-east-1.amazonaws.com/lakerunner"

# --- required ---
region=""
version=""
cluster_name=""
private_subnets=""
task_sg_id=""
cleanup_task_role_arn=""
cleanup_execution_role_arn=""
deployer_role_arn=""
yes_flag="false"

# --- optional ---
lakerunner_stack_name="cardinal-lakerunner"
cleanup_stack_name="cardinal-cleanup"
template_base_url="$DEFAULT_TEMPLATE_BASE_URL"
wait_self_delete="false"

# --- internal test hooks (pure data transforms; no AWS) ---
internal_plan_text=""

usage() {
    cat <<'EOF'
Usage: cleanup-lakerunner.sh [options]

Required:
  --region REGION                       AWS region.
  --version VERSION                     Published template tag, e.g. v0.0.46.
  --cluster-name NAME                   Customer's ECS cluster.
  --private-subnets CSV                 Subnets for the cleanup task ENI.
  --task-sg-id SG_ID                    Security group for the cleanup task ENI.
  --cleanup-task-role-arn ARN           Privileged task role.
  --cleanup-execution-role-arn ARN      Fargate execution role.
  --deployer-role-arn ARN               CFN service role (cardinal-cfn-deployer).
  --yes                                 Confirm destructive operation.

Optional:
  --lakerunner-stack-name NAME          Default: cardinal-lakerunner.
  --cleanup-stack-name NAME             Default: cardinal-cleanup.
  --template-base-url URL               Default: cardinal-cfn-us-east-1 bucket.
  --wait-self-delete                    Wait for the cleanup stack's own
                                        delete-complete (off by default).

Exit codes:
  0  task succeeded
  1  task failed, ownership-tag skip occurred, or self-delete wait failed
  2  pre-flight / input validation failure
EOF
}

log()  { printf '[cleanup-lakerunner] %s\n' "$*" >&2; }
fail() { code="$1"; shift; log "$*"; exit "$code"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --region)                       region="$2";                       shift 2 ;;
        --version)                      version="$2";                      shift 2 ;;
        --cluster-name)                 cluster_name="$2";                 shift 2 ;;
        --private-subnets)              private_subnets="$2";              shift 2 ;;
        --task-sg-id)                   task_sg_id="$2";                   shift 2 ;;
        --cleanup-task-role-arn)        cleanup_task_role_arn="$2";        shift 2 ;;
        --cleanup-execution-role-arn)   cleanup_execution_role_arn="$2";   shift 2 ;;
        --deployer-role-arn)            deployer_role_arn="$2";            shift 2 ;;
        --lakerunner-stack-name)        lakerunner_stack_name="$2";        shift 2 ;;
        --cleanup-stack-name)           cleanup_stack_name="$2";           shift 2 ;;
        --template-base-url)            template_base_url="$2";            shift 2 ;;
        --wait-self-delete)             wait_self_delete="true";           shift   ;;
        --yes)                          yes_flag="true";                   shift   ;;
        --internal-plan-text)           internal_plan_text="$2";           shift 2 ;;
        -h|--help)                      usage; exit 0 ;;
        *)                              usage; fail 2 "unknown argument: $1" ;;
    esac
done

# Pure data transform: emit the human-readable plan from environment-supplied
# JSON. Used by --internal-plan-text test hook.
if [ -n "$internal_plan_text" ]; then
    printf '%s' "$internal_plan_text" | python3 -c '
import json, sys
p = json.load(sys.stdin)
print("Plan:")
print("  region:        " + p["region"])
print("  cluster:       " + p["cluster"])
print("  lakerunner:    delete CFN stack " + p["lakerunner_stack"])
print("  data layer:    wipe (with ownership-tag enforcement)")
print("  cleanup stack: self-delete " + p["cleanup_stack"])
'
    exit 0
fi

required_missing=""
[ -z "$region" ]                       && required_missing="$required_missing --region"
[ -z "$version" ]                      && required_missing="$required_missing --version"
[ -z "$cluster_name" ]                 && required_missing="$required_missing --cluster-name"
[ -z "$private_subnets" ]              && required_missing="$required_missing --private-subnets"
[ -z "$task_sg_id" ]                   && required_missing="$required_missing --task-sg-id"
[ -z "$cleanup_task_role_arn" ]        && required_missing="$required_missing --cleanup-task-role-arn"
[ -z "$cleanup_execution_role_arn" ]   && required_missing="$required_missing --cleanup-execution-role-arn"
[ -z "$deployer_role_arn" ]            && required_missing="$required_missing --deployer-role-arn"
if [ -n "$required_missing" ]; then
    fail 2 "missing required:$required_missing"
fi

if [ "$yes_flag" != "true" ]; then
    cat <<EOF >&2
This will tear down a Cardinal install in the following AWS account/region:
  region:        $region
  cluster:       $cluster_name
  lakerunner:    delete CFN stack $lakerunner_stack_name
  data layer:    drain + delete S3 ingest, RDS, SQS, secrets, SSM
                 (only resources tagged Application=cardinal-lakerunner,
                  ManagedBy=cardinal-data-setup-script; others are skipped)
  cleanup stack: $cleanup_stack_name (created and self-deleted)

Re-run with --yes to proceed.
EOF
    exit 2
fi

# ---------------------------------------------------------------------------
# Stage 1: handle stranded cleanup stack from a prior aborted run.
# ---------------------------------------------------------------------------
existing_status=$(aws --region "$region" cloudformation describe-stacks \
    --stack-name "$cleanup_stack_name" \
    --query 'Stacks[0].StackStatus' --output text 2>/dev/null \
    || echo "DOES_NOT_EXIST")
if [ "$existing_status" != "DOES_NOT_EXIST" ]; then
    log "found existing $cleanup_stack_name (status: $existing_status); deleting first"
    aws --region "$region" cloudformation delete-stack \
        --stack-name "$cleanup_stack_name" \
        --role-arn "$deployer_role_arn"
    aws --region "$region" cloudformation wait stack-delete-complete \
        --stack-name "$cleanup_stack_name" \
        || fail 1 "stranded $cleanup_stack_name delete failed"
fi

# ---------------------------------------------------------------------------
# Stage 2: create the cleanup stack with --role-arn $deployer_role_arn.
# ---------------------------------------------------------------------------
template_url="${template_base_url}/${version}/cardinal-cleanup.yaml"
log "creating $cleanup_stack_name from $template_url"
aws --region "$region" cloudformation create-stack \
    --stack-name "$cleanup_stack_name" \
    --template-url "$template_url" \
    --role-arn "$deployer_role_arn" \
    --capabilities CAPABILITY_IAM \
    --parameters \
        ParameterKey=LakerunnerStackName,ParameterValue="$lakerunner_stack_name" \
        ParameterKey=CleanupTaskRoleArn,ParameterValue="$cleanup_task_role_arn" \
        ParameterKey=CleanupExecutionRoleArn,ParameterValue="$cleanup_execution_role_arn" \
        ParameterKey=ClusterName,ParameterValue="$cluster_name" \
        ParameterKey=DeployerRoleArn,ParameterValue="$deployer_role_arn" \
    >/dev/null
aws --region "$region" cloudformation wait stack-create-complete \
    --stack-name "$cleanup_stack_name" \
    || fail 1 "$cleanup_stack_name create failed; check stack events"

td_arn=$(aws --region "$region" cloudformation describe-stacks \
    --stack-name "$cleanup_stack_name" \
    --query "Stacks[0].Outputs[?OutputKey=='TaskDefinitionArn'].OutputValue" \
    --output text)
log_group=$(aws --region "$region" cloudformation describe-stacks \
    --stack-name "$cleanup_stack_name" \
    --query "Stacks[0].Outputs[?OutputKey=='LogGroupName'].OutputValue" \
    --output text)
[ -n "$td_arn" ]    || fail 1 "could not read TaskDefinitionArn output"
[ -n "$log_group" ] || fail 1 "could not read LogGroupName output"

log "task definition: $td_arn"
log "log group: $log_group"

# ---------------------------------------------------------------------------
# Stage 3: launch the cleanup task.
# ---------------------------------------------------------------------------
subnet_args=$(printf '%s' "$private_subnets" | sed 's/,/, /g')
network_config="awsvpcConfiguration={subnets=[$subnet_args],securityGroups=[$task_sg_id],assignPublicIp=DISABLED}"

task_arn=$(aws --region "$region" ecs run-task \
    --cluster "$cluster_name" \
    --launch-type FARGATE \
    --task-definition "$td_arn" \
    --network-configuration "$network_config" \
    --query 'tasks[0].taskArn' --output text)
if [ -z "$task_arn" ] || [ "$task_arn" = "None" ]; then
    fail 1 "ecs:RunTask returned no taskArn"
fi
task_id="${task_arn##*/}"
log "task: $task_arn"

# ---------------------------------------------------------------------------
# Stage 4: wait for RUNNING, then tail logs until STOPPED.
# ---------------------------------------------------------------------------
log "waiting for task to reach RUNNING"
i=0
while [ $i -lt 60 ]; do
    s=$(aws --region "$region" ecs describe-tasks --cluster "$cluster_name" \
        --tasks "$task_arn" --query 'tasks[0].lastStatus' --output text 2>/dev/null \
        || echo PENDING)
    if [ "$s" = "RUNNING" ]; then
        log "task RUNNING"
        break
    fi
    if [ "$s" = "STOPPED" ]; then
        log "task stopped during startup:"
        aws --region "$region" ecs describe-tasks --cluster "$cluster_name" \
            --tasks "$task_arn" \
            --query 'tasks[0].{stopCode:stopCode,stoppedReason:stoppedReason,containers:containers[*].{name:name,exitCode:exitCode,reason:reason}}' \
            --output json >&2
        exit 1
    fi
    sleep 4
    i=$((i+1))
done

log "tailing logs from $log_group/cleanup/cleanup/$task_id"
stream="cleanup/cleanup/$task_id"
next=""
exit_code=""
i=0
while [ $i -lt 240 ]; do
    if [ -n "$next" ]; then
        out=$(aws --region "$region" logs get-log-events \
            --log-group-name "$log_group" --log-stream-name "$stream" \
            --start-from-head --next-token "$next" --output json 2>/dev/null \
            || echo '{}')
    else
        out=$(aws --region "$region" logs get-log-events \
            --log-group-name "$log_group" --log-stream-name "$stream" \
            --start-from-head --output json 2>/dev/null || echo '{}')
    fi
    printf '%s' "$out" | jq -r '.events[]?.message' 2>/dev/null || true
    new_next=$(printf '%s' "$out" | jq -r '.nextForwardToken // empty' 2>/dev/null)
    [ -n "$new_next" ] && next="$new_next"
    s=$(aws --region "$region" ecs describe-tasks --cluster "$cluster_name" \
        --tasks "$task_arn" --query 'tasks[0].lastStatus' --output text 2>/dev/null \
        || echo RUNNING)
    if [ "$s" = "STOPPED" ]; then
        exit_code=$(aws --region "$region" ecs describe-tasks --cluster "$cluster_name" \
            --tasks "$task_arn" \
            --query 'tasks[0].containers[0].exitCode' --output text)
        break
    fi
    sleep 5
    i=$((i+1))
done

if [ -z "$exit_code" ] || [ "$exit_code" = "None" ]; then
    log "WARNING: task still RUNNING after tail deadline; treating as failure"
    exit_code=1
fi
log "task exit code: $exit_code"

# ---------------------------------------------------------------------------
# Stage 5: optionally wait for the self-delete to finish.
# ---------------------------------------------------------------------------
if [ "$wait_self_delete" = "true" ]; then
    log "waiting for $cleanup_stack_name stack-delete-complete"
    if ! aws --region "$region" cloudformation wait stack-delete-complete \
            --stack-name "$cleanup_stack_name"; then
        log "WARNING: self-delete wait failed; investigate the stranded stack"
        exit 1
    fi
fi

exit "$exit_code"
