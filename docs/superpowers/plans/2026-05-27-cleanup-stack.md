# Cardinal cleanup stack implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `cardinal-cleanup.yaml` (a published CFN stack that delivers a vetted ECS Fargate task definition) and `scripts/cleanup-lakerunner.sh` (a Jenkins driver). The driver creates the cleanup stack and launches the task; the task drains lakerunner ECS services, deletes the `cardinal-lakerunner` stack, wipes the cardinal-* data layer with ownership-tag enforcement, then self-deletes the cleanup stack. Operator role stays narrow.

**Architecture:** Troposphere generator at `src/cardinal_cfn/cardinal_cleanup.py` emits `generated-templates/cardinal-cleanup.yaml`; the inline POSIX-sh script lives in `src/cardinal_cfn/cleanup_script.py` as a Python string constant and is embedded in the task definition's `EntryPoint`. Driver script is POSIX shell + AWS CLI v2 + jq, mirroring `scripts/deploy-lakerunner.sh` conventions. Tests use the existing `tests/templates/` (cloud-radar) and `tests/unit/` (script-internal-hook) patterns. No new dependencies.

**Tech Stack:** Python 3 + troposphere (existing), POSIX shell, AWS CLI v2, jq, pytest, cloud-radar, cfn-lint, shellcheck (optional in tests).

**Spec:** `docs/superpowers/specs/2026-05-27-cleanup-stack-design.md` (authoritative).

---

## File map

**Created:**

- `src/cardinal_cfn/cleanup_script.py` — POSIX-sh teardown script as a Python string constant. Pure data; no troposphere imports. Importable from tests for shell-lint and fixture testing.
- `src/cardinal_cfn/cardinal_cleanup.py` — troposphere generator. `python3 -m cardinal_cfn.cardinal_cleanup` emits the YAML.
- `scripts/cleanup-lakerunner.sh` — operator driver (POSIX shell). Self-contained.
- `tests/templates/test_cardinal_cleanup.py` — cloud-radar assertions on the generated template.
- `tests/unit/test_cardinal_cleanup_module.py` — generator-module tests + inline-shell unit tests via fixtures.
- `tests/unit/test_cleanup_script_lint.py` — `sh -n` + optional `shellcheck` lint on the inline shell.
- `tests/unit/test_cleanup_lakerunner_sh.py` — driver internal-hook tests.
- `tests/unit/test_cleanup_lakerunner_lint.py` — `sh -n` + optional `shellcheck` on the driver.
- `docs/operations/cleanup.md` — operator runbook.

**Modified:**

- `build.sh` — add a `python3 -m cardinal_cfn.cardinal_cleanup` invocation and add the output file to the `cfn-lint` argument list.
- `Makefile` — add `generated-templates/cardinal-cleanup.yaml` to the `lint` target's cfn-lint invocation.

**Not modified (read-only context):**

- `do-not-commit-cleanup.sh` — reference implementation; lift logic, do not edit.
- `scripts/data-setup.sh` — read for tag conventions and exact resource names.
- `src/cardinal_cfn/policies.py`, `src/cardinal_cfn/naming.py` — existing helpers (not needed for cleanup, since the template owns only Delete-policy resources).

---

## Phase 1 — Inline shell script and its tests

The shell lives in `src/cardinal_cfn/cleanup_script.py` as one long string constant. We build it up function-by-function, with tests-first for each piece that has nontrivial logic (drain-discovery JSON parsing, ownership-tag predicate). Pure-shell helpers (e.g. `log()`) we lift from `do-not-commit-cleanup.sh` without re-testing — they are exercised by the integration tests in Phase 5.

### Task 1: Create cleanup_script.py skeleton

**Files:**

- Create: `src/cardinal_cfn/cleanup_script.py`

- [ ] **Step 1: Create the file with a docstring and an empty SCRIPT constant**

```python
"""POSIX-sh teardown script body for the cardinal-cleanup ECS task.

This module exposes a single constant, ``SCRIPT``, containing the full
inline shell that runs inside the cleanup container. The string is
embedded verbatim into the AWS::ECS::TaskDefinition's EntryPoint by
``cardinal_cleanup.py``. Keep this module dependency-free (no
troposphere) so tests can import and lint the shell directly.

The script is POSIX (``sh``), not bash. The container image
(``public.ecr.aws/aws-cli/aws-cli:latest``) provides ``sh``, ``aws``,
and ``python3`` — and crucially NOT ``jq`` — so JSON parsing uses
inline ``python3 -c`` snippets.
"""

SCRIPT = r"""#!/bin/sh
set -eu

# Body filled in by subsequent tasks.
"""
```

- [ ] **Step 2: Commit**

```bash
git add src/cardinal_cfn/cleanup_script.py
git commit -m "cleanup: add cleanup_script.py skeleton"
```

### Task 2: Add shell-lint test

**Files:**

- Create: `tests/unit/test_cleanup_script_lint.py`

- [ ] **Step 1: Write the failing test**

```python
"""Lint the inline cleanup shell body."""

import shutil
import subprocess
from pathlib import Path

import pytest

from cardinal_cfn.cleanup_script import SCRIPT


def _write_script(tmp_path):
    path = tmp_path / "cleanup.sh"
    path.write_text(SCRIPT)
    return path


def test_script_parses_with_posix_sh(tmp_path):
    """The shell body must be syntactically valid POSIX sh."""
    path = _write_script(tmp_path)
    result = subprocess.run(["sh", "-n", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, f"sh -n failed: {result.stderr}"


def test_script_passes_shellcheck(tmp_path):
    """Optional shellcheck pass. Skipped if shellcheck is not on PATH."""
    if shutil.which("shellcheck") is None:
        pytest.skip("shellcheck not installed on this runner")
    path = _write_script(tmp_path)
    result = subprocess.run(
        ["shellcheck", "-s", "sh", "-S", "warning", str(path)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"shellcheck failed:\n{result.stdout}\n{result.stderr}"
    )
```

- [ ] **Step 2: Run the test to verify it passes (skeleton is trivially valid)**

Run: `make install && .venv/bin/pytest tests/unit/test_cleanup_script_lint.py -v`
Expected: 2 passed (or 1 passed, 1 skipped if shellcheck absent).

- [ ] **Step 3: Commit**

```bash
git add tests/unit/test_cleanup_script_lint.py
git commit -m "cleanup: lint the inline shell with sh -n and optional shellcheck"
```

### Task 3: Add the pre-step (security-critical: account/region derivation)

**Files:**

- Modify: `src/cardinal_cfn/cleanup_script.py`

- [ ] **Step 1: Replace the placeholder body with the pre-step**

Replace the `SCRIPT = r"""..."""` constant in `src/cardinal_cfn/cleanup_script.py` with:

```python
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
"""
```

- [ ] **Step 2: Run the lint tests**

Run: `.venv/bin/pytest tests/unit/test_cleanup_script_lint.py -v`
Expected: 2 passed (or 1 passed, 1 skipped if no shellcheck).

- [ ] **Step 3: Commit**

```bash
git add src/cardinal_cfn/cleanup_script.py
git commit -m "cleanup: pre-step account+region derivation and ownership predicate"
```

### Task 4: Add ownership-predicate fixture tests

**Files:**

- Create: `tests/unit/test_cleanup_script_ownership.py`

- [ ] **Step 1: Write the failing tests**

```python
"""Fixture tests for the ownership_ok shell helper in cleanup_script.SCRIPT."""

import json
import subprocess
import textwrap
from pathlib import Path

from cardinal_cfn.cleanup_script import SCRIPT

# The helper exists inside SCRIPT. We rip it out and run it standalone with
# canned JSON input, so we don't have to mock STS / ECS metadata for the
# parent script's pre-step.
_PRELUDE = textwrap.dedent(
    """
    log() { :; }
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
    """
)


def _run(tags):
    payload = json.dumps(tags)
    result = subprocess.run(
        ["sh", "-c", f'{_PRELUDE}\nownership_ok \'{payload}\''],
        capture_output=True,
        text=True,
    )
    return result.returncode


def test_ownership_ok_with_both_tags():
    assert _run([
        {"Key": "Application", "Value": "cardinal-lakerunner"},
        {"Key": "ManagedBy",   "Value": "cardinal-data-setup-script"},
    ]) == 0


def test_ownership_rejects_missing_application():
    assert _run([
        {"Key": "ManagedBy", "Value": "cardinal-data-setup-script"},
    ]) == 1


def test_ownership_rejects_missing_managed_by():
    assert _run([
        {"Key": "Application", "Value": "cardinal-lakerunner"},
    ]) == 1


def test_ownership_rejects_wrong_application():
    assert _run([
        {"Key": "Application", "Value": "rogue"},
        {"Key": "ManagedBy",   "Value": "cardinal-data-setup-script"},
    ]) == 1


def test_ownership_rejects_wrong_managed_by():
    assert _run([
        {"Key": "Application", "Value": "cardinal-lakerunner"},
        {"Key": "ManagedBy",   "Value": "someone-else"},
    ]) == 1


def test_ownership_rejects_empty():
    assert _run([]) == 1


def test_helper_string_is_present_in_main_script():
    """Regression: if the helper drifts, our standalone copy is stale."""
    assert "ownership_ok()" in SCRIPT
    assert "cardinal-data-setup-script" in SCRIPT
```

- [ ] **Step 2: Run the tests**

Run: `.venv/bin/pytest tests/unit/test_cleanup_script_ownership.py -v`
Expected: 7 passed.

- [ ] **Step 3: Commit**

```bash
git add tests/unit/test_cleanup_script_ownership.py
git commit -m "cleanup: tests for ownership_ok predicate"
```

### Task 5: Add the drain step to the inline shell

**Files:**

- Modify: `src/cardinal_cfn/cleanup_script.py`

- [ ] **Step 1: Append the drain helpers + drain step before the closing `"""`**

Append before the closing triple-quote in `SCRIPT`:

```
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
```

- [ ] **Step 2: Run the lint tests**

Run: `.venv/bin/pytest tests/unit/test_cleanup_script_lint.py -v`
Expected: 2 passed (or 1 + 1 skipped).

- [ ] **Step 3: Commit**

```bash
git add src/cardinal_cfn/cleanup_script.py
git commit -m "cleanup: drain step (CFN-discovered services, batch describe, drain wait)"
```

### Task 6: Add a fixture test for discover_services JSON parsing

**Files:**

- Create: `tests/unit/test_cleanup_script_discover.py`

- [ ] **Step 1: Write the test**

The shell `discover_services` shells out to AWS. We isolate just the python3 JSON-parsing fragment and test it with canned `list-stack-resources` output.

```python
"""Fixture tests for the JSON parsing inside discover_services."""

import json
import subprocess
import textwrap

# This is the literal python -c fragment used inside discover_services. If
# the shell version changes, update here and assert presence in SCRIPT.
PYSRC = textwrap.dedent(
    """
    import json, sys
    data = json.load(sys.stdin)
    for r in data.get("StackResourceSummaries", []):
        if r["ResourceType"] == "AWS::ECS::Service" and r.get("PhysicalResourceId"):
            print("SERVICE\\t" + r["PhysicalResourceId"])
        elif r["ResourceType"] == "AWS::CloudFormation::Stack" and r.get("PhysicalResourceId"):
            print("STACK\\t" + r["PhysicalResourceId"])
    """
).strip()


def _parse(payload):
    result = subprocess.run(
        ["python3", "-c", PYSRC],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return [line.split("\t", 1) for line in result.stdout.strip().splitlines() if line]


def test_extracts_service_arn():
    parsed = _parse({"StackResourceSummaries": [{
        "ResourceType": "AWS::ECS::Service",
        "PhysicalResourceId": "arn:aws:ecs:us-east-1:111:service/c/svc-A",
    }]})
    assert parsed == [["SERVICE", "arn:aws:ecs:us-east-1:111:service/c/svc-A"]]


def test_extracts_nested_stack():
    parsed = _parse({"StackResourceSummaries": [{
        "ResourceType": "AWS::CloudFormation::Stack",
        "PhysicalResourceId": "arn:aws:cloudformation:us-east-1:111:stack/cardinal-lakerunner-ServicesQuery-X/Y",
    }]})
    assert parsed == [["STACK",
        "arn:aws:cloudformation:us-east-1:111:stack/cardinal-lakerunner-ServicesQuery-X/Y"]]


def test_mixed_resources():
    parsed = _parse({"StackResourceSummaries": [
        {"ResourceType": "AWS::ECS::Service",
         "PhysicalResourceId": "arn:aws:ecs:us-east-1:111:service/c/svc-A"},
        {"ResourceType": "AWS::ECS::TaskDefinition",
         "PhysicalResourceId": "arn:aws:ecs:us-east-1:111:task-definition/family:1"},
        {"ResourceType": "AWS::CloudFormation::Stack",
         "PhysicalResourceId": "arn:aws:cloudformation:us-east-1:111:stack/child/uuid"},
        {"ResourceType": "AWS::ECS::Service",
         "PhysicalResourceId": "arn:aws:ecs:us-east-1:111:service/c/svc-B"},
    ]})
    assert parsed == [
        ["SERVICE", "arn:aws:ecs:us-east-1:111:service/c/svc-A"],
        ["STACK",   "arn:aws:cloudformation:us-east-1:111:stack/child/uuid"],
        ["SERVICE", "arn:aws:ecs:us-east-1:111:service/c/svc-B"],
    ]


def test_skips_resources_without_physical_id():
    parsed = _parse({"StackResourceSummaries": [
        {"ResourceType": "AWS::ECS::Service", "PhysicalResourceId": None},
    ]})
    assert parsed == []


def test_empty_summary():
    assert _parse({"StackResourceSummaries": []}) == []
    assert _parse({}) == []
```

- [ ] **Step 2: Run the tests**

Run: `.venv/bin/pytest tests/unit/test_cleanup_script_discover.py -v`
Expected: 6 passed.

- [ ] **Step 3: Commit**

```bash
git add tests/unit/test_cleanup_script_discover.py
git commit -m "cleanup: tests for discover_services JSON parsing"
```

### Task 7: Add the lakerunner-stack delete step

**Files:**

- Modify: `src/cardinal_cfn/cleanup_script.py`

- [ ] **Step 1: Append the lakerunner-delete step**

Append before the closing triple-quote:

```
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
```

- [ ] **Step 2: Run lint**

Run: `.venv/bin/pytest tests/unit/test_cleanup_script_lint.py -v`
Expected: pass.

- [ ] **Step 3: Commit**

```bash
git add src/cardinal_cfn/cleanup_script.py
git commit -m "cleanup: step 2 -- delete cardinal-lakerunner with deployer role"
```

### Task 8: Add the S3-ingest wipe step (drain + multipart abort + delete)

**Files:**

- Modify: `src/cardinal_cfn/cleanup_script.py`

- [ ] **Step 1: Append S3 wipe with ownership check + multipart abort**

```
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

OWNERSHIP_SKIPS=0
delete_s3_bucket
```

- [ ] **Step 2: Run lint**

Run: `.venv/bin/pytest tests/unit/test_cleanup_script_lint.py -v`
Expected: pass.

- [ ] **Step 3: Commit**

```bash
git add src/cardinal_cfn/cleanup_script.py
git commit -m "cleanup: step 3 -- drain S3, abort multipart, delete with ownership gate"
```

### Task 9: Add RDS, RDS subnet group, SQS delete steps

**Files:**

- Modify: `src/cardinal_cfn/cleanup_script.py`

- [ ] **Step 1: Append**

```
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
```

- [ ] **Step 2: Run lint**

Run: `.venv/bin/pytest tests/unit/test_cleanup_script_lint.py -v`
Expected: pass.

- [ ] **Step 3: Commit**

```bash
git add src/cardinal_cfn/cleanup_script.py
git commit -m "cleanup: steps 4-6 -- RDS, RDS subnet group, SQS with ownership gate"
```

### Task 10: Add secret + SSM delete steps

**Files:**

- Modify: `src/cardinal_cfn/cleanup_script.py`

- [ ] **Step 1: Append**

```
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
```

- [ ] **Step 2: Run lint**

Run: `.venv/bin/pytest tests/unit/test_cleanup_script_lint.py -v`
Expected: pass.

- [ ] **Step 3: Commit**

```bash
git add src/cardinal_cfn/cleanup_script.py
git commit -m "cleanup: steps 7-8 -- secrets and SSM with ownership gate"
```

### Task 11: Add self-delete and exit logic

**Files:**

- Modify: `src/cardinal_cfn/cleanup_script.py`

- [ ] **Step 1: Append**

```
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
```

- [ ] **Step 2: Run lint**

Run: `.venv/bin/pytest tests/unit/test_cleanup_script_lint.py -v`
Expected: pass.

- [ ] **Step 3: Commit**

```bash
git add src/cardinal_cfn/cleanup_script.py
git commit -m "cleanup: step 9 -- self-delete cleanup stack with deployer role"
```

---

## Phase 2 — Troposphere generator and template tests

### Task 12: Write a failing template test

**Files:**

- Create: `tests/templates/test_cardinal_cleanup.py`

- [ ] **Step 1: Write the test**

```python
"""Cloud-radar assertions on cardinal-cleanup.yaml."""

from pathlib import Path

import pytest
from cloud_radar.cf.unit import Template


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
GENERATED = REPO_ROOT / "generated-templates" / "cardinal-cleanup.yaml"


@pytest.fixture(scope="module")
def template():
    if not GENERATED.exists():
        pytest.skip("run `make build` first")
    return Template.from_yaml(str(GENERATED), {})


def test_has_log_group_with_stack_name_suffix(template):
    rendered = template.render({
        "LakerunnerStackName":     "cardinal-lakerunner",
        "CleanupTaskRoleArn":      "arn:aws:iam::111:role/task",
        "CleanupExecutionRoleArn": "arn:aws:iam::111:role/exec",
        "ClusterName":             "the-cluster",
        "DeployerRoleArn":         "arn:aws:iam::111:role/deployer",
    })
    lg = rendered.get_resource("CleanupLogGroup")
    assert lg["Type"] == "AWS::Logs::LogGroup"
    assert lg["Properties"]["RetentionInDays"] == 7
    assert lg["DeletionPolicy"] == "Delete"
    # The name must include the stack name suffix; under cloud-radar the
    # stack name defaults to "cf" but the !Sub uses ${AWS::StackName}.
    assert lg["Properties"]["LogGroupName"].startswith("/aws/ecs/cardinal-cleanup/")


def test_task_definition_shape(template):
    rendered = template.render({
        "LakerunnerStackName":     "cardinal-lakerunner",
        "CleanupTaskRoleArn":      "arn:aws:iam::111:role/task",
        "CleanupExecutionRoleArn": "arn:aws:iam::111:role/exec",
        "ClusterName":             "the-cluster",
        "DeployerRoleArn":         "arn:aws:iam::111:role/deployer",
    })
    td = rendered.get_resource("CleanupTaskDefinition")
    props = td["Properties"]
    assert props["Family"] == "cardinal-cleanup"
    assert props["RequiresCompatibilities"] == ["FARGATE"]
    assert props["NetworkMode"] == "awsvpc"
    assert props["Cpu"] == "512"
    assert props["Memory"] == "1024"
    assert props["TaskRoleArn"] == "arn:aws:iam::111:role/task"
    assert props["ExecutionRoleArn"] == "arn:aws:iam::111:role/exec"

    containers = props["ContainerDefinitions"]
    assert len(containers) == 1
    c = containers[0]
    assert c["Name"] == "cleanup"
    assert c["Image"] == "public.ecr.aws/aws-cli/aws-cli:latest"
    assert c["Essential"] is True
    # Script lives in EntryPoint, NOT Command -- prevents RunTask command override.
    assert c["EntryPoint"][:2] == ["/bin/sh", "-c"]
    assert "ownership_ok()" in c["EntryPoint"][2]
    assert c["Command"] == []


def test_task_environment_contains_required_vars(template):
    rendered = template.render({
        "LakerunnerStackName":     "cardinal-lakerunner",
        "CleanupTaskRoleArn":      "arn:aws:iam::111:role/task",
        "CleanupExecutionRoleArn": "arn:aws:iam::111:role/exec",
        "ClusterName":             "the-cluster",
        "DeployerRoleArn":         "arn:aws:iam::111:role/deployer",
    })
    env_pairs = {
        e["Name"]: e["Value"]
        for e in rendered.get_resource("CleanupTaskDefinition")["Properties"]
                          ["ContainerDefinitions"][0]["Environment"]
    }
    assert env_pairs["CLUSTER_NAME"]          == "the-cluster"
    assert env_pairs["LAKERUNNER_STACK_NAME"] == "cardinal-lakerunner"
    assert env_pairs["DEPLOYER_ROLE_ARN"]     == "arn:aws:iam::111:role/deployer"
    # The remaining envs use CFN intrinsics; their resolved values come from
    # cloud-radar's pseudo-parameter substitution.
    assert "CLEANUP_STACK_NAME" in env_pairs
    assert "AWS_REGION" in env_pairs
    assert "AWS_ACCOUNT_ID" in env_pairs


def test_outputs(template):
    rendered = template.render({
        "LakerunnerStackName":     "cardinal-lakerunner",
        "CleanupTaskRoleArn":      "arn:aws:iam::111:role/task",
        "CleanupExecutionRoleArn": "arn:aws:iam::111:role/exec",
        "ClusterName":             "the-cluster",
        "DeployerRoleArn":         "arn:aws:iam::111:role/deployer",
    })
    outputs = rendered.template["Outputs"]
    assert "TaskDefinitionArn" in outputs
    assert "LogGroupName" in outputs


def test_no_iam_resources(template):
    """The cleanup stack must not create any IAM. Roles are customer-supplied."""
    rendered = template.render({
        "LakerunnerStackName":     "cardinal-lakerunner",
        "CleanupTaskRoleArn":      "arn:aws:iam::111:role/task",
        "CleanupExecutionRoleArn": "arn:aws:iam::111:role/exec",
        "ClusterName":             "the-cluster",
        "DeployerRoleArn":         "arn:aws:iam::111:role/deployer",
    })
    for name, resource in rendered.template["Resources"].items():
        assert not resource["Type"].startswith("AWS::IAM::"), (
            f"resource {name} of type {resource['Type']} violates 'no IAM' rule"
        )
```

- [ ] **Step 2: Run the test (expect skip because the template doesn't exist yet)**

Run: `.venv/bin/pytest tests/templates/test_cardinal_cleanup.py -v`
Expected: 5 skipped (with reason "run `make build` first").

- [ ] **Step 3: Commit**

```bash
git add tests/templates/test_cardinal_cleanup.py
git commit -m "cleanup: failing template tests (skipped until generator exists)"
```

### Task 13: Create the troposphere generator

**Files:**

- Create: `src/cardinal_cfn/cardinal_cleanup.py`

- [ ] **Step 1: Write the generator**

```python
"""cardinal-cleanup: stand-alone CFN root template for end-to-end teardown.

Companion to cardinal-lakerunner.yaml. Holds a single Fargate task
definition whose container runs the inline POSIX-sh teardown body from
``cardinal_cfn.cleanup_script.SCRIPT``. The script lives in EntryPoint
(not Command) so an operator with ecs:RunTask cannot substitute their
own command via containerOverrides.

See ``docs/superpowers/specs/2026-05-27-cleanup-stack-design.md``.
"""

import sys

from troposphere import (
    GetAtt,
    Output,
    Parameter,
    Ref,
    Sub,
    Template,
)
from troposphere.ecs import (
    ContainerDefinition,
    Environment,
    LogConfiguration,
    TaskDefinition,
)
from troposphere.logs import LogGroup

from cardinal_cfn.cleanup_script import SCRIPT


def build_template() -> Template:
    t = Template(
        Description=(
            "Cardinal cleanup task. Wipes a cardinal-lakerunner install and "
            "self-deletes. Operator-launched via ecs:RunTask after the "
            "stack reaches CREATE_COMPLETE."
        ),
    )

    p_lakerunner = t.add_parameter(Parameter(
        "LakerunnerStackName",
        Type="String",
        Default="cardinal-lakerunner",
        Description="The cardinal-lakerunner CFN stack name to tear down.",
    ))
    p_task_role = t.add_parameter(Parameter(
        "CleanupTaskRoleArn",
        Type="String",
        Description=(
            "Privileged IAM role the cleanup task assumes. See operator runbook "
            "for the required policy."
        ),
    ))
    p_exec_role = t.add_parameter(Parameter(
        "CleanupExecutionRoleArn",
        Type="String",
        Description=(
            "Standard Fargate execution role (ECR pull + log writes). "
            "May be the same role as CleanupTaskRoleArn."
        ),
    ))
    p_cluster = t.add_parameter(Parameter(
        "ClusterName",
        Type="String",
        Description="ECS cluster the cleanup task is launched into.",
    ))
    p_deployer = t.add_parameter(Parameter(
        "DeployerRoleArn",
        Type="String",
        Description=(
            "CFN service role (cardinal-cfn-deployer). The in-task delete-stack "
            "calls pass this as --role-arn so the task role itself does not "
            "need stack-mechanics verbs."
        ),
    ))

    log_group = t.add_resource(LogGroup(
        "CleanupLogGroup",
        LogGroupName=Sub("/aws/ecs/cardinal-cleanup/${AWS::StackName}"),
        RetentionInDays=7,
        DeletionPolicy="Delete",
        UpdateReplacePolicy="Delete",
    ))

    task_def = t.add_resource(TaskDefinition(
        "CleanupTaskDefinition",
        Family="cardinal-cleanup",
        RequiresCompatibilities=["FARGATE"],
        NetworkMode="awsvpc",
        Cpu="512",
        Memory="1024",
        TaskRoleArn=Ref(p_task_role),
        ExecutionRoleArn=Ref(p_exec_role),
        ContainerDefinitions=[ContainerDefinition(
            Name="cleanup",
            Image="public.ecr.aws/aws-cli/aws-cli:latest",
            Essential=True,
            # Script is in EntryPoint so ecs:RunTask containerOverrides.command
            # cannot bypass it (containerOverrides has no entryPoint override).
            EntryPoint=["/bin/sh", "-c", SCRIPT],
            Command=[],
            Environment=[
                Environment(Name="AWS_REGION",            Value=Ref("AWS::Region")),
                Environment(Name="AWS_ACCOUNT_ID",        Value=Ref("AWS::AccountId")),
                Environment(Name="CLUSTER_NAME",          Value=Ref(p_cluster)),
                Environment(Name="LAKERUNNER_STACK_NAME", Value=Ref(p_lakerunner)),
                Environment(Name="CLEANUP_STACK_NAME",    Value=Ref("AWS::StackName")),
                Environment(Name="DEPLOYER_ROLE_ARN",     Value=Ref(p_deployer)),
            ],
            LogConfiguration=LogConfiguration(
                LogDriver="awslogs",
                Options={
                    "awslogs-group":         Ref(log_group),
                    "awslogs-region":        Ref("AWS::Region"),
                    "awslogs-stream-prefix": "cleanup",
                },
            ),
        )],
    ))

    t.add_output(Output(
        "TaskDefinitionArn",
        Value=Ref(task_def),
        Description="ARN the driver passes to ecs:RunTask.",
    ))
    t.add_output(Output(
        "LogGroupName",
        Value=Ref(log_group),
        Description="Log group the driver tails for cleanup-task output.",
    ))

    return t


def main() -> None:
    sys.stdout.write(build_template().to_yaml())


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Emit the template manually and run tests**

Run:

```bash
mkdir -p generated-templates
PYTHONPATH=src python3 -m cardinal_cfn.cardinal_cleanup > generated-templates/cardinal-cleanup.yaml
.venv/bin/pytest tests/templates/test_cardinal_cleanup.py -v
```

Expected: 5 passed.

- [ ] **Step 3: Commit**

```bash
git add src/cardinal_cfn/cardinal_cleanup.py
git commit -m "cleanup: troposphere generator for cardinal-cleanup.yaml"
```

### Task 14: Generated-template size assertion

**Files:**

- Create: `tests/unit/test_cardinal_cleanup_module.py`

- [ ] **Step 1: Write the tests**

```python
"""Generator-module sanity tests for cardinal_cleanup."""

from pathlib import Path

import pytest

from cardinal_cfn import cardinal_cleanup
from cardinal_cfn.cleanup_script import SCRIPT


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
GENERATED = REPO_ROOT / "generated-templates" / "cardinal-cleanup.yaml"


def test_template_builds():
    """The generator must produce a valid troposphere Template."""
    t = cardinal_cleanup.build_template()
    yaml_text = t.to_yaml()
    assert "AWS::ECS::TaskDefinition" in yaml_text
    assert "AWS::Logs::LogGroup" in yaml_text
    # The full shell body is embedded into the YAML literally; check a few
    # distinctive markers from each phase survive serialization.
    assert "ownership_ok()" in yaml_text
    assert "drain_services" in yaml_text
    assert "self_delete" in yaml_text


def test_template_under_size_limit():
    """CFN's CreateStack template size limit is 1 MiB; warn at 800 KiB."""
    t = cardinal_cleanup.build_template()
    yaml_size = len(t.to_yaml().encode("utf-8"))
    assert yaml_size < 800_000, f"template is {yaml_size} bytes; close to CFN 1 MiB limit"


def test_no_iam_resources_in_template():
    t = cardinal_cleanup.build_template()
    for name, resource in t.resources.items():
        assert not resource.resource_type.startswith("AWS::IAM::"), (
            f"resource {name} of type {resource.resource_type} violates 'no IAM' rule"
        )


def test_generated_file_matches_module():
    """`make build` is the source of truth; if the file drifts, regenerate."""
    if not GENERATED.exists():
        pytest.skip("run `make build` first")
    rebuilt = cardinal_cleanup.build_template().to_yaml()
    on_disk = GENERATED.read_text()
    assert rebuilt == on_disk, (
        "generated-templates/cardinal-cleanup.yaml is stale; "
        "run `make build` to regenerate."
    )


def test_script_is_referenced():
    """Sanity: the generator must actually embed SCRIPT, not a placeholder."""
    yaml_text = cardinal_cleanup.build_template().to_yaml()
    assert SCRIPT.strip().splitlines()[0] in yaml_text
```

- [ ] **Step 2: Run the tests**

Run: `.venv/bin/pytest tests/unit/test_cardinal_cleanup_module.py -v`
Expected: 5 passed.

- [ ] **Step 3: Commit**

```bash
git add tests/unit/test_cardinal_cleanup_module.py
git commit -m "cleanup: generator-module sanity + size + freshness tests"
```

---

## Phase 3 — Build and lint integration

### Task 15: Wire cardinal-cleanup into build.sh

**Files:**

- Modify: `build.sh:28-29` (after `cardinal-infrastructure` block, before `cardinal-lakerunner`).
- Modify: `build.sh:53-57` (cfn-lint argument list).

- [ ] **Step 1: Read the current build.sh to confirm line numbers**

Run: `grep -n cardinal- build.sh`
Expected: lines that include `cardinal_infrastructure`, `cardinal-lakerunner`, the `for child in` loop, and the cfn-lint invocation.

- [ ] **Step 2: Insert the cardinal-cleanup generation step after cardinal-infrastructure**

Edit `build.sh`. After the line:

```sh
python3 -m cardinal_cfn.cardinal_infrastructure > generated-templates/cardinal-infrastructure.yaml
```

Insert:

```sh

echo "Generating cardinal-cleanup.yaml..."
python3 -m cardinal_cfn.cardinal_cleanup > generated-templates/cardinal-cleanup.yaml
```

- [ ] **Step 3: Add cardinal-cleanup.yaml to the cfn-lint invocation**

In the cfn-lint call near the end of `build.sh`, after `generated-templates/cardinal-infrastructure.yaml \` add:

```
         generated-templates/cardinal-cleanup.yaml \
```

- [ ] **Step 4: Run `make build` and verify**

Run: `make build`
Expected: prints `Generating cardinal-cleanup.yaml...`, `cfn-lint` runs with no errors, `generated-templates/cardinal-cleanup.yaml` exists.

- [ ] **Step 5: Commit**

```bash
git add build.sh
git commit -m "cleanup: wire cardinal-cleanup.yaml into build.sh"
```

### Task 16: Wire cardinal-cleanup into Makefile lint

**Files:**

- Modify: `Makefile:37-43` (`lint` target).

- [ ] **Step 1: Add the new template to the lint invocation**

Edit the `lint:` target so the cfn-lint command lists `generated-templates/cardinal-cleanup.yaml` after `generated-templates/cardinal-infrastructure.yaml`:

```makefile
lint:	## Run cfn-lint on every generated template
	source $(VENV_DIR)/bin/activate && cfn-lint \
	  generated-templates/cardinal-vpc.yaml \
	  generated-templates/cardinal-alb-sg.yaml \
	  generated-templates/cardinal-infrastructure.yaml \
	  generated-templates/cardinal-cleanup.yaml \
	  generated-templates/cardinal-lakerunner.yaml \
	  generated-templates/cardinal-lakerunner/*.yaml
```

- [ ] **Step 2: Run `make lint` and verify**

Run: `make lint`
Expected: cfn-lint completes with no errors on cardinal-cleanup.yaml.

- [ ] **Step 3: Commit**

```bash
git add Makefile
git commit -m "cleanup: add cardinal-cleanup.yaml to Makefile lint target"
```

---

## Phase 4 — Driver script and tests

### Task 17: Driver skeleton with usage and arg parsing

**Files:**

- Create: `scripts/cleanup-lakerunner.sh`

- [ ] **Step 1: Write the skeleton**

```sh
#!/bin/sh
# Tear down a cardinal-lakerunner install end-to-end via a privileged ECS
# Fargate task. Companion to scripts/deploy-lakerunner.sh.
#
# Self-contained: POSIX shell + AWS CLI v2 + jq. No Python.
#
# The script creates a cardinal-cleanup CFN stack (which delivers a vetted
# task definition), launches the cleanup task into the customer's ECS
# cluster, tails the task's logs, and exits with the task's exit code.
# The task itself does the heavy lifting: drain ECS services, delete the
# cardinal-lakerunner stack, wipe the cardinal-* data layer (S3, RDS, SQS,
# secrets, SSM) with ownership-tag enforcement, and self-delete the cleanup
# stack.
#
# See docs/operations/cleanup.md for the runbook and docs/superpowers/specs/
# 2026-05-27-cleanup-stack-design.md for the design.

set -eu

DEFAULT_TEMPLATE_BASE_URL="https://cardinal-cfn-us-east-1.s3.us-east-1.amazonaws.com/lakerunner"

# --- required ---
region=""
version=""
cluster_name=""
private_subnets=""
task_sg_id=""
cleanup_task_role_arn=""
cleanup_execution_role_arn=""
deployer_role_arn=""
yes="false"

# --- optional ---
lakerunner_stack_name="cardinal-lakerunner"
cleanup_stack_name="cardinal-cleanup"
template_base_url="$DEFAULT_TEMPLATE_BASE_URL"
wait_self_delete="false"

# --- internal test hooks (pure data transforms; no AWS) ---
internal_plan_text=""

usage() {
    cat <<'EOF'
Usage: cleanup-lakerunner.sh [options]

Required:
  --region REGION                       AWS region.
  --version VERSION                     Published template tag, e.g. v0.0.46.
  --cluster-name NAME                   Customer's ECS cluster.
  --private-subnets CSV                 Subnets for the cleanup task ENI.
  --task-sg-id SG_ID                    Security group for the cleanup task ENI.
  --cleanup-task-role-arn ARN           Privileged task role.
  --cleanup-execution-role-arn ARN      Fargate execution role.
  --deployer-role-arn ARN               CFN service role (cardinal-cfn-deployer).
  --yes                                 Confirm destructive operation.

Optional:
  --lakerunner-stack-name NAME          Default: cardinal-lakerunner.
  --cleanup-stack-name NAME             Default: cardinal-cleanup.
  --template-base-url URL               Default: cardinal-cfn-us-east-1 bucket.
  --wait-self-delete                    Wait for the cleanup stack's own
                                        delete-complete (off by default).

Exit codes:
  0  task succeeded
  1  task failed, ownership-tag skip occurred, or self-delete wait failed
  2  pre-flight / input validation failure
EOF
}

log()  { printf '[cleanup-lakerunner] %s\n' "$*" >&2; }
fail() { code="$1"; shift; log "$*"; exit "$code"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --region)                       region="$2";                       shift 2 ;;
        --version)                      version="$2";                      shift 2 ;;
        --cluster-name)                 cluster_name="$2";                 shift 2 ;;
        --private-subnets)              private_subnets="$2";              shift 2 ;;
        --task-sg-id)                   task_sg_id="$2";                   shift 2 ;;
        --cleanup-task-role-arn)        cleanup_task_role_arn="$2";        shift 2 ;;
        --cleanup-execution-role-arn)   cleanup_execution_role_arn="$2";   shift 2 ;;
        --deployer-role-arn)            deployer_role_arn="$2";            shift 2 ;;
        --lakerunner-stack-name)        lakerunner_stack_name="$2";        shift 2 ;;
        --cleanup-stack-name)           cleanup_stack_name="$2";           shift 2 ;;
        --template-base-url)            template_base_url="$2";            shift 2 ;;
        --wait-self-delete)             wait_self_delete="true";           shift   ;;
        --yes)                          yes="true";                        shift   ;;
        --internal-plan-text)           internal_plan_text="$2";           shift 2 ;;
        -h|--help)                      usage; exit 0 ;;
        *)                              usage; fail 2 "unknown argument: $1" ;;
    esac
done

# Pure data transform: emit the human-readable plan from environment-supplied
# JSON. Used by --internal-plan-text test hook.
if [ -n "$internal_plan_text" ]; then
    python3 -c "
import json, sys
p = json.loads('''$internal_plan_text''')
print('Plan:')
print('  region:        ' + p['region'])
print('  cluster:       ' + p['cluster'])
print('  lakerunner:    delete CFN stack ' + p['lakerunner_stack'])
print('  data layer:    wipe (with ownership-tag enforcement)')
print('  cleanup stack: self-delete ' + p['cleanup_stack'])
"
    exit 0
fi

for req in region version cluster_name private_subnets task_sg_id \
           cleanup_task_role_arn cleanup_execution_role_arn deployer_role_arn; do
    eval "val=\$$req"
    [ -z "$val" ] && fail 2 "missing required --${req//_/-}"
done

if [ "$yes" != "true" ]; then
    cat <<EOF >&2
This will tear down a Cardinal install in the following AWS account/region:
  region:        $region
  cluster:       $cluster_name
  lakerunner:    delete CFN stack $lakerunner_stack_name
  data layer:    drain + delete S3 ingest, RDS, SQS, secrets, SSM
                 (only resources tagged Application=cardinal-lakerunner,
                  ManagedBy=cardinal-data-setup-script; others are skipped)
  cleanup stack: $cleanup_stack_name (created and self-deleted)

Re-run with --yes to proceed.
EOF
    exit 2
fi

# Stages below filled in by subsequent tasks.
log "TODO: pre-delete stranded stack, create-stack, run-task, tail, exit"
exit 1
```

- [ ] **Step 2: Make it executable and sanity-check**

Run:

```bash
chmod +x scripts/cleanup-lakerunner.sh
scripts/cleanup-lakerunner.sh --help
sh -n scripts/cleanup-lakerunner.sh
```

Expected: usage text printed, `sh -n` exits 0.

- [ ] **Step 3: Commit**

```bash
git add scripts/cleanup-lakerunner.sh
git commit -m "cleanup: driver skeleton with arg parsing and usage"
```

### Task 18: Add lint test for the driver

**Files:**

- Create: `tests/unit/test_cleanup_lakerunner_lint.py`

- [ ] **Step 1: Write the test**

```python
"""Lint the cleanup-lakerunner.sh driver script."""

import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "cleanup-lakerunner.sh"


def test_script_exists_and_is_executable():
    assert SCRIPT.exists()
    assert SCRIPT.stat().st_mode & 0o111


def test_sh_n_passes():
    result = subprocess.run(["sh", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, f"sh -n failed: {result.stderr}"


def test_shellcheck_passes():
    if shutil.which("shellcheck") is None:
        pytest.skip("shellcheck not installed on this runner")
    result = subprocess.run(
        ["shellcheck", "-s", "sh", "-S", "warning", str(SCRIPT)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"shellcheck failed:\n{result.stdout}\n{result.stderr}"
    )
```

- [ ] **Step 2: Run**

Run: `.venv/bin/pytest tests/unit/test_cleanup_lakerunner_lint.py -v`
Expected: 3 passed (or 2 + 1 skipped if no shellcheck).

- [ ] **Step 3: Commit**

```bash
git add tests/unit/test_cleanup_lakerunner_lint.py
git commit -m "cleanup: lint the driver script"
```

### Task 19: Driver flow — stranded-stack pre-delete and create-stack

**Files:**

- Modify: `scripts/cleanup-lakerunner.sh` (replace the `TODO` block).

- [ ] **Step 1: Replace the TODO block with the create flow**

Replace the last block (`log "TODO: pre-delete stranded stack..."` and `exit 1`) with:

```sh
# ---------------------------------------------------------------------------
# Stage 1: handle stranded cleanup stack from a prior aborted run.
# ---------------------------------------------------------------------------
existing_status=$(aws --region "$region" cloudformation describe-stacks \
    --stack-name "$cleanup_stack_name" \
    --query 'Stacks[0].StackStatus' --output text 2>/dev/null \
    || echo "DOES_NOT_EXIST")
if [ "$existing_status" != "DOES_NOT_EXIST" ]; then
    log "found existing $cleanup_stack_name (status: $existing_status); deleting first"
    aws --region "$region" cloudformation delete-stack \
        --stack-name "$cleanup_stack_name" \
        --role-arn "$deployer_role_arn"
    aws --region "$region" cloudformation wait stack-delete-complete \
        --stack-name "$cleanup_stack_name" \
        || fail 1 "stranded $cleanup_stack_name delete failed"
fi

# ---------------------------------------------------------------------------
# Stage 2: create the cleanup stack with --role-arn $deployer_role_arn.
# ---------------------------------------------------------------------------
template_url="${template_base_url}/${version}/cardinal-cleanup.yaml"
log "creating $cleanup_stack_name from $template_url"
aws --region "$region" cloudformation create-stack \
    --stack-name "$cleanup_stack_name" \
    --template-url "$template_url" \
    --role-arn "$deployer_role_arn" \
    --capabilities CAPABILITY_IAM \
    --parameters \
        ParameterKey=LakerunnerStackName,ParameterValue="$lakerunner_stack_name" \
        ParameterKey=CleanupTaskRoleArn,ParameterValue="$cleanup_task_role_arn" \
        ParameterKey=CleanupExecutionRoleArn,ParameterValue="$cleanup_execution_role_arn" \
        ParameterKey=ClusterName,ParameterValue="$cluster_name" \
        ParameterKey=DeployerRoleArn,ParameterValue="$deployer_role_arn" \
    >/dev/null
aws --region "$region" cloudformation wait stack-create-complete \
    --stack-name "$cleanup_stack_name" \
    || fail 1 "$cleanup_stack_name create failed; check stack events"

td_arn=$(aws --region "$region" cloudformation describe-stacks \
    --stack-name "$cleanup_stack_name" \
    --query "Stacks[0].Outputs[?OutputKey=='TaskDefinitionArn'].OutputValue" \
    --output text)
log_group=$(aws --region "$region" cloudformation describe-stacks \
    --stack-name "$cleanup_stack_name" \
    --query "Stacks[0].Outputs[?OutputKey=='LogGroupName'].OutputValue" \
    --output text)
[ -n "$td_arn" ]   || fail 1 "could not read TaskDefinitionArn output"
[ -n "$log_group" ] || fail 1 "could not read LogGroupName output"

log "task definition: $td_arn"
log "log group: $log_group"

# TODO: stage 3 (run-task), stage 4 (tail), stage 5 (exit code).
exit 1
```

- [ ] **Step 2: Lint**

Run: `.venv/bin/pytest tests/unit/test_cleanup_lakerunner_lint.py -v`
Expected: pass.

- [ ] **Step 3: Commit**

```bash
git add scripts/cleanup-lakerunner.sh
git commit -m "cleanup: driver stages 1-2 (pre-delete stranded + create-stack)"
```

### Task 20: Driver flow — run-task, tail, exit

**Files:**

- Modify: `scripts/cleanup-lakerunner.sh` (replace the `TODO` block at the end).

- [ ] **Step 1: Replace the closing `TODO` + `exit 1` with the rest of the flow**

```sh
# ---------------------------------------------------------------------------
# Stage 3: launch the cleanup task.
# ---------------------------------------------------------------------------
subnet_args=$(printf '%s' "$private_subnets" | sed 's/,/, /g')
network_config="awsvpcConfiguration={subnets=[$subnet_args],securityGroups=[$task_sg_id],assignPublicIp=DISABLED}"

task_arn=$(aws --region "$region" ecs run-task \
    --cluster "$cluster_name" \
    --launch-type FARGATE \
    --task-definition "$td_arn" \
    --network-configuration "$network_config" \
    --query 'tasks[0].taskArn' --output text)
[ -z "$task_arn" ] || [ "$task_arn" = "None" ] \
    && fail 1 "ecs:RunTask returned no taskArn"
task_id="${task_arn##*/}"
log "task: $task_arn"

# ---------------------------------------------------------------------------
# Stage 4: wait for RUNNING, then tail logs until STOPPED.
# ---------------------------------------------------------------------------
log "waiting for task to reach RUNNING"
i=0
while [ $i -lt 60 ]; do
    s=$(aws --region "$region" ecs describe-tasks --cluster "$cluster_name" \
        --tasks "$task_arn" --query 'tasks[0].lastStatus' --output text 2>/dev/null \
        || echo PENDING)
    [ "$s" = "RUNNING" ] && { log "task RUNNING"; break; }
    if [ "$s" = "STOPPED" ]; then
        log "task stopped during startup:"
        aws --region "$region" ecs describe-tasks --cluster "$cluster_name" \
            --tasks "$task_arn" \
            --query 'tasks[0].{stopCode:stopCode,stoppedReason:stoppedReason,containers:containers[*].{name:name,exitCode:exitCode,reason:reason}}' \
            --output json >&2
        exit 1
    fi
    sleep 4
    i=$((i+1))
done

log "tailing logs from $log_group/cleanup/cleanup/$task_id"
stream="cleanup/cleanup/$task_id"
next=""
exit_code=""
i=0
while [ $i -lt 240 ]; do
    if [ -n "$next" ]; then
        out=$(aws --region "$region" logs get-log-events \
            --log-group-name "$log_group" --log-stream-name "$stream" \
            --start-from-head --next-token "$next" --output json 2>/dev/null \
            || echo '{}')
    else
        out=$(aws --region "$region" logs get-log-events \
            --log-group-name "$log_group" --log-stream-name "$stream" \
            --start-from-head --output json 2>/dev/null || echo '{}')
    fi
    printf '%s' "$out" | jq -r '.events[]?.message' 2>/dev/null || true
    new_next=$(printf '%s' "$out" | jq -r '.nextForwardToken // empty' 2>/dev/null)
    [ -n "$new_next" ] && next="$new_next"
    s=$(aws --region "$region" ecs describe-tasks --cluster "$cluster_name" \
        --tasks "$task_arn" --query 'tasks[0].lastStatus' --output text 2>/dev/null \
        || echo RUNNING)
    if [ "$s" = "STOPPED" ]; then
        exit_code=$(aws --region "$region" ecs describe-tasks --cluster "$cluster_name" \
            --tasks "$task_arn" \
            --query 'tasks[0].containers[0].exitCode' --output text)
        break
    fi
    sleep 5
    i=$((i+1))
done

if [ -z "$exit_code" ] || [ "$exit_code" = "None" ]; then
    log "WARNING: task still RUNNING after tail deadline; treating as failure"
    exit_code=1
fi
log "task exit code: $exit_code"

# ---------------------------------------------------------------------------
# Stage 5: optionally wait for the self-delete to finish.
# ---------------------------------------------------------------------------
if [ "$wait_self_delete" = "true" ]; then
    log "waiting for $cleanup_stack_name stack-delete-complete"
    if ! aws --region "$region" cloudformation wait stack-delete-complete \
            --stack-name "$cleanup_stack_name"; then
        log "WARNING: self-delete wait failed; investigate the stranded stack"
        exit 1
    fi
fi

exit "$exit_code"
```

- [ ] **Step 2: Lint**

Run: `.venv/bin/pytest tests/unit/test_cleanup_lakerunner_lint.py -v`
Expected: pass.

- [ ] **Step 3: Commit**

```bash
git add scripts/cleanup-lakerunner.sh
git commit -m "cleanup: driver stages 3-5 (run-task, tail logs, exit)"
```

### Task 21: Internal-hook tests for the driver's plan text and arg parsing

**Files:**

- Create: `tests/unit/test_cleanup_lakerunner_sh.py`

- [ ] **Step 1: Write the test file**

```python
"""Driver internal-hook tests (pure data transforms; no AWS calls)."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "cleanup-lakerunner.sh"


def _require_python3():
    if shutil.which("python3") is None:
        pytest.skip("python3 not on PATH")


def _run(*args):
    return subprocess.run(["sh", str(SCRIPT), *args], capture_output=True, text=True)


def test_help_exits_zero():
    result = _run("--help")
    assert result.returncode == 0
    assert "Usage: cleanup-lakerunner.sh" in result.stdout


def test_missing_args_exits_2():
    result = _run("--region", "us-east-1", "--yes")
    assert result.returncode == 2
    assert "missing required" in result.stderr.lower()


def test_no_yes_prints_plan_and_exits_2():
    result = _run(
        "--region", "us-east-1",
        "--version", "v0.0.46",
        "--cluster-name", "the-cluster",
        "--private-subnets", "subnet-aaa,subnet-bbb",
        "--task-sg-id", "sg-ccc",
        "--cleanup-task-role-arn", "arn:aws:iam::1:role/task",
        "--cleanup-execution-role-arn", "arn:aws:iam::1:role/exec",
        "--deployer-role-arn", "arn:aws:iam::1:role/dep",
    )
    assert result.returncode == 2
    assert "Re-run with --yes" in result.stderr
    assert "the-cluster" in result.stderr
    assert "cardinal-lakerunner" in result.stderr
    assert "cardinal-cleanup" in result.stderr


def test_unknown_arg_exits_2():
    result = _run("--bogus", "value")
    assert result.returncode == 2
    assert "unknown argument" in result.stderr.lower()


def test_internal_plan_text_pure_transform():
    _require_python3()
    payload = json.dumps({
        "region": "us-east-2",
        "cluster": "prod-cluster",
        "lakerunner_stack": "cardinal-lakerunner",
        "cleanup_stack": "cardinal-cleanup",
    })
    result = _run("--internal-plan-text", payload)
    assert result.returncode == 0
    assert "us-east-2" in result.stdout
    assert "prod-cluster" in result.stdout
    assert "cardinal-lakerunner" in result.stdout
    assert "cardinal-cleanup" in result.stdout
```

- [ ] **Step 2: Run the tests**

Run: `.venv/bin/pytest tests/unit/test_cleanup_lakerunner_sh.py -v`
Expected: 5 passed.

- [ ] **Step 3: Commit**

```bash
git add tests/unit/test_cleanup_lakerunner_sh.py
git commit -m "cleanup: internal-hook tests for driver plan text and arg parsing"
```

---

## Phase 5 — Operator runbook

### Task 22: Write docs/operations/cleanup.md

**Files:**

- Create: `docs/operations/cleanup.md`

- [ ] **Step 1: Write the runbook**

```markdown
# Tearing down a Cardinal install (cleanup-lakerunner.sh)

This document is for operators (Jenkins, or an authorized human) tearing
down a full Cardinal install end-to-end. The procedure deletes the
`cardinal-lakerunner` CloudFormation stack and the `cardinal-*` data
resources that `scripts/data-setup.sh` provisioned, but **only those**:
the customer's ECS cluster, Cloud Map namespace, IAM roles, security
groups, subnets, and VPC are not touched.

For partial cleanup (e.g. leaving the lakerunner stack in place, or only
wiping the data layer), this is the wrong tool. Use the AWS CLI directly.

## When to use this

- Full uninstall of a Cardinal sandbox / pre-prod environment.
- After a botched install, when the operator role can deploy stacks but
  can't delete data-bearing resources.

## When NOT to use this

- Production. There is no undo. Snapshots are explicitly disabled.
- Single-resource cleanup (e.g. just the RDS). Use `aws rds delete-db-instance` directly.

## Prerequisites

1. The `cardinal-cfn-deployer` CFN service role (or equivalent) — same
   role used by `scripts/deploy-lakerunner.sh`.
2. A privileged ECS task role (and execution role) that the cleanup task
   will assume. See "Required IAM policies" below for the exact statement.
3. The operator's own IAM role with the policies in the same section.
4. The customer's ECS cluster name, two private subnets, and a security
   group the task can use.
5. AWS CLI v2 and `jq` on the runner.

## Running

```sh
scripts/cleanup-lakerunner.sh \
    --region us-east-1 \
    --version v0.0.46 \
    --cluster-name <CLUSTER> \
    --private-subnets subnet-aaa,subnet-bbb \
    --task-sg-id sg-ccc \
    --cleanup-task-role-arn      arn:aws:iam::<ACCT>:role/cardinal-cleanup \
    --cleanup-execution-role-arn arn:aws:iam::<ACCT>:role/cardinal-cleanup-exec \
    --deployer-role-arn          arn:aws:iam::<ACCT>:role/cardinal-cfn-deployer \
    --yes
```

Without `--yes` the script prints the plan and exits with code 2 so
Jenkins can show the operator the blast radius before confirming.

## What happens

1. Driver creates `cardinal-cleanup` (`--role-arn cardinal-cfn-deployer`)
   from the published `cardinal-cleanup.yaml`. The stack provisions a
   `cardinal-cleanup` task definition and a log group.
2. Driver launches the task in the customer's cluster.
3. Inside the task:
   - Derive account from `sts:GetCallerIdentity` and region from the ECS
     task metadata endpoint (not from `AWS_REGION` / `AWS_ACCOUNT_ID` env).
   - Walk `cardinal-lakerunner` root + nested children via
     `cloudformation:ListStackResources` to find ECS services, set
     `DesiredCount=0`, stop running and pending tasks, wait up to 5
     minutes for `runningCount=0 AND pendingCount=0`.
   - `cloudformation:DeleteStack cardinal-lakerunner
     --role-arn $DEPLOYER_ROLE_ARN`, wait for delete-complete.
   - For each cardinal-* data resource (S3 ingest bucket, RDS instance,
     RDS subnet group, SQS queue, the three cardinal-* secrets, the two
     /cardinal/* SSM parameters): read tags via the resource's own API,
     **refuse to delete unless `Application=cardinal-lakerunner` AND
     `ManagedBy=cardinal-data-setup-script`** are both present.
     Resources tagged correctly are wiped; others are logged and skipped
     and the final exit code is 1.
   - Fire `cloudformation:DeleteStack cardinal-cleanup
     --role-arn $DEPLOYER_ROLE_ARN` asynchronously and exit. CFN handles
     the cleanup-stack teardown after the task ends.
4. Driver tails logs to its stdout; exits with the task's exit code.

## Exit codes

- `0` — task succeeded; cleanup stack self-delete in progress.
- `1` — task ran but failed (non-zero exit code), or ownership-tag skip
  occurred and the cleanup is partial, or self-delete wait failed.
- `2` — pre-flight failure: missing argument, missing `--yes`, etc.

## Recovery from a failed run

- `cardinal-cleanup` is left in CREATE_COMPLETE state if create succeeded
  but the task failed. Re-running the driver auto-deletes the stranded
  stack before creating a fresh one (no manual intervention required).
- If the cleanup task partially completed before failing, re-run the
  driver. Every data-layer step is idempotent (already-absent treated as
  success).
- If the ownership-tag check refused a delete, inspect the resource's
  actual tags with `aws <service> list-tags-for-resource ...`. If the
  resource is legitimately Cardinal-owned, manually apply the expected
  tags and re-run. If it isn't, leave it alone.

## Required IAM policies

### Operator role (Jenkins)

```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "cloudformation:CreateStack",
                "cloudformation:DescribeStacks",
                "cloudformation:DescribeStackEvents",
                "cloudformation:DeleteStack"
            ],
            "Resource": "arn:aws:cloudformation:*:*:stack/cardinal-cleanup/*",
            "Condition": {
                "StringEquals": {
                    "cloudformation:RoleArn": "<DeployerRoleArn>"
                },
                "StringLike": {
                    "cloudformation:TemplateUrl": [
                        "https://cardinal-cfn.s3.us-east-2.amazonaws.com/lakerunner/*/cardinal-cleanup.yaml",
                        "https://cardinal-cfn-us-east-1.s3.us-east-1.amazonaws.com/lakerunner/*/cardinal-cleanup.yaml"
                    ]
                }
            }
        },
        {
            "Effect": "Allow",
            "Action": "ecs:RunTask",
            "Resource": "arn:aws:ecs:<REGION>:<ACCOUNT>:task-definition/cardinal-cleanup:*",
            "Condition": {
                "ArnEquals":    { "ecs:cluster": "arn:aws:ecs:<REGION>:<ACCOUNT>:cluster/<CLUSTER>" },
                "StringEquals": { "ecs:enable-execute-command": "false" }
            }
        },
        {
            "Effect": "Allow",
            "Action": "ecs:DescribeTasks",
            "Resource": "arn:aws:ecs:<REGION>:<ACCOUNT>:task/<CLUSTER>/*",
            "Condition": {
                "ArnEquals": { "ecs:cluster": "arn:aws:ecs:<REGION>:<ACCOUNT>:cluster/<CLUSTER>" }
            }
        },
        {
            "Effect": "Allow",
            "Action": "iam:PassRole",
            "Resource": [
                "<CleanupTaskRoleArn>",
                "<CleanupExecutionRoleArn>"
            ],
            "Condition": {
                "StringEquals": { "iam:PassedToService": "ecs-tasks.amazonaws.com" }
            }
        },
        {
            "Effect": "Allow",
            "Action": "iam:PassRole",
            "Resource": "<DeployerRoleArn>",
            "Condition": {
                "StringEquals": { "iam:PassedToService": "cloudformation.amazonaws.com" }
            }
        },
        {
            "Effect": "Allow",
            "Action": [
                "logs:GetLogEvents"
            ],
            "Resource": "arn:aws:logs:*:*:log-group:/aws/ecs/cardinal-cleanup/*:log-stream:*"
        },
        {
            "Effect": "Allow",
            "Action": "logs:DescribeLogStreams",
            "Resource": "arn:aws:logs:*:*:log-group:/aws/ecs/cardinal-cleanup/*"
        }
    ]
}
```

Air-gapped customers replace the two `TemplateUrl` patterns with their
exact mirror URL. **Do not wildcard the bucket host** — any bucket
matching `cardinal-cfn-*` could be created by anyone.

### Cleanup task role

Trust policy must allow `ecs-tasks.amazonaws.com:AssumeRole`. The
permissions policy is documented in
`docs/superpowers/specs/2026-05-27-cleanup-stack-design.md` under
"Customer-supplied `CleanupTaskRoleArn` — required policy". All ARNs in
that policy are **account+region pinned** (no `*:*` wildcards) so an
operator who tries to redirect the task at another regional install via
environment overrides is blocked at IAM.

### Cleanup execution role

`AmazonECSTaskExecutionRolePolicy` plus:

```json
{
    "Effect": "Allow",
    "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
    "Resource": "arn:aws:logs:*:*:log-group:/aws/ecs/cardinal-cleanup/*:log-stream:*"
}
```

## Network requirements

The cleanup task runs in private subnets without public IPs. It must
reach the public ECR image (`public.ecr.aws/aws-cli/aws-cli:latest`) at
launch and several AWS APIs during the run.

- **Image pull from `public.ecr.aws`:** requires either NAT egress or a
  customer mirror of the image into private ECR. The standard `ecr-api`
  / `ecr-dkr` interface endpoints do not serve `public.ecr.aws`. Without
  NAT, the customer mirrors the image, rebuilds `cardinal-cleanup.yaml`
  with the mirrored URI (the image is intentionally not a stack
  parameter — see the spec), and points the operator role's
  `TemplateUrl` condition at the mirrored template.
- **AWS API endpoints** (via NAT or VPC interface endpoints):
  cloudformation, ecs, s3, rds, sqs, secretsmanager, ssm, logs, sts.
```

- [ ] **Step 2: Verify markdown renders**

Run: `cat docs/operations/cleanup.md | head -5` (visual sanity check).

- [ ] **Step 3: Commit**

```bash
git add docs/operations/cleanup.md
git commit -m "cleanup: operator runbook"
```

---

## Phase 6 — Final verification

### Task 23: Run the full test suite and cfn-lint

- [ ] **Step 1: Clean rebuild**

Run:

```bash
rm -rf generated-templates
make build
```

Expected: every generator emits its template; cfn-lint reports no errors.

- [ ] **Step 2: Full pytest**

Run: `make test`
Expected: every test passes (or skips with reason).

- [ ] **Step 3: Verify the spec/plan/code triple is consistent**

Run:

```bash
grep -c "ownership_ok" src/cardinal_cfn/cleanup_script.py generated-templates/cardinal-cleanup.yaml
grep -c "self_delete" src/cardinal_cfn/cleanup_script.py generated-templates/cardinal-cleanup.yaml
grep -c "EntryPoint" generated-templates/cardinal-cleanup.yaml
```

Expected: non-zero counts everywhere.

- [ ] **Step 4: Stage everything and commit a placeholder if needed**

If `make build` produced any incidental whitespace diff:

```bash
git status
# If clean: nothing to do.
# If dirty: review the diff, then
git add -p
git commit -m "cleanup: regenerate templates"
```

- [ ] **Step 5: Final spec/plan consistency check**

Open the spec and the plan side-by-side and confirm:

- Every spec section that requires an artifact has a task creating it
  (cleanup_script.py, cardinal_cleanup.py, cardinal-cleanup.yaml,
  cleanup-lakerunner.sh, cleanup.md, tests).
- The IAM placeholders in `cleanup.md` (`<DeployerRoleArn>`,
  `<CleanupTaskRoleArn>`, `<REGION>`, `<ACCOUNT>`, `<CLUSTER>`) match
  the spec's IAM section.
- The shell `SCRIPT` constant order matches the spec's "Inline shell —
  sequence and semantics" steps 1 through 9.

### Task 24: Manual end-to-end on sandbox

Out-of-scope for the automated plan; documented in the spec's "Testing"
and the runbook. Hold for a human operator with a disposable Cardinal
install in account 493746473138 / us-east-1 (or equivalent sandbox).
