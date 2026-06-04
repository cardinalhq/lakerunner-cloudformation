#!/bin/sh
# Jenkins job 1: deploy the cardinal-lakerunner-infra-base stack.
#
# This is the head of the chain -- it has no upstream stacks.  It owns the IAM
# roles, security groups, cooked bucket, and license/admin secrets that every
# downstream stack consumes.  It seeds NO org content: Lakerunner installs
# admin-key-only and Maestro provisions the org via /api/v1/provision, so
# ORGANIZATION_ID lives on the services driver, not here.
#
# Self-contained single-file driver: this front-half composes the published
# template URL from TEMPLATE_BASE_URL + VERSION and builds the PARAMS block, then
# falls through into the engine embedded below (scripts-src/build.sh stitches the
# two; do not edit the generated copy).  Pure environment-variable interface (no
# flags).

set -eu

DEFAULT_TEMPLATE_BASE_URL="https://cardinal-cfn-us-east-1.s3.us-east-1.amazonaws.com/lakerunner"
TEMPLATE_KEY="cardinal-lakerunner-infra-base.yaml"
# Baked at publish time (scripts-src/build.sh).  STACK_VERSION defaults to this.
DEFAULT_STACK_VERSION="@@STACK_VERSION@@"

# Resolve optional execution-role managed-policy ARNs into the global
# EXEC_EXTRA_ARNS (a CSV, possibly empty), from two operator inputs:
#   EXECUTION_ROLE_POLICY_ARNS         ready-made managed-policy ARNs (CSV) -- the
#                                      "proper ops" path; passed through as-is.
#   EXECUTION_ROLE_POLICY_JSON[_FILE]  a pasted IAM policy document (multi-line
#                                      ok).  CFN cannot attach a string policy
#                                      (no Lambda in this product), so the driver
#                                      flattens it (jq -c) and creates/updates a
#                                      customer-managed policy named
#                                      <STACK_NAME>-exec-extra, then attaches its
#                                      ARN via the stack's ManagedPolicyArns.
# Called directly (not in $()) so a validation error can exit the whole script.
resolve_exec_role_policy_arns() {
    EXEC_EXTRA_ARNS="${EXECUTION_ROLE_POLICY_ARNS:-}"
    _erp_json=""
    if [ -n "${EXECUTION_ROLE_POLICY_JSON:-}" ]; then
        _erp_json="$EXECUTION_ROLE_POLICY_JSON"
    elif [ -n "${EXECUTION_ROLE_POLICY_JSON_FILE:-}" ]; then
        [ -r "$EXECUTION_ROLE_POLICY_JSON_FILE" ] || { echo "[deploy-lakerunner-infra-base] ERROR: cannot read EXECUTION_ROLE_POLICY_JSON_FILE: $EXECUTION_ROLE_POLICY_JSON_FILE" >&2; exit 2; }
        _erp_json=$(cat "$EXECUTION_ROLE_POLICY_JSON_FILE")
    fi
    [ -n "$_erp_json" ] || return 0

    command -v jq >/dev/null 2>&1 || { echo "[deploy-lakerunner-infra-base] ERROR: jq is required for EXECUTION_ROLE_POLICY_JSON" >&2; exit 2; }
    _erp_doc=$(printf '%s' "$_erp_json" | jq -c . 2>/dev/null) || { echo "[deploy-lakerunner-infra-base] ERROR: EXECUTION_ROLE_POLICY_JSON is not valid JSON" >&2; exit 2; }
    _erp_name="${STACK_NAME}-exec-extra"
    _erp_acct=$(aws sts get-caller-identity --query Account --output text)
    _erp_arn="arn:aws:iam::${_erp_acct}:policy/${_erp_name}"
    if _erp_def=$(aws iam get-policy --policy-arn "$_erp_arn" --query 'Policy.DefaultVersionId' --output text 2>/dev/null); then
        # IAM caps a policy at 5 versions; drop the non-default ones before adding.
        for _erp_v in $(aws iam list-policy-versions --policy-arn "$_erp_arn" --query 'Versions[].VersionId' --output text); do
            [ "$_erp_v" = "$_erp_def" ] && continue
            aws iam delete-policy-version --policy-arn "$_erp_arn" --version-id "$_erp_v" >/dev/null 2>&1 || true
        done
        aws iam create-policy-version --policy-arn "$_erp_arn" --policy-document "$_erp_doc" --set-as-default >/dev/null
        echo "[deploy-lakerunner-infra-base] updated execution-role managed policy: $_erp_arn" >&2
    else
        aws iam create-policy --policy-name "$_erp_name" --policy-document "$_erp_doc" --description "Cardinal execution-role extra policy for $STACK_NAME" >/dev/null
        echo "[deploy-lakerunner-infra-base] created execution-role managed policy: $_erp_arn" >&2
    fi
    if [ -n "$EXEC_EXTRA_ARNS" ]; then
        EXEC_EXTRA_ARNS="$EXEC_EXTRA_ARNS,$_erp_arn"
    else
        EXEC_EXTRA_ARNS="$_erp_arn"
    fi
}

usage() {
    cat <<EOF
deploy-lakerunner-infra-base.sh -- deploy the cardinal-lakerunner-infra-base stack.

All inputs come from environment variables (no flags).

Required:
  STACK_NAME           Stack to create/update.
  REGION               AWS region (never defaulted; must be set explicitly).
  VPC_ID               VPC for the security groups.
  CLUSTER_ARN          Customer-supplied ECS cluster ARN.
  LICENSE_DATA         Cardinal license token (z64:...), seeds the license
                       secret. Or supply LICENSE_DATA_FILE instead.

Optional (template defaults preserved when unset):
  STACK_VERSION                Published template version to deploy. Default: the
                               version baked into this driver ($DEFAULT_STACK_VERSION).
                               (VERSION is accepted as a legacy alias.)
  LICENSE_DATA_FILE            Path to a file holding the license token
                               (fallback for LICENSE_DATA).
  ALB_SCHEME                   internet-facing | internal (template default: internal).
  ALB_ALLOWED_CIDR1            ALB ingress CIDR allowlist (template default 10.0.0.0/8).
  ALB_ALLOWED_CIDR2            (template default 172.16.0.0/12).
  ALB_ALLOWED_CIDR3            (template default 192.168.0.0/16).
  COOKED_BUCKET_NAME           Explicit cooked bucket name.
  CONFIGURE_BUCKET_PUBLIC_ACCESS_BLOCK
                               'true' to set the cooked bucket's S3 Block Public
                               Access config (template default 'false': not set).
  LICENSE_SECRET_NAME          (template default cardinal-license).
  ADMIN_KEY_SECRET_NAME        (template default cardinal-admin-key).
  EXECUTION_ROLE_POLICY_ARNS   Comma-separated managed-policy ARNs to attach to
                               the ECS task execution role (e.g. ECR pull-through
                               import, cross-account ECR, KMS decrypt).
  EXECUTION_ROLE_POLICY_JSON   A pasted IAM policy document (multi-line ok). The
                               driver flattens it and creates/updates a customer
                               managed policy <STACK_NAME>-exec-extra, then
                               attaches it. Requires jq + iam:CreatePolicy/
                               CreatePolicyVersion. Or use *_FILE for a path.
  EXECUTION_ROLE_POLICY_JSON_FILE  Path fallback for EXECUTION_ROLE_POLICY_JSON.
  TEMPLATE_BASE_URL            Default: $DEFAULT_TEMPLATE_BASE_URL
  DEPLOYER_ROLE_ARN            Passed to create-change-set.
  NO_EXECUTE                   Non-empty: change-set only, do not execute.
EOF
}

case "${1:-}" in
    -h|--help) usage; exit 0 ;;
    "") : ;;
    *) echo "[deploy-lakerunner-infra-base] ERROR: this script takes no arguments; configure it via environment variables" >&2; usage >&2; exit 2 ;;
esac

# --- Required-input check (collect all missing, fail once). -------------------
missing=""
[ -z "${STACK_NAME:-}" ] && missing="$missing STACK_NAME"
[ -z "${REGION:-}" ] && missing="$missing REGION"
[ -z "${VPC_ID:-}" ] && missing="$missing VPC_ID"
[ -z "${CLUSTER_ARN:-}" ] && missing="$missing CLUSTER_ARN"
{ [ -z "${LICENSE_DATA:-}" ] && [ -z "${LICENSE_DATA_FILE:-}" ]; } && missing="$missing LICENSE_DATA"
if [ -n "$missing" ]; then
    usage >&2
    echo "[deploy-lakerunner-infra-base] ERROR: missing required: $(echo "$missing" | sed 's/^ //; s/ /, /g')" >&2
    exit 2
fi

# LICENSE_DATA (direct token) wins; LICENSE_DATA_FILE is the file fallback.
if [ -n "${LICENSE_DATA:-}" ]; then
    license_data="$LICENSE_DATA"
else
    if [ ! -r "$LICENSE_DATA_FILE" ]; then
        echo "[deploy-lakerunner-infra-base] ERROR: cannot read LICENSE_DATA_FILE: $LICENSE_DATA_FILE" >&2
        exit 2
    fi
    license_data=$(cat "$LICENSE_DATA_FILE")
fi

template_base_url="${TEMPLATE_BASE_URL:-$DEFAULT_TEMPLATE_BASE_URL}"
# STACK_VERSION (preferred) or the legacy VERSION alias, else the baked default.
stack_version="${STACK_VERSION:-${VERSION:-$DEFAULT_STACK_VERSION}}"

# --- Compose the deploy-stack.sh environment. --------------------------------
TEMPLATE_URL="$template_base_url/$stack_version/$TEMPLATE_KEY"

# Build PARAMS (newline-separated Key=Value).  Required values always present;
# optional ones added only when set so the template default applies otherwise.
params="VpcId=$VPC_ID
ClusterArn=$CLUSTER_ARN
LicenseData=$license_data"
[ -n "${ALB_SCHEME:-}" ] && params="$params
AlbScheme=$ALB_SCHEME"
[ -n "${ALB_ALLOWED_CIDR1:-}" ] && params="$params
AlbAllowedCidr1=$ALB_ALLOWED_CIDR1"
[ -n "${ALB_ALLOWED_CIDR2:-}" ] && params="$params
AlbAllowedCidr2=$ALB_ALLOWED_CIDR2"
[ -n "${ALB_ALLOWED_CIDR3:-}" ] && params="$params
AlbAllowedCidr3=$ALB_ALLOWED_CIDR3"
[ -n "${COOKED_BUCKET_NAME:-}" ] && params="$params
CookedBucketName=$COOKED_BUCKET_NAME"
[ -n "${CONFIGURE_BUCKET_PUBLIC_ACCESS_BLOCK:-}" ] && params="$params
ConfigureBucketPublicAccessBlock=$CONFIGURE_BUCKET_PUBLIC_ACCESS_BLOCK"
[ -n "${LICENSE_SECRET_NAME:-}" ] && params="$params
LicenseSecretName=$LICENSE_SECRET_NAME"
[ -n "${ADMIN_KEY_SECRET_NAME:-}" ] && params="$params
AdminKeySecretName=$ADMIN_KEY_SECRET_NAME"

# Optional execution-role extra managed policies (pasted JSON and/or ARNs).
EXEC_EXTRA_ARNS=""
resolve_exec_role_policy_arns
[ -n "$EXEC_EXTRA_ARNS" ] && params="$params
ExecutionRoleExtraPolicyArns=$EXEC_EXTRA_ARNS"

PARAMS="$params"
FROM_STACKS=""
MAPS=""

export TEMPLATE_URL PARAMS FROM_STACKS MAPS
