#!/bin/sh
# Sweep DELETE_FAILED / DELETE_SKIPPED resources left behind by a failed
# cardinal stack delete, by launching a one-shot privileged Fargate task.
#
# Unlike dev-scripts/cleanup-lakerunner.sh -- which delivers its task via the
# cardinal-cleanup CFN stack -- this driver registers the task definition
# directly with the ECS API, runs it, tails its logs, then DEREGISTERS and
# deletes the task definition once the task stops (best-effort).
#
# The container body is embedded below as a quoted heredoc, written to a temp
# file at runtime and JSON-encoded into the task definition's EntryPoint. (A
# heredoc nested inside $(...) is mishandled by bash 3.2 / macOS /bin/sh, so
# the body goes to a file rather than a command substitution.) The task
# discovers what to delete by querying the target stack for DELETE_FAILED /
# DELETE_SKIPPED resources, then sweeps IAM roles, security groups, secrets,
# SSM parameters, and S3 buckets.
#
# Self-contained: a single POSIX-sh file + AWS CLI v2 + python3. No jq, no CFN.

set -eu

DEFAULT_IMAGE="public.ecr.aws/aws-cli/aws-cli:latest"
DEFAULT_FAMILY="cardinal-sweep-stranded"

# --- required ---
region=""
stack_name=""
cluster_name=""
private_subnets=""
task_sg_id=""
task_role_arn=""
yes_flag="false"

# --- optional ---
execution_role_arn=""
image="$DEFAULT_IMAGE"
family="$DEFAULT_FAMILY"
retry_stack_delete="false"
keep_task_def="false"

# Set once the task definition is registered; the EXIT trap reads it.
td_arn=""
task_body_file=""

usage() {
    cat <<'EOF'
Usage: sweep-stranded-resources.sh [options]

Launch a privileged Fargate task that deletes the DELETE_FAILED /
DELETE_SKIPPED resources stranded by a failed cardinal stack delete.

Required:
  --region REGION              AWS region.
  --stack-name NAME            The CFN stack whose stranded resources to sweep.
  --cluster-name NAME          ECS cluster to launch the sweep task into.
  --private-subnets CSV        Subnets for the task ENI.
  --task-sg-id SG_ID           Security group for the task ENI. MUST NOT be a
                               security group you want this run to delete --
                               the task self-skips its own SG (attached ENI
                               blocks the delete).
  --task-role-arn ARN          Privileged ("superadmin") IAM role the task
                               assumes. Needs cloudformation:ListStackResources
                               plus iam/ec2/secretsmanager/ssm/s3 delete verbs,
                               and a trust policy allowing ecs-tasks.amazonaws.com.
  --yes                        Confirm destructive operation.

Optional:
  --execution-role-arn ARN     Fargate execution role (ECR pull + logs +
                               logs:CreateLogGroup). Default: same as
                               --task-role-arn.
  --image URI                  Default: public.ecr.aws/aws-cli/aws-cli:latest.
  --family NAME                 Task-definition family. Default: cardinal-sweep-stranded.
  --retry-stack-delete         After a successful sweep, re-issue delete-stack
                               on --stack-name and wait for it to complete.
  --keep-task-def              Do not deregister/delete the task definition on exit.

Exit codes:
  0  task succeeded (all stranded resources deleted)
  1  task failed, or one or more resources failed/were skipped
  2  pre-flight / input validation failure
EOF
}

log()  { printf '[sweep-driver] %s\n' "$*" >&2; }
warn() { printf '[sweep-driver] WARN: %s\n' "$*" >&2; }
fail() { c="$1"; shift; log "$*"; exit "$c"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --region)              region="$2";              shift 2 ;;
        --stack-name)          stack_name="$2";          shift 2 ;;
        --cluster-name)        cluster_name="$2";        shift 2 ;;
        --private-subnets)     private_subnets="$2";     shift 2 ;;
        --task-sg-id)          task_sg_id="$2";          shift 2 ;;
        --task-role-arn)       task_role_arn="$2";       shift 2 ;;
        --execution-role-arn)  execution_role_arn="$2";  shift 2 ;;
        --image)               image="$2";               shift 2 ;;
        --family)              family="$2";              shift 2 ;;
        --retry-stack-delete)  retry_stack_delete="true"; shift  ;;
        --keep-task-def)       keep_task_def="true";     shift   ;;
        --yes)                 yes_flag="true";          shift   ;;
        -h|--help)             usage; exit 0 ;;
        *)                     usage; fail 2 "unknown argument: $1" ;;
    esac
done

required_missing=""
[ -z "$region" ]          && required_missing="$required_missing --region"
[ -z "$stack_name" ]      && required_missing="$required_missing --stack-name"
[ -z "$cluster_name" ]    && required_missing="$required_missing --cluster-name"
[ -z "$private_subnets" ] && required_missing="$required_missing --private-subnets"
[ -z "$task_sg_id" ]      && required_missing="$required_missing --task-sg-id"
[ -z "$task_role_arn" ]   && required_missing="$required_missing --task-role-arn"
[ -n "$required_missing" ] && fail 2 "missing required:$required_missing"

[ -n "$execution_role_arn" ] || execution_role_arn="$task_role_arn"
command -v python3 >/dev/null 2>&1 || fail 2 "python3 not found on PATH"

if [ "$yes_flag" != "true" ]; then
    cat <<EOF >&2
This launches a privileged Fargate task that DELETES the DELETE_FAILED /
DELETE_SKIPPED resources of this stack:
  region:      $region
  stack:       $stack_name
  cluster:     $cluster_name
  task role:   $task_role_arn
  task SG:     $task_sg_id (excluded from the sweep)

Secrets are hard-deleted (no recovery window). Re-run with --yes to proceed.
EOF
    exit 2
fi

# Deregister + delete the task definition, and drop the temp body file, on any
# exit (the requested cleanup).
on_exit() {
    [ -n "$task_body_file" ] && rm -f "$task_body_file"
    [ -n "$td_arn" ] || return 0
    if [ "$keep_task_def" = "true" ]; then
        log "keeping task definition $td_arn (--keep-task-def)"
        return 0
    fi
    log "deregistering task definition $td_arn"
    aws --region "$region" ecs deregister-task-definition --task-definition "$td_arn" >/dev/null 2>&1 \
        || warn "deregister-task-definition failed"
    aws --region "$region" ecs delete-task-definitions --task-definitions "$td_arn" >/dev/null 2>&1 \
        || warn "delete-task-definitions failed (left INACTIVE)"
}
trap on_exit EXIT

aws --region "$region" sts get-caller-identity >/dev/null \
    || fail 2 "aws sts get-caller-identity failed; check credentials/region"

log_group="/aws/ecs/cardinal-sweep/$family"

# ===========================================================================
# Write the container body to a temp file. Runs inside the Fargate task under
# the privileged ("superadmin") task role. Quoted heredoc => taken literally,
# so the inner python3 -c '...' single quotes are safe. Exit codes: 0 = all
# swept; 1 = a step failed or was skipped (incl. the task's own SG);
# 2 = pre-flight.
# ===========================================================================
task_body_file="$(mktemp "${TMPDIR:-/tmp}/sweep-task.XXXXXX")" || fail 1 "mktemp failed"
cat > "$task_body_file" <<'SWEEP_TASK_EOF'
#!/bin/sh
set -eu

log()  { printf '[sweep] %s\n' "$*"; }
warn() { printf '[sweep] WARN: %s\n' "$*"; }
fail() { printf '[sweep] FATAL: %s\n' "$*"; exit 2; }

[ -n "${ECS_CONTAINER_METADATA_URI_V4:-}" ] \
    || fail "ECS_CONTAINER_METADATA_URI_V4 unset; not running under ECS Fargate?"
[ -n "${TARGET_STACK_NAME:-}" ] || fail "TARGET_STACK_NAME is unset"
# TASK_SG_ID is this task's own ENI security group; it is excluded from the
# SG sweep because an attached ENI blocks delete-security-group.
: "${TASK_SG_ID:=}"

# Region is derived from authoritative ECS task metadata, not from an env var
# the operator could override via ecs:RunTask containerOverrides.
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
[ -n "$ACCOUNT" ] && [ "$ACCOUNT" != "None" ] \
    || fail "could not derive account from sts:GetCallerIdentity"
REGION="$(python3 -c '
import json, os, urllib.request
uri = os.environ["ECS_CONTAINER_METADATA_URI_V4"]
with urllib.request.urlopen(uri + "/task", timeout=5) as r:
    print(json.load(r)["TaskARN"].split(":")[3])
')"
[ -n "$REGION" ] || fail "could not derive region from ECS task metadata"
export AWS_DEFAULT_REGION="$REGION"
unset AWS_REGION || true

log "account=$ACCOUNT region=$REGION stack=$TARGET_STACK_NAME task_sg=${TASK_SG_ID:-<none>}"

# Discover stranded resources (DELETE_FAILED or DELETE_SKIPPED) as tab-separated
# "<ResourceType>\t<PhysicalResourceId>" rows. Physical IDs for these types
# never contain whitespace, so awk field-splitting is safe.
resources="$(aws cloudformation list-stack-resources \
    --stack-name "$TARGET_STACK_NAME" \
    --query "StackResourceSummaries[?ResourceStatus=='DELETE_FAILED' || ResourceStatus=='DELETE_SKIPPED'].[ResourceType,PhysicalResourceId]" \
    --output text)"
if [ -z "$resources" ]; then
    log "no DELETE_FAILED/DELETE_SKIPPED resources in $TARGET_STACK_NAME; nothing to do"
    exit 0
fi
log "stranded resources:"
printf '%s\n' "$resources" | sed 's/^/  /'

ids_for() { printf '%s\n' "$resources" | awk -v t="$1" '$1==t {print $2}'; }

rc=0   # non-fatal failures bump this; task exits 1 so the driver notices.

# Flag any stranded type this script does not know how to sweep.
printf '%s\n' "$resources" | awk '{print $1}' | sort -u | while IFS= read -r t; do
    case "$t" in
        AWS::IAM::Role|AWS::EC2::SecurityGroup|AWS::SecretsManager::Secret|AWS::SSM::Parameter|AWS::S3::Bucket) ;;
        *) printf '[sweep] WARN: unhandled stranded resource type: %s (left in place)\n' "$t" ;;
    esac
done

# IAM roles. PhysicalResourceId is the role NAME. A role will not delete while
# it has attached managed policies, inline policies, or instance-profile
# membership, so clear those first.
for role in $(ids_for AWS::IAM::Role); do
    log "iam role: $role"
    for p in $(aws iam list-attached-role-policies --role-name "$role" \
            --query 'AttachedPolicies[].PolicyArn' --output text 2>/dev/null); do
        aws iam detach-role-policy --role-name "$role" --policy-arn "$p" \
            || { warn "detach $p from $role failed"; rc=1; }
    done
    for p in $(aws iam list-role-policies --role-name "$role" \
            --query 'PolicyNames[]' --output text 2>/dev/null); do
        aws iam delete-role-policy --role-name "$role" --policy-name "$p" \
            || { warn "delete inline $p on $role failed"; rc=1; }
    done
    for ip in $(aws iam list-instance-profiles-for-role --role-name "$role" \
            --query 'InstanceProfiles[].InstanceProfileName' --output text 2>/dev/null); do
        aws iam remove-role-from-instance-profile --instance-profile-name "$ip" --role-name "$role" \
            || { warn "remove $role from instance profile $ip failed"; rc=1; }
    done
    if aws iam delete-role --role-name "$role"; then
        log "iam role: deleted $role"
    else
        warn "delete-role $role failed"; rc=1
    fi
done

# Security groups. Two passes: strip every rule from all swept SGs first (so
# cross-references between them do not block deletion), then delete. The
# task's own ENI SG is skipped -- its attached ENI would make the delete fail.
sgs=""
for sg in $(ids_for AWS::EC2::SecurityGroup); do
    if [ "$sg" = "$TASK_SG_ID" ]; then
        warn "security group $sg is this task's own ENI SG; skipping (delete it manually after the task stops, or re-run from a different SG)"
        rc=1
        continue
    fi
    sgs="$sgs $sg"
done
for sg in $sgs; do
    ing="$(aws ec2 describe-security-groups --group-ids "$sg" \
        --query 'SecurityGroups[0].IpPermissions' --output json 2>/dev/null || echo '[]')"
    egr="$(aws ec2 describe-security-groups --group-ids "$sg" \
        --query 'SecurityGroups[0].IpPermissionsEgress' --output json 2>/dev/null || echo '[]')"
    [ "$ing" = "[]" ] || aws ec2 revoke-security-group-ingress --group-id "$sg" --ip-permissions "$ing" >/dev/null 2>&1 || true
    [ "$egr" = "[]" ] || aws ec2 revoke-security-group-egress  --group-id "$sg" --ip-permissions "$egr" >/dev/null 2>&1 || true
done
for sg in $sgs; do
    if aws ec2 delete-security-group --group-id "$sg" 2>/dev/null; then
        log "security group: deleted $sg"
    else
        warn "delete-security-group $sg failed (ENI still attached? referenced elsewhere?)"; rc=1
    fi
done

# Secrets. PhysicalResourceId is the secret ARN. Hard delete (no recovery
# window) to match a teardown; drop --force-delete-without-recovery if you
# want the 7-30 day window.
for s in $(ids_for AWS::SecretsManager::Secret); do
    if aws secretsmanager delete-secret --secret-id "$s" --force-delete-without-recovery >/dev/null; then
        log "secret: deleted $s"
    else
        warn "delete-secret $s failed"; rc=1
    fi
done

# SSM parameters. PhysicalResourceId is the parameter name.
for n in $(ids_for AWS::SSM::Parameter); do
    if aws ssm delete-parameter --name "$n"; then
        log "ssm parameter: deleted $n"
    else
        warn "delete-parameter $n failed"; rc=1
    fi
done

# S3 buckets. Empty all object versions + delete markers (handles versioned
# and non-versioned buckets), then delete the bucket.
empty_bucket() {
    b="$1"
    while :; do
        payload="$(aws s3api list-object-versions --bucket "$b" --max-items 500 --output json 2>/dev/null | python3 -c '
import json, sys
raw = sys.stdin.read().strip()
d = json.loads(raw) if raw else {}
items = []
for key in ("Versions", "DeleteMarkers"):
    for o in d.get(key) or []:
        items.append({"Key": o["Key"], "VersionId": o["VersionId"]})
print(json.dumps({"Objects": items, "Quiet": True}) if items else "")
')"
        [ -n "$payload" ] || break
        aws s3api delete-objects --bucket "$b" --delete "$payload" >/dev/null 2>&1 || return 1
    done
}
for b in $(ids_for AWS::S3::Bucket); do
    log "s3 bucket: emptying $b"
    if ! empty_bucket "$b"; then
        warn "could not fully empty $b"; rc=1
    fi
    if aws s3api delete-bucket --bucket "$b" 2>/dev/null; then
        log "s3 bucket: deleted $b"
    else
        warn "delete-bucket $b failed (not empty?)"; rc=1
    fi
done

if [ "$rc" -eq 0 ]; then
    log "sweep complete; all stranded resources deleted"
else
    log "sweep finished with one or more failures/skips (see WARN lines above)"
fi
exit "$rc"
SWEEP_TASK_EOF

# Build the task-definition JSON with python3 (reading the body from the temp
# file) so the embedded shell body is JSON-encoded into EntryPoint without
# escaping headaches. python3 -c (not a heredoc) keeps this safe inside $(...).
td_json="$(
    FAMILY="$family" IMAGE="$image" \
    TASK_ROLE_ARN="$task_role_arn" EXECUTION_ROLE_ARN="$execution_role_arn" \
    REGION="$region" LOG_GROUP="$log_group" \
    TARGET_STACK_NAME="$stack_name" TASK_SG_ID="$task_sg_id" \
    TASK_BODY_FILE="$task_body_file" \
    python3 -c '
import json, os
with open(os.environ["TASK_BODY_FILE"]) as f:
    body = f.read()
td = {
    "family": os.environ["FAMILY"],
    "requiresCompatibilities": ["FARGATE"],
    "networkMode": "awsvpc",
    "cpu": "512",
    "memory": "1024",
    "runtimePlatform": {"cpuArchitecture": "ARM64", "operatingSystemFamily": "LINUX"},
    "taskRoleArn": os.environ["TASK_ROLE_ARN"],
    "executionRoleArn": os.environ["EXECUTION_ROLE_ARN"],
    "containerDefinitions": [{
        "name": "sweep",
        "image": os.environ["IMAGE"],
        "essential": True,
        "entryPoint": ["/bin/sh", "-c", body],
        "command": [],
        "environment": [
            {"name": "TARGET_STACK_NAME", "value": os.environ["TARGET_STACK_NAME"]},
            {"name": "TASK_SG_ID",        "value": os.environ["TASK_SG_ID"]},
        ],
        "logConfiguration": {
            "logDriver": "awslogs",
            "options": {
                "awslogs-group": os.environ["LOG_GROUP"],
                "awslogs-region": os.environ["REGION"],
                "awslogs-stream-prefix": "sweep",
                "awslogs-create-group": "true",
            },
        },
    }],
}
print(json.dumps(td))
')"
[ -n "$td_json" ] || fail 1 "failed to build task-definition JSON"

td_arn="$(aws --region "$region" ecs register-task-definition \
    --cli-input-json "$td_json" \
    --query 'taskDefinition.taskDefinitionArn' --output text)"
[ -n "$td_arn" ] && [ "$td_arn" != "None" ] || fail 1 "register-task-definition failed"
log "registered task definition: $td_arn"

# Launch the sweep task.
subnet_args="$(printf '%s' "$private_subnets" | sed 's/,/, /g')"
network_config="awsvpcConfiguration={subnets=[$subnet_args],securityGroups=[$task_sg_id],assignPublicIp=DISABLED}"

task_arn="$(aws --region "$region" ecs run-task \
    --cluster "$cluster_name" \
    --launch-type FARGATE \
    --task-definition "$td_arn" \
    --network-configuration "$network_config" \
    --query 'tasks[0].taskArn' --output text)"
[ -n "$task_arn" ] && [ "$task_arn" != "None" ] || fail 1 "ecs:RunTask returned no taskArn"
task_id="${task_arn##*/}"
log "task: $task_arn"

# Wait for RUNNING (or early STOPPED).
log "waiting for task to reach RUNNING"
i=0
while [ $i -lt 60 ]; do
    s="$(aws --region "$region" ecs describe-tasks --cluster "$cluster_name" \
        --tasks "$task_arn" --query 'tasks[0].lastStatus' --output text 2>/dev/null || echo PENDING)"
    if [ "$s" = "RUNNING" ]; then log "task RUNNING"; break; fi
    if [ "$s" = "STOPPED" ]; then
        log "task stopped during startup:"
        aws --region "$region" ecs describe-tasks --cluster "$cluster_name" --tasks "$task_arn" \
            --query 'tasks[0].{stopCode:stopCode,stoppedReason:stoppedReason,containers:containers[*].{name:name,exitCode:exitCode,reason:reason}}' \
            --output json >&2
        exit 1
    fi
    sleep 4
    i=$((i+1))
done

# Tail logs until STOPPED.
stream="sweep/sweep/$task_id"
log "tailing logs from $log_group/$stream"
next=""
exit_code=""
i=0
while [ $i -lt 240 ]; do
    if [ -n "$next" ]; then
        out="$(aws --region "$region" logs get-log-events --log-group-name "$log_group" \
            --log-stream-name "$stream" --start-from-head --next-token "$next" --output json 2>/dev/null || echo '{}')"
    else
        out="$(aws --region "$region" logs get-log-events --log-group-name "$log_group" \
            --log-stream-name "$stream" --start-from-head --output json 2>/dev/null || echo '{}')"
    fi
    printf '%s' "$out" | python3 -c 'import json,sys
raw=sys.stdin.read().strip()
d=json.loads(raw) if raw else {}
for e in d.get("events",[]):
    print(e.get("message",""))' 2>/dev/null || true
    new_next="$(printf '%s' "$out" | python3 -c 'import json,sys
raw=sys.stdin.read().strip()
d=json.loads(raw) if raw else {}
print(d.get("nextForwardToken","") or "")' 2>/dev/null || true)"
    [ -n "$new_next" ] && next="$new_next"
    s="$(aws --region "$region" ecs describe-tasks --cluster "$cluster_name" \
        --tasks "$task_arn" --query 'tasks[0].lastStatus' --output text 2>/dev/null || echo RUNNING)"
    if [ "$s" = "STOPPED" ]; then
        exit_code="$(aws --region "$region" ecs describe-tasks --cluster "$cluster_name" \
            --tasks "$task_arn" --query 'tasks[0].containers[0].exitCode' --output text)"
        break
    fi
    sleep 5
    i=$((i+1))
done

if [ -z "$exit_code" ] || [ "$exit_code" = "None" ]; then
    warn "task still RUNNING after tail deadline; treating as failure"
    exit_code=1
fi
log "task exit code: $exit_code"

# Optionally retry the stack delete now that stranded resources are gone.
if [ "$retry_stack_delete" = "true" ] && [ "$exit_code" = "0" ]; then
    log "re-issuing delete-stack on $stack_name"
    aws --region "$region" cloudformation delete-stack --stack-name "$stack_name"
    aws --region "$region" cloudformation wait stack-delete-complete --stack-name "$stack_name" \
        || warn "stack delete did not complete; check stack events"
fi

exit "$exit_code"
