#!/bin/sh
# Tear down a deployed cardinal-lakerunner CloudFormation stack.
#
# Companion to scripts/deploy-lakerunner.sh.  Self-contained: depends only on
# a POSIX shell, the AWS CLI v2, and jq.
#
# Two install shapes:
#
# 1. Post-pivot installs (current).  The lakerunner stack owns no Retain or
#    Snapshot resources -- the data layer (RDS, S3 ingest, secrets, SSM,
#    SQS) lives outside both stacks, managed by the cardinal-data-setup
#    Lambda.  delete-stack alone removes everything the stack owns; the
#    post-delete cleanup helpers below no-op because the legacy nested
#    stacks (StorageStack, DatabaseStack, ConfigStack) are absent.  Wiping
#    the data layer is a separate operator-driven step -- see
#    docs/operations/tearing-down.md.
#
# 2. Legacy installs (pre-pivot).  The lakerunner stack still embeds the
#    data layer with Retain / Snapshot policies:
#      - cardinal-ingest-<account>-<region>-<InstallIdLong>   (S3, Retain)
#      - cardinal/<InstallIdLong>/license                      (Secrets, Retain)
#      - cardinal/<InstallIdLong>/admin-api-key                (Secrets, Retain)
#      - DbMasterSecret (auto-named, tagged db-master)         (Secrets, Retain)
#      - <stack>-Db-* final snapshot                           (RDS, Snapshot)
#    This script's post-delete cleanup pass handles those survivors.
#
# The deployer role (cardinal-cfn-deployer) is only assumable by
# cloudformation.amazonaws.com, so --deployer-role-arn is passed only to
# `delete-stack`.  The post-stack cleanup AWS CLI calls (s3api, secretsmanager,
# rds) run as the calling identity.

set -eu

stack_name=""
region=""
deployer_role_arn=""
yes="false"
keep_data="false"
keep_bucket="false"
keep_secrets="false"
keep_snapshot="false"

# Internal-only test hooks (pure data transforms, no AWS calls).
internal_format_plan=""
internal_check_yes=""

# State held across stages for the abort handler.
work_dir=""

usage() {
    cat <<'EOF'
Usage: teardown-lakerunner.sh [options]

Required:
  --stack-name NAME           Existing stack to tear down (root cardinal-lakerunner stack).
  --region REGION             AWS region of the stack.
  --yes                       Confirm destructive operation.  Without this the
                              script prints the plan and exits.

Optional:
  --deployer-role-arn ARN     Role ARN for the delete-stack call.  All other
                              cleanup AWS calls run as the calling identity.
  --keep-data                 Skip ALL post-delete cleanup (bucket, secrets, snapshot).
  --keep-bucket               Skip ingest-bucket drain + delete.
  --keep-secrets              Skip force-delete of retained secrets.
  --keep-snapshot             Skip RDS final snapshot delete.

Exit codes:
  0  success
  1  generic / AWS / cleanup failure
  2  pre-flight / input validation failure
EOF
}

log() {
    printf '[%s] %s\n' "teardown-lakerunner" "$*" >&2
}

fail() {
    code="$1"
    shift
    printf '[teardown-lakerunner] ERROR: %s\n' "$*" >&2
    exit "$code"
}

# ---------------------------------------------------------------------------
# Internal: render the human-readable cleanup plan from a captured-state JSON
# blob.  Used by --internal-format-plan for tests and by the main pipeline.
#
# Input shape (stdin or $1):
#   { "InstallIdLong": "...", "BucketName": "...",
#     "LicenseSecretArn": "...", "AdminApiKeySecretArn": "...",
#     "DbSecretArn": "...", "DbInstanceIdentifier": "..." }
# ---------------------------------------------------------------------------
format_plan() {
    state_file="$1"
    keep_bucket_flag="$2"
    keep_secrets_flag="$3"
    keep_snapshot_flag="$4"

    if [ ! -r "$state_file" ]; then
        fail 2 "cannot read state file: $state_file"
    fi

    jq -r \
        --arg keep_bucket "$keep_bucket_flag" \
        --arg keep_secrets "$keep_secrets_flag" \
        --arg keep_snapshot "$keep_snapshot_flag" \
        '
        def fmt(name; value): if (value // "") == "" then "  - " + name + ": (not found)" else "  - " + name + ": " + value end;
        [
          "Resources scheduled for cleanup AFTER the stack is deleted:",
          (if $keep_bucket == "true" then "  (S3 ingest bucket: SKIPPED via --keep-bucket)" else fmt("S3 ingest bucket"; .BucketName) end),
          (if $keep_secrets == "true" then "  (Retained secrets: SKIPPED via --keep-secrets)" else
            ([
              fmt("Secret (license)"; .LicenseSecretArn),
              fmt("Secret (admin-api-key)"; .AdminApiKeySecretArn),
              fmt("Secret (db-master)"; .DbSecretArn)
            ] | join("\n")) end),
          (if $keep_snapshot == "true" then "  (RDS final snapshot: SKIPPED via --keep-snapshot)" else
            "  - RDS final snapshot(s) for DB instance: " + (if (.DbInstanceIdentifier // "") == "" then "(not found)" else .DbInstanceIdentifier end) end)
        ] | join("\n")
        ' "$state_file"
}

# ---------------------------------------------------------------------------
# Internal: returns "ok" if --yes was supplied alongside an otherwise valid
# arg set.  Returns "missing-yes" otherwise.  Used by --internal-check-yes.
# ---------------------------------------------------------------------------
check_yes() {
    if [ "$yes" = "true" ]; then
        printf 'ok\n'
    else
        printf 'missing-yes\n'
    fi
}

# ---------------------------------------------------------------------------
# Pre-flight tool check.
# ---------------------------------------------------------------------------
preflight() {
    missing=""
    for tool in aws jq; do
        if ! command -v "$tool" >/dev/null 2>&1; then
            missing="$missing $tool"
        fi
    done
    if [ -n "$missing" ]; then
        cat >&2 <<EOF
[teardown-lakerunner] ERROR: required tool(s) not found:$missing
Install hints:
  Debian/Ubuntu : sudo apt-get install -y awscli jq
  Amazon Linux  : sudo yum install -y aws-cli jq
  Alpine        : sudo apk add aws-cli jq
  macOS (brew)  : brew install awscli jq
EOF
        exit 2
    fi
}

# ---------------------------------------------------------------------------
# Best-effort cleanup on abort.
# ---------------------------------------------------------------------------
cleanup() {
    rc=$?
    if [ -n "$work_dir" ] && [ -d "$work_dir" ]; then
        rm -rf "$work_dir"
    fi
    exit "$rc"
}

# ---------------------------------------------------------------------------
# Look up the physical ID (nested-stack ARN) of a child stack by its logical
# ID under the root stack.  Returns empty string if the child does not exist.
# ---------------------------------------------------------------------------
get_nested_stack_id() {
    parent="$1"
    logical_id="$2"
    aws cloudformation describe-stack-resource \
        --stack-name "$parent" \
        --logical-resource-id "$logical_id" \
        --region "$region" \
        --query 'StackResourceDetail.PhysicalResourceId' \
        --output text 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# Look up a single output value from a stack.  Empty string if missing.
# ---------------------------------------------------------------------------
get_stack_output() {
    stack="$1"
    key="$2"
    aws cloudformation describe-stacks \
        --stack-name "$stack" \
        --region "$region" \
        --query "Stacks[0].Outputs[?OutputKey=='$key'].OutputValue | [0]" \
        --output text 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# Capture the survivors' identifiers from the running stack, write them to
# $work_dir/state.json.  Must run BEFORE delete-stack — once the stack is
# gone the nested stacks are too, and we lose the ability to discover the
# physical IDs.
# ---------------------------------------------------------------------------
capture_state() {
    log "discovering nested stacks under $stack_name"
    storage_stack=$(get_nested_stack_id "$stack_name" "StorageStack")
    database_stack=$(get_nested_stack_id "$stack_name" "DatabaseStack")
    config_stack=$(get_nested_stack_id "$stack_name" "ConfigStack")

    install_id_long=$(get_stack_output "$stack_name" "InstallIdLong")

    bucket_name=""
    if [ -n "$storage_stack" ] && [ "$storage_stack" != "None" ]; then
        bucket_name=$(get_stack_output "$storage_stack" "BucketName")
    fi

    db_secret_arn=""
    db_instance_id=""
    if [ -n "$database_stack" ] && [ "$database_stack" != "None" ]; then
        db_secret_arn=$(get_stack_output "$database_stack" "DbSecretArn")
        db_instance_id=$(aws cloudformation describe-stack-resource \
            --stack-name "$database_stack" \
            --logical-resource-id "Db" \
            --region "$region" \
            --query 'StackResourceDetail.PhysicalResourceId' \
            --output text 2>/dev/null || true)
    fi

    license_secret_arn=""
    admin_secret_arn=""
    if [ -n "$config_stack" ] && [ "$config_stack" != "None" ]; then
        license_secret_arn=$(get_stack_output "$config_stack" "LicenseSecretArn")
        admin_secret_arn=$(get_stack_output "$config_stack" "AdminApiKeySecretArn")
    fi

    # Normalize "None" (returned by the CLI when a query yields no value) to "".
    [ "$install_id_long" = "None" ] && install_id_long=""
    [ "$bucket_name" = "None" ] && bucket_name=""
    [ "$db_secret_arn" = "None" ] && db_secret_arn=""
    [ "$db_instance_id" = "None" ] && db_instance_id=""
    [ "$license_secret_arn" = "None" ] && license_secret_arn=""
    [ "$admin_secret_arn" = "None" ] && admin_secret_arn=""

    jq -n \
        --arg install_id_long "$install_id_long" \
        --arg bucket "$bucket_name" \
        --arg db_secret "$db_secret_arn" \
        --arg db_id "$db_instance_id" \
        --arg license "$license_secret_arn" \
        --arg admin "$admin_secret_arn" \
        '{
          InstallIdLong: $install_id_long,
          BucketName: $bucket,
          DbSecretArn: $db_secret,
          DbInstanceIdentifier: $db_id,
          LicenseSecretArn: $license,
          AdminApiKeySecretArn: $admin
        }' >"$work_dir/state.json"
}

# ---------------------------------------------------------------------------
# Drain and delete the retained ingest bucket.  Handles versioned buckets
# (the lifecycle rules can leave delete markers and noncurrent versions).
# ---------------------------------------------------------------------------
drain_and_delete_bucket() {
    bucket="$1"
    if [ -z "$bucket" ]; then
        log "no bucket name captured; skipping bucket cleanup"
        return 0
    fi

    if ! aws s3api head-bucket --bucket "$bucket" --region "$region" >/dev/null 2>&1; then
        log "bucket $bucket already gone; skipping"
        return 0
    fi

    log "draining bucket $bucket (object versions + delete markers)"
    while :; do
        payload=$(aws s3api list-object-versions \
            --bucket "$bucket" \
            --region "$region" \
            --max-items 1000 \
            --output json 2>/dev/null || printf '{}\n')
        objects=$(printf '%s\n' "$payload" | jq -c '
            {Objects: ((.Versions // []) + (.DeleteMarkers // []))
              | map({Key: .Key, VersionId: .VersionId})}
        ')
        count=$(printf '%s\n' "$objects" | jq '.Objects | length')
        if [ "$count" = "0" ]; then
            break
        fi
        log "  deleting batch of $count object version(s)"
        printf '%s\n' "$objects" >"$work_dir/delete-batch.json"
        aws s3api delete-objects \
            --bucket "$bucket" \
            --region "$region" \
            --delete "file://$work_dir/delete-batch.json" \
            >/dev/null
    done

    log "deleting bucket $bucket"
    aws s3api delete-bucket --bucket "$bucket" --region "$region"
}

# ---------------------------------------------------------------------------
# Force-delete a Secrets Manager secret with no recovery window.  Tolerates
# the secret already being absent.
# ---------------------------------------------------------------------------
force_delete_secret() {
    arn="$1"
    label="$2"
    if [ -z "$arn" ]; then
        log "no $label secret captured; skipping"
        return 0
    fi
    if ! aws secretsmanager describe-secret \
            --secret-id "$arn" \
            --region "$region" >/dev/null 2>&1; then
        log "$label secret $arn already gone; skipping"
        return 0
    fi
    log "force-deleting $label secret: $arn"
    aws secretsmanager delete-secret \
        --secret-id "$arn" \
        --region "$region" \
        --force-delete-without-recovery >/dev/null
}

# ---------------------------------------------------------------------------
# Find and delete every manual snapshot for the captured DB instance.
# DeletionPolicy: Snapshot creates exactly one final snapshot, but a
# customer or rotation could have created others — delete them all.
# ---------------------------------------------------------------------------
delete_db_snapshots() {
    db_id="$1"
    if [ -z "$db_id" ]; then
        log "no DB instance identifier captured; skipping snapshot cleanup"
        return 0
    fi
    log "looking up manual snapshots for DB instance $db_id"
    snapshots=$(aws rds describe-db-snapshots \
        --db-instance-identifier "$db_id" \
        --snapshot-type manual \
        --region "$region" \
        --query 'DBSnapshots[*].DBSnapshotIdentifier' \
        --output text 2>/dev/null || true)
    if [ -z "$snapshots" ] || [ "$snapshots" = "None" ]; then
        log "no manual snapshots found for $db_id"
        return 0
    fi
    for snap in $snapshots; do
        log "deleting RDS snapshot $snap"
        aws rds delete-db-snapshot \
            --db-snapshot-identifier "$snap" \
            --region "$region" >/dev/null
    done
}

# ---------------------------------------------------------------------------
# Main pipeline.
# ---------------------------------------------------------------------------
parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --stack-name) stack_name="$2"; shift 2 ;;
            --region) region="$2"; shift 2 ;;
            --deployer-role-arn) deployer_role_arn="$2"; shift 2 ;;
            --yes) yes="true"; shift ;;
            --keep-data) keep_data="true"; shift ;;
            --keep-bucket) keep_bucket="true"; shift ;;
            --keep-secrets) keep_secrets="true"; shift ;;
            --keep-snapshot) keep_snapshot="true"; shift ;;
            --internal-format-plan)
                internal_format_plan="$2"
                shift 2
                ;;
            --internal-check-yes)
                internal_check_yes="true"
                shift
                ;;
            -h|--help) usage; exit 0 ;;
            *) fail 2 "unknown argument: $1" ;;
        esac
    done

    if [ "$keep_data" = "true" ]; then
        keep_bucket="true"
        keep_secrets="true"
        keep_snapshot="true"
    fi
}

main() {
    parse_args "$@"

    # Internal test hooks bypass AWS entirely.
    if [ -n "$internal_format_plan" ]; then
        if ! command -v jq >/dev/null 2>&1; then
            fail 2 "jq is required for --internal-format-plan"
        fi
        format_plan \
            "$internal_format_plan" \
            "$keep_bucket" \
            "$keep_secrets" \
            "$keep_snapshot"
        return 0
    fi
    if [ "$internal_check_yes" = "true" ]; then
        check_yes
        return 0
    fi

    preflight

    if [ -z "$stack_name" ] || [ -z "$region" ]; then
        usage >&2
        fail 2 "--stack-name and --region are required"
    fi

    log "verifying stack $stack_name exists in $region"
    if ! aws cloudformation describe-stacks \
            --stack-name "$stack_name" \
            --region "$region" >/dev/null 2>&1; then
        fail 2 "stack '$stack_name' not found in region '$region'"
    fi

    account_id=$(aws sts get-caller-identity --query Account --output text 2>/dev/null || echo "unknown")
    log "AWS account: $account_id  region: $region  stack: $stack_name"

    work_dir=$(mktemp -d)

    capture_state

    log "captured state:"
    cat "$work_dir/state.json" >&2

    plan=$(format_plan "$work_dir/state.json" "$keep_bucket" "$keep_secrets" "$keep_snapshot")
    printf '%s\n' "$plan" >&2

    if [ "$yes" != "true" ]; then
        log "DRY RUN: pass --yes to delete the stack and clean up the resources above."
        return 0
    fi

    log "deleting stack $stack_name"
    if [ -n "$deployer_role_arn" ]; then
        aws cloudformation delete-stack \
            --stack-name "$stack_name" \
            --region "$region" \
            --role-arn "$deployer_role_arn"
    else
        aws cloudformation delete-stack \
            --stack-name "$stack_name" \
            --region "$region"
    fi

    log "waiting for stack-delete-complete (this can take 10+ minutes)"
    if ! aws cloudformation wait stack-delete-complete \
            --stack-name "$stack_name" \
            --region "$region"; then
        final_status=$(aws cloudformation describe-stacks \
            --stack-name "$stack_name" \
            --region "$region" \
            --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo "DELETED")
        if [ "$final_status" != "DELETED" ] && [ "$final_status" != "DELETE_COMPLETE" ]; then
            fail 1 "stack did not reach DELETE_COMPLETE; final status: $final_status"
        fi
    fi
    log "stack deleted"

    bucket=$(jq -r '.BucketName // ""' "$work_dir/state.json")
    license_arn=$(jq -r '.LicenseSecretArn // ""' "$work_dir/state.json")
    admin_arn=$(jq -r '.AdminApiKeySecretArn // ""' "$work_dir/state.json")
    db_secret_arn=$(jq -r '.DbSecretArn // ""' "$work_dir/state.json")
    db_id=$(jq -r '.DbInstanceIdentifier // ""' "$work_dir/state.json")

    if [ "$keep_bucket" = "true" ]; then
        log "skipping bucket cleanup (--keep-bucket / --keep-data)"
    else
        drain_and_delete_bucket "$bucket"
    fi

    if [ "$keep_secrets" = "true" ]; then
        log "skipping secret force-delete (--keep-secrets / --keep-data)"
    else
        force_delete_secret "$license_arn" "license"
        force_delete_secret "$admin_arn" "admin-api-key"
        force_delete_secret "$db_secret_arn" "db-master"
    fi

    if [ "$keep_snapshot" = "true" ]; then
        log "skipping RDS snapshot delete (--keep-snapshot / --keep-data)"
    else
        delete_db_snapshots "$db_id"
    fi

    log "tear-down complete"
    return 0
}

trap cleanup EXIT INT TERM HUP

main "$@"
