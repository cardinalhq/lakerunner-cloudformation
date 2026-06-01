#!/bin/sh
# Generic chained CloudFormation deploy driver for the 5-stack satellite-ingest
# topology.  Create the stack if missing, otherwise update it in place, pulling
# parameter values from upstream stacks' Outputs so the per-stack Jenkins jobs
# can run as a chain.
#
# Self-contained: depends only on a POSIX shell, the AWS CLI v2, and jq.  Does
# not depend on the cardinal_cfn Python package.
#
# Mode is auto-detected from describe-stacks: missing stack -> CREATE,
# existing stack -> UPDATE.
#
# Per-parameter resolution precedence (from the target template's
# get-template-summary Parameters list):
#   1. --param Key=Value          explicit override (highest precedence)
#   2. --map TargetParam=SrcKey   value of an upstream Output named SrcKey
#   3. --from-stack output        an upstream Output whose key == the param name
#   4. UPDATE only: UsePreviousValue:true (carry the current stack value)
#   5. the template's Default
#   6. otherwise FAIL, listing the unresolved required parameters
#
# See docs/operations/jenkins-chained-deploy.md for operator documentation and
# the per-stack wrappers (deploy-lakerunner-infra-base.sh, etc.).

set -eu

CHANGE_SET_PREFIX="cardinal-deploy-"

stack_name=""
template_url=""
region=""
deployer_role_arn=""
no_execute="false"

# Repeatable collections, stored newline-delimited in shell variables.
from_stacks=""      # one stack name per line
param_overrides=""  # one Key=Value per line
maps=""             # one TargetParam=SourceOutputKey per line

# Internal test hook: resolve parameters from local JSON fixtures and print the
# resolved list, without touching AWS.
internal_resolve=""
internal_resolve_summary=""
internal_resolve_upstream=""
internal_resolve_current=""

# State held across stages so the abort handler can clean up.
change_set_name=""
work_dir=""

usage() {
    cat <<'EOF'
Usage: deploy-stack.sh --stack-name NAME --template-url URL --region REGION [options]

Required:
  --stack-name NAME           Stack to create or upgrade.
  --template-url URL          S3 URL of the generated template.
  --region    REGION          AWS region.

Chaining (all repeatable):
  --from-stack NAME           Pull Outputs from NAME; an Output whose key equals
                              a target parameter name supplies that parameter.
  --param Key=Value           Explicit parameter override (highest precedence).
  --map TargetParam=SrcKey    Assign TargetParam from upstream Output SrcKey.

Optional:
  --deployer-role-arn ARN     Pass via --role-arn to create-change-set.
  --no-execute                Create and describe the change set, then stop.

Exit codes:
  0  success or no-op
  1  generic / AWS / change set failure
  2  pre-flight / input validation failure
EOF
}

log() {
    printf '[%s] %s\n' "deploy-stack" "$*" >&2
}

fail() {
    code="$1"
    shift
    printf '[deploy-stack] ERROR: %s\n' "$*" >&2
    exit "$code"
}

# ---------------------------------------------------------------------------
# Pure parameter resolver.  Reads three JSON files:
#   summary_file  : the target template's get-template-summary Parameters array
#   upstream_file : a JSON object {ParamName: Value} of all candidate upstream
#                   values (from --from-stack outputs, --map'd outputs, and
#                   --param overrides), already merged in precedence order by
#                   the caller (later wins).  An object key present here is a
#                   resolved value.
#   current_file  : the current stack's Parameters array (UPDATE) or [] (CREATE)
# mode is "create" or "update".
# Prints the resolved CloudFormation --parameters list to stdout.
# ---------------------------------------------------------------------------
resolve_params() {
    summary_file="$1"
    upstream_file="$2"
    current_file="$3"
    mode="$4"

    if [ ! -r "$summary_file" ]; then
        fail 2 "cannot read template-summary file: $summary_file"
    fi
    if [ ! -r "$upstream_file" ]; then
        fail 2 "cannot read upstream-values file: $upstream_file"
    fi
    if [ ! -r "$current_file" ]; then
        fail 2 "cannot read current-stack-params file: $current_file"
    fi

    resolved=$(jq -nc \
        --slurpfile sum "$summary_file" \
        --slurpfile up "$upstream_file" \
        --slurpfile cur "$current_file" \
        --arg mode "$mode" \
        '
        ($sum[0] // []) as $params
        | ($up[0] // {}) as $upstream
        | ($cur[0] // []) as $current
        | ($current | map({(.ParameterKey): .ParameterValue}) | add // {}) as $current_by_key
        | $params
        | map(
            . as $p
            | $p.ParameterKey as $k
            | ($upstream | has($k)) as $rule_upstream
            | (($mode == "update") and ($current_by_key | has($k))) as $rule_carry
            | (has("DefaultValue")) as $rule_default
            | if $rule_upstream then
                {ParameterKey: $k, ParameterValue: ($upstream[$k] | tostring)}
              elif $rule_carry then
                {ParameterKey: $k, UsePreviousValue: true}
              elif $rule_default then
                {ParameterKey: $k, ParameterValue: $p.DefaultValue}
              else
                {ParameterKey: $k, _missing: true}
              end
          )
        '
    )

    missing=$(printf '%s' "$resolved" | jq -r '[.[] | select(._missing == true) | .ParameterKey] | join(", ")')
    if [ -n "$missing" ]; then
        fail 2 "no value (override, --map, upstream output, current value, or default) for required parameter(s): $missing"
    fi

    printf '%s\n' "$resolved" | jq '.'
}

# ---------------------------------------------------------------------------
# Classify a change set status + reason as one of {success, noop, failure}.
# ---------------------------------------------------------------------------
classify_status() {
    status="$1"
    reason="$2"
    case "$status" in
        CREATE_COMPLETE) printf 'success\n'; return 0 ;;
    esac
    case "$reason" in
        *"didn't contain changes"*|*"No updates are to be performed"*)
            printf 'noop\n'; return 0 ;;
    esac
    printf 'failure\n'
    return 0
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
[deploy-stack] ERROR: required tool(s) not found:$missing
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
# CFN call wrapper that adds --role-arn when configured.  Only create-change-set
# accepts --role-arn; CFN reuses that role during execute-change-set.  All other
# AWS calls must invoke `aws cloudformation` directly.  The lint test enforces
# this -- keep it that way.
# ---------------------------------------------------------------------------
cfntool() {
    if [ -n "$deployer_role_arn" ]; then
        aws cloudformation "$@" --role-arn "$deployer_role_arn"
    else
        aws cloudformation "$@"
    fi
}

cleanup() {
    rc=$?
    if [ "$rc" -ne 0 ] && [ -n "$change_set_name" ] && [ -n "$stack_name" ] && [ -n "$region" ]; then
        log "cleanup: deleting change set $change_set_name"
        aws cloudformation delete-change-set \
            --stack-name "$stack_name" \
            --change-set-name "$change_set_name" \
            --region "$region" >/dev/null 2>&1 || true
    fi
    if [ -n "$work_dir" ] && [ -d "$work_dir" ]; then
        rm -rf "$work_dir"
    fi
    exit "$rc"
}

parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --stack-name) stack_name="$2"; shift 2 ;;
            --template-url) template_url="$2"; shift 2 ;;
            --region) region="$2"; shift 2 ;;
            --from-stack) from_stacks="${from_stacks}$2
"; shift 2 ;;
            --param) param_overrides="${param_overrides}$2
"; shift 2 ;;
            --map) maps="${maps}$2
"; shift 2 ;;
            --deployer-role-arn) deployer_role_arn="$2"; shift 2 ;;
            --no-execute) no_execute="true"; shift ;;
            --internal-resolve-params)
                internal_resolve="true"
                internal_resolve_summary="$2"
                internal_resolve_upstream="$3"
                internal_resolve_current="$4"
                shift 4
                ;;
            -h|--help) usage; exit 0 ;;
            *) fail 2 "unknown argument: $1" ;;
        esac
    done
}

# Build the merged upstream-values JSON object into "$1", in precedence order
# (lowest first so later assignments win):  --from-stack -> --map -> --param.
build_upstream_values() {
    out_file="$1"

    merged='{}'

    # 3. --from-stack: every Output keyed by its OutputKey.
    if [ -n "$from_stacks" ]; then
        printf '%s' "$from_stacks" | while IFS= read -r sname; do
            [ -n "$sname" ] || continue
            log "pulling outputs from upstream stack: $sname"
            aws cloudformation describe-stacks \
                --stack-name "$sname" \
                --region "$region" \
                --query 'Stacks[0].Outputs' \
                --output json
            printf '\037'
        done >"$out_file.fromstacks"
        # Each stack's outputs array is separated by a 0x1f byte.  Fold them
        # into one object (later stacks win on key collision).
        merged=$(jq -nc --arg merged "$merged" '
            $merged | fromjson
        ' 2>/dev/null || echo '{}')
        # shellcheck disable=SC2030,SC2031
        merged=$(
            tr '\037' '\n' < "$out_file.fromstacks" \
            | jq -sc --argjson base "$merged" '
                reduce (.[] | select(. != null)) as $arr ($base;
                    . + (($arr // []) | map({(.OutputKey): .OutputValue}) | add // {}))
            '
        )
        rm -f "$out_file.fromstacks"
    fi

    # 2. --map TargetParam=SourceOutputKey: resolve SourceOutputKey from the
    #    already-merged from-stack outputs.
    if [ -n "$maps" ]; then
        printf '%s' "$maps" | while IFS= read -r entry; do
            [ -n "$entry" ] || continue
            tgt=${entry%%=*}
            src=${entry#*=}
            if [ "$tgt" = "$entry" ] || [ -z "$tgt" ] || [ -z "$src" ]; then
                fail 2 "--map expects TargetParam=SourceOutputKey, got: $entry"
            fi
            printf '%s\t%s\n' "$tgt" "$src"
        done >"$out_file.maps"
        merged=$(
            jq -nc --argjson m "$merged" --rawfile maps "$out_file.maps" '
                ($maps | split("\n") | map(select(length > 0)) | map(split("\t"))) as $pairs
                | reduce $pairs[] as $p ($m;
                    ($p[1]) as $src
                    | if ($m | has($src)) then . + {($p[0]): $m[$src]}
                      else error("--map source output not found upstream: " + $src + " (for " + $p[0] + ")")
                      end)
            '
        ) || fail 2 "failed resolving --map entries (see message above)"
        rm -f "$out_file.maps"
    fi

    # 1. --param Key=Value: highest precedence.
    if [ -n "$param_overrides" ]; then
        printf '%s' "$param_overrides" | while IFS= read -r entry; do
            [ -n "$entry" ] || continue
            key=${entry%%=*}
            val=${entry#*=}
            if [ "$key" = "$entry" ] || [ -z "$key" ]; then
                fail 2 "--param expects Key=Value, got: $entry"
            fi
            printf '%s\t%s\n' "$key" "$val"
        done >"$out_file.params"
        merged=$(
            jq -nc --argjson m "$merged" --rawfile params "$out_file.params" '
                ($params | split("\n") | map(select(length > 0)) | map(split("\t"))) as $pairs
                | reduce $pairs[] as $p ($m; . + {($p[0]): ($p[1:] | join("\t"))})
            '
        )
        rm -f "$out_file.params"
    fi

    printf '%s\n' "$merged" >"$out_file"
}

main() {
    parse_args "$@"

    # Internal test hook: resolve from fixtures only, never touch AWS.
    if [ -n "$internal_resolve" ]; then
        if ! command -v jq >/dev/null 2>&1; then
            fail 2 "jq is required for --internal-resolve-params"
        fi
        # Mode is inferred from whether the current-params fixture is non-empty.
        mode="create"
        if [ -s "$internal_resolve_current" ] \
            && [ "$(jq 'length' "$internal_resolve_current" 2>/dev/null || echo 0)" != "0" ]; then
            mode="update"
        fi
        resolve_params \
            "$internal_resolve_summary" \
            "$internal_resolve_upstream" \
            "$internal_resolve_current" \
            "$mode"
        return 0
    fi

    preflight

    if [ -z "$stack_name" ] || [ -z "$template_url" ] || [ -z "$region" ]; then
        usage >&2
        fail 2 "--stack-name, --template-url, and --region are required"
    fi

    log "checking whether stack $stack_name exists in $region"
    existing_status=$(aws cloudformation describe-stacks \
            --stack-name "$stack_name" \
            --region "$region" \
            --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo "")
    case "$existing_status" in
        REVIEW_IN_PROGRESS|ROLLBACK_COMPLETE)
            log "found stale $existing_status stack; deleting before CREATE"
            aws cloudformation delete-stack \
                --stack-name "$stack_name" \
                --region "$region" >/dev/null 2>&1 || true
            aws cloudformation wait stack-delete-complete \
                --stack-name "$stack_name" \
                --region "$region" >/dev/null 2>&1 || true
            existing_status=""
            ;;
    esac
    if [ -n "$existing_status" ]; then
        mode="update"
        cs_type="UPDATE"
        wait_target="stack-update-complete"
        target_status="UPDATE_COMPLETE"
    else
        mode="create"
        cs_type="CREATE"
        wait_target="stack-create-complete"
        target_status="CREATE_COMPLETE"
    fi
    log "mode: $mode"

    account_id=$(aws sts get-caller-identity --query Account --output text 2>/dev/null || echo "unknown")
    log "AWS account: $account_id  region: $region  stack: $stack_name"
    log "resolving template: $template_url"

    work_dir=$(mktemp -d)

    log "fetching new template parameter schema via get-template-summary"
    aws cloudformation get-template-summary \
        --template-url "$template_url" \
        --region "$region" \
        --query 'Parameters' \
        --output json >"$work_dir/summary.json"

    log "collecting upstream parameter values"
    build_upstream_values "$work_dir/upstream.json"

    if [ "$mode" = "update" ]; then
        aws cloudformation describe-stacks \
            --stack-name "$stack_name" \
            --region "$region" \
            --query 'Stacks[0].Parameters' \
            --output json >"$work_dir/current.json"
    else
        printf '[]\n' >"$work_dir/current.json"
    fi

    log "resolving parameters (mode=$mode)"
    resolve_params \
        "$work_dir/summary.json" \
        "$work_dir/upstream.json" \
        "$work_dir/current.json" \
        "$mode" \
        >"$work_dir/parameters.json"

    change_set_name="${CHANGE_SET_PREFIX}$(date +%s)"
    log "creating change set: $change_set_name (type=$cs_type)"
    cfntool create-change-set \
        --stack-name "$stack_name" \
        --change-set-name "$change_set_name" \
        --change-set-type "$cs_type" \
        --template-url "$template_url" \
        --parameters "file://$work_dir/parameters.json" \
        --capabilities CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND \
        --region "$region" >/dev/null

    log "waiting for change set to reach a terminal state"
    aws cloudformation wait change-set-create-complete \
        --stack-name "$stack_name" \
        --change-set-name "$change_set_name" \
        --region "$region" >/dev/null 2>&1 || true

    cs_status=$(aws cloudformation describe-change-set \
        --stack-name "$stack_name" \
        --change-set-name "$change_set_name" \
        --region "$region" \
        --query 'Status' --output text)
    cs_reason=$(aws cloudformation describe-change-set \
        --stack-name "$stack_name" \
        --change-set-name "$change_set_name" \
        --region "$region" \
        --query 'StatusReason' --output text 2>/dev/null || echo "")

    classification=$(classify_status "$cs_status" "$cs_reason")
    case "$classification" in
        noop)
            log "change set is a no-op; nothing to apply"
            aws cloudformation delete-change-set \
                --stack-name "$stack_name" \
                --change-set-name "$change_set_name" \
                --region "$region" >/dev/null 2>&1 || true
            change_set_name=""
            return 0
            ;;
        failure)
            fail 1 "change set $change_set_name failed: status=$cs_status reason=$cs_reason"
            ;;
    esac

    log "change set summary:"
    aws cloudformation describe-change-set \
        --stack-name "$stack_name" \
        --change-set-name "$change_set_name" \
        --region "$region" \
        --query 'Changes[*].ResourceChange.{Action:Action,Type:ResourceType,Logical:LogicalResourceId,Replacement:Replacement}' \
        --output table

    if [ "$no_execute" = "true" ]; then
        cs_arn=$(aws cloudformation describe-change-set \
            --stack-name "$stack_name" \
            --change-set-name "$change_set_name" \
            --region "$region" \
            --query 'ChangeSetId' --output text)
        log "--no-execute set; leaving change set in place"
        log "change set name: $change_set_name"
        log "change set ARN:  $cs_arn"
        change_set_name=""
        return 0
    fi

    log "executing change set"
    aws cloudformation execute-change-set \
        --stack-name "$stack_name" \
        --change-set-name "$change_set_name" \
        --region "$region" >/dev/null

    log "waiting for $wait_target"
    if ! aws cloudformation wait "$wait_target" \
            --stack-name "$stack_name" \
            --region "$region"; then
        final_status=$(aws cloudformation describe-stacks \
            --stack-name "$stack_name" \
            --region "$region" \
            --query 'Stacks[0].StackStatus' --output text)
        fail 1 "stack did not reach $target_status; final status: $final_status"
    fi

    change_set_name=""

    log "stack outputs:"
    aws cloudformation describe-stacks \
        --stack-name "$stack_name" \
        --region "$region" \
        --query 'Stacks[0].Outputs' \
        --output table

    log "deploy complete (mode=$mode)"
    return 0
}

trap cleanup EXIT INT TERM HUP

main "$@"
