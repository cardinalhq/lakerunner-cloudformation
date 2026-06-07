#!/bin/sh
# Tear down a per-stack Cardinal Lakerunner install: delete the five cardinal-*
# stacks in reverse-dependency order, then remove the retained, fixed-name
# resources that would otherwise block a fresh re-create. The customer-supplied
# (or lrdev-*) VPC and ECS cluster are left untouched.
#
# Self-contained: POSIX sh + AWS CLI v2 + jq. Environment-variable driven.
# Destructive: requires CONFIRM=DELETE. Without it, prints the plan and exits 0.
#
# Replaces the legacy dev-scripts/teardown-lakerunner.sh / cleanup-lakerunner.sh,
# which target the retired monolithic cardinal-lakerunner / cardinal-infrastructure
# stacks. See docs/operations/dev-environment.md.

set -eu

TAG="teardown-cardinal"
log()  { printf '[%s] [%s] %s\n' "$(date -u +%H:%M:%SZ)" "$TAG" "$*" >&2; }
fail() { code="$1"; shift; printf '[%s] ERROR: %s\n' "$TAG" "$*" >&2; exit "$code"; }

usage() {
    cat <<'EOF'
Usage: teardown-cardinal.sh   (environment-variable driven)

Deletes the per-stack Cardinal Lakerunner install (five cardinal-* stacks) and
wipes the retained survivors. Leaves the VPC and ECS cluster alone.

Required:
  REGION                      AWS region of the install.
  CONFIRM=DELETE              Required to actually destroy. Without it the
                              script prints the plan and exits 0.

Optional (stack name overrides; defaults shown):
  SERVICES_STACK              cardinal-lakerunner-services
  SAT_SERVICES_STACK          cardinal-satellite-services
  SAT_INFRA_STACK             cardinal-satellite-infra-base
  RDS_STACK                   cardinal-lakerunner-infra-rds
  INFRA_BASE_STACK            cardinal-lakerunner-infra-base

Optional (survivor handling):
  KEEP_SECRETS=true           Skip force-delete of cardinal-license /
                              cardinal-admin-key / cardinal-db-master.
  KEEP_BUCKETS=true           Skip empty+delete of the cooked / otel-raw buckets.
  DELETE_SNAPSHOTS=true       Also delete RDS snapshots whose id starts with the
                              RDS stack name (default: keep them; they cost only
                              storage and never block a re-create).
  COOKED_BUCKET / RAW_BUCKET  Override the derived bucket names
                              (cardinal-cooked-<acct>-<region> /
                              cardinal-otel-raw-<acct>-<region>).
  DEPLOYER_ROLE_ARN           Passed only to delete-stack (--role-arn).

Exit codes: 0 success / 1 failure / 2 input validation.
EOF
}

case "${1:-}" in
    -h|--help) usage; exit 0 ;;
    "") : ;;
    *) fail 2 "this script takes no arguments; configure it via environment variables" ;;
esac

[ -n "${REGION:-}" ] || { usage >&2; fail 2 "REGION is required"; }

SERVICES_STACK="${SERVICES_STACK:-cardinal-lakerunner-services}"
SAT_SERVICES_STACK="${SAT_SERVICES_STACK:-cardinal-satellite-services}"
SAT_INFRA_STACK="${SAT_INFRA_STACK:-cardinal-satellite-infra-base}"
RDS_STACK="${RDS_STACK:-cardinal-lakerunner-infra-rds}"
INFRA_BASE_STACK="${INFRA_BASE_STACK:-cardinal-lakerunner-infra-base}"
SECRETS="cardinal-license cardinal-admin-key cardinal-db-master"

# Reverse-dependency delete order.
STACKS="$SERVICES_STACK $SAT_SERVICES_STACK $SAT_INFRA_STACK $RDS_STACK $INFRA_BASE_STACK"

if [ "${CONFIRM:-}" != "DELETE" ]; then
    cat >&2 <<EOF
PLAN (set CONFIRM=DELETE to execute) — region $REGION:
  Delete stacks, in order: $STACKS
  Empty + delete buckets: $([ "${KEEP_BUCKETS:-}" = "true" ] && echo "(skipped: KEEP_BUCKETS)" || echo "${COOKED_BUCKET:-cardinal-cooked-<acct>-$REGION} ${RAW_BUCKET:-cardinal-otel-raw-<acct>-$REGION}")
  Force-delete secrets: $([ "${KEEP_SECRETS:-}" = "true" ] && echo "(skipped: KEEP_SECRETS)" || echo "$SECRETS")
  RDS snapshots: $([ "${DELETE_SNAPSHOTS:-}" = "true" ] && echo "delete (prefix $RDS_STACK)" || echo "kept")
  VPC + ECS cluster: left untouched.
EOF
    exit 0
fi

command -v aws >/dev/null 2>&1 || fail 1 "aws CLI v2 is required"
command -v jq  >/dev/null 2>&1 || fail 1 "jq is required"

ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
[ -n "$ACCOUNT" ] && [ "$ACCOUNT" != "None" ] || fail 1 "could not resolve AWS account id"
COOKED_BUCKET="${COOKED_BUCKET:-cardinal-cooked-$ACCOUNT-$REGION}"
RAW_BUCKET="${RAW_BUCKET:-cardinal-otel-raw-$ACCOUNT-$REGION}"

empty_bucket() {
    b="$1"
    if aws s3 ls "s3://$b" --region "$REGION" >/dev/null 2>&1; then
        log "emptying s3://$b"
        aws s3 rm "s3://$b" --recursive --region "$REGION" >/dev/null 2>&1 || true
    fi
}

delete_stack() {
    s="$1"
    if ! aws cloudformation describe-stacks --stack-name "$s" --region "$REGION" >/dev/null 2>&1; then
        log "$s already gone"; return 0
    fi
    log "deleting $s"
    if [ -n "${DEPLOYER_ROLE_ARN:-}" ]; then
        aws cloudformation delete-stack --stack-name "$s" --region "$REGION" --role-arn "$DEPLOYER_ROLE_ARN"
    else
        aws cloudformation delete-stack --stack-name "$s" --region "$REGION"
    fi
    aws cloudformation wait stack-delete-complete --stack-name "$s" --region "$REGION" \
        || fail 1 "$s did not reach DELETE_COMPLETE (check DELETE_FAILED events)"
    log "$s deleted"
}

# Empty the data buckets first so their owning stacks (which may use a Delete
# policy) don't fail deletion on a non-empty bucket.
if [ "${KEEP_BUCKETS:-}" != "true" ]; then
    empty_bucket "$RAW_BUCKET"
    empty_bucket "$COOKED_BUCKET"
fi

for s in $STACKS; do
    delete_stack "$s"
done

if [ "${KEEP_SECRETS:-}" != "true" ]; then
    for sec in $SECRETS; do
        if aws secretsmanager describe-secret --secret-id "$sec" --region "$REGION" >/dev/null 2>&1; then
            aws secretsmanager delete-secret --secret-id "$sec" --force-delete-without-recovery --region "$REGION" >/dev/null \
                && log "deleted secret $sec"
        fi
    done
fi

if [ "${KEEP_BUCKETS:-}" != "true" ]; then
    for b in "$COOKED_BUCKET" "$RAW_BUCKET"; do
        if aws s3 ls "s3://$b" --region "$REGION" >/dev/null 2>&1; then
            aws s3 rb "s3://$b" --force --region "$REGION" >/dev/null 2>&1 && log "removed bucket $b"
        fi
    done
fi

if [ "${DELETE_SNAPSHOTS:-}" = "true" ]; then
    snaps="$(aws rds describe-db-snapshots --region "$REGION" --snapshot-type manual \
        --query "DBSnapshots[?starts_with(DBSnapshotIdentifier, '$RDS_STACK')].DBSnapshotIdentifier" \
        --output text 2>/dev/null || true)"
    for snap in $snaps; do
        aws rds delete-db-snapshot --db-snapshot-identifier "$snap" --region "$REGION" >/dev/null 2>&1 \
            && log "deleted snapshot $snap"
    done
fi

log "teardown complete (VPC + ECS cluster left intact)"
