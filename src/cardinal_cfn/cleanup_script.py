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

The script implements the four-step teardown ordered as:

  1. drain + delete the cardinal-lakerunner CFN stack
  2. empty the ingest S3 bucket
  3. delete the cardinal-infrastructure CFN stack (CFN snapshots+deletes
     the RDS instance because its DeletionPolicy is Snapshot; every
     other data-layer resource has DeletionPolicy: Retain and survives)
  4. sweep the Retain'd resources (S3 bucket itself, secrets, SSM
     parameters, SQS queue, RDS subnet group, the RDS final snapshot)

Then self-deletes the cardinal-cleanup stack.
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
[ -n "${INFRA_STACK_NAME:-}" ]      || fail "INFRA_STACK_NAME is unset"
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
log "lakerunner_stack=$LAKERUNNER_STACK_NAME infra_stack=$INFRA_STACK_NAME"
log "cleanup_stack=$CLEANUP_STACK_NAME cluster=$CLUSTER_NAME"

# ---------------------------------------------------------------------------
# Pre-step: discover the physical IDs of every resource the cardinal-
# infrastructure stack owns. We must do this BEFORE we delete the stack;
# once the wrapper is gone we cannot recover the CFN-generated names.
#
# DBInstance is the only resource with DeletionPolicy: Snapshot. Everything
# else is Retain, so step 3 (delete-stack) leaves them behind for step 4 to
# sweep up.
# ---------------------------------------------------------------------------
INGEST_BUCKET=""
DB_INSTANCE_ID=""
DB_SUBNET_GROUP=""
INGEST_QUEUE_URL=""
SECRET_IDS=""   # space-separated ARNs / names
SSM_PARAMS=""   # space-separated names

INFRA_EXISTS=false
if aws cloudformation describe-stacks --stack-name "$INFRA_STACK_NAME" >/dev/null 2>&1; then
    INFRA_EXISTS=true
    log "discovering retained resources from $INFRA_STACK_NAME"
    infra_res_json=$(aws cloudformation list-stack-resources \
        --stack-name "$INFRA_STACK_NAME" --output json)
    discovered=$(printf '%s' "$infra_res_json" | python3 -c '
import json, sys
data = json.load(sys.stdin)
out = {
    "bucket": "", "db_instance": "", "db_subnet_group": "",
    "queue_url": "", "secrets": [], "ssm_params": [],
}
for r in data.get("StackResourceSummaries", []):
    t   = r.get("ResourceType", "")
    pid = r.get("PhysicalResourceId", "")
    if not pid:
        continue
    if   t == "AWS::S3::Bucket":               out["bucket"] = pid
    elif t == "AWS::RDS::DBInstance":          out["db_instance"] = pid
    elif t == "AWS::RDS::DBSubnetGroup":       out["db_subnet_group"] = pid
    elif t == "AWS::SQS::Queue":               out["queue_url"] = pid
    elif t == "AWS::SecretsManager::Secret":   out["secrets"].append(pid)
    elif t == "AWS::SSM::Parameter":           out["ssm_params"].append(pid)
print(json.dumps(out))
')
    INGEST_BUCKET=$(printf '%s' "$discovered"    | python3 -c 'import json,sys; print(json.load(sys.stdin)["bucket"])')
    DB_INSTANCE_ID=$(printf '%s' "$discovered"   | python3 -c 'import json,sys; print(json.load(sys.stdin)["db_instance"])')
    DB_SUBNET_GROUP=$(printf '%s' "$discovered"  | python3 -c 'import json,sys; print(json.load(sys.stdin)["db_subnet_group"])')
    INGEST_QUEUE_URL=$(printf '%s' "$discovered" | python3 -c 'import json,sys; print(json.load(sys.stdin)["queue_url"])')
    SECRET_IDS=$(printf '%s' "$discovered"       | python3 -c 'import json,sys; print(" ".join(json.load(sys.stdin)["secrets"]))')
    SSM_PARAMS=$(printf '%s' "$discovered"       | python3 -c 'import json,sys; print(" ".join(json.load(sys.stdin)["ssm_params"]))')
    log "discovered:"
    log "  bucket           = $INGEST_BUCKET"
    log "  db_instance      = $DB_INSTANCE_ID"
    log "  db_subnet_group  = $DB_SUBNET_GROUP"
    log "  queue_url        = $INGEST_QUEUE_URL"
    log "  secrets          = $SECRET_IDS"
    log "  ssm_params       = $SSM_PARAMS"
else
    log "$INFRA_STACK_NAME absent; step 3 and step 4 sweep will be no-ops"
fi

# ===========================================================================
# Step 1 -- drain Cardinal ECS services + delete the cardinal-lakerunner CFN
# stack.
#
# Drain: walk the lakerunner stack tree, find every AWS::ECS::Service, set
# desiredCount=0, stop running and pending tasks, wait up to 5 minutes for
# zero. delete-stack on a draining service can race the deployment, so we do
# the drain ourselves first.
# ===========================================================================

# Recursively yields each lakerunner-owned ECS service ARN by walking the
# root + any AWS::CloudFormation::Stack children, paginated via NextToken.
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

# Extracts the last "/"-separated segment of an ECS service ARN.
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

# ===========================================================================
# Step 2 -- empty the ingest S3 bucket.
#
# Bucket has DeletionPolicy: Retain so it survives step 3 unchanged. We empty
# it now so step 4 can delete-bucket cleanly (delete-bucket requires empty).
# ===========================================================================
empty_ingest_bucket() {
    if [ -z "$INGEST_BUCKET" ]; then
        log "ingest bucket unknown (infra stack absent); skipping empty"
        return 0
    fi
    if ! aws s3api head-bucket --bucket "$INGEST_BUCKET" >/dev/null 2>&1; then
        log "ingest bucket $INGEST_BUCKET already absent"
        return 0
    fi
    log "emptying bucket $INGEST_BUCKET"
    aws s3 rm "s3://$INGEST_BUCKET" --recursive --only-show-errors || true
    while :; do
        listing=$(aws s3api list-object-versions --bucket "$INGEST_BUCKET" \
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
        aws s3api delete-objects --bucket "$INGEST_BUCKET" --delete "$delete_json" >/dev/null
    done
    log "aborting in-flight multipart uploads"
    aws s3api list-multipart-uploads --bucket "$INGEST_BUCKET" --output json 2>/dev/null \
    | python3 -c '
import json, sys
data = json.load(sys.stdin)
for u in data.get("Uploads", []):
    print(u["Key"] + "\t" + u["UploadId"])
' | while IFS="$(printf '\t')" read -r key upload_id; do
        [ -z "$key" ] && continue
        aws s3api abort-multipart-upload --bucket "$INGEST_BUCKET" \
            --key "$key" --upload-id "$upload_id" >/dev/null || true
    done
}

empty_ingest_bucket

# ===========================================================================
# Step 3 -- delete the cardinal-infrastructure CFN stack.
#
# CFN-side ordering: DBInstance has DeletionPolicy: Snapshot, so CFN takes a
# final snapshot and deletes the RDS instance. Every other data-layer
# resource has DeletionPolicy: Retain and survives this step; step 4 sweeps
# them up. Crucially the secret/RDS ordering is CFN's problem here, not
# ours -- if we manually delete the DB or the master secret first we hit a
# "secret can't be found" error during the in-flight snapshot.
# ===========================================================================
delete_infra_stack() {
    if [ "$INFRA_EXISTS" != "true" ]; then
        log "$INFRA_STACK_NAME already absent"
        return 0
    fi
    log "deleting CFN stack $INFRA_STACK_NAME (10+ min for RDS snapshot)"
    aws cloudformation delete-stack \
        --stack-name "$INFRA_STACK_NAME" \
        --role-arn "$DEPLOYER_ROLE_ARN"
    if ! aws cloudformation wait stack-delete-complete --stack-name "$INFRA_STACK_NAME"; then
        log "ERROR: stack-delete-complete wait failed for $INFRA_STACK_NAME"
        log "       step 4 will still try to wipe the retained data resources"
        return 1
    fi
    log "stack $INFRA_STACK_NAME deleted"
}

delete_infra_stack

# ===========================================================================
# Step 4 -- sweep the Retain'd resources.
#
# No data is preserved. We use the physical IDs we discovered from the infra
# stack BEFORE step 3, so a CFN-generated name in the previous deploy is no
# obstacle.
# ===========================================================================

# 4a -- S3 bucket (now empty from step 2)
delete_ingest_bucket() {
    if [ -z "$INGEST_BUCKET" ]; then return 0; fi
    if ! aws s3api head-bucket --bucket "$INGEST_BUCKET" >/dev/null 2>&1; then
        log "ingest bucket $INGEST_BUCKET already absent"
        return 0
    fi
    log "deleting bucket $INGEST_BUCKET"
    aws s3api delete-bucket --bucket "$INGEST_BUCKET"
}

# 4b -- secrets (DB master + license + admin-key; all force-deleted with no
# recovery window since the user accepted destructive cleanup)
delete_secrets() {
    for sid in $SECRET_IDS; do
        if ! aws secretsmanager describe-secret --secret-id "$sid" >/dev/null 2>&1; then
            log "secret $sid already absent"
            continue
        fi
        log "force-deleting secret $sid"
        aws secretsmanager delete-secret --secret-id "$sid" \
            --force-delete-without-recovery >/dev/null
    done
}

# 4c -- SSM parameters (storage profiles + api keys)
delete_ssm() {
    for p in $SSM_PARAMS; do
        if ! aws ssm get-parameter --name "$p" >/dev/null 2>&1; then
            log "SSM parameter $p already absent"
            continue
        fi
        log "deleting SSM parameter $p"
        aws ssm delete-parameter --name "$p"
    done
}

# 4d -- SQS ingest queue
delete_sqs() {
    if [ -z "$INGEST_QUEUE_URL" ]; then return 0; fi
    if ! aws sqs get-queue-attributes --queue-url "$INGEST_QUEUE_URL" \
            --attribute-names QueueArn >/dev/null 2>&1; then
        log "SQS queue $INGEST_QUEUE_URL already absent"
        return 0
    fi
    log "deleting SQS queue $INGEST_QUEUE_URL"
    aws sqs delete-queue --queue-url "$INGEST_QUEUE_URL"
}

# 4e -- RDS subnet group (only deletable after the DBInstance is gone; CFN's
# delete-stack handled the DBInstance in step 3).
delete_db_subnet_group() {
    if [ -z "$DB_SUBNET_GROUP" ]; then return 0; fi
    if ! aws rds describe-db-subnet-groups \
            --db-subnet-group-name "$DB_SUBNET_GROUP" >/dev/null 2>&1; then
        log "RDS subnet group $DB_SUBNET_GROUP already absent"
        return 0
    fi
    log "deleting RDS subnet group $DB_SUBNET_GROUP"
    aws rds delete-db-subnet-group --db-subnet-group-name "$DB_SUBNET_GROUP"
}

# 4f -- RDS snapshots produced by step 3 (Snapshot DeletionPolicy)
delete_rds_snapshots() {
    if [ -z "$DB_INSTANCE_ID" ]; then return 0; fi
    snaps=$(aws rds describe-db-snapshots \
        --db-instance-identifier "$DB_INSTANCE_ID" \
        --snapshot-type manual \
        --query 'DBSnapshots[].DBSnapshotIdentifier' --output text 2>/dev/null || echo "")
    [ -z "$snaps" ] && return 0
    for snap in $snaps; do
        log "deleting RDS snapshot $snap"
        aws rds delete-db-snapshot --db-snapshot-identifier "$snap" >/dev/null || true
    done
}

delete_ingest_bucket
delete_secrets
delete_ssm
delete_sqs
delete_db_subnet_group
delete_rds_snapshots

# ===========================================================================
# Step 5 -- self-delete the cardinal-cleanup stack. Async; we do NOT wait,
# because CFN will tear down the very task definition and log group we are
# running under. The driver observes task STOPPED via describe-tasks long
# before CFN finishes.
# ===========================================================================
self_delete() {
    log "firing async delete-stack on $CLEANUP_STACK_NAME"
    if ! aws cloudformation delete-stack \
            --stack-name "$CLEANUP_STACK_NAME" \
            --role-arn "$DEPLOYER_ROLE_ARN"; then
        log "WARNING: self-delete API call errored; the destructive work is done,"
        log "         the operator should delete $CLEANUP_STACK_NAME manually."
    fi
}

self_delete
log "cleanup complete"
exit 0
"""
