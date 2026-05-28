"""POSIX-sh teardown script body for the cardinal-cleanup ECS task.

This module exposes a single constant, ``SCRIPT``, containing the full
inline shell that runs inside the cleanup container. The string is
embedded verbatim into the AWS::ECS::TaskDefinition's EntryPoint by
``cardinal_cleanup.py``. Keep this module dependency-free (no
troposphere) so tests can import and lint the shell directly.

The script is POSIX (``sh``), not bash. The container image
(``public.ecr.aws/aws-cli/aws-cli:latest``) provides ``sh``, ``aws``,
and ``python3`` -- and crucially NOT ``jq`` -- so JSON parsing uses
inline ``python3 -c`` snippets.
"""

SCRIPT = r"""#!/bin/sh
set -eu

# ---------------------------------------------------------------------------
# Pre-step: derive account + region from authoritative sources (NOT from env
# vars the operator could override via ecs:RunTask containerOverrides).
# ---------------------------------------------------------------------------

log() { printf '[cleanup] %s\n' "$*"; }
fail() { log "FATAL: $*"; exit 2; }

[ -n "${ECS_CONTAINER_METADATA_URI_V4:-}" ] \
    || fail "ECS_CONTAINER_METADATA_URI_V4 is unset; not running under ECS Fargate?"
[ -n "${LAKERUNNER_STACK_NAME:-}" ] || fail "LAKERUNNER_STACK_NAME is unset"
[ -n "${CLEANUP_STACK_NAME:-}" ]    || fail "CLEANUP_STACK_NAME is unset"
[ -n "${DEPLOYER_ROLE_ARN:-}" ]     || fail "DEPLOYER_ROLE_ARN is unset"
[ -n "${CLUSTER_NAME:-}" ]          || fail "CLUSTER_NAME is unset"

ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
[ -n "$ACCOUNT" ] && [ "$ACCOUNT" != "None" ] \
    || fail "could not derive account from sts:GetCallerIdentity"

REGION="$(python3 -c '
import json, os, urllib.request
uri = os.environ["ECS_CONTAINER_METADATA_URI_V4"]
with urllib.request.urlopen(uri + "/task", timeout=5) as r:
    arn = json.load(r)["TaskARN"]
print(arn.split(":")[3])
')"
[ -n "$REGION" ] || fail "could not derive region from ECS task metadata"

# All subsequent AWS CLI calls hit this region regardless of any operator-
# supplied AWS_REGION / AWS_DEFAULT_REGION envs.
export AWS_DEFAULT_REGION="$REGION"
unset AWS_REGION

log "account=$ACCOUNT region=$REGION"
log "lakerunner_stack=$LAKERUNNER_STACK_NAME cleanup_stack=$CLEANUP_STACK_NAME cluster=$CLUSTER_NAME"

# Deterministic resource names that data-setup.sh creates. These are baked in
# as literals -- env-overriding them would not help an attacker because IAM
# scopes are pinned to these exact names.
BUCKET="cardinal-ingest-${ACCOUNT}-${REGION}"
DB_ID="cardinal-db"
DB_SUBNET_GROUP="cardinal-db-subnet-group"
QUEUE_NAME="cardinal-ingest"
SECRETS="cardinal-db-master cardinal-license cardinal-admin-key"
SSM_PARAMS="/cardinal/storage-profiles /cardinal/api-keys"

OWNERSHIP_SKIPS=0

# Ownership-tag predicate. Returns 0 if the supplied JSON tag list (canonical
# AWS shape: array of {Key,Value}) has Application=cardinal-lakerunner AND
# ManagedBy=cardinal-data-setup-script. Otherwise returns 1.
ownership_ok() {
    printf '%s' "$1" | python3 -c '
import json, sys
tags = json.load(sys.stdin)
got = {t["Key"]: t["Value"] for t in tags}
ok = (
    got.get("Application") == "cardinal-lakerunner"
    and got.get("ManagedBy") == "cardinal-data-setup-script"
)
sys.exit(0 if ok else 1)
'
}

# ---------------------------------------------------------------------------
# Step 1 -- drain Cardinal ECS services.
#
# We never list services off the cluster; we walk cardinal-lakerunner's
# stack-resource tree and pull AWS::ECS::Service physical IDs (full ARNs).
# This means a non-Cardinal service in the same cluster is invisible to us.
# ---------------------------------------------------------------------------

# Recursively yields each lakerunner-owned ECS service ARN by walking the
# root + any AWS::CloudFormation::Stack children, paginated via NextToken.
# Output: one service ARN per line, on stdout.
discover_services() {
    stack_name="$1"
    next=""
    while :; do
        if [ -n "$next" ]; then
            page=$(aws cloudformation list-stack-resources \
                --stack-name "$stack_name" --next-token "$next" --output json)
        else
            page=$(aws cloudformation list-stack-resources \
                --stack-name "$stack_name" --output json 2>/dev/null \
                || { log "  stack $stack_name absent during drain discovery"; return 0; })
        fi
        printf '%s' "$page" | python3 -c '
import json, sys
data = json.load(sys.stdin)
for r in data.get("StackResourceSummaries", []):
    if r["ResourceType"] == "AWS::ECS::Service" and r.get("PhysicalResourceId"):
        print("SERVICE\t" + r["PhysicalResourceId"])
    elif r["ResourceType"] == "AWS::CloudFormation::Stack" and r.get("PhysicalResourceId"):
        print("STACK\t" + r["PhysicalResourceId"])
'       | while IFS="$(printf '\t')" read -r kind value; do
            case "$kind" in
                SERVICE) printf '%s\n' "$value" ;;
                STACK)   discover_services "$value" ;;
            esac
        done
        next=$(printf '%s' "$page" | python3 -c '
import json, sys
print(json.load(sys.stdin).get("NextToken") or "")
')
        [ -z "$next" ] && break
    done
}

# Extracts the last "/"-separated segment of an ECS service ARN -- the name
# AWS CLI's --service / --service-name flags expect (they reject full ARNs).
service_name_of() { printf '%s' "${1##*/}"; }

drain_services() {
    log "drain: discovering Cardinal services via CloudFormation"
    services_file=$(mktemp)
    discover_services "$LAKERUNNER_STACK_NAME" >"$services_file" || true
    if [ ! -s "$services_file" ]; then
        log "drain: no services found (stack may already be deleted)"
        rm -f "$services_file"
        return 0
    fi
    log "drain: $(wc -l <"$services_file" | tr -d ' ') Cardinal services to drain"

    while IFS= read -r svc_arn; do
        svc_name=$(service_name_of "$svc_arn")
        log "  desired=0 on $svc_name"
        aws ecs update-service --cluster "$CLUSTER_NAME" --service "$svc_name" \
            --desired-count 0 >/dev/null || true
        for status in RUNNING PENDING; do
            aws ecs list-tasks --cluster "$CLUSTER_NAME" --service-name "$svc_name" \
                --desired-status "$status" --query 'taskArns[]' --output text 2>/dev/null \
            | tr '\t' '\n' | while IFS= read -r task_arn; do
                [ -z "$task_arn" ] && continue
                [ "$task_arn" = "None" ] && continue
                log "  stop-task $task_arn"
                aws ecs stop-task --cluster "$CLUSTER_NAME" --task "$task_arn" \
                    --reason "cardinal-cleanup drain" >/dev/null || true
            done
        done
    done <"$services_file"

    log "drain: waiting up to 5 minutes for runningCount=0 pendingCount=0"
    deadline=$(( $(date +%s) + 300 ))
    any_running=1
    while [ "$(date +%s)" -lt "$deadline" ]; do
        any_running=0
        # describe-services is capped at 10 services per call.
        split -l 10 "$services_file" "${services_file}.batch."
        for batch in "${services_file}".batch.*; do
            arns=$(tr '\n' ' ' <"$batch")
            out=$(aws ecs describe-services --cluster "$CLUSTER_NAME" \
                    --services $arns --output json 2>/dev/null || echo '{}')
            still=$(printf '%s' "$out" | python3 -c '
import json, sys
data = json.load(sys.stdin)
print(sum(1 for s in data.get("services", []) if s.get("runningCount", 0) or s.get("pendingCount", 0)))
')
            [ "$still" != "0" ] && any_running=1
        done
        rm -f "${services_file}".batch.*
        [ "$any_running" -eq 0 ] && { log "drain: all services drained"; break; }
        sleep 10
    done
    if [ "$any_running" -ne 0 ]; then
        log "drain: WARNING -- timed out waiting for zero; delete-stack will force-stop"
    fi
    rm -f "$services_file"
}

drain_services

# ---------------------------------------------------------------------------
# Step 2 -- delete the cardinal-lakerunner CFN stack.
# Always uses $DEPLOYER_ROLE_ARN as --role-arn so the task-role IAM only
# needs iam:PassRole on the single deployer-role ARN.
# ---------------------------------------------------------------------------
delete_lakerunner_stack() {
    status=$(aws cloudformation describe-stacks \
        --stack-name "$LAKERUNNER_STACK_NAME" \
        --query 'Stacks[0].StackStatus' --output text 2>/dev/null \
        || echo "DOES_NOT_EXIST")
    if [ "$status" = "DOES_NOT_EXIST" ]; then
        log "lakerunner stack $LAKERUNNER_STACK_NAME already absent"
        return 0
    fi
    log "deleting CFN stack $LAKERUNNER_STACK_NAME (status: $status)"
    aws cloudformation delete-stack \
        --stack-name "$LAKERUNNER_STACK_NAME" \
        --role-arn "$DEPLOYER_ROLE_ARN"
    log "waiting for $LAKERUNNER_STACK_NAME stack-delete-complete (10+ min)"
    if ! aws cloudformation wait stack-delete-complete --stack-name "$LAKERUNNER_STACK_NAME"; then
        log "ERROR: stack-delete-complete wait failed for $LAKERUNNER_STACK_NAME"
        return 1
    fi
    log "stack $LAKERUNNER_STACK_NAME deleted"
}

delete_lakerunner_stack

# ---------------------------------------------------------------------------
# Step 3 -- drain + delete the S3 ingest bucket.
# Ownership-tag gated. Handles versioned buckets and in-flight multipart
# uploads (delete-bucket fails BucketNotEmpty otherwise).
# ---------------------------------------------------------------------------
delete_s3_bucket() {
    if ! aws s3api head-bucket --bucket "$BUCKET" >/dev/null 2>&1; then
        log "S3 bucket $BUCKET already absent"
        return 0
    fi
    tag_set=$(aws s3api get-bucket-tagging --bucket "$BUCKET" \
        --query 'TagSet' --output json 2>/dev/null || echo '[]')
    if ! ownership_ok "$tag_set"; then
        log "REFUSE: S3 bucket $BUCKET missing Cardinal ownership tags; skipping"
        OWNERSHIP_SKIPS=$((OWNERSHIP_SKIPS + 1))
        return 0
    fi
    log "draining bucket $BUCKET"
    aws s3 rm "s3://$BUCKET" --recursive --only-show-errors || true
    while :; do
        listing=$(aws s3api list-object-versions --bucket "$BUCKET" \
            --max-items 1000 --output json 2>/dev/null || printf '{}')
        delete_json=$(printf '%s' "$listing" | python3 -c '
import json, sys
p = json.load(sys.stdin)
items = (p.get("Versions") or []) + (p.get("DeleteMarkers") or [])
out = [{"Key": o["Key"], "VersionId": o["VersionId"]} for o in items]
if not out:
    sys.exit(1)
print(json.dumps({"Objects": out, "Quiet": True}))
') || break
        log "  deleting versioned batch"
        aws s3api delete-objects --bucket "$BUCKET" --delete "$delete_json" >/dev/null
    done
    log "aborting in-flight multipart uploads"
    aws s3api list-multipart-uploads --bucket "$BUCKET" --output json 2>/dev/null \
    | python3 -c '
import json, sys
data = json.load(sys.stdin)
for u in data.get("Uploads", []):
    print(u["Key"] + "\t" + u["UploadId"])
' | while IFS="$(printf '\t')" read -r key upload_id; do
        [ -z "$key" ] && continue
        aws s3api abort-multipart-upload --bucket "$BUCKET" \
            --key "$key" --upload-id "$upload_id" >/dev/null || true
    done
    log "deleting bucket $BUCKET"
    aws s3api delete-bucket --bucket "$BUCKET"
}

delete_s3_bucket

# ---------------------------------------------------------------------------
# Step 4 + 5 -- RDS instance + subnet group. Each ownership-tag gated.
# ---------------------------------------------------------------------------
delete_rds() {
    db_arn=$(aws rds describe-db-instances --db-instance-identifier "$DB_ID" \
        --query 'DBInstances[0].DBInstanceArn' --output text 2>/dev/null || echo "")
    if [ -z "$db_arn" ] || [ "$db_arn" = "None" ]; then
        log "RDS $DB_ID already absent"
    else
        tags=$(aws rds list-tags-for-resource --resource-name "$db_arn" \
            --query 'TagList' --output json 2>/dev/null || echo '[]')
        if ! ownership_ok "$tags"; then
            log "REFUSE: RDS $DB_ID missing Cardinal ownership tags; skipping"
            OWNERSHIP_SKIPS=$((OWNERSHIP_SKIPS + 1))
        else
            prot=$(aws rds describe-db-instances --db-instance-identifier "$DB_ID" \
                --query 'DBInstances[0].DeletionProtection' --output text)
            if [ "$prot" = "True" ]; then
                log "disabling deletion-protection on $DB_ID"
                aws rds modify-db-instance --db-instance-identifier "$DB_ID" \
                    --no-deletion-protection --apply-immediately >/dev/null
            fi
            log "deleting RDS $DB_ID (skip-final-snapshot)"
            aws rds delete-db-instance --db-instance-identifier "$DB_ID" \
                --skip-final-snapshot --delete-automated-backups >/dev/null
            log "waiting for $DB_ID delete (5-10 min)"
            aws rds wait db-instance-deleted --db-instance-identifier "$DB_ID"
        fi
    fi

    sg_arn=$(aws rds describe-db-subnet-groups --db-subnet-group-name "$DB_SUBNET_GROUP" \
        --query 'DBSubnetGroups[0].DBSubnetGroupArn' --output text 2>/dev/null || echo "")
    if [ -z "$sg_arn" ] || [ "$sg_arn" = "None" ]; then
        log "RDS subnet group $DB_SUBNET_GROUP already absent"
        return 0
    fi
    sg_tags=$(aws rds list-tags-for-resource --resource-name "$sg_arn" \
        --query 'TagList' --output json 2>/dev/null || echo '[]')
    if ! ownership_ok "$sg_tags"; then
        log "REFUSE: RDS subnet group missing Cardinal ownership tags; skipping"
        OWNERSHIP_SKIPS=$((OWNERSHIP_SKIPS + 1))
        return 0
    fi
    log "deleting RDS subnet group $DB_SUBNET_GROUP"
    aws rds delete-db-subnet-group --db-subnet-group-name "$DB_SUBNET_GROUP"
}

# ---------------------------------------------------------------------------
# Step 6 -- SQS queue. Ownership-tag gated.
# ---------------------------------------------------------------------------
delete_sqs() {
    url=$(aws sqs get-queue-url --queue-name "$QUEUE_NAME" \
        --query QueueUrl --output text 2>/dev/null || echo "")
    if [ -z "$url" ] || [ "$url" = "None" ]; then
        log "SQS $QUEUE_NAME already absent"
        return 0
    fi
    tags=$(aws sqs list-queue-tags --queue-url "$url" --output json 2>/dev/null || echo '{}')
    tag_list=$(printf '%s' "$tags" | python3 -c '
import json, sys
data = json.load(sys.stdin)
print(json.dumps([{"Key": k, "Value": v} for k, v in (data.get("Tags") or {}).items()]))
')
    if ! ownership_ok "$tag_list"; then
        log "REFUSE: SQS $QUEUE_NAME missing Cardinal ownership tags; skipping"
        OWNERSHIP_SKIPS=$((OWNERSHIP_SKIPS + 1))
        return 0
    fi
    log "deleting SQS $url"
    aws sqs delete-queue --queue-url "$url"
}

delete_rds
delete_sqs

# ---------------------------------------------------------------------------
# Step 7 -- the three cardinal-* secrets. Each ownership-tag gated and
# force-deleted (no recovery window).
# ---------------------------------------------------------------------------
delete_secrets() {
    for s in $SECRETS; do
        out=$(aws secretsmanager describe-secret --secret-id "$s" \
            --output json 2>/dev/null || echo "")
        if [ -z "$out" ]; then
            log "secret $s already absent"
            continue
        fi
        tags=$(printf '%s' "$out" | python3 -c '
import json, sys
print(json.dumps(json.load(sys.stdin).get("Tags") or []))
')
        if ! ownership_ok "$tags"; then
            log "REFUSE: secret $s missing Cardinal ownership tags; skipping"
            OWNERSHIP_SKIPS=$((OWNERSHIP_SKIPS + 1))
            continue
        fi
        log "force-deleting secret $s"
        aws secretsmanager delete-secret --secret-id "$s" \
            --force-delete-without-recovery >/dev/null
    done
}

# ---------------------------------------------------------------------------
# Step 8 -- the two /cardinal/* SSM parameters. Each ownership-tag gated.
# ---------------------------------------------------------------------------
delete_ssm() {
    for p in $SSM_PARAMS; do
        if ! aws ssm get-parameter --name "$p" >/dev/null 2>&1; then
            log "SSM parameter $p already absent"
            continue
        fi
        tags=$(aws ssm list-tags-for-resource --resource-type Parameter \
            --resource-id "$p" --query 'TagList' --output json 2>/dev/null || echo '[]')
        if ! ownership_ok "$tags"; then
            log "REFUSE: SSM parameter $p missing Cardinal ownership tags; skipping"
            OWNERSHIP_SKIPS=$((OWNERSHIP_SKIPS + 1))
            continue
        fi
        log "deleting SSM parameter $p"
        aws ssm delete-parameter --name "$p"
    done
}

delete_secrets
delete_ssm

# ---------------------------------------------------------------------------
# Step 9 -- self-delete the cardinal-cleanup stack. Async; we do NOT wait,
# because CFN will tear down the very task definition and log group we're
# running under. The driver observes task STOPPED via describe-tasks long
# before CFN finishes.
#
# The delete-stack call passes --role-arn $DEPLOYER_ROLE_ARN because the
# task role does not own (and should not own) ecs:DeregisterTaskDefinition
# or logs:DeleteLogGroup.
# ---------------------------------------------------------------------------
self_delete() {
    log "firing async delete-stack on $CLEANUP_STACK_NAME"
    if ! aws cloudformation delete-stack \
            --stack-name "$CLEANUP_STACK_NAME" \
            --role-arn "$DEPLOYER_ROLE_ARN"; then
        log "WARNING: self-delete API call errored; the destructive work is done,"
        log "         the operator should delete $CLEANUP_STACK_NAME manually."
    fi
}

# Exit non-zero if any ownership skip happened, so the driver can surface
# the partial cleanup. Self-delete is fired regardless (destructive work is
# already done) and does not influence the exit code.
self_delete
if [ "$OWNERSHIP_SKIPS" -ne 0 ]; then
    log "cleanup INCOMPLETE: $OWNERSHIP_SKIPS resource(s) skipped due to ownership tag mismatch"
    exit 1
fi
log "cleanup complete"
exit 0
"""
