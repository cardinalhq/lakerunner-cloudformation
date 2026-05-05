#!/bin/sh
# Deploy a cardinal-lakerunner CloudFormation stack -- create it if missing,
# otherwise upgrade it in place to the requested template version.
#
# Self-contained: depends only on a POSIX shell, the AWS CLI v2, and jq.
# Does not depend on the cardinal_cfn Python package or any other contents of
# the lakerunner-cloudformation repo.
#
# Mode is auto-detected from describe-stacks: missing stack -> CREATE,
# existing stack -> UPDATE.  Install-only flags are ignored on UPDATE.
#
# See docs/operations/jenkins-deploy.md for operator documentation and
# docs/superpowers/specs/2026-05-01-jenkins-stack-upgrade-design.md for the
# original upgrade-only design (this script is the create-or-update successor).

set -eu

DEFAULT_TEMPLATE_BASE_URL="https://cardinal-cfn.s3.us-east-2.amazonaws.com/lakerunner"
CHANGE_SET_PREFIX="cardinal-deploy-"

# There is no "latest" tag in the published template bucket -- only versioned
# tags (vX.Y.Z).  --version is required; defaulting to "latest" silently 404s.
stack_name=""
region=""
template_base_url="$DEFAULT_TEMPLATE_BASE_URL"
version=""
deployer_role_arn=""
refresh_image_defaults="true"
no_execute="false"

# Install-only flags.  Ignored on UPDATE.
cli_vpc_id=""
cli_private_subnets=""
cli_certificate_arn=""
cli_dex_admin_email=""
cli_dex_admin_password_hash=""
cli_oidc_superadmin_emails=""
cli_license_data_file=""
cli_certificate_body_file=""
cli_certificate_private_key_file=""
cli_certificate_chain_file=""

# Internal test hooks.
internal_resolve_params=""
internal_resolve_params_arg2=""
internal_resolve_params_tbu=""
internal_resolve_create_params=""
internal_resolve_create_params_arg2=""
internal_classify_status=""
internal_classify_reason=""

# State held across stages so the abort handler can clean up.
change_set_name=""
work_dir=""

usage() {
    cat <<'EOF'
Usage: deploy-lakerunner.sh [options]

Required:
  --stack-name NAME           Stack to create or upgrade.
  --region    REGION          AWS region.
  --version   VERSION         Published template tag, e.g. v0.0.38.

Optional (both modes):
  --template-base-url URL     Default: https://cardinal-cfn.s3.us-east-2.amazonaws.com/lakerunner
  --deployer-role-arn ARN     Pass via --role-arn to create-change-set.
  --no-refresh-image-defaults UPDATE only: carry image params forward.
  --no-execute                Create and describe the change set, then stop.

Install-only (used when the stack does not yet exist; ignored on UPDATE):
  --vpc-id VPC                Required for install.
  --private-subnets CSV       Required for install (comma-separated subnet IDs).
  --certificate-arn ARN       Existing ACM cert (alternative to PEM import).
  --certificate-body-file PATH         PEM cert (used when --certificate-arn empty).
  --certificate-private-key-file PATH  PEM private key (used when --certificate-arn empty).
  --certificate-chain-file PATH        Optional intermediate chain PEM.
  --dex-admin-email EMAIL     DEX admin login (default: admin@cardinal.local).
  --dex-admin-password-hash HASH       Bcrypt hash for the DEX admin password. Required for install.
  --oidc-superadmin-emails CSV         Comma-separated maestro superadmin allowlist.
  --license-data-file PATH    Path to license JSON. Required for install.

Exit codes:
  0  success or no-op
  1  generic / AWS / change set failure
  2  pre-flight / input validation failure
EOF
}

log() {
    printf '[%s] %s\n' "deploy-lakerunner" "$*" >&2
}

fail() {
    code="$1"
    shift
    printf '[deploy-lakerunner] ERROR: %s\n' "$*" >&2
    exit "$code"
}

# ---------------------------------------------------------------------------
# Internal: pure parameter-resolution function for UPDATE mode.  Reads two
# JSON files, prints the resolved parameters list to stdout.
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
# Internal: pure parameter-resolution function for CREATE mode.  Reads the
# new template params and a JSON object of CLI-supplied overrides keyed by
# ParameterKey, prints the resolved parameters list to stdout.
# ---------------------------------------------------------------------------
resolve_create_params() {
    new_template_file="$1"
    overrides_file="$2"

    if [ ! -r "$new_template_file" ]; then
        fail 2 "cannot read template-summary file: $new_template_file"
    fi
    if [ ! -r "$overrides_file" ]; then
        fail 2 "cannot read overrides file: $overrides_file"
    fi

    resolved=$(jq -nc \
        --slurpfile newp "$new_template_file" \
        --slurpfile ovr "$overrides_file" \
        '
        ($newp[0] // []) as $newparams
        | ($ovr[0] // {}) as $overrides
        | $newparams
        | map(
            . as $p
            | $p.ParameterKey as $k
            | ($overrides | has($k)) as $rule_override
            | (has("DefaultValue")) as $rule_default
            | if $rule_override then
                {ParameterKey: $k, ParameterValue: $overrides[$k]}
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
        fail 2 "create requires values for parameter(s) with no default and no flag: $missing"
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
    for tool in aws jq; do
        if ! command -v "$tool" >/dev/null 2>&1; then
            missing="$missing $tool"
        fi
    done
    if [ -n "$missing" ]; then
        cat >&2 <<EOF
[deploy-lakerunner] ERROR: required tool(s) not found:$missing
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
# CFN call wrapper that adds --role-arn when configured.
#
# In this script the only call that accepts --role-arn is create-change-set:
# CFN attaches the role to the change set itself, then uses that role during
# execute-change-set automatically.  All other AWS CLI calls (including
# execute-change-set, get-template-summary, describe-*, wait) reject --role-arn
# and must invoke `aws cloudformation` directly.  tests/test_deploy_lakerunner_lint.py
# enforces this -- keep it that way.
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
            --vpc-id) cli_vpc_id="$2"; shift 2 ;;
            --private-subnets) cli_private_subnets="$2"; shift 2 ;;
            --certificate-arn) cli_certificate_arn="$2"; shift 2 ;;
            --certificate-body-file) cli_certificate_body_file="$2"; shift 2 ;;
            --certificate-private-key-file) cli_certificate_private_key_file="$2"; shift 2 ;;
            --certificate-chain-file) cli_certificate_chain_file="$2"; shift 2 ;;
            --dex-admin-email) cli_dex_admin_email="$2"; shift 2 ;;
            --dex-admin-password-hash) cli_dex_admin_password_hash="$2"; shift 2 ;;
            --oidc-superadmin-emails) cli_oidc_superadmin_emails="$2"; shift 2 ;;
            --license-data-file) cli_license_data_file="$2"; shift 2 ;;
            --internal-resolve-params)
                internal_resolve_params="$2"
                internal_resolve_params_arg2="$3"
                shift 3
                ;;
            --internal-template-base-url)
                internal_resolve_params_tbu="$2"
                shift 2
                ;;
            --internal-resolve-create-params)
                internal_resolve_create_params="$2"
                internal_resolve_create_params_arg2="$3"
                shift 3
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

# Read a file's full contents into stdout.  Empty path -> empty output.
read_file_or_empty() {
    path="$1"
    if [ -z "$path" ]; then
        printf ''
        return 0
    fi
    if [ ! -r "$path" ]; then
        fail 2 "cannot read file: $path"
    fi
    cat "$path"
}

# Build the create-mode overrides JSON object from CLI flags.  Empty values
# are omitted so the new-template default applies for those keys.
build_create_overrides() {
    out_file="$1"
    effective_tbu="$2"

    license_data=$(read_file_or_empty "$cli_license_data_file")
    cert_body=$(read_file_or_empty "$cli_certificate_body_file")
    cert_pkey=$(read_file_or_empty "$cli_certificate_private_key_file")
    cert_chain=$(read_file_or_empty "$cli_certificate_chain_file")

    jq -n \
        --arg vpc_id "$cli_vpc_id" \
        --arg private_subnets "$cli_private_subnets" \
        --arg cert_arn "$cli_certificate_arn" \
        --arg cert_body "$cert_body" \
        --arg cert_pkey "$cert_pkey" \
        --arg cert_chain "$cert_chain" \
        --arg license_data "$license_data" \
        --arg dex_email "$cli_dex_admin_email" \
        --arg dex_hash "$cli_dex_admin_password_hash" \
        --arg oidc_emails "$cli_oidc_superadmin_emails" \
        --arg tbu "$effective_tbu" \
        '{
            VpcId: $vpc_id,
            PrivateSubnets: $private_subnets,
            CertificateArn: $cert_arn,
            CertificateBody: $cert_body,
            CertificatePrivateKey: $cert_pkey,
            CertificateChain: $cert_chain,
            LicenseData: $license_data,
            DexAdminEmail: $dex_email,
            DexAdminPasswordHash: $dex_hash,
            OidcSuperadminEmails: $oidc_emails,
            TemplateBaseUrl: $tbu
        } | with_entries(select(.value != ""))' >"$out_file"
}

main() {
    parse_args "$@"

    # Internal test hooks.  These do not touch AWS and bypass the rest of the
    # pipeline.  jq is required; aws is not.
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
    if [ -n "$internal_resolve_create_params" ]; then
        if ! command -v jq >/dev/null 2>&1; then
            fail 2 "jq is required for --internal-resolve-create-params"
        fi
        resolve_create_params \
            "$internal_resolve_create_params" \
            "$internal_resolve_create_params_arg2"
        return 0
    fi
    if [ -n "$internal_classify_status" ]; then
        classify_status "$internal_classify_status" "$internal_classify_reason"
        return 0
    fi

    preflight

    if [ -z "$stack_name" ] || [ -z "$region" ] || [ -z "$version" ]; then
        usage >&2
        fail 2 "--stack-name, --region, and --version are required"
    fi

    log "checking whether stack $stack_name exists in $region"
    # REVIEW_IN_PROGRESS and ROLLBACK_COMPLETE both indicate a stack record
    # with no live resources:
    #   - REVIEW_IN_PROGRESS: a CREATE-type change set was described but
    #     never executed (`--no-execute` followed by no Apply, or a manual
    #     change-set delete).
    #   - ROLLBACK_COMPLETE: the initial CREATE failed and CloudFormation
    #     rolled everything back; the empty stack record remains and CFN
    #     refuses any further UPDATE against it.
    # Both must be deleted before a fresh CREATE can succeed, and both are
    # safe to delete (no surviving resources).  Treat the stack as
    # nonexistent for mode selection.
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

    template_url="$template_base_url/$version/cardinal-lakerunner.yaml"
    log "resolving template: $template_url"

    work_dir=$(mktemp -d)

    log "fetching new template parameter schema via get-template-summary"
    # get-template-summary is read-only and does not accept --role-arn.
    aws cloudformation get-template-summary \
        --template-url "$template_url" \
        --region "$region" \
        --query 'Parameters' \
        --output json >"$work_dir/new-params.json"

    # The TemplateBaseUrl parameter encodes the version path nested children
    # are loaded from.  It MUST track the version we're deploying, otherwise
    # the root will load the OLD nested templates and fail in confusing ways.
    effective_template_base_url="$template_base_url/$version/cardinal-lakerunner/"

    if [ "$mode" = "update" ]; then
        log "fetching current stack parameters"
        aws cloudformation describe-stacks \
            --stack-name "$stack_name" \
            --region "$region" \
            --query 'Stacks[0].Parameters' \
            --output json >"$work_dir/current-params.json"

        log "resolving parameters (refresh-image-defaults=$refresh_image_defaults, TemplateBaseUrl=$effective_template_base_url)"
        resolve_params \
            "$work_dir/new-params.json" \
            "$work_dir/current-params.json" \
            "$refresh_image_defaults" \
            "$effective_template_base_url" \
            >"$work_dir/parameters.json"
    else
        log "building create-mode overrides from CLI flags"
        build_create_overrides "$work_dir/overrides.json" "$effective_template_base_url"

        log "resolving parameters (TemplateBaseUrl=$effective_template_base_url)"
        resolve_create_params \
            "$work_dir/new-params.json" \
            "$work_dir/overrides.json" \
            >"$work_dir/parameters.json"
    fi

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
