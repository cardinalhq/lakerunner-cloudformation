# Cardinal CFN — prereqs split implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the current "two roots, twelve nested children" CFN distribution with two privileged-identity shell scripts (`cardinal-prereqs.sh`, `cardinal-data-setup.sh`) plus two flat CFN stacks (`cardinal-infra-app`, `cardinal-lakerunner`), per the spec at `docs/superpowers/specs/2026-05-06-cardinal-cfn-prereqs-split-design.md`.

**Architecture:** Out-of-CFN scripts (privileged identity, idempotent ensure-style) own all the resource types the deployer can't update or delete (IAM, SGs, RDS, S3 ingest, secrets, SSM). Two flat CFN stacks (deployer-managed) own everything else (cluster, ALB, target groups, log groups, cloud-map, ECS task defs/services, migration). Manual parameter copy-paste between stages, no `Fn::ImportValue` and no nested children. Single shared task role; no `InstallId`.

**Tech Stack:** Python + troposphere (template generation), POSIX shell + AWS CLI v2 + jq (operator scripts), pytest + cloud-radar + shellcheck (tests).

---

## Scope cuts (deliberate)

These items appear in the spec but are deferred from this PR. Each is small enough to add later without restructuring the layers above.

- **Cert-import Lambda** (`app/cert.py`, `cardinal-cert-lambda-role`, the PEM-import path on the app stack). Customers must pass `--certificate-arn` of an existing ACM cert. The PEM-import flag and the optional Lambda role are stubbed in the prereqs script (commented hooks) but not implemented end-to-end. Why: it's strictly optional, doubles the test surface for the cert path, and can be added in a focused follow-up without touching anything else.
- **`--verify` mode** on both setup scripts. Documented in the spec as a drift-check feature; not implemented in this PR. The implicit drift check on every re-run (exit 2 on mismatched config) covers the common case.
- **`delete-prereqs.sh` / `delete-data-setup.sh`** convenience scripts. Sandbox cleanup is a `aws iam delete-role` / `aws ec2 delete-security-group` / etc. one-liner that doesn't need a wrapper; documented in `installing.md` instead.
- **`install-parameters.md` auto-generated table.** Replaced by per-stack `*-params.example.json` files with `_note` fields per parameter. Lower-friction, same information.
- **Existing-install migration tooling.** No production installs of the prior layout exist; the spec's migration section is documented as a manual procedure if one ever shows up.

---

## File structure

### New files

```
src/cardinal_cfn/
    iam_policies.py                       # shared IAM policy-document builders
    prereqs/
        __init__.py
        roles.py                          # role/trust/policy data structures
        security_groups.py                # SG + ingress data structures
        render.py                         # generates cardinal-prereqs.sh
    data_setup/
        __init__.py
        rds.py                            # ensure_db_subnet_group, ensure_db_instance, ensure_db_master_secret
        storage.py                        # ensure_s3_bucket/lifecycle, ensure_sqs_queue/policy, ensure_s3_notification
        secrets.py                        # ensure_license, ensure_internal_keys, ensure_admin_key, ensure_maestro_db
        ssm.py                            # ensure_ssm_param
        render.py                         # generates cardinal-data-setup.sh
    app/
        __init__.py
        cluster.py                        # ECS cluster + Cloud Map namespace
        alb.py                            # ALB, listeners, target groups, listener rules
        logs.py                           # per-service log groups
        cloudmap.py                       # per-service Cloud Map service entries
        root.py                           # cardinal-infra-app root template
    lakerunner/
        __init__.py
        services_query.py
        services_process.py
        services_control.py
        otel.py
        maestro.py
        migration.py
        root.py                           # cardinal-lakerunner root template

scripts/
    deploy-cardinal-stack.sh
    teardown-cardinal-stack.sh

tests/unit/
    test_iam_policies.py
    test_naming_contract.py
    test_tagging.py
    test_prereqs_render.py
    test_data_setup_render.py

tests/templates/
    test_app_template.py
    test_lakerunner_template.py
    test_app_lakerunner_handoff.py        # parameter chain end-to-end check

tests/scripts/
    __init__.py
    test_prereqs_script.py                # shellcheck + golden + smoke
    test_data_setup_script.py
    test_deploy_script.py

docs/operations/
    installing.md                         # new customer runbook
```

### Files modified

- `src/cardinal_cfn/naming.py` — drop `InstallId` references, rename helpers, expose shared tag-set helper used by both shell-script generators and CFN templates.
- `Makefile` — new build targets, new lint glob.
- `build.sh` — generates the new artifact set (two scripts + two templates + the VPC).
- `.github/workflows/release.yml` — publish the new artifact set; drop nested-child publishing.
- `.cfnlintrc` — keep current ignores; ensure no install-id-derived rules linger.
- `pytest.ini` — ensure `tests/scripts/` is collected.
- `README.md`, `README-BUILDING.md` — point at the new layout.
- `docs/operations/permissions-infrastructure.md` — rewrite for the new deployer policy.
- `docs/operations/permissions-lakerunner.md` — rewrite for the single shared task role.
- `docs/operations/deploying.md` — rewrite to point at `installing.md`.
- `docs/operations/tearing-down.md` — rewrite for the new layered teardown.
- `docs/operations/end-to-end-test-plan.md` — rewrite for the new layout.

### Files deleted

- `src/cardinal_cfn/install_id.py`
- `src/cardinal_cfn/root.py` (replaced by `app/root.py` + `lakerunner/root.py`)
- `src/cardinal_cfn/children/` (entire directory; resources are reshuffled)
- `src/cardinal_cfn/cardinal_deployer.py` if present (deployer-role generator)
- `scripts/deploy-lakerunner.sh`
- `scripts/teardown-lakerunner.sh`
- `jenkins/Jenkinsfile.lakerunner` and the `jenkins/` directory if empty
- `docs/operations/jenkins-deploy.md`
- `tests/unit/test_install_id.py`
- `tests/unit/test_no_install_id_in_children.py`
- `tests/unit/test_cert_lambda.py`
- `tests/unit/test_jenkinsfile_lakerunner.py`
- `tests/unit/test_deploy_lakerunner.py`
- `tests/unit/test_deploy_lakerunner_lint.py`
- `tests/unit/test_teardown_lakerunner.py`
- `tests/unit/test_teardown_lakerunner_lint.py`
- `tests/unit/test_migration_lambda.py`
- `tests/unit/test_services_common.py`
- `tests/templates/test_alb.py`, `test_cardinal_deployer.py`, `test_cert.py`, `test_cluster.py`, `test_config.py`, `test_database.py`, `test_maestro.py`, `test_migration.py`, `test_otel.py`, `test_root_wiring.py`, `test_root.py`, `test_services_control.py`, `test_services_process.py`, `test_services_query.py`, `test_storage.py` — all replaced by `test_app_template.py` + `test_lakerunner_template.py` + `test_app_lakerunner_handoff.py`.

---

## Task 1 — Foundation: drop InstallId, unify tags, add iam_policies module

**Files:**

- Modify: `src/cardinal_cfn/naming.py`
- Create: `src/cardinal_cfn/iam_policies.py`
- Modify: `tests/unit/test_naming.py`
- Create: `tests/unit/test_iam_policies.py`
- Delete: `src/cardinal_cfn/install_id.py`, `tests/unit/test_install_id.py`, `tests/unit/test_no_install_id_in_children.py`

- [ ] **Step 1: Write failing tests for the new naming module**

`tests/unit/test_naming.py` (replace contents):

```python
from cardinal_cfn.naming import (
    PROJECT,
    APPLICATION,
    cardinal_tags,
    name_tag,
    secret_name,
    ssm_param_name,
    log_group_name,
    LakerunnerComponent,
)


def test_tags_carry_required_keys():
    tags = cardinal_tags(component="task-role", managed_by="cardinal-prereqs-script")
    rendered = tags.to_dict()
    keys = {item["Key"] for item in rendered}
    assert {"Application", "Component", "ManagedBy", "Name"} <= keys


def test_managed_by_required():
    import pytest
    with pytest.raises(ValueError):
        cardinal_tags(component="x", managed_by="")


def test_name_tag_emits_plain_string_no_install_id():
    assert name_tag(role="ingest-bucket") == "cardinal-ingest-bucket"


def test_secret_name_uses_dash_prefix_no_install_id():
    assert secret_name(purpose="db-master") == "cardinal-db-master"


def test_ssm_param_name_uses_slash_prefix_no_install_id():
    assert ssm_param_name(key="storage-profiles") == "/cardinal/storage-profiles"


def test_log_group_name_uses_slash_prefix():
    assert log_group_name(service="query-api") == "/cardinal/query-api"


def test_lakerunner_components_are_known():
    assert LakerunnerComponent.QUERY_API.value == "query-api"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `make test-unit`
Expected: FAIL — module doesn't have new symbols yet.

- [ ] **Step 3: Implement the new naming module**

`src/cardinal_cfn/naming.py` (replace contents):

```python
"""Naming and tag conventions for Cardinal resources.

One install per AWS account+region. No InstallId: physical names use
plain ``cardinal-*`` / ``cardinal/*`` prefixes. The same tag set is
applied identically by the shell-script generators and the CFN
template generators; differing only in the ``ManagedBy`` value.
"""

from __future__ import annotations

from enum import Enum

from troposphere import Tags


PROJECT = "cardinal"
APPLICATION = "cardinal-lakerunner"


class LakerunnerComponent(str, Enum):
    """Service identities — used as physical-name suffixes and tag values."""

    QUERY_API = "query-api"
    QUERY_WORKER = "query-worker"
    PROCESS_LOGS = "process-logs"
    PROCESS_METRICS = "process-metrics"
    PROCESS_TRACES = "process-traces"
    PUBSUB_SQS = "pubsub-sqs"
    SWEEPER = "sweeper"
    MONITORING = "monitoring"
    ADMIN_API = "admin-api"
    ALERT_EVALUATOR = "alert-evaluator"
    OTEL_COLLECTOR = "otel-collector"
    MAESTRO = "maestro"
    DEX = "dex"
    MIGRATOR = "migrator"


def cardinal_tags(*, component: str, managed_by: str, install_version: str | None = None) -> Tags:
    """CFN ``Tags`` value carrying the standard tag set.

    ``managed_by`` is required and identifies which layer owns the
    resource. ``install_version`` is the lakerunner template version
    that last touched the resource; when omitted (e.g. inside generators
    where the version is set later) callers must add it via a separate
    ``Tags`` merge before emitting the final template.
    """

    if not managed_by:
        raise ValueError("managed_by is required")

    items: dict[str, str] = {
        "Application": APPLICATION,
        "Component": component,
        "ManagedBy": managed_by,
        "Name": f"cardinal-{component}",
    }
    if install_version:
        items["cardinal:install-version"] = install_version
    return Tags(**items)


def name_tag(*, role: str) -> str:
    """Plain string for resources that take a ``Name=`` arg directly."""

    return f"cardinal-{role}"


def secret_name(*, purpose: str) -> str:
    """Explicit Secrets Manager secret name. Suffix appended by AWS."""

    return f"cardinal-{purpose}"


def ssm_param_name(*, key: str) -> str:
    """Explicit SSM parameter name. Leading slash required."""

    return f"/cardinal/{key}"


def log_group_name(*, service: str) -> str:
    """Per-service CloudWatch log group name."""

    return f"/cardinal/{service}"
```

- [ ] **Step 4: Implement iam_policies module with tests**

`tests/unit/test_iam_policies.py`:

```python
from cardinal_cfn.iam_policies import (
    secrets_read_policy_doc,
    ssm_read_policy_doc,
    s3_rw_policy_doc,
    sqs_rw_policy_doc,
    logs_write_policy_doc,
    ecs_describe_policy_doc,
    bedrock_invoke_policy_doc,
    pass_role_policy_doc,
    ecs_run_task_policy_doc,
)


def test_secrets_read_doc_scopes_to_cardinal_prefix():
    doc = secrets_read_policy_doc(account_id="123", region="us-east-2")
    assert doc["Version"] == "2012-10-17"
    [stmt] = doc["Statement"]
    assert stmt["Effect"] == "Allow"
    assert stmt["Action"] == ["secretsmanager:GetSecretValue"]
    assert stmt["Resource"] == [
        "arn:aws:secretsmanager:us-east-2:123:secret:cardinal-*"
    ]


def test_ssm_read_doc_scopes_to_cardinal_prefix():
    doc = ssm_read_policy_doc(account_id="123", region="us-east-2")
    [stmt] = doc["Statement"]
    assert "ssm:GetParameter" in stmt["Action"]
    assert stmt["Resource"] == [
        "arn:aws:ssm:us-east-2:123:parameter/cardinal/*"
    ]


def test_s3_rw_doc_scopes_to_named_bucket():
    doc = s3_rw_policy_doc(bucket_name="cardinal-ingest-123-us-east-2")
    actions = {a for stmt in doc["Statement"] for a in stmt["Action"]}
    assert "s3:GetObject" in actions
    assert "s3:PutObject" in actions
    resources = {r for stmt in doc["Statement"] for r in stmt["Resource"]}
    assert "arn:aws:s3:::cardinal-ingest-123-us-east-2" in resources
    assert "arn:aws:s3:::cardinal-ingest-123-us-east-2/*" in resources


def test_sqs_rw_doc_scopes_to_named_queue():
    doc = sqs_rw_policy_doc(account_id="123", region="us-east-2", queue_name="cardinal-ingest")
    [stmt] = doc["Statement"]
    assert stmt["Resource"] == [
        "arn:aws:sqs:us-east-2:123:cardinal-ingest"
    ]


def test_logs_write_doc_scopes_to_cardinal_prefix():
    doc = logs_write_policy_doc(account_id="123", region="us-east-2")
    [stmt] = doc["Statement"]
    assert stmt["Resource"] == [
        "arn:aws:logs:us-east-2:123:log-group:/cardinal/*",
        "arn:aws:logs:us-east-2:123:log-group:/cardinal/*:*",
    ]


def test_ecs_describe_doc_uses_cluster_condition():
    doc = ecs_describe_policy_doc(cluster_arn="arn:aws:ecs:us-east-2:123:cluster/cardinal")
    [stmt] = doc["Statement"]
    assert "ecs:DescribeServices" in stmt["Action"]
    assert "ecs:UpdateService" in stmt["Action"]
    assert stmt["Resource"] == "*"
    assert stmt["Condition"]["ArnEquals"]["ecs:cluster"] == "arn:aws:ecs:us-east-2:123:cluster/cardinal"


def test_bedrock_invoke_doc_scoped_to_foundation_models():
    doc = bedrock_invoke_policy_doc(region="us-east-2")
    [stmt] = doc["Statement"]
    assert stmt["Resource"] == ["arn:aws:bedrock:us-east-2::foundation-model/*"]


def test_pass_role_doc_lists_specific_arns():
    doc = pass_role_policy_doc(role_arns=["arn:aws:iam::123:role/cardinal-task-role"])
    [stmt] = doc["Statement"]
    assert stmt["Action"] == ["iam:PassRole"]
    assert stmt["Resource"] == ["arn:aws:iam::123:role/cardinal-task-role"]


def test_run_task_doc_uses_cluster_condition():
    doc = ecs_run_task_policy_doc(
        cluster_arn="arn:aws:ecs:us-east-2:123:cluster/cardinal",
        task_definition_family="cardinal-migrator",
        account_id="123",
        region="us-east-2",
    )
    actions = {a for stmt in doc["Statement"] for a in stmt["Action"]}
    assert {"ecs:RunTask", "ecs:DescribeTasks"} <= actions
    run_stmt = next(s for s in doc["Statement"] if "ecs:RunTask" in s["Action"])
    assert run_stmt["Resource"] == [
        "arn:aws:ecs:us-east-2:123:task-definition/cardinal-migrator:*"
    ]
    assert run_stmt["Condition"]["ArnEquals"]["ecs:cluster"] == "arn:aws:ecs:us-east-2:123:cluster/cardinal"
```

`src/cardinal_cfn/iam_policies.py`:

```python
"""Pure-data IAM policy-document builders.

Used by the shell-script generators (which inline the JSON into
``aws iam put-role-policy --policy-document file://...`` calls) and
by the CFN template generators (which embed the same dicts as
``Policies=[Policy(PolicyDocument=...)]`` on consuming resources).

Every builder returns a plain ``dict`` shaped as a valid AWS IAM
policy document. Pass concrete account/region/ARN values; do not
parameterize via Sub strings — the shell-script generator can't
emit Sub.
"""

from __future__ import annotations


def _doc(*statements: dict) -> dict:
    return {"Version": "2012-10-17", "Statement": list(statements)}


def secrets_read_policy_doc(*, account_id: str, region: str) -> dict:
    return _doc({
        "Effect": "Allow",
        "Action": ["secretsmanager:GetSecretValue"],
        "Resource": [
            f"arn:aws:secretsmanager:{region}:{account_id}:secret:cardinal-*"
        ],
    })


def ssm_read_policy_doc(*, account_id: str, region: str) -> dict:
    return _doc({
        "Effect": "Allow",
        "Action": [
            "ssm:GetParameter",
            "ssm:GetParameters",
            "ssm:GetParametersByPath",
        ],
        "Resource": [f"arn:aws:ssm:{region}:{account_id}:parameter/cardinal/*"],
    })


def s3_rw_policy_doc(*, bucket_name: str) -> dict:
    return _doc(
        {
            "Effect": "Allow",
            "Action": [
                "s3:GetBucketLocation",
                "s3:ListBucket",
                "s3:GetBucketNotification",
            ],
            "Resource": [f"arn:aws:s3:::{bucket_name}"],
        },
        {
            "Effect": "Allow",
            "Action": [
                "s3:GetObject",
                "s3:PutObject",
                "s3:DeleteObject",
                "s3:AbortMultipartUpload",
            ],
            "Resource": [f"arn:aws:s3:::{bucket_name}/*"],
        },
    )


def sqs_rw_policy_doc(*, account_id: str, region: str, queue_name: str) -> dict:
    return _doc({
        "Effect": "Allow",
        "Action": [
            "sqs:ReceiveMessage",
            "sqs:DeleteMessage",
            "sqs:SendMessage",
            "sqs:GetQueueAttributes",
            "sqs:GetQueueUrl",
            "sqs:ChangeMessageVisibility",
        ],
        "Resource": [f"arn:aws:sqs:{region}:{account_id}:{queue_name}"],
    })


def logs_write_policy_doc(*, account_id: str, region: str) -> dict:
    return _doc({
        "Effect": "Allow",
        "Action": [
            "logs:CreateLogStream",
            "logs:PutLogEvents",
            "logs:DescribeLogStreams",
        ],
        "Resource": [
            f"arn:aws:logs:{region}:{account_id}:log-group:/cardinal/*",
            f"arn:aws:logs:{region}:{account_id}:log-group:/cardinal/*:*",
        ],
    })


def ecs_describe_policy_doc(*, cluster_arn: str) -> dict:
    return _doc({
        "Effect": "Allow",
        "Action": [
            "ecs:DescribeServices",
            "ecs:DescribeTasks",
            "ecs:ListTasks",
            "ecs:UpdateService",
        ],
        "Resource": "*",
        "Condition": {"ArnEquals": {"ecs:cluster": cluster_arn}},
    })


def bedrock_invoke_policy_doc(*, region: str) -> dict:
    return _doc({
        "Effect": "Allow",
        "Action": [
            "bedrock:InvokeModel",
            "bedrock:InvokeModelWithResponseStream",
        ],
        "Resource": [f"arn:aws:bedrock:{region}::foundation-model/*"],
    })


def pass_role_policy_doc(*, role_arns: list[str]) -> dict:
    return _doc({
        "Effect": "Allow",
        "Action": ["iam:PassRole"],
        "Resource": list(role_arns),
    })


def ecs_run_task_policy_doc(*, cluster_arn: str, task_definition_family: str, account_id: str, region: str) -> dict:
    return _doc(
        {
            "Effect": "Allow",
            "Action": ["ecs:RunTask"],
            "Resource": [
                f"arn:aws:ecs:{region}:{account_id}:task-definition/{task_definition_family}:*"
            ],
            "Condition": {"ArnEquals": {"ecs:cluster": cluster_arn}},
        },
        {
            "Effect": "Allow",
            "Action": ["ecs:DescribeTasks"],
            "Resource": "*",
            "Condition": {"ArnEquals": {"ecs:cluster": cluster_arn}},
        },
    )
```

- [ ] **Step 5: Delete obsolete InstallId code and tests**

```bash
rm src/cardinal_cfn/install_id.py
rm tests/unit/test_install_id.py
rm tests/unit/test_no_install_id_in_children.py
```

- [ ] **Step 6: Run unit tests, expect new ones pass and removed ones gone**

Run: `make test-unit`

Note: Many tests will fail because old children still import `install_id`. That's expected; they'll be removed in later tasks. To unblock `test_naming.py` and `test_iam_policies.py` specifically:

Run: `.venv/bin/pytest tests/unit/test_naming.py tests/unit/test_iam_policies.py -v`
Expected: PASS for both.

- [ ] **Step 7: Commit**

```bash
git add src/cardinal_cfn/naming.py src/cardinal_cfn/iam_policies.py \
        tests/unit/test_naming.py tests/unit/test_iam_policies.py
git rm src/cardinal_cfn/install_id.py tests/unit/test_install_id.py \
        tests/unit/test_no_install_id_in_children.py
git commit -m "refactor: drop InstallId, unify tag/naming, add iam_policies"
```

---

## Task 2 — Prereqs script generator

**Files:**

- Create: `src/cardinal_cfn/prereqs/__init__.py`, `roles.py`, `security_groups.py`, `render.py`
- Create: `tests/unit/test_prereqs_render.py`
- Create: `tests/scripts/__init__.py`, `tests/scripts/test_prereqs_script.py`

- [ ] **Step 1: Write the data-structure tests**

`tests/unit/test_prereqs_render.py`:

```python
import json
import re

from cardinal_cfn.prereqs.render import render_prereqs_script
from cardinal_cfn.prereqs.roles import expected_role_specs
from cardinal_cfn.prereqs.security_groups import expected_sg_specs


def test_role_specs_cover_required_roles():
    names = {spec.name for spec in expected_role_specs(account_id="123", region="us-east-2", cluster_arn="arn:...:cluster/cardinal")}
    assert names == {
        "cardinal-task-role",
        "cardinal-execution-role",
        "cardinal-migration-lambda-role",
    }


def test_task_role_inline_policy_is_valid_json():
    [task_role] = [r for r in expected_role_specs(
        account_id="123", region="us-east-2",
        cluster_arn="arn:aws:ecs:us-east-2:123:cluster/cardinal"
    ) if r.name == "cardinal-task-role"]
    parsed = json.loads(task_role.inline_policy_json)
    assert parsed["Version"] == "2012-10-17"


def test_sg_specs_cover_required_sgs():
    names = {spec.name for spec in expected_sg_specs()}
    assert names == {"cardinal-task-sg", "cardinal-alb-sg", "cardinal-db-sg"}


def test_render_emits_posix_shell():
    out = render_prereqs_script()
    assert out.startswith("#!/bin/sh\n") or out.startswith("#!/usr/bin/env sh\n")
    assert "set -eu" in out


def test_render_emits_create_role_calls_for_each_role():
    out = render_prereqs_script()
    for role in ["cardinal-task-role", "cardinal-execution-role", "cardinal-migration-lambda-role"]:
        assert f"ensure_role {role}" in out or f'NAME="{role}"' in out


def test_render_emits_create_sg_calls_for_each_sg():
    out = render_prereqs_script()
    for sg in ["cardinal-task-sg", "cardinal-alb-sg", "cardinal-db-sg"]:
        assert sg in out


def test_render_emits_no_install_id_references():
    out = render_prereqs_script()
    assert "InstallId" not in out


def test_render_emits_output_json_writer():
    out = render_prereqs_script()
    assert "--output-file" in out
    assert "TaskRoleArn" in out
    assert "TaskSgId" in out


def test_render_uses_cardinal_tags_on_creates():
    out = render_prereqs_script()
    # tags applied inline to create-role and create-security-group
    assert "Application=cardinal-lakerunner" in out
    assert "ManagedBy=cardinal-prereqs-script" in out
```

- [ ] **Step 2: Implement role and SG data structures**

`src/cardinal_cfn/prereqs/__init__.py`: empty.

`src/cardinal_cfn/prereqs/roles.py`:

```python
"""Role specifications — pure data, used by the shell-script renderer."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from cardinal_cfn import iam_policies


_ECS_TASKS_TRUST = {
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "ecs-tasks.amazonaws.com"},
        "Action": "sts:AssumeRole",
    }],
}

_LAMBDA_TRUST = {
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "lambda.amazonaws.com"},
        "Action": "sts:AssumeRole",
    }],
}


@dataclass(frozen=True)
class RoleSpec:
    name: str
    description: str
    trust_policy_json: str
    inline_policy_name: str
    inline_policy_json: str
    managed_policy_arns: tuple[str, ...] = field(default_factory=tuple)


def _task_role_inline_policy(*, account_id: str, region: str, cluster_arn: str) -> dict:
    bucket = f"cardinal-ingest-{account_id}-{region}"
    return {
        "Version": "2012-10-17",
        "Statement": [
            *iam_policies.s3_rw_policy_doc(bucket_name=bucket)["Statement"],
            *iam_policies.sqs_rw_policy_doc(account_id=account_id, region=region, queue_name="cardinal-ingest")["Statement"],
            *iam_policies.ssm_read_policy_doc(account_id=account_id, region=region)["Statement"],
            *iam_policies.secrets_read_policy_doc(account_id=account_id, region=region)["Statement"],
            *iam_policies.logs_write_policy_doc(account_id=account_id, region=region)["Statement"],
            *iam_policies.ecs_describe_policy_doc(cluster_arn=cluster_arn)["Statement"],
            *iam_policies.bedrock_invoke_policy_doc(region=region)["Statement"],
        ],
    }


def _execution_role_inline_policy(*, account_id: str, region: str) -> dict:
    return {
        "Version": "2012-10-17",
        "Statement": [
            *iam_policies.secrets_read_policy_doc(account_id=account_id, region=region)["Statement"],
            *iam_policies.ssm_read_policy_doc(account_id=account_id, region=region)["Statement"],
        ],
    }


def _migration_lambda_role_inline_policy(*, account_id: str, region: str, cluster_arn: str) -> dict:
    task_role_arn = f"arn:aws:iam::{account_id}:role/cardinal-task-role"
    execution_role_arn = f"arn:aws:iam::{account_id}:role/cardinal-execution-role"
    return {
        "Version": "2012-10-17",
        "Statement": [
            *iam_policies.ecs_run_task_policy_doc(
                cluster_arn=cluster_arn,
                task_definition_family="cardinal-migrator",
                account_id=account_id,
                region=region,
            )["Statement"],
            *iam_policies.pass_role_policy_doc(role_arns=[task_role_arn, execution_role_arn])["Statement"],
            {
                "Effect": "Allow",
                "Action": [
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                ],
                "Resource": "*",
            },
        ],
    }


def expected_role_specs(*, account_id: str, region: str, cluster_arn: str) -> list[RoleSpec]:
    return [
        RoleSpec(
            name="cardinal-task-role",
            description="Shared task role for every Cardinal ECS task",
            trust_policy_json=json.dumps(_ECS_TASKS_TRUST, sort_keys=True),
            inline_policy_name="cardinal-task-role-policy",
            inline_policy_json=json.dumps(
                _task_role_inline_policy(account_id=account_id, region=region, cluster_arn=cluster_arn),
                sort_keys=True,
            ),
        ),
        RoleSpec(
            name="cardinal-execution-role",
            description="ECS task execution role (image pull + secrets/ssm resolve)",
            trust_policy_json=json.dumps(_ECS_TASKS_TRUST, sort_keys=True),
            inline_policy_name="cardinal-execution-role-policy",
            inline_policy_json=json.dumps(
                _execution_role_inline_policy(account_id=account_id, region=region),
                sort_keys=True,
            ),
            managed_policy_arns=(
                "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy",
            ),
        ),
        RoleSpec(
            name="cardinal-migration-lambda-role",
            description="Migration Lambda execution role (one-shot RunTask of the migrator)",
            trust_policy_json=json.dumps(_LAMBDA_TRUST, sort_keys=True),
            inline_policy_name="cardinal-migration-lambda-role-policy",
            inline_policy_json=json.dumps(
                _migration_lambda_role_inline_policy(account_id=account_id, region=region, cluster_arn=cluster_arn),
                sort_keys=True,
            ),
        ),
    ]
```

`src/cardinal_cfn/prereqs/security_groups.py`:

```python
"""Security-group specifications — pure data, used by the renderer."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class IngressRule:
    description: str
    protocol: str            # "tcp", "udp", or "-1"
    from_port: int
    to_port: int
    source_kind: str         # "self", "sg", "cidr"
    source_value: str        # "" for self, sg name for sg, cidr for cidr


@dataclass(frozen=True)
class SgSpec:
    name: str
    description: str
    ingress: tuple[IngressRule, ...] = field(default_factory=tuple)


def expected_sg_specs() -> list[SgSpec]:
    return [
        SgSpec(
            name="cardinal-task-sg",
            description="Cardinal ECS tasks; allows intra-cluster traffic and ALB ingress",
            ingress=(
                IngressRule("self all-tcp", "tcp", 0, 65535, "self", ""),
                IngressRule("from ALB", "tcp", 0, 65535, "sg", "cardinal-alb-sg"),
            ),
        ),
        SgSpec(
            name="cardinal-alb-sg",
            description="Cardinal internal ALB",
            ingress=(
                IngressRule("https", "tcp", 443, 443, "cidr", "0.0.0.0/0"),
                IngressRule("admin-https", "tcp", 9443, 9443, "cidr", "0.0.0.0/0"),
            ),
        ),
        SgSpec(
            name="cardinal-db-sg",
            description="Cardinal RDS Postgres",
            ingress=(
                IngressRule("postgres from tasks", "tcp", 5432, 5432, "sg", "cardinal-task-sg"),
            ),
        ),
    ]
```

- [ ] **Step 3: Implement renderer**

`src/cardinal_cfn/prereqs/render.py`:

```python
"""Generates ``cardinal-prereqs.sh`` — POSIX shell, idempotent ensure_* steps."""

from __future__ import annotations

from textwrap import dedent

from cardinal_cfn.prereqs.security_groups import expected_sg_specs


SHELL_HEADER = """\
#!/bin/sh
# cardinal-prereqs.sh -- create IAM roles + security groups Cardinal needs.
#
# Run once per AWS account+region with a privileged identity. Idempotent:
# matching resource is a no-op, drifted resource exits 2 with a diff.
#
# After successful run, hand the printed Key=Value block (or --output-file
# JSON) to whoever runs cardinal-data-setup.sh and then the two CFN stacks.

set -eu

PROJECT="cardinal"
APPLICATION="cardinal-lakerunner"
MANAGED_BY="cardinal-prereqs-script"

REGION=""
VPC_ID=""
OUTPUT_FILE=""

usage() {
    cat <<'EOF'
Usage: cardinal-prereqs.sh --region REGION --vpc-id VPC [--output-file PATH]

Required:
  --region        AWS region.
  --vpc-id        VPC ID where the SGs live.

Optional:
  --output-file   Write a JSON object of {ParameterKey: Value} to this path.

Exit codes:
  0  success or no-op
  1  AWS / unexpected failure
  2  drift detected or input/preflight failure
EOF
}

log() { printf '[%s] %s\\n' "cardinal-prereqs" "$*" >&2; }
fail() { code="$1"; shift; printf '[cardinal-prereqs] ERROR: %s\\n' "$*" >&2; exit "$code"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --region) REGION="$2"; shift 2 ;;
        --vpc-id) VPC_ID="$2"; shift 2 ;;
        --output-file) OUTPUT_FILE="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; fail 2 "unknown argument: $1" ;;
    esac
done

[ -n "$REGION" ] || { usage >&2; fail 2 "--region required"; }
[ -n "$VPC_ID" ] || { usage >&2; fail 2 "--vpc-id required"; }

for tool in aws jq; do
    command -v "$tool" >/dev/null 2>&1 || fail 2 "required tool not found: $tool"
done

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
CLUSTER_ARN="arn:aws:ecs:${REGION}:${ACCOUNT_ID}:cluster/cardinal"
TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT INT TERM HUP

TAGS_INLINE="Key=Application,Value=${APPLICATION} Key=Project,Value=${PROJECT} Key=ManagedBy,Value=${MANAGED_BY}"

# ---------------------------------------------------------------------------
# Per-config-item idempotency helpers
# ---------------------------------------------------------------------------
ensure_role() {
    role_name="$1"
    trust_file="$2"
    description="$3"
    existing=$(aws iam get-role --role-name "$role_name" --query 'Role.AssumeRolePolicyDocument' --output json 2>/dev/null || echo "")
    if [ -z "$existing" ]; then
        log "creating role $role_name"
        aws iam create-role \\
            --role-name "$role_name" \\
            --assume-role-policy-document "file://$trust_file" \\
            --description "$description" \\
            --tags Key=Application,Value="$APPLICATION" \\
                   Key=Project,Value="$PROJECT" \\
                   Key=ManagedBy,Value="$MANAGED_BY" \\
                   Key=Component,Value="$role_name" \\
                   Key=Name,Value="$role_name" >/dev/null
    else
        actual=$(printf '%s' "$existing" | jq -S .)
        wanted=$(jq -S . "$trust_file")
        if [ "$actual" != "$wanted" ]; then
            fail 2 "role $role_name exists with drifted trust policy"
        fi
        log "role $role_name exists, trust policy matches"
    fi
}

ensure_inline_policy() {
    role_name="$1"
    policy_name="$2"
    policy_file="$3"
    existing=$(aws iam get-role-policy --role-name "$role_name" --policy-name "$policy_name" --query 'PolicyDocument' --output json 2>/dev/null || echo "")
    wanted=$(jq -S . "$policy_file")
    if [ -z "$existing" ]; then
        log "putting inline policy $policy_name on $role_name"
        aws iam put-role-policy --role-name "$role_name" --policy-name "$policy_name" --policy-document "file://$policy_file" >/dev/null
    else
        actual=$(printf '%s' "$existing" | jq -S .)
        if [ "$actual" != "$wanted" ]; then
            fail 2 "inline policy $policy_name on $role_name has drifted"
        fi
        log "inline policy $policy_name on $role_name matches"
    fi
}

ensure_managed_policy_attached() {
    role_name="$1"
    policy_arn="$2"
    if aws iam list-attached-role-policies --role-name "$role_name" --query "AttachedPolicies[?PolicyArn==\\`$policy_arn\\`].PolicyArn" --output text | grep -q "$policy_arn"; then
        log "managed policy $policy_arn already attached to $role_name"
    else
        log "attaching managed policy $policy_arn to $role_name"
        aws iam attach-role-policy --role-name "$role_name" --policy-arn "$policy_arn" >/dev/null
    fi
}

ensure_sg() {
    sg_name="$1"
    description="$2"
    sg_id=$(aws ec2 describe-security-groups --filters Name=vpc-id,Values="$VPC_ID" Name=group-name,Values="$sg_name" --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || echo "None")
    if [ "$sg_id" = "None" ] || [ -z "$sg_id" ]; then
        log "creating security group $sg_name"
        sg_id=$(aws ec2 create-security-group \\
            --group-name "$sg_name" \\
            --description "$description" \\
            --vpc-id "$VPC_ID" \\
            --tag-specifications "ResourceType=security-group,Tags=[{Key=Application,Value=$APPLICATION},{Key=Project,Value=$PROJECT},{Key=ManagedBy,Value=$MANAGED_BY},{Key=Component,Value=$sg_name},{Key=Name,Value=$sg_name}]" \\
            --query 'GroupId' --output text)
    else
        log "security group $sg_name exists ($sg_id)"
    fi
    eval "${2:+:}"
    printf '%s\\n' "$sg_id"
}

ensure_ingress_rule_self() {
    sg_id="$1"; protocol="$2"; from="$3"; to="$4"
    if aws ec2 describe-security-groups --group-ids "$sg_id" --query "SecurityGroups[0].IpPermissions[?IpProtocol==\\`$protocol\\` && FromPort==\\`$from\\` && ToPort==\\`$to\\` && UserIdGroupPairs[?GroupId==\\`$sg_id\\`]]" --output text | grep -q .; then
        return 0
    fi
    log "authorize self ingress $protocol $from-$to on $sg_id"
    aws ec2 authorize-security-group-ingress --group-id "$sg_id" --ip-permissions "IpProtocol=$protocol,FromPort=$from,ToPort=$to,UserIdGroupPairs=[{GroupId=$sg_id}]" >/dev/null
}

ensure_ingress_rule_sg() {
    sg_id="$1"; src_sg_id="$2"; protocol="$3"; from="$4"; to="$5"
    if aws ec2 describe-security-groups --group-ids "$sg_id" --query "SecurityGroups[0].IpPermissions[?IpProtocol==\\`$protocol\\` && FromPort==\\`$from\\` && ToPort==\\`$to\\` && UserIdGroupPairs[?GroupId==\\`$src_sg_id\\`]]" --output text | grep -q .; then
        return 0
    fi
    log "authorize sg ingress $protocol $from-$to from $src_sg_id on $sg_id"
    aws ec2 authorize-security-group-ingress --group-id "$sg_id" --ip-permissions "IpProtocol=$protocol,FromPort=$from,ToPort=$to,UserIdGroupPairs=[{GroupId=$src_sg_id}]" >/dev/null
}

ensure_ingress_rule_cidr() {
    sg_id="$1"; cidr="$2"; protocol="$3"; from="$4"; to="$5"
    if aws ec2 describe-security-groups --group-ids "$sg_id" --query "SecurityGroups[0].IpPermissions[?IpProtocol==\\`$protocol\\` && FromPort==\\`$from\\` && ToPort==\\`$to\\` && IpRanges[?CidrIp==\\`$cidr\\`]]" --output text | grep -q .; then
        return 0
    fi
    log "authorize cidr ingress $protocol $from-$to from $cidr on $sg_id"
    aws ec2 authorize-security-group-ingress --group-id "$sg_id" --ip-permissions "IpProtocol=$protocol,FromPort=$from,ToPort=$to,IpRanges=[{CidrIp=$cidr}]" >/dev/null
}
"""


def _emit_role_doc_files() -> str:
    """Render shell that writes the trust + inline policy JSON to TMP_DIR.

    The role specs depend on AccountId, Region, ClusterArn — only known at
    runtime — so the script regenerates them with a small inline jq helper
    rather than baking them in at build time.
    """

    return dedent("""
        # ---------------------------------------------------------------------
        # Render role policy documents (depend on AccountId / Region / Cluster)
        # ---------------------------------------------------------------------
        cat >"$TMP_DIR/trust-ecs-tasks.json" <<'JSON'
        {"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ecs-tasks.amazonaws.com"},"Action":"sts:AssumeRole"}]}
        JSON
        cat >"$TMP_DIR/trust-lambda.json" <<'JSON'
        {"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}
        JSON

        BUCKET_NAME="cardinal-ingest-${ACCOUNT_ID}-${REGION}"
        QUEUE_ARN="arn:aws:sqs:${REGION}:${ACCOUNT_ID}:cardinal-ingest"
        TASK_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/cardinal-task-role"
        EXECUTION_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/cardinal-execution-role"

        jq -n \\
            --arg bucket "$BUCKET_NAME" \\
            --arg queue_arn "$QUEUE_ARN" \\
            --arg secrets_arn "arn:aws:secretsmanager:${REGION}:${ACCOUNT_ID}:secret:cardinal-*" \\
            --arg ssm_arn "arn:aws:ssm:${REGION}:${ACCOUNT_ID}:parameter/cardinal/*" \\
            --arg log_group_arn "arn:aws:logs:${REGION}:${ACCOUNT_ID}:log-group:/cardinal/*" \\
            --arg log_group_arn_streams "arn:aws:logs:${REGION}:${ACCOUNT_ID}:log-group:/cardinal/*:*" \\
            --arg cluster_arn "$CLUSTER_ARN" \\
            --arg bedrock_arn "arn:aws:bedrock:${REGION}::foundation-model/*" \\
            '{Version:"2012-10-17",Statement:[
              {Effect:"Allow",Action:["s3:GetBucketLocation","s3:ListBucket","s3:GetBucketNotification"],Resource:["arn:aws:s3:::"+$bucket]},
              {Effect:"Allow",Action:["s3:GetObject","s3:PutObject","s3:DeleteObject","s3:AbortMultipartUpload"],Resource:["arn:aws:s3:::"+$bucket+"/*"]},
              {Effect:"Allow",Action:["sqs:ReceiveMessage","sqs:DeleteMessage","sqs:SendMessage","sqs:GetQueueAttributes","sqs:GetQueueUrl","sqs:ChangeMessageVisibility"],Resource:[$queue_arn]},
              {Effect:"Allow",Action:["ssm:GetParameter","ssm:GetParameters","ssm:GetParametersByPath"],Resource:[$ssm_arn]},
              {Effect:"Allow",Action:["secretsmanager:GetSecretValue"],Resource:[$secrets_arn]},
              {Effect:"Allow",Action:["logs:CreateLogStream","logs:PutLogEvents","logs:DescribeLogStreams"],Resource:[$log_group_arn,$log_group_arn_streams]},
              {Effect:"Allow",Action:["ecs:DescribeServices","ecs:DescribeTasks","ecs:ListTasks","ecs:UpdateService"],Resource:"*",Condition:{ArnEquals:{"ecs:cluster":$cluster_arn}}},
              {Effect:"Allow",Action:["bedrock:InvokeModel","bedrock:InvokeModelWithResponseStream"],Resource:[$bedrock_arn]}
            ]}' >"$TMP_DIR/cardinal-task-role-policy.json"

        jq -n \\
            --arg secrets_arn "arn:aws:secretsmanager:${REGION}:${ACCOUNT_ID}:secret:cardinal-*" \\
            --arg ssm_arn "arn:aws:ssm:${REGION}:${ACCOUNT_ID}:parameter/cardinal/*" \\
            '{Version:"2012-10-17",Statement:[
              {Effect:"Allow",Action:["secretsmanager:GetSecretValue"],Resource:[$secrets_arn]},
              {Effect:"Allow",Action:["ssm:GetParameter","ssm:GetParameters","ssm:GetParametersByPath"],Resource:[$ssm_arn]}
            ]}' >"$TMP_DIR/cardinal-execution-role-policy.json"

        jq -n \\
            --arg cluster_arn "$CLUSTER_ARN" \\
            --arg taskdef_arn "arn:aws:ecs:${REGION}:${ACCOUNT_ID}:task-definition/cardinal-migrator:*" \\
            --arg task_role_arn "$TASK_ROLE_ARN" \\
            --arg exec_role_arn "$EXECUTION_ROLE_ARN" \\
            '{Version:"2012-10-17",Statement:[
              {Effect:"Allow",Action:["ecs:RunTask"],Resource:[$taskdef_arn],Condition:{ArnEquals:{"ecs:cluster":$cluster_arn}}},
              {Effect:"Allow",Action:["ecs:DescribeTasks"],Resource:"*",Condition:{ArnEquals:{"ecs:cluster":$cluster_arn}}},
              {Effect:"Allow",Action:["iam:PassRole"],Resource:[$task_role_arn,$exec_role_arn]},
              {Effect:"Allow",Action:["logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents"],Resource:"*"}
            ]}' >"$TMP_DIR/cardinal-migration-lambda-role-policy.json"
    """).lstrip()


def _emit_role_creation() -> str:
    return dedent("""
        # ---------------------------------------------------------------------
        # Roles
        # ---------------------------------------------------------------------
        ensure_role cardinal-task-role "$TMP_DIR/trust-ecs-tasks.json" "Shared task role for every Cardinal ECS task"
        ensure_inline_policy cardinal-task-role cardinal-task-role-policy "$TMP_DIR/cardinal-task-role-policy.json"

        ensure_role cardinal-execution-role "$TMP_DIR/trust-ecs-tasks.json" "ECS task execution role"
        ensure_managed_policy_attached cardinal-execution-role arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy
        ensure_inline_policy cardinal-execution-role cardinal-execution-role-policy "$TMP_DIR/cardinal-execution-role-policy.json"

        ensure_role cardinal-migration-lambda-role "$TMP_DIR/trust-lambda.json" "Migration Lambda execution role"
        ensure_inline_policy cardinal-migration-lambda-role cardinal-migration-lambda-role-policy "$TMP_DIR/cardinal-migration-lambda-role-policy.json"

        TASK_ROLE_ARN_OUT=$(aws iam get-role --role-name cardinal-task-role --query 'Role.Arn' --output text)
        EXECUTION_ROLE_ARN_OUT=$(aws iam get-role --role-name cardinal-execution-role --query 'Role.Arn' --output text)
        MIGRATION_LAMBDA_ROLE_ARN_OUT=$(aws iam get-role --role-name cardinal-migration-lambda-role --query 'Role.Arn' --output text)
    """).lstrip()


def _emit_sg_creation() -> str:
    return dedent("""
        # ---------------------------------------------------------------------
        # Security groups (create all first, THEN ingress rules)
        # ---------------------------------------------------------------------
        TASK_SG_ID=$(ensure_sg cardinal-task-sg "Cardinal ECS tasks; intra-cluster + ALB ingress")
        ALB_SG_ID=$(ensure_sg cardinal-alb-sg "Cardinal internal ALB")
        DB_SG_ID=$(ensure_sg cardinal-db-sg "Cardinal RDS Postgres")

        ensure_ingress_rule_self "$TASK_SG_ID" tcp 0 65535
        ensure_ingress_rule_sg   "$TASK_SG_ID" "$ALB_SG_ID" tcp 0 65535
        ensure_ingress_rule_cidr "$ALB_SG_ID" 0.0.0.0/0 tcp 443 443
        ensure_ingress_rule_cidr "$ALB_SG_ID" 0.0.0.0/0 tcp 9443 9443
        ensure_ingress_rule_sg   "$DB_SG_ID" "$TASK_SG_ID" tcp 5432 5432
    """).lstrip()


def _emit_output() -> str:
    return dedent("""
        # ---------------------------------------------------------------------
        # Output (printed to stdout; written to --output-file as JSON if given)
        # ---------------------------------------------------------------------
        printf 'Key=TaskRoleArn,Value=%s\\n' "$TASK_ROLE_ARN_OUT"
        printf 'Key=ExecutionRoleArn,Value=%s\\n' "$EXECUTION_ROLE_ARN_OUT"
        printf 'Key=MigrationLambdaRoleArn,Value=%s\\n' "$MIGRATION_LAMBDA_ROLE_ARN_OUT"
        printf 'Key=TaskSgId,Value=%s\\n' "$TASK_SG_ID"
        printf 'Key=AlbSgId,Value=%s\\n' "$ALB_SG_ID"
        printf 'Key=DbSgId,Value=%s\\n' "$DB_SG_ID"

        if [ -n "$OUTPUT_FILE" ]; then
            jq -n \\
                --arg task_role "$TASK_ROLE_ARN_OUT" \\
                --arg exec_role "$EXECUTION_ROLE_ARN_OUT" \\
                --arg mig_role "$MIGRATION_LAMBDA_ROLE_ARN_OUT" \\
                --arg task_sg "$TASK_SG_ID" \\
                --arg alb_sg "$ALB_SG_ID" \\
                --arg db_sg "$DB_SG_ID" \\
                '{TaskRoleArn:$task_role, ExecutionRoleArn:$exec_role, MigrationLambdaRoleArn:$mig_role, TaskSgId:$task_sg, AlbSgId:$alb_sg, DbSgId:$db_sg}' \\
                >"$OUTPUT_FILE"
            log "wrote $OUTPUT_FILE"
        fi

        log "done"
    """).lstrip()


def render_prereqs_script() -> str:
    parts = [
        SHELL_HEADER,
        _emit_role_doc_files(),
        _emit_role_creation(),
        _emit_sg_creation(),
        _emit_output(),
    ]
    return "\n".join(parts) + "\n"


if __name__ == "__main__":
    import sys
    sys.stdout.write(render_prereqs_script())
```

- [ ] **Step 4: Add shellcheck test for the rendered script**

`tests/scripts/__init__.py`: empty.

`tests/scripts/test_prereqs_script.py`:

```python
import shutil
import subprocess
from pathlib import Path

import pytest

from cardinal_cfn.prereqs.render import render_prereqs_script


def test_rendered_script_passes_shellcheck(tmp_path: Path):
    if shutil.which("shellcheck") is None:
        pytest.skip("shellcheck not installed")
    script = tmp_path / "cardinal-prereqs.sh"
    script.write_text(render_prereqs_script())
    result = subprocess.run(
        ["shellcheck", "-s", "sh", str(script)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_rendered_script_has_required_helpers():
    out = render_prereqs_script()
    for fn in ["ensure_role", "ensure_inline_policy", "ensure_managed_policy_attached", "ensure_sg"]:
        assert f"{fn}()" in out


def test_rendered_script_orders_sg_creation_before_ingress():
    out = render_prereqs_script()
    sg_create = out.find('TASK_SG_ID=$(ensure_sg cardinal-task-sg')
    first_ingress = out.find('ensure_ingress_rule_self')
    assert sg_create != -1 and first_ingress != -1
    assert sg_create < first_ingress
```

- [ ] **Step 5: Run tests**

Run: `.venv/bin/pytest tests/unit/test_prereqs_render.py tests/scripts/test_prereqs_script.py -v`
Expected: PASS (skipping shellcheck if not installed; install with `brew install shellcheck` for full coverage).

- [ ] **Step 6: Commit**

```bash
git add src/cardinal_cfn/prereqs/ tests/unit/test_prereqs_render.py \
        tests/scripts/__init__.py tests/scripts/test_prereqs_script.py
git commit -m "feat(prereqs): IAM roles + SG shell-script generator with idempotent ensure_* helpers"
```

---

## Task 3 — Data-setup script generator

**Files:**

- Create: `src/cardinal_cfn/data_setup/__init__.py`, `rds.py`, `storage.py`, `secrets.py`, `ssm.py`, `render.py`
- Create: `tests/unit/test_data_setup_render.py`
- Create: `tests/scripts/test_data_setup_script.py`

This task follows the same shape as Task 2: data structures (one module per resource group), a renderer that emits `cardinal-data-setup.sh`, and tests that verify the rendered shell passes shellcheck and contains the expected `ensure_*` calls in the right order.

The renderer emits the following ensure-functions (signatures spelled out in the spec's *Idempotency model* subsection):

```
ensure_db_subnet_group         ensure_secret_with_value
ensure_db_instance             ensure_ssm_parameter
wait_db_available
ensure_db_master_secret_value
ensure_s3_bucket
ensure_s3_lifecycle
ensure_s3_block_public_access
ensure_sqs_queue
ensure_sqs_policy
ensure_s3_notification
```

- [ ] **Step 1: Write the renderer test sketch**

`tests/unit/test_data_setup_render.py`:

```python
from cardinal_cfn.data_setup.render import render_data_setup_script


def test_render_emits_posix_shell():
    out = render_data_setup_script()
    assert out.startswith("#!/bin/sh\n")
    assert "set -eu" in out


def test_render_contains_required_ensure_functions():
    out = render_data_setup_script()
    for fn in [
        "ensure_db_subnet_group",
        "ensure_db_instance",
        "wait_db_available",
        "ensure_db_master_secret_value",
        "ensure_s3_bucket",
        "ensure_s3_lifecycle",
        "ensure_s3_block_public_access",
        "ensure_sqs_queue",
        "ensure_sqs_policy",
        "ensure_s3_notification",
        "ensure_secret_with_value",
        "ensure_ssm_parameter",
    ]:
        assert f"{fn}()" in out


def test_render_orders_queue_before_policy_before_notification():
    out = render_data_setup_script()
    q = out.find("ensure_sqs_queue cardinal-ingest")
    p = out.find("ensure_sqs_policy ")
    n = out.find("ensure_s3_notification ")
    assert 0 <= q < p < n


def test_render_orders_db_before_master_secret_value():
    out = render_data_setup_script()
    d = out.find("ensure_db_instance cardinal-db")
    s = out.find("ensure_db_master_secret_value")
    assert 0 <= d < s


def test_render_uses_deterministic_resource_names():
    out = render_data_setup_script()
    assert 'BUCKET_NAME="cardinal-ingest-${ACCOUNT_ID}-${REGION}"' in out
    assert "cardinal-ingest" in out
    assert "cardinal-db" in out
    for purpose in ["cardinal-db-master", "cardinal-license", "cardinal-internal-keys", "cardinal-admin-key", "cardinal-maestro-db"]:
        assert purpose in out


def test_render_writes_output_json_with_required_keys():
    out = render_data_setup_script()
    for key in [
        "DbEndpoint", "DbPort", "DbName", "DbMasterSecretArn",
        "MaestroDbSecretArn", "IngestBucketName", "IngestQueueUrl",
        "IngestQueueArn", "LicenseSecretArn", "InternalKeysSecretArn",
        "AdminKeySecretArn", "StorageProfilesParamName", "ApiKeysParamName",
    ]:
        assert key in out


def test_render_emits_no_install_id_references():
    out = render_data_setup_script()
    assert "InstallId" not in out
```

- [ ] **Step 2: Implement `data_setup/render.py`**

Implement `src/cardinal_cfn/data_setup/render.py` as a single function `render_data_setup_script() -> str` that emits the full POSIX shell script. Structure mirrors Task 2: a `SHELL_HEADER` block (shebang, `set -eu`, arg parsing for `--region`, `--vpc-id`, `--private-subnets`, `--db-sg-id`, `--license-data-file`, `--dex-admin-email`, `--dex-admin-password-hash-file`, `--oidc-superadmin-emails`, `--db-instance-class`, `--db-allocated-storage`, `--bucket-lifecycle-days`, `--output-file`, `--help`), helper definitions for every `ensure_*` listed in the test, and an ordered call sequence.

Constraints the renderer's output must satisfy (encoded in test cases above):

- All `ensure_*` helpers describe-then-act exactly as in Task 2.
- SQS queue → SQS queue policy (granting `s3.amazonaws.com` SendMessage with `aws:SourceAccount` condition) → S3 bucket → S3 lifecycle → S3 block-public-access → S3→SQS notification, in that order.
- DB subnet group → DB instance → wait-available → DB master secret value (computed from the instance's endpoint).
- Secrets are `cardinal-license` (read from `--license-data-file`), `cardinal-internal-keys` (32-byte hex from `openssl rand -hex 32`), `cardinal-admin-key` (32-byte hex), `cardinal-maestro-db` (JSON pointing at the same DB instance with a maestro-specific username/database created via a follow-on `aws rds-data` execute or a one-off psql command — for now, a placeholder JSON is acceptable as long as `MaestroDbSecretArn` is emitted; the maestro service reads it at runtime).
- SSM params `/cardinal/storage-profiles` and `/cardinal/api-keys`: created with placeholder JSON (`{}` for both, replaced by the operator post-install via `aws ssm put-parameter --overwrite`). A `_note` echoed during run reminds the operator.

Structure the renderer with helper functions per output block (`_emit_header`, `_emit_helpers`, `_emit_storage`, `_emit_database`, `_emit_secrets`, `_emit_ssm`, `_emit_output`).

Skip details that don't affect the public test surface — they're free to be implemented straightforwardly using the same shapes from `prereqs/render.py`.

- [ ] **Step 3: Add data_setup_script test**

`tests/scripts/test_data_setup_script.py`:

```python
import shutil
import subprocess
from pathlib import Path

import pytest

from cardinal_cfn.data_setup.render import render_data_setup_script


def test_data_setup_script_passes_shellcheck(tmp_path: Path):
    if shutil.which("shellcheck") is None:
        pytest.skip("shellcheck not installed")
    script = tmp_path / "cardinal-data-setup.sh"
    script.write_text(render_data_setup_script())
    result = subprocess.run(
        ["shellcheck", "-s", "sh", str(script)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
```

- [ ] **Step 4: Run + commit**

Run: `.venv/bin/pytest tests/unit/test_data_setup_render.py tests/scripts/test_data_setup_script.py -v`

```bash
git add src/cardinal_cfn/data_setup/ tests/unit/test_data_setup_render.py tests/scripts/test_data_setup_script.py
git commit -m "feat(data-setup): RDS/S3/SQS/secrets shell-script generator with ensure_* idempotency"
```

---

## Task 4 — App stack template

**Files:**

- Create: `src/cardinal_cfn/app/__init__.py`, `cluster.py`, `alb.py`, `logs.py`, `cloudmap.py`, `root.py`
- Create: `tests/templates/test_app_template.py`

The app stack consolidates resources from the existing `children/cluster.py`, `children/alb.py`, the per-service log-group pieces in `children/services_*.py`, and the Cloud Map service entries currently sprinkled across `children/services_*.py`. **Cert Lambda is skipped** (see Scope cuts).

Strategy: lift the existing troposphere code from each `children/*.py` source, drop `InstallId` parameter declarations and `Sub` references, switch physical names to plain `cardinal-*` strings, switch tag emission to `cardinal_tags(component=..., managed_by="cardinal-infra-app-stack")`. Each module exposes an `add_to(template: Template) -> None` function called by `app/root.py`.

Inputs (parameters declared by `app/root.py`):

```
VpcId                         AWS::EC2::VPC::Id
PrivateSubnets                CommaDelimitedList
TaskSgId                      String
AlbSgId                       String
CertificateArn                String
LogRetentionDays              Number, default 30
TemplateBaseUrl               String, default <release URL>
```

Outputs (declared by `app/root.py`):

```
ClusterArn, ClusterName
AlbDnsName, AlbHostedZoneId
CertificateArn (pass-through)
QueryApiTargetGroupArn, AdminApiTargetGroupArn,
MaestroHttpsTargetGroupArn, MaestroDexTargetGroupArn,
OtelGrpcTargetGroupArn

QueryApiLogGroupName, QueryWorkerLogGroupName,
ProcessLogsLogGroupName, ProcessMetricsLogGroupName,
ProcessTracesLogGroupName, PubsubSqsLogGroupName,
SweeperLogGroupName, MonitoringLogGroupName,
AdminApiLogGroupName, AlertEvaluatorLogGroupName,
OtelCollectorLogGroupName, MaestroLogGroupName,
DexLogGroupName, MigratorLogGroupName

QueryApiCloudMapServiceArn, QueryWorkerCloudMapServiceArn, ...
(one per ECS service that needs service discovery)
```

- [ ] **Step 1: Lift cluster.py**

`src/cardinal_cfn/app/cluster.py`: define `add_to(template)` that creates `AWS::ECS::Cluster` with `ClusterName="cardinal"`, plus `AWS::ServiceDiscovery::PrivateDnsNamespace` named `cardinal.local` rooted in `VpcId`. Tag both with `cardinal_tags(component="cluster", managed_by="cardinal-infra-app-stack")`.

- [ ] **Step 2: Lift logs.py**

`src/cardinal_cfn/app/logs.py`: one `AWS::Logs::LogGroup` per `LakerunnerComponent` value, name `/cardinal/<service>`, retention from the `LogRetentionDays` parameter.

- [ ] **Step 3: Lift cloudmap.py**

`src/cardinal_cfn/app/cloudmap.py`: one `AWS::ServiceDiscovery::Service` per ECS service that needs DNS discovery, in the namespace from `cluster.py`.

- [ ] **Step 4: Lift alb.py**

`src/cardinal_cfn/app/alb.py`: ALB (`Scheme: internal`), 443 + 9443 listeners, target groups + listener rules per HTTP-fronted service. Listener-rule priorities consumed from `listener_priorities.LISTENER_RULE_PRIORITIES`. Health check paths follow the existing `children/alb.py` definitions.

- [ ] **Step 5: Wire up root.py**

`src/cardinal_cfn/app/root.py`:

```python
"""cardinal-infra-app root template generator."""

from troposphere import Template

from cardinal_cfn.app import cluster, logs, cloudmap, alb
from cardinal_cfn.parameters import add_parameter_group_metadata


VERSION = __import__("os").environ.get("CARDINAL_VERSION", "dev")


def build_template() -> Template:
    template = Template()
    template.set_description(f"Cardinal Lakerunner — application infrastructure (cluster, ALB, logs, cloud-map). version={VERSION}")
    template.set_metadata({"cardinal:version": VERSION})

    # parameters: VpcId, PrivateSubnets, TaskSgId, AlbSgId, CertificateArn,
    # LogRetentionDays, TemplateBaseUrl
    # ... (declare each via cardinal_cfn.parameters helpers)

    cluster.add_to(template)
    logs.add_to(template)
    cloudmap.add_to(template)
    alb.add_to(template)

    return template


if __name__ == "__main__":
    print(build_template().to_yaml())
```

- [ ] **Step 6: Test it**

`tests/templates/test_app_template.py`: cloud-radar-style assertions covering every public-surface invariant — every Output exists, every Parameter is declared, the cluster name is literally `"cardinal"`, the listener rule priorities match `LISTENER_RULE_PRIORITIES`, log group names match `/cardinal/<service>` per LakerunnerComponent. Use the existing per-template tests (`tests/templates/test_alb.py`, `test_cluster.py`, etc.) as the assertion library; collapse them into one file scoped to the new template.

- [ ] **Step 7: Run + commit**

Run: `.venv/bin/pytest tests/templates/test_app_template.py -v`

```bash
git add src/cardinal_cfn/app/ tests/templates/test_app_template.py
git commit -m "feat(app): cardinal-infra-app stack — cluster + ALB + logs + cloud-map"
```

---

## Task 5 — Lakerunner stack template

**Files:**

- Create: `src/cardinal_cfn/lakerunner/__init__.py`, `services_query.py`, `services_process.py`, `services_control.py`, `otel.py`, `maestro.py`, `migration.py`, `root.py`
- Create: `tests/templates/test_lakerunner_template.py`

Strategy: lift `children/services_query.py`, `children/services_process.py`, `children/services_control.py`, `children/otel.py`, `children/maestro.py`, `children/migration.py`, plus the relevant pieces of `children/services_common.py`. Each service module emits ECS task definition + ECS service. The migration module emits the migration Lambda + custom resource.

Inputs (parameters declared by `lakerunner/root.py`):

```
PrivateSubnets                CommaDelimitedList
TaskRoleArn, ExecutionRoleArn, MigrationLambdaRoleArn, TaskSgId
ClusterArn, ClusterName
DbEndpoint, DbPort, DbName, DbMasterSecretArn, MaestroDbSecretArn
IngestBucketName, IngestQueueUrl, IngestQueueArn
LicenseSecretArn, InternalKeysSecretArn, AdminKeySecretArn
StorageProfilesParamName, ApiKeysParamName

# Per-service: target-group ARN, log group name, cloud-map service ARN
QueryApiTargetGroupArn, QueryApiLogGroupName, QueryApiCloudMapServiceArn
... (each service)

# Sizing (carry forward existing parameters from the current root.py)
QueryApiReplicas, QueryApiCpu, QueryApiMemory, ...

# Images
LakerunnerImage, MaestroImage, OtelImage, DexImage
```

Each ECS task definition uses `TaskRoleArn` (single shared) for `TaskRoleArn=` and `ExecutionRoleArn` for `ExecutionRoleArn=`. The migration Lambda uses `MigrationLambdaRoleArn`. Service-discovery via `ServiceRegistries=[ServiceRegistry(RegistryArn=Ref('<Service>CloudMapServiceArn'))]`. Log driver `awslogs`, group from the matching parameter.

- [ ] **Step 1: Lift each service module**

Create one module per service group, each exposing `add_to(template)`. Lift task-definition + service definitions verbatim from the matching `children/*.py`, dropping all `InstallId` references and switching parameter wiring to the new shape (target group / log group / cloud-map ARNs come in as parameters instead of `Fn::ImportValue`).

- [ ] **Step 2: Migration custom resource**

`src/cardinal_cfn/lakerunner/migration.py`: one `AWS::Lambda::Function` (Python 3.11, inline code copied from `children/migration_lambda.py`), one `AWS::CloudFormation::CustomResource` with `MigrationVersion=Ref('LakerunnerImage')` so any image change triggers re-migration. The Lambda calls `ecs:RunTask` on the migrator task definition (family `cardinal-migrator`).

- [ ] **Step 3: Wire up root.py**

`src/cardinal_cfn/lakerunner/root.py`: parallel to `app/root.py`, declares the parameters, calls each module's `add_to(template)`, returns the template.

- [ ] **Step 4: Test it**

`tests/templates/test_lakerunner_template.py`: cloud-radar assertions — every parameter listed above is declared, every service's ECS task definition uses the parameter-supplied role ARNs, the migrator task definition family is literally `cardinal-migrator`, the migration custom resource's `MigrationVersion` is `Ref(LakerunnerImage)`.

- [ ] **Step 5: Run + commit**

Run: `.venv/bin/pytest tests/templates/test_lakerunner_template.py -v`

```bash
git add src/cardinal_cfn/lakerunner/ tests/templates/test_lakerunner_template.py
git commit -m "feat(lakerunner): cardinal-lakerunner stack — task defs + services + migration"
```

---

## Task 6 — Cross-cutting tests: naming contract, tagging, handoff

**Files:**

- Create: `tests/unit/test_naming_contract.py`
- Create: `tests/unit/test_tagging.py`
- Create: `tests/templates/test_app_lakerunner_handoff.py`

- [ ] **Step 1: Naming-contract test**

`tests/unit/test_naming_contract.py`: Asserts that the resource ARN patterns the prereqs script writes into IAM policies match the physical names declared in `app/root.py`, `lakerunner/root.py`, and the data-setup script renderer.

```python
from cardinal_cfn.prereqs.roles import expected_role_specs
from cardinal_cfn.app.root import build_template as build_app
from cardinal_cfn.lakerunner.root import build_template as build_lakerunner
from cardinal_cfn.data_setup.render import render_data_setup_script


def test_task_role_bucket_arn_matches_data_setup_bucket_name():
    [task_role] = [r for r in expected_role_specs(
        account_id="123", region="us-east-2",
        cluster_arn="arn:aws:ecs:us-east-2:123:cluster/cardinal",
    ) if r.name == "cardinal-task-role"]
    assert "arn:aws:s3:::cardinal-ingest-123-us-east-2" in task_role.inline_policy_json
    assert 'BUCKET_NAME="cardinal-ingest-${ACCOUNT_ID}-${REGION}"' in render_data_setup_script()


def test_task_role_queue_arn_matches_data_setup_queue_name():
    [task_role] = [r for r in expected_role_specs(
        account_id="123", region="us-east-2",
        cluster_arn="arn:aws:ecs:us-east-2:123:cluster/cardinal",
    ) if r.name == "cardinal-task-role"]
    assert "arn:aws:sqs:us-east-2:123:cardinal-ingest" in task_role.inline_policy_json
    assert 'ensure_sqs_queue cardinal-ingest' in render_data_setup_script()


def test_task_role_cluster_condition_matches_app_template_cluster_name():
    template_yaml = build_app().to_yaml()
    assert "ClusterName: cardinal\n" in template_yaml or 'ClusterName: "cardinal"' in template_yaml


def test_migration_lambda_runs_on_cardinal_migrator_family():
    [mig_role] = [r for r in expected_role_specs(
        account_id="123", region="us-east-2",
        cluster_arn="arn:aws:ecs:us-east-2:123:cluster/cardinal",
    ) if r.name == "cardinal-migration-lambda-role"]
    assert "task-definition/cardinal-migrator:*" in mig_role.inline_policy_json
    template_yaml = build_lakerunner().to_yaml()
    assert "Family: cardinal-migrator" in template_yaml


def test_log_group_prefix_matches_task_role_logs_arn():
    [task_role] = [r for r in expected_role_specs(
        account_id="123", region="us-east-2",
        cluster_arn="arn:aws:ecs:us-east-2:123:cluster/cardinal",
    ) if r.name == "cardinal-task-role"]
    assert "log-group:/cardinal/*" in task_role.inline_policy_json
    template_yaml = build_app().to_yaml()
    assert "LogGroupName: /cardinal/query-api" in template_yaml
```

- [ ] **Step 2: Tagging test**

`tests/unit/test_tagging.py`:

```python
import yaml

from cardinal_cfn.app.root import build_template as build_app
from cardinal_cfn.lakerunner.root import build_template as build_lakerunner
from cardinal_cfn.prereqs.render import render_prereqs_script
from cardinal_cfn.data_setup.render import render_data_setup_script


REQUIRED = {"Application", "Component", "ManagedBy", "Name"}


def _all_tag_sets(template_yaml: str) -> list[set[str]]:
    parsed = yaml.safe_load(template_yaml)
    sets: list[set[str]] = []
    for resource in parsed["Resources"].values():
        props = resource.get("Properties", {})
        tags = props.get("Tags")
        if isinstance(tags, list):
            sets.append({entry["Key"] for entry in tags if isinstance(entry, dict) and "Key" in entry})
    return sets


def test_app_template_resources_carry_required_tags():
    for tag_keys in _all_tag_sets(build_app().to_yaml()):
        assert REQUIRED <= tag_keys


def test_lakerunner_template_resources_carry_required_tags():
    for tag_keys in _all_tag_sets(build_lakerunner().to_yaml()):
        assert REQUIRED <= tag_keys


def test_prereqs_script_applies_required_tags():
    out = render_prereqs_script()
    for key in REQUIRED:
        assert f"Key={key}," in out


def test_data_setup_script_applies_required_tags():
    out = render_data_setup_script()
    for key in REQUIRED:
        assert f"Key={key}," in out


def test_managed_by_is_unique_per_layer():
    assert "ManagedBy=cardinal-prereqs-script" in render_prereqs_script()
    assert "ManagedBy=cardinal-data-setup-script" in render_data_setup_script()
    assert "cardinal-infra-app-stack" in build_app().to_yaml()
    assert "cardinal-lakerunner-stack" in build_lakerunner().to_yaml()
```

- [ ] **Step 3: Handoff test**

`tests/templates/test_app_lakerunner_handoff.py`: Asserts every Output of the app stack has a matching Parameter on the lakerunner stack.

```python
import yaml

from cardinal_cfn.app.root import build_template as build_app
from cardinal_cfn.lakerunner.root import build_template as build_lakerunner


def test_app_outputs_align_with_lakerunner_parameters():
    app_outputs = set(yaml.safe_load(build_app().to_yaml())["Outputs"].keys())
    lr_params = set(yaml.safe_load(build_lakerunner().to_yaml())["Parameters"].keys())
    # Every app output must have a matching lakerunner parameter.
    missing = app_outputs - lr_params
    assert not missing, f"app outputs not consumable by lakerunner: {missing}"
```

- [ ] **Step 4: Run + commit**

Run: `.venv/bin/pytest tests/unit/test_naming_contract.py tests/unit/test_tagging.py tests/templates/test_app_lakerunner_handoff.py -v`

```bash
git add tests/unit/test_naming_contract.py tests/unit/test_tagging.py tests/templates/test_app_lakerunner_handoff.py
git commit -m "test: naming contract + uniform tagging + app→lakerunner handoff"
```

---

## Task 7 — Operator scripts (deploy + teardown)

**Files:**

- Create: `scripts/deploy-cardinal-stack.sh`
- Create: `scripts/teardown-cardinal-stack.sh`
- Create: `tests/scripts/test_deploy_script.py`
- Delete: `scripts/deploy-lakerunner.sh`, `scripts/teardown-lakerunner.sh`, `jenkins/Jenkinsfile.lakerunner`

- [ ] **Step 1: Generalize deploy-lakerunner.sh into deploy-cardinal-stack.sh**

Lift the existing logic from `scripts/deploy-lakerunner.sh` (parameter resolution, change-set lifecycle, no-op classification, REVIEW_IN_PROGRESS / ROLLBACK_COMPLETE recovery). Add a `--kind app|lakerunner` flag that picks `template_url`. Drop all `--deployer-role-arn` plumbing and the `cfntool()` wrapper — call `aws cloudformation` directly. Take parameters as a single `--parameters-file path.json` containing a JSON array of `{ParameterKey, ParameterValue}` dicts.

For `--kind=lakerunner` UPDATE: image params auto-refresh to template defaults (existing `resolve_params` jq logic carries over).
For `--kind=app` UPDATE: every parameter uses previous value unless the parameters-file overrides it.

Always override `TemplateBaseUrl` with `<base>/<version>/`.

- [ ] **Step 2: Generalize teardown into teardown-cardinal-stack.sh**

Lift `scripts/teardown-lakerunner.sh` into `scripts/teardown-cardinal-stack.sh`. Drop the data-stack branch (data is shell-script-managed). Both `--kind=app` and `--kind=lakerunner` are straight `delete-stack + wait`.

- [ ] **Step 3: Lint test**

`tests/scripts/test_deploy_script.py`: lift `tests/unit/test_deploy_lakerunner_lint.py` and `tests/unit/test_deploy_lakerunner.py`, retarget to the new script. Drop assertions about `--role-arn` (since we no longer pass it). Keep assertions about no-op classification, REVIEW_IN_PROGRESS handling, and parameter resolution.

- [ ] **Step 4: Delete old scripts**

```bash
git rm scripts/deploy-lakerunner.sh scripts/teardown-lakerunner.sh \
       jenkins/Jenkinsfile.lakerunner \
       tests/unit/test_deploy_lakerunner.py \
       tests/unit/test_deploy_lakerunner_lint.py \
       tests/unit/test_teardown_lakerunner.py \
       tests/unit/test_teardown_lakerunner_lint.py \
       tests/unit/test_jenkinsfile_lakerunner.py
rmdir jenkins 2>/dev/null || true
```

- [ ] **Step 5: Run + commit**

Run: `.venv/bin/pytest tests/scripts/test_deploy_script.py -v`

```bash
git add scripts/deploy-cardinal-stack.sh scripts/teardown-cardinal-stack.sh \
        tests/scripts/test_deploy_script.py
git commit -m "feat(scripts): generic deploy-cardinal-stack + teardown-cardinal-stack"
```

---

## Task 8 — Build pipeline + release workflow

**Files:**

- Modify: `Makefile`, `build.sh`, `.github/workflows/release.yml`

- [ ] **Step 1: Update build.sh**

Replace the contents of `build.sh` with a version that runs:

```sh
python3 -m cardinal_cfn.cardinal_vpc           > generated-templates/cardinal-vpc.yaml
python3 -m cardinal_cfn.prereqs.render         > generated-templates/cardinal-prereqs.sh
python3 -m cardinal_cfn.data_setup.render      > generated-templates/cardinal-data-setup.sh
python3 -m cardinal_cfn.app.root               > generated-templates/cardinal-infra-app.yaml
python3 -m cardinal_cfn.lakerunner.root        > generated-templates/cardinal-lakerunner.yaml
chmod +x generated-templates/cardinal-prereqs.sh generated-templates/cardinal-data-setup.sh
cfn-lint generated-templates/cardinal-vpc.yaml \
         generated-templates/cardinal-infra-app.yaml \
         generated-templates/cardinal-lakerunner.yaml
shellcheck -s sh generated-templates/cardinal-prereqs.sh generated-templates/cardinal-data-setup.sh || true
```

Drop the `cardinal-deployer-role.yaml` and the children loop entirely.

- [ ] **Step 2: Update Makefile**

Replace `lint` target to glob `generated-templates/cardinal-{vpc,infra-app,lakerunner}.yaml`. Drop `test-jenkins` target. Update `clean` to remove `tests/scripts/__pycache__`.

- [ ] **Step 3: Update release.yml**

Update the publishing step to upload:

```
generated-templates/cardinal-vpc.yaml
generated-templates/cardinal-prereqs.sh
generated-templates/cardinal-data-setup.sh
generated-templates/cardinal-infra-app.yaml
generated-templates/cardinal-lakerunner.yaml
```

…to `s3://cardinal-cfn/lakerunner/<version>/`. Drop the `cardinal-lakerunner/` subdirectory upload.

- [ ] **Step 4: Test the build**

Run: `make clean && make build`
Expected: PASS, all 5 artifacts in `generated-templates/`.

- [ ] **Step 5: Run full test suite**

Run: `make test`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add Makefile build.sh .github/workflows/release.yml
git commit -m "build: generate prereqs/data-setup scripts + two flat templates"
```

---

## Task 9 — Documentation

**Files:**

- Create: `docs/operations/installing.md`
- Modify: `docs/operations/permissions-infrastructure.md`, `permissions-lakerunner.md`, `deploying.md`, `tearing-down.md`, `end-to-end-test-plan.md`, `README.md`, `README-BUILDING.md`
- Delete: `docs/operations/jenkins-deploy.md`

- [ ] **Step 1: Write `installing.md`**

The 5-step runbook from the spec's *Customer runbook* section, expanded with one-paragraph per step covering what to copy where. Include example invocations for each script and CFN call.

- [ ] **Step 2: Rewrite `permissions-infrastructure.md`**

Replace the existing IAM/SG/RDS/S3/Secrets/SSM tables with: (a) what the prereqs-script principal needs (full IAM + EC2 SG + STS GetCallerIdentity); (b) what the data-setup-script principal needs (RDS, S3, SQS, Secrets, SSM creates); (c) what the deployer principal needs (the trimmed list from the spec's *Deployer permissions* section).

- [ ] **Step 3: Rewrite `permissions-lakerunner.md`**

Replace the per-service-role table with the single `cardinal-task-role` definition. Document the trade-off (any task can read any `cardinal-*` secret). Note that the future per-service-role tightening is a parameter-surface expansion.

- [ ] **Step 4: Update `deploying.md` to point at `installing.md`**

Strip out the old per-stack instructions; leave a one-paragraph pointer.

- [ ] **Step 5: Rewrite `tearing-down.md`**

Layer-by-layer teardown: lakerunner stack → app stack → (data is left alone unless customer's IT explicitly tears it down) → prereqs are left alone. Document the manual cleanup commands for anyone who really wants a fresh slate.

- [ ] **Step 6: Rewrite `end-to-end-test-plan.md`**

The 5-step install + a 5-step upgrade + a 3-step partial-teardown matching the new layout.

- [ ] **Step 7: Update READMEs**

`README.md` and `README-BUILDING.md`: point at the new layout, drop nested-children references, drop `InstallId`.

- [ ] **Step 8: Delete `jenkins-deploy.md`**

```bash
git rm docs/operations/jenkins-deploy.md
```

- [ ] **Step 9: Commit**

```bash
git add docs/
git commit -m "docs: install runbook + rewritten permissions/deploy/teardown for new layout"
```

---

## Task 10 — Old code cleanup

**Files:**

- Delete: `src/cardinal_cfn/children/`, `src/cardinal_cfn/root.py`, `src/cardinal_cfn/cardinal_deployer.py` (if present)
- Delete: tests covering the deleted modules (listed in *Files deleted* up top)

- [ ] **Step 1: Delete old generators**

```bash
git rm -r src/cardinal_cfn/children/
git rm src/cardinal_cfn/root.py
git rm -f src/cardinal_cfn/cardinal_deployer.py
```

- [ ] **Step 2: Delete old tests**

```bash
git rm tests/templates/test_alb.py tests/templates/test_cardinal_deployer.py \
       tests/templates/test_cert.py tests/templates/test_cluster.py \
       tests/templates/test_config.py tests/templates/test_database.py \
       tests/templates/test_maestro.py tests/templates/test_migration.py \
       tests/templates/test_otel.py tests/templates/test_root_wiring.py \
       tests/templates/test_root.py tests/templates/test_services_control.py \
       tests/templates/test_services_process.py tests/templates/test_services_query.py \
       tests/templates/test_storage.py \
       tests/unit/test_cert_lambda.py tests/unit/test_migration_lambda.py \
       tests/unit/test_services_common.py
```

- [ ] **Step 3: Run full suite + lint**

Run: `make clean && make build && make test`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git commit -m "chore: drop old children/, old root, deployer-role, retired tests"
```

---

## Task 11 — Final verification

- [ ] **Step 1: Re-run end-to-end build + tests**

Run: `make clean && make build && make test`
Expected: PASS, build emits exactly:

```
generated-templates/cardinal-vpc.yaml
generated-templates/cardinal-prereqs.sh
generated-templates/cardinal-data-setup.sh
generated-templates/cardinal-infra-app.yaml
generated-templates/cardinal-lakerunner.yaml
```

- [ ] **Step 2: Codex review**

Hand the completed branch to Codex via `mcp__plugin_code-colaboration_codex__codex` with a prompt summarizing the changes and asking for the same three-question review (architectural breakage / false alarms / biggest remaining risk). Fold any architectural fixes into a follow-up commit on the same branch.

- [ ] **Step 3: PR**

Push the branch, open a PR titled `refactor: out-of-CFN prereqs/data setup + two flat CFN stacks`, body summarizing: spec link, scope cuts, breaking changes (template URLs and parameter shapes change; existing installs are not auto-migrated).
