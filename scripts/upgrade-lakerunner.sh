#!/bin/sh
# Safely upgrade an existing cardinal-lakerunner CloudFormation stack to a
# newer published template version.
#
# Self-contained: depends only on a POSIX shell, the AWS CLI v2, jq, and curl.
# Does not depend on the cardinal_cfn Python package or any other contents of
# the lakerunner-cloudformation repo.
#
# See docs/superpowers/specs/2026-05-01-jenkins-stack-upgrade-design.md for the
# full design.

set -eu

DEFAULT_TEMPLATE_BASE_URL="https://cardinal-cfn.s3.us-east-2.amazonaws.com/lakerunner"
DEFAULT_VERSION="latest"
CHANGE_SET_PREFIX="cardinal-upgrade-"

stack_name=""
region=""
template_base_url="$DEFAULT_TEMPLATE_BASE_URL"
version="$DEFAULT_VERSION"
deployer_role_arn=""
refresh_image_defaults="true"
no_execute="false"
internal_resolve_params=""
internal_resolve_params_arg2=""
internal_resolve_params_tbu=""
internal_classify_status=""
internal_classify_reason=""

# State held across stages so the abort handler can clean up.
change_set_name=""
work_dir=""

usage() {
    cat <<'EOF'
Usage: upgrade-lakerunner.sh [options]

Required:
  --stack-name NAME           Existing stack to upgrade.
  --region    REGION          AWS region of the stack.

Optional:
  --template-base-url URL     Default: https://cardinal-cfn.s3.us-east-2.amazonaws.com/lakerunner
  --version VERSION           Default: latest
  --deployer-role-arn ARN     Pass via --role-arn to all CloudFormation calls.
  --no-refresh-image-defaults Carry image params forward instead of taking new template defaults.
  --no-execute                Create and describe the change set, then stop.

Exit codes:
  0  success or no-op
  1  generic / AWS / change set failure
  2  pre-flight / input validation failure
EOF
}

log() {
    printf '[%s] %s\n' "upgrade-lakerunner" "$*" >&2
}

fail() {
    code="$1"
    shift
    printf '[upgrade-lakerunner] ERROR: %s\n' "$*" >&2
    exit "$code"
}

# ---------------------------------------------------------------------------
# Internal: pure parameter-resolution function used by tests and by the main
# pipeline.  Reads two JSON files, prints the resolved parameters list to
# stdout.
# ---------------------------------------------------------------------------
resolve_params() {
    new_template_file="$1"
    current_stack_file="$2"
    refresh_images="$3"
    # Optional: an explicit value for the TemplateBaseUrl parameter.  Carried
    # forward from the previous stack value would point nested children at the
    # OLD version, so we always override here when provided.
    explicit_template_base_url="${4:-}"

    if [ ! -r "$new_template_file" ]; then
        fail 2 "cannot read template-summary file: $new_template_file"
    fi
    if [ ! -r "$current_stack_file" ]; then
        fail 2 "cannot read current-stack-params file: $current_stack_file"
    fi

    # Build the resolved parameter list.  jq does the per-rule branching.
    resolved=$(jq -nc \
        --slurpfile newp "$new_template_file" \
        --slurpfile curp "$current_stack_file" \
        --arg refresh "$refresh_images" \
        --arg tbu "$explicit_template_base_url" \
        '
        ($newp[0] // []) as $newparams
        | ($curp[0] // []) as $current
        | ($current | map({(.ParameterKey): .ParameterValue}) | add // {}) as $current_by_key
        | $newparams
        | map(
            . as $p
            | $p.ParameterKey as $k
            | (($k == "TemplateBaseUrl") and ($tbu != "")) as $rule_tbu_override
            | (($k | endswith("Image")) and ($refresh == "true") and (has("DefaultValue"))) as $rule_image_refresh
            | ($current_by_key | has($k)) as $rule_carry
            | (has("DefaultValue")) as $rule_new_default
            | if $rule_tbu_override then
                {ParameterKey: $k, ParameterValue: $tbu}
              elif $rule_image_refresh then
                {ParameterKey: $k, ParameterValue: $p.DefaultValue}
              elif $rule_carry then
                {ParameterKey: $k, UsePreviousValue: true}
              elif $rule_new_default then
                {ParameterKey: $k, ParameterValue: $p.DefaultValue}
              else
                {ParameterKey: $k, _missing: true}
              end
          )
        '
    )

    # Detect missing parameters (rule 4 hard-fail).
    missing=$(printf '%s' "$resolved" | jq -r '[.[] | select(._missing == true) | .ParameterKey] | join(", ")')
    if [ -n "$missing" ]; then
        fail 2 "new template requires values for parameter(s) with no default and no current value: $missing"
    fi

    printf '%s\n' "$resolved" | jq '.'
}

# ---------------------------------------------------------------------------
# Internal: classify a change set status + reason as one of {success, noop,
# failure}.  Used by tests and by the main pipeline.
# ---------------------------------------------------------------------------
classify_status() {
    status="$1"
    reason="$2"

    case "$status" in
        CREATE_COMPLETE)
            printf 'success\n'
            return 0
            ;;
    esac

    # AWS reports several phrasings when a change set has no actual changes.
    # Match the common ones.
    case "$reason" in
        *"didn't contain changes"*|*"No updates are to be performed"*)
            printf 'noop\n'
            return 0
            ;;
    esac

    printf 'failure\n'
    return 0
}

# ---------------------------------------------------------------------------
# Pre-flight tool check.  First action; clear errors when a tool is missing.
# ---------------------------------------------------------------------------
preflight() {
    missing=""
    for tool in aws jq curl; do
        if ! command -v "$tool" >/dev/null 2>&1; then
            missing="$missing $tool"
        fi
    done
    if [ -n "$missing" ]; then
        cat >&2 <<EOF
[upgrade-lakerunner] ERROR: required tool(s) not found:$missing
Install hints:
  Debian/Ubuntu : sudo apt-get install -y awscli jq curl
  Amazon Linux  : sudo yum install -y aws-cli jq curl
  Alpine        : sudo apk add aws-cli jq curl
  macOS (brew)  : brew install awscli jq curl
EOF
        exit 2
    fi
}

# ---------------------------------------------------------------------------
# CFN call wrapper that adds --role-arn when configured.
#
# In this script the only call that accepts --role-arn is create-change-set:
# CFN attaches the role to the change set itself, then uses that role during
# execute-change-set automatically.  All other AWS CLI calls (including
# execute-change-set, get-template-summary, describe-*, wait) reject --role-arn
# and must invoke `aws cloudformation` directly.  Tests/test_upgrade_lakerunner_lint.py
# enforces this — keep it that way.
# ---------------------------------------------------------------------------
cfntool() {
    if [ -n "$deployer_role_arn" ]; then
        aws cloudformation "$@" --role-arn "$deployer_role_arn"
    else
        aws cloudformation "$@"
    fi
}

# ---------------------------------------------------------------------------
# Best-effort cleanup on abort.
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# Main pipeline.
# ---------------------------------------------------------------------------
parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --stack-name) stack_name="$2"; shift 2 ;;
            --region) region="$2"; shift 2 ;;
            --template-base-url) template_base_url="$2"; shift 2 ;;
            --version) version="$2"; shift 2 ;;
            --deployer-role-arn) deployer_role_arn="$2"; shift 2 ;;
            --refresh-image-defaults) refresh_image_defaults="true"; shift ;;
            --no-refresh-image-defaults) refresh_image_defaults="false"; shift ;;
            --no-execute) no_execute="true"; shift ;;
            --internal-resolve-params)
                internal_resolve_params="$2"
                internal_resolve_params_arg2="$3"
                shift 3
                ;;
            --internal-template-base-url)
                internal_resolve_params_tbu="$2"
                shift 2
                ;;
            --internal-classify-changeset-status)
                internal_classify_status="$2"
                internal_classify_reason="$3"
                shift 3
                ;;
            -h|--help) usage; exit 0 ;;
            *) fail 2 "unknown argument: $1" ;;
        esac
    done
}

main() {
    parse_args "$@"

    # Internal test hooks.  These do not touch AWS and bypass the rest of the
    # pipeline.  jq is required; aws and curl are not.
    if [ -n "$internal_resolve_params" ]; then
        if ! command -v jq >/dev/null 2>&1; then
            fail 2 "jq is required for --internal-resolve-params"
        fi
        resolve_params \
            "$internal_resolve_params" \
            "$internal_resolve_params_arg2" \
            "$refresh_image_defaults" \
            "$internal_resolve_params_tbu"
        return 0
    fi
    if [ -n "$internal_classify_status" ]; then
        classify_status "$internal_classify_status" "$internal_classify_reason"
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

    template_url="$template_base_url/$version/cardinal-lakerunner.yaml"
    log "resolving template: $template_url"
    if ! curl -sIfL "$template_url" >/dev/null; then
        fail 1 "template URL not reachable (HTTP HEAD failed): $template_url"
    fi

    work_dir=$(mktemp -d)

    log "fetching new template parameter schema via get-template-summary"
    # get-template-summary is read-only and does not accept --role-arn.
    aws cloudformation get-template-summary \
        --template-url "$template_url" \
        --region "$region" \
        --query 'Parameters' \
        --output json >"$work_dir/new-params.json"

    log "fetching current stack parameters"
    aws cloudformation describe-stacks \
        --stack-name "$stack_name" \
        --region "$region" \
        --query 'Stacks[0].Parameters' \
        --output json >"$work_dir/current-params.json"

    # The TemplateBaseUrl parameter encodes the version path nested children
    # are loaded from.  It MUST track the version we're upgrading to, otherwise
    # the new root will load the OLD nested templates and fail in confusing
    # ways.  Compute it from the same flags used to fetch the root template.
    effective_template_base_url="$template_base_url/$version/cardinal-lakerunner/"

    log "resolving parameters (refresh-image-defaults=$refresh_image_defaults, TemplateBaseUrl=$effective_template_base_url)"
    resolve_params \
        "$work_dir/new-params.json" \
        "$work_dir/current-params.json" \
        "$refresh_image_defaults" \
        "$effective_template_base_url" \
        >"$work_dir/parameters.json"

    change_set_name="${CHANGE_SET_PREFIX}$(date +%s)"
    log "creating change set: $change_set_name"
    cfntool create-change-set \
        --stack-name "$stack_name" \
        --change-set-name "$change_set_name" \
        --change-set-type UPDATE \
        --template-url "$template_url" \
        --parameters "file://$work_dir/parameters.json" \
        --capabilities CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND \
        --region "$region" >/dev/null

    log "waiting for change set to reach a terminal state"
    # The waiter exits non-zero on FAILED; we handle that ourselves so we can
    # distinguish no-op from real failure.
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
    # execute-change-set does not accept --role-arn; the role attached to the
    # change set at create time is used automatically during execution.
    aws cloudformation execute-change-set \
        --stack-name "$stack_name" \
        --change-set-name "$change_set_name" \
        --region "$region" >/dev/null

    log "waiting for stack-update-complete"
    if ! aws cloudformation wait stack-update-complete \
            --stack-name "$stack_name" \
            --region "$region"; then
        final_status=$(aws cloudformation describe-stacks \
            --stack-name "$stack_name" \
            --region "$region" \
            --query 'Stacks[0].StackStatus' --output text)
        fail 1 "stack did not reach UPDATE_COMPLETE; final status: $final_status"
    fi

    change_set_name=""

    log "stack outputs:"
    aws cloudformation describe-stacks \
        --stack-name "$stack_name" \
        --region "$region" \
        --query 'Stacks[0].Outputs' \
        --output table

    log "upgrade complete"
    return 0
}

trap cleanup EXIT INT TERM HUP

main "$@"
