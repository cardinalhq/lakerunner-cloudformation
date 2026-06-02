#!/usr/bin/env bash
# Shared helpers for the dev-scripts/*.sh family.
# Source this from a script; do not execute it directly.
#
# Conventions
# -----------
# - Every deploy script accepts inputs via environment variables only. The
#   defaults in each script never bake in customer-specific identifiers.
# - Stdout is structured for Jenkins log readability: timestamped lines with a
#   tag prefix that the operator can grep.
# - Exit non-zero on any failure; Jenkins will mark the job red.
# - Long-running waits stream stack events so the operator can see what AWS is
#   actually doing instead of watching a static "WAITING..." line.

set -euo pipefail

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_TAG="${LOG_TAG:-deploy}"

log()  { printf '[%s] [%s] %s\n' "$(date -u +%H:%M:%SZ)" "$LOG_TAG" "$*" >&2; }
die()  { log "FATAL: $*"; exit 1; }
warn() { log "WARN:  $*"; }

# ---------------------------------------------------------------------------
# Sanity checks
# ---------------------------------------------------------------------------
require_env() {
  local name="$1"
  local val="${!name:-}"
  if [ -z "$val" ]; then
    die "required env var $name is unset or empty"
  fi
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "required command '$1' not on PATH"
}

# Verify the runner can talk to AWS in the requested region and has a usable
# identity. Prints the account + identity so the Jenkins log shows who deployed.
preflight_aws() {
  local region="$1"
  need_cmd aws
  need_cmd jq
  local who
  who="$(aws sts get-caller-identity --output json 2>&1)" || \
    die "sts:GetCallerIdentity failed in $region: $who"
  log "deployer: $(echo "$who" | jq -r '"\(.Account) as \(.Arn)"')"
  aws --region "$region" ec2 describe-regions --region-names "$region" \
    >/dev/null 2>&1 || die "region $region is not reachable from this identity"
}

# HEAD the template URL so we fail loudly if the operator picked a non-existent
# version instead of producing a confusing CFN "ValidationError" later.
verify_template_published() {
  local url="$1"
  need_cmd curl
  local code
  code="$(curl -sI -o /dev/null -w '%{http_code}' "$url")"
  if [ "$code" != "200" ]; then
    die "template not reachable at $url (HTTP $code)"
  fi
  log "template OK: $url"
}

# ---------------------------------------------------------------------------
# Stack lifecycle
# ---------------------------------------------------------------------------
describe_stack_status() {
  local region="$1" stack="$2"
  aws cloudformation describe-stacks --region "$region" --stack-name "$stack" \
    --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo "DOES_NOT_EXIST"
}

# Idempotent deploy. Decides create-stack vs update-stack from current status.
# Args: region, stack, template-url, parameters-file, capabilities-csv.
deploy_stack() {
  local region="$1" stack="$2" template_url="$3" params_file="$4" capabilities_csv="$5"
  local capabilities_arr=()
  if [ -n "$capabilities_csv" ]; then
    IFS=',' read -r -a capabilities_arr <<< "$capabilities_csv"
  fi

  local status
  status="$(describe_stack_status "$region" "$stack")"
  log "$stack current state: $status"

  local action waiter
  case "$status" in
    DOES_NOT_EXIST)
      action=create-stack; waiter=stack-create-complete ;;
    ROLLBACK_COMPLETE|REVIEW_IN_PROGRESS|CREATE_FAILED)
      log "$stack is $status; deleting before recreating..."
      aws cloudformation delete-stack --region "$region" --stack-name "$stack"
      aws cloudformation wait stack-delete-complete --region "$region" --stack-name "$stack"
      action=create-stack; waiter=stack-create-complete ;;
    *_COMPLETE)
      action=update-stack; waiter=stack-update-complete ;;
    *)
      die "$stack is in non-actionable state $status; refusing to touch it" ;;
  esac

  log "running $action on $stack"
  set +e
  local out rc
  if [ "${#capabilities_arr[@]}" -gt 0 ]; then
    out="$(aws cloudformation "$action" --region "$region" --stack-name "$stack" \
            --template-url "$template_url" \
            --parameters "file://$params_file" \
            --capabilities "${capabilities_arr[@]}" 2>&1)"
  else
    out="$(aws cloudformation "$action" --region "$region" --stack-name "$stack" \
            --template-url "$template_url" \
            --parameters "file://$params_file" 2>&1)"
  fi
  rc=$?
  set -e
  if [ "$rc" -ne 0 ]; then
    if echo "$out" | grep -q "No updates are to be performed"; then
      log "$stack: no updates required"
      return 0
    fi
    log "$out"
    die "$action failed for $stack"
  fi

  log "waiting for $waiter; tailing events..."
  follow_events_until_terminal "$region" "$stack" "$waiter"
}

# Stream stack events in real time while the waiter runs. Exits when the
# waiter exits; on failure, prints the failed events so the operator does not
# have to scroll through the AWS console.
follow_events_until_terminal() {
  local region="$1" stack="$2" waiter="$3"

  aws cloudformation wait "$waiter" --region "$region" --stack-name "$stack" &
  local wait_pid=$!

  local last_event_ts=""
  while kill -0 "$wait_pid" 2>/dev/null; do
    local events
    events="$(aws cloudformation describe-stack-events --region "$region" --stack-name "$stack" \
                --query 'reverse(StackEvents[].[Timestamp,LogicalResourceId,ResourceStatus,ResourceStatusReason])' \
                --output json 2>/dev/null || echo '[]')"
    echo "$events" | jq -r --arg last "$last_event_ts" '
      .[] | select(.[0] > $last) |
      "[\(.[0])] \(.[2]) \(.[1]) \(.[3] // "")"
    '
    last_event_ts="$(echo "$events" | jq -r '.[-1][0] // ""')"
    sleep 10
  done

  if wait "$wait_pid"; then
    log "$stack reached terminal-success state"
  else
    log "$stack FAILED; recent CREATE_FAILED / UPDATE_FAILED events:"
    aws cloudformation describe-stack-events --region "$region" --stack-name "$stack" \
      --query 'StackEvents[?contains(ResourceStatus, `FAILED`)].[Timestamp,LogicalResourceId,ResourceStatus,ResourceStatusReason]' \
      --output table >&2
    return 1
  fi
}

# Print stack outputs as `KEY=value` lines for downstream scripts to consume.
dump_outputs_as_env() {
  local region="$1" stack="$2"
  aws cloudformation describe-stacks --region "$region" --stack-name "$stack" \
    --query 'Stacks[0].Outputs[].[OutputKey,OutputValue]' --output text \
    | awk '{print $1"="$2}'
}

# Build a parameters file from a sequence of KEY=VALUE pairs.
write_params_file() {
  local out="$1"; shift
  python3 - "$out" "$@" <<'PY'
import json, sys
out_path, *kvs = sys.argv[1:]
params = []
for kv in kvs:
    k, _, v = kv.partition("=")
    params.append({"ParameterKey": k, "ParameterValue": v})
with open(out_path, "w") as f:
    json.dump(params, f, indent=2)
PY
}
