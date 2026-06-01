# cardinal-satellite-infra-base Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the standalone `cardinal-satellite-infra-base` CloudFormation template — the per-source-account ingest primitive (raw S3 bucket + SQS queue + S3→SQS notification + a cross-account role the Lakerunner poller assumes).

**Architecture:** A new troposphere generator module `src/cardinal_cfn/satellite_infra_base.py` with a `build() -> Template` entry point and a `__main__` that prints YAML, mirroring `cardinal_infrastructure.py` / `lrdev_baseinfra.py`. It is a top-level (root) stack, not a nested child. It is wired into `build.sh` and the `Makefile`/`build.sh` cfn-lint lists, and covered by `tests/templates/test_satellite_infra_base.py` using the existing cloud-radar-free pattern (`json.loads(module.build().to_json())` + assertions).

**Tech Stack:** Python 3, troposphere, pytest, cfn-lint. No new dependencies.

This plan is the first of several from the design spec `docs/superpowers/specs/2026-05-31-multi-account-satellite-ingest-design.md`. It implements only `satellite-infra-base`. The `satellite-services`, `lakerunner-infra-base`, `lakerunner-infra-rds`, and `lakerunner-services` stacks are separate plans.

## Design anchors (from the spec)

- Pull model: nothing pushes to the Lakerunner account. The bucket notifies only its own in-account/in-region queue. The only cross-account relationship is the role's trust policy naming the Lakerunner principal.
- Cross-account auth is **AssumeRole**: this stack creates the role; the Lakerunner poller assumes it. Role permissions cover only this bucket (read + delete, because delete-source-after-processing is ON) and this queue (consume).
- Raw bucket is **ephemeral** → `DeletionPolicy: Delete` (deleting the stack reclaims it). The queue is `Delete` too.
- Naming: deployed stack name `cardinal-satellite-infra-base`; resources use CFN-generated physical names + `Name` tag, `cardinal-` prefix. The bucket has an optional explicit name (default `cardinal-otel-raw-<account>-<region>`).

## File Structure

- Create: `src/cardinal_cfn/satellite_infra_base.py` — the generator. One responsibility: emit the satellite-infra-base template. ~180 lines.
- Create: `tests/templates/test_satellite_infra_base.py` — per-template assertions.
- Modify: `build.sh` — add a generation line + add the new template to the cfn-lint list.
- Modify: `Makefile:38-44` — add the new template to the `lint` target's file list.

`generated-templates/` is gitignored; never commit generated YAML. Commit source, tests, `build.sh`, `Makefile`.

---

### Task 1: Module skeleton, parameters, and build wiring

**Files:**
- Create: `src/cardinal_cfn/satellite_infra_base.py`
- Create: `tests/templates/test_satellite_infra_base.py`
- Modify: `build.sh`
- Modify: `Makefile`

- [ ] **Step 1: Write the failing test**

Create `tests/templates/test_satellite_infra_base.py`:

```python
"""Tests for the cardinal-satellite-infra-base standalone template."""

import json

import pytest

from cardinal_cfn import satellite_infra_base


@pytest.fixture
def td():
    return json.loads(satellite_infra_base.build().to_json())


def test_required_parameters(td):
    for n in (
        "LakerunnerPrincipal",
        "ExternalId",
        "RawBucketName",
        "RawBucketLifecycleDays",
    ):
        assert n in td["Parameters"], f"missing parameter: {n}"


def test_description_mentions_pull_model(td):
    assert "pull" in td["Description"].lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/templates/test_satellite_infra_base.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'cardinal_cfn.satellite_infra_base'`

- [ ] **Step 3: Write minimal implementation**

Create `src/cardinal_cfn/satellite_infra_base.py`:

```python
"""cardinal-satellite-infra-base: per-source-account ingest primitive.

Standalone stack a source ("satellite") account deploys to expose its raw
OTEL telemetry to a Lakerunner install in another account, using a pull
model: an in-account/in-region raw bucket + SQS queue + S3->SQS
notification, plus a cross-account IAM role the Lakerunner poller assumes
to read/delete the raw objects and consume the queue.

Nothing here pushes to the Lakerunner account; the only cross-account
relationship is the role's trust policy naming the Lakerunner principal.
"""

from troposphere import (
    Equals,
    GetAtt,
    If,
    Not,
    Output,
    Parameter,
    Ref,
    Sub,
    Tags,
    Template,
)
from troposphere.iam import Policy, Role
from troposphere.s3 import (
    AbortIncompleteMultipartUpload,
    Bucket,
    BucketEncryption,
    LifecycleConfiguration,
    LifecycleRule,
    NotificationConfiguration,
    PublicAccessBlockConfiguration,
    QueueConfigurations,
    ServerSideEncryptionByDefault,
    ServerSideEncryptionRule,
)
from troposphere.sqs import Queue, QueuePolicy

APPLICATION = "cardinal-lakerunner"
PROJECT = "cardinal"
MANAGED_BY = "cardinal-cfn"


def _tags(*, component: str) -> Tags:
    return Tags(
        Application=APPLICATION,
        Project=PROJECT,
        ManagedBy=MANAGED_BY,
        Component=component,
        Name=f"cardinal-{component}",
    )


def _delete(resource):
    resource.DeletionPolicy = "Delete"
    resource.UpdateReplacePolicy = "Delete"
    return resource


def build() -> Template:
    t = Template()
    t.set_description(
        "Cardinal satellite infra base: per-source-account raw ingest bucket, "
        "SQS queue, S3->SQS notification, and the cross-account role the "
        "Lakerunner poller assumes. Pull model; nothing pushes to Lakerunner."
    )

    t.add_parameter(
        Parameter(
            "LakerunnerPrincipal",
            Type="String",
            Description=(
                "ARN of the Lakerunner principal allowed to assume the access "
                "role (the poller role ARN, or the Lakerunner account root ARN "
                "arn:aws:iam::<acct>:root)."
            ),
            AllowedPattern=r"^arn:aws[a-zA-Z-]*:iam::\d{12}:(root|role/.+)$",
        )
    )

    t.add_parameter(
        Parameter(
            "ExternalId",
            Type="String",
            Default="",
            Description=(
                "Optional sts:ExternalId required on AssumeRole "
                "(confused-deputy mitigation). Blank disables the check."
            ),
        )
    )

    t.add_parameter(
        Parameter(
            "RawBucketName",
            Type="String",
            Default="",
            Description=(
                "Name for the raw ingest bucket. Blank uses the default "
                "cardinal-otel-raw-<account>-<region>."
            ),
        )
    )

    t.add_parameter(
        Parameter(
            "RawBucketLifecycleDays",
            Type="Number",
            Default=7,
            MinValue=1,
            Description=(
                "Days after which raw objects expire. Raw is ephemeral "
                "(Lakerunner deletes after processing); this bounds orphans."
            ),
        )
    )

    return t


if __name__ == "__main__":
    print(build().to_yaml(), end="")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/templates/test_satellite_infra_base.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Wire into build.sh**

In `build.sh`, after the `cardinal-cleanup.yaml` generation block (the line `python3 -m cardinal_cfn.cardinal_cleanup > generated-templates/cardinal-cleanup.yaml`), add:

```sh
echo "Generating cardinal-satellite-infra-base.yaml..."
python3 -m cardinal_cfn.satellite_infra_base > generated-templates/cardinal-satellite-infra-base.yaml
```

Then add the new file to the `cfn-lint` invocation near the bottom of `build.sh` — change the lint block to include it:

```sh
cfn-lint generated-templates/lrdev-vpc.yaml \
         generated-templates/lrdev-baseinfra.yaml \
         generated-templates/cardinal-infrastructure.yaml \
         generated-templates/cardinal-cleanup.yaml \
         generated-templates/cardinal-satellite-infra-base.yaml \
         generated-templates/cardinal-lakerunner.yaml \
         generated-templates/cardinal-lakerunner/*.yaml || \
  echo "cfn-lint completed with warnings"
```

- [ ] **Step 6: Wire into Makefile lint target**

In `Makefile`, in the `lint:` target (lines 38-44), add the new template to the `cfn-lint` file list:

```makefile
lint:	## Run cfn-lint on every generated template
	source $(VENV_DIR)/bin/activate && cfn-lint \
	  generated-templates/lrdev-vpc.yaml \
	  generated-templates/lrdev-baseinfra.yaml \
	  generated-templates/cardinal-infrastructure.yaml \
	  generated-templates/cardinal-cleanup.yaml \
	  generated-templates/cardinal-satellite-infra-base.yaml \
	  generated-templates/cardinal-lakerunner.yaml \
	  generated-templates/cardinal-lakerunner/*.yaml
```

- [ ] **Step 7: Generate and confirm it builds**

Run: `PYTHONPATH=src .venv/bin/python -m cardinal_cfn.satellite_infra_base | head -20`
Expected: YAML beginning with `Description: 'Cardinal satellite infra base:` and a `Parameters:` section.

- [ ] **Step 8: Commit**

```bash
git add src/cardinal_cfn/satellite_infra_base.py tests/templates/test_satellite_infra_base.py build.sh Makefile
git commit -m "feat(satellite): scaffold cardinal-satellite-infra-base generator + params"
```

---

### Task 2: SQS queue + S3 source policy

**Files:**
- Modify: `src/cardinal_cfn/satellite_infra_base.py`
- Test: `tests/templates/test_satellite_infra_base.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/templates/test_satellite_infra_base.py`:

```python
def test_queue_is_delete_policy(td):
    q = td["Resources"]["RawIngestQueue"]
    assert q["DeletionPolicy"] == "Delete"
    assert q["UpdateReplacePolicy"] == "Delete"


def test_queue_policy_allows_s3_same_account_only(td):
    stmt = td["Resources"]["RawIngestQueuePolicy"]["Properties"][
        "PolicyDocument"
    ]["Statement"][0]
    assert stmt["Principal"] == {"Service": "s3.amazonaws.com"}
    assert "sqs:SendMessage" in stmt["Action"]
    assert stmt["Condition"]["StringEquals"]["aws:SourceAccount"] == {
        "Ref": "AWS::AccountId"
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/templates/test_satellite_infra_base.py -v`
Expected: FAIL with `KeyError: 'RawIngestQueue'`

- [ ] **Step 3: Write minimal implementation**

In `src/cardinal_cfn/satellite_infra_base.py`, inside `build()`, immediately before `return t`, add the bucket-name helper, conditions, queue, and queue policy:

```python
    t.add_condition("UseDefaultBucketName", Equals(Ref("RawBucketName"), ""))
    t.add_condition("HasExternalId", Not(Equals(Ref("ExternalId"), "")))

    bucket_name_value = If(
        "UseDefaultBucketName",
        Sub("cardinal-otel-raw-${AWS::AccountId}-${AWS::Region}"),
        Ref("RawBucketName"),
    )

    queue = t.add_resource(
        _delete(Queue("RawIngestQueue", Tags=_tags(component="otel-raw-queue")))
    )

    t.add_resource(
        QueuePolicy(
            "RawIngestQueuePolicy",
            Queues=[Ref(queue)],
            PolicyDocument={
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {"Service": "s3.amazonaws.com"},
                        "Action": [
                            "sqs:SendMessage",
                            "sqs:GetQueueAttributes",
                            "sqs:GetQueueUrl",
                        ],
                        "Resource": GetAtt(queue, "Arn"),
                        "Condition": {
                            "StringEquals": {
                                "aws:SourceAccount": Ref("AWS::AccountId")
                            },
                            "ArnLike": {
                                "aws:SourceArn": Sub(
                                    "arn:${AWS::Partition}:s3:::${BucketName}",
                                    BucketName=bucket_name_value,
                                )
                            },
                        },
                    }
                ],
            },
        )
    )
```

Note: `bucket_name_value` and `queue` are referenced by later tasks (Task 3 bucket, Task 4 role, Task 5 outputs). Keep them as locals in `build()`.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/templates/test_satellite_infra_base.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add src/cardinal_cfn/satellite_infra_base.py tests/templates/test_satellite_infra_base.py
git commit -m "feat(satellite): SQS ingest queue + S3 source queue policy"
```

---

### Task 3: S3 raw bucket (encryption, public-access-block, lifecycle, notification)

**Files:**
- Modify: `src/cardinal_cfn/satellite_infra_base.py`
- Test: `tests/templates/test_satellite_infra_base.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/templates/test_satellite_infra_base.py`:

```python
def test_bucket_is_delete_policy(td):
    b = td["Resources"]["RawIngestBucket"]
    assert b["DeletionPolicy"] == "Delete"
    assert b["UpdateReplacePolicy"] == "Delete"


def test_bucket_blocks_public_access(td):
    pab = td["Resources"]["RawIngestBucket"]["Properties"][
        "PublicAccessBlockConfiguration"
    ]
    assert pab == {
        "BlockPublicAcls": True,
        "BlockPublicPolicy": True,
        "IgnorePublicAcls": True,
        "RestrictPublicBuckets": True,
    }


def test_bucket_is_encrypted(td):
    enc = td["Resources"]["RawIngestBucket"]["Properties"]["BucketEncryption"]
    rule = enc["ServerSideEncryptionConfiguration"][0]
    assert rule["ServerSideEncryptionByDefault"]["SSEAlgorithm"] == "AES256"


def test_bucket_notifies_its_own_queue(td):
    qcfg = td["Resources"]["RawIngestBucket"]["Properties"][
        "NotificationConfiguration"
    ]["QueueConfigurations"][0]
    assert qcfg["Event"] == "s3:ObjectCreated:*"
    assert qcfg["Queue"] == {"Fn::GetAtt": ["RawIngestQueue", "Arn"]}


def test_bucket_depends_on_queue_policy(td):
    assert td["Resources"]["RawIngestBucket"]["DependsOn"] == "RawIngestQueuePolicy"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/templates/test_satellite_infra_base.py -v`
Expected: FAIL with `KeyError: 'RawIngestBucket'`

- [ ] **Step 3: Write minimal implementation**

In `src/cardinal_cfn/satellite_infra_base.py`, inside `build()`, immediately after the `RawIngestQueuePolicy` resource and before `return t`, add:

```python
    t.add_resource(
        _delete(
            Bucket(
                "RawIngestBucket",
                # S3 validates the SQS notification target when the bucket's
                # notification config is applied and fails if the queue policy
                # is not yet in place, so the bucket is created after it.
                DependsOn="RawIngestQueuePolicy",
                BucketName=bucket_name_value,
                PublicAccessBlockConfiguration=PublicAccessBlockConfiguration(
                    BlockPublicAcls=True,
                    BlockPublicPolicy=True,
                    IgnorePublicAcls=True,
                    RestrictPublicBuckets=True,
                ),
                BucketEncryption=BucketEncryption(
                    ServerSideEncryptionConfiguration=[
                        ServerSideEncryptionRule(
                            ServerSideEncryptionByDefault=(
                                ServerSideEncryptionByDefault(
                                    SSEAlgorithm="AES256"
                                )
                            )
                        )
                    ]
                ),
                LifecycleConfiguration=LifecycleConfiguration(
                    Rules=[
                        LifecycleRule(
                            Id="cardinal-otel-raw-expire",
                            Status="Enabled",
                            Prefix="",
                            ExpirationInDays=Ref("RawBucketLifecycleDays"),
                            AbortIncompleteMultipartUpload=(
                                AbortIncompleteMultipartUpload(
                                    DaysAfterInitiation=1
                                )
                            ),
                        )
                    ]
                ),
                NotificationConfiguration=NotificationConfiguration(
                    QueueConfigurations=[
                        QueueConfigurations(
                            Event="s3:ObjectCreated:*",
                            Queue=GetAtt(queue, "Arn"),
                        )
                    ]
                ),
                Tags=_tags(component="otel-raw-bucket"),
            )
        )
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/templates/test_satellite_infra_base.py -v`
Expected: PASS (9 passed)

- [ ] **Step 5: Commit**

```bash
git add src/cardinal_cfn/satellite_infra_base.py tests/templates/test_satellite_infra_base.py
git commit -m "feat(satellite): ephemeral raw S3 bucket with encryption, PAB, lifecycle, notification"
```

---

### Task 4: Cross-account access role (AssumeRole; read+delete S3, consume SQS)

**Files:**
- Modify: `src/cardinal_cfn/satellite_infra_base.py`
- Test: `tests/templates/test_satellite_infra_base.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/templates/test_satellite_infra_base.py`:

```python
def test_role_trusts_lakerunner_principal(td):
    trust = td["Resources"]["LakerunnerAccessRole"]["Properties"][
        "AssumeRolePolicyDocument"
    ]["Statement"][0]
    assert trust["Principal"] == {"AWS": {"Ref": "LakerunnerPrincipal"}}
    assert trust["Action"] == "sts:AssumeRole"


def test_role_external_id_is_conditional(td):
    trust = td["Resources"]["LakerunnerAccessRole"]["Properties"][
        "AssumeRolePolicyDocument"
    ]["Statement"][0]
    assert trust["Condition"] == {
        "Fn::If": [
            "HasExternalId",
            {"StringEquals": {"sts:ExternalId": {"Ref": "ExternalId"}}},
            {"Ref": "AWS::NoValue"},
        ]
    }


def test_role_can_read_and_delete_raw(td):
    stmts = td["Resources"]["LakerunnerAccessRole"]["Properties"]["Policies"][
        0
    ]["PolicyDocument"]["Statement"]
    s3 = next(s for s in stmts if s["Sid"] == "RawBucketReadDelete")
    assert {"s3:GetObject", "s3:DeleteObject", "s3:ListBucket"}.issubset(
        set(s3["Action"])
    )


def test_role_can_consume_only_its_queue(td):
    stmts = td["Resources"]["LakerunnerAccessRole"]["Properties"]["Policies"][
        0
    ]["PolicyDocument"]["Statement"]
    sqs = next(s for s in stmts if s["Sid"] == "RawQueueConsume")
    assert sqs["Resource"] == {"Fn::GetAtt": ["RawIngestQueue", "Arn"]}
    assert "sqs:ReceiveMessage" in sqs["Action"]
    assert "sqs:DeleteMessage" in sqs["Action"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/templates/test_satellite_infra_base.py -v`
Expected: FAIL with `KeyError: 'LakerunnerAccessRole'`

- [ ] **Step 3: Write minimal implementation**

In `src/cardinal_cfn/satellite_infra_base.py`, inside `build()`, immediately after the `RawIngestBucket` resource and before `return t`, add:

```python
    t.add_resource(
        Role(
            "LakerunnerAccessRole",
            AssumeRolePolicyDocument={
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {"AWS": Ref("LakerunnerPrincipal")},
                        "Action": "sts:AssumeRole",
                        "Condition": If(
                            "HasExternalId",
                            {
                                "StringEquals": {
                                    "sts:ExternalId": Ref("ExternalId")
                                }
                            },
                            Ref("AWS::NoValue"),
                        ),
                    }
                ],
            },
            Policies=[
                Policy(
                    PolicyName="cardinal-satellite-access",
                    PolicyDocument={
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Sid": "RawBucketReadDelete",
                                "Effect": "Allow",
                                "Action": [
                                    "s3:GetObject",
                                    "s3:DeleteObject",
                                    "s3:ListBucket",
                                    "s3:GetBucketLocation",
                                ],
                                "Resource": [
                                    Sub(
                                        "arn:${AWS::Partition}:s3:::"
                                        "${BucketName}",
                                        BucketName=bucket_name_value,
                                    ),
                                    Sub(
                                        "arn:${AWS::Partition}:s3:::"
                                        "${BucketName}/*",
                                        BucketName=bucket_name_value,
                                    ),
                                ],
                            },
                            {
                                "Sid": "RawQueueConsume",
                                "Effect": "Allow",
                                "Action": [
                                    "sqs:ReceiveMessage",
                                    "sqs:DeleteMessage",
                                    "sqs:GetQueueAttributes",
                                    "sqs:GetQueueUrl",
                                    "sqs:ChangeMessageVisibility",
                                ],
                                "Resource": GetAtt(queue, "Arn"),
                            },
                        ],
                    },
                )
            ],
            Tags=_tags(component="satellite-access-role"),
        )
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/templates/test_satellite_infra_base.py -v`
Expected: PASS (13 passed)

- [ ] **Step 5: Commit**

```bash
git add src/cardinal_cfn/satellite_infra_base.py tests/templates/test_satellite_infra_base.py
git commit -m "feat(satellite): cross-account AssumeRole scoped to this bucket+queue"
```

---

### Task 5: Outputs + pull-model invariant test

**Files:**
- Modify: `src/cardinal_cfn/satellite_infra_base.py`
- Test: `tests/templates/test_satellite_infra_base.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/templates/test_satellite_infra_base.py`:

```python
def test_outputs_present(td):
    for o in (
        "RawBucketName",
        "RawQueueUrl",
        "RawQueueArn",
        "LakerunnerAccessRoleArn",
        "Region",
    ):
        assert o in td["Outputs"], f"missing output: {o}"


def test_pull_model_no_remote_notification_target(td):
    """Pull invariant: the bucket notifies only its own in-stack queue;
    no resource targets a remote/central queue, and there is no outbound
    push to the Lakerunner account."""
    qcfg = td["Resources"]["RawIngestBucket"]["Properties"][
        "NotificationConfiguration"
    ]["QueueConfigurations"]
    assert all(
        c["Queue"] == {"Fn::GetAtt": ["RawIngestQueue", "Arn"]} for c in qcfg
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/templates/test_satellite_infra_base.py -v`
Expected: FAIL with `KeyError: 'RawBucketName'` (in `td["Outputs"]`)

- [ ] **Step 3: Write minimal implementation**

In `src/cardinal_cfn/satellite_infra_base.py`, inside `build()`, immediately after the `LakerunnerAccessRole` resource and before `return t`, add:

```python
    t.add_output(
        Output(
            "RawBucketName",
            Description="Raw ingest bucket name.",
            Value=Ref("RawIngestBucket"),
        )
    )
    t.add_output(
        Output(
            "RawQueueUrl",
            Description="Raw ingest SQS queue URL.",
            Value=Ref(queue),
        )
    )
    t.add_output(
        Output(
            "RawQueueArn",
            Description="Raw ingest SQS queue ARN.",
            Value=GetAtt(queue, "Arn"),
        )
    )
    t.add_output(
        Output(
            "LakerunnerAccessRoleArn",
            Description="ARN of the role the Lakerunner poller assumes.",
            Value=GetAtt("LakerunnerAccessRole", "Arn"),
        )
    )
    t.add_output(
        Output(
            "Region",
            Description="Region of this satellite's bucket/queue.",
            Value=Ref("AWS::Region"),
        )
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/templates/test_satellite_infra_base.py -v`
Expected: PASS (15 passed)

- [ ] **Step 5: Commit**

```bash
git add src/cardinal_cfn/satellite_infra_base.py tests/templates/test_satellite_infra_base.py
git commit -m "feat(satellite): stack outputs + pull-model invariant test"
```

---

### Task 6: Full build, cfn-lint, and suite green

**Files:** none (verification + fixups only)

- [ ] **Step 1: Generate the template and lint it**

Run:
```bash
PYTHONPATH=src .venv/bin/python -m cardinal_cfn.satellite_infra_base > /tmp/satellite-infra-base.yaml
.venv/bin/cfn-lint /tmp/satellite-infra-base.yaml
```
Expected: no errors. Warnings are tolerable if explainable (see `.cfnlintrc`). If cfn-lint reports an error (exit non-zero), fix the generator and re-run before continuing.

- [ ] **Step 2: Run the full build**

Run: `make build`
Expected: prints `Generating cardinal-satellite-infra-base.yaml...` among the others and ends with the lint summary; `generated-templates/cardinal-satellite-infra-base.yaml` exists.

- [ ] **Step 3: Run the full test suite**

Run: `make test`
Expected: all tests pass, including `tests/templates/test_satellite_infra_base.py` (15 passed there).

- [ ] **Step 4: Commit any fixups**

If Steps 1-3 required generator changes:
```bash
git add src/cardinal_cfn/satellite_infra_base.py
git commit -m "fix(satellite): cfn-lint and build fixups"
```
If nothing changed, skip this commit.

---

## Self-Review (completed during planning)

- **Spec coverage:** This plan covers only the `satellite-infra-base` row of the spec's stack table plus the pull-model and AssumeRole decisions as they apply to that stack. Raw bucket ephemerality (`Delete`), read+delete S3, consume SQS, in-account notification, and the trust-policy-only cross-account relationship are each implemented and tested. The collector/ALB (`satellite-services`), the lakerunner-account stacks, and the registration mechanism are explicitly out of scope here (separate plans / deferred per spec).
- **Placeholder scan:** none — every step contains full code or an exact command.
- **Type consistency:** logical IDs are stable across tasks (`RawIngestQueue`, `RawIngestQueuePolicy`, `RawIngestBucket`, `LakerunnerAccessRole`); `bucket_name_value` and `queue` are introduced in Task 2 and reused in Tasks 3-5; parameter names (`LakerunnerPrincipal`, `ExternalId`, `RawBucketName`, `RawBucketLifecycleDays`) match between the generator, the conditions, and the tests.

## Notes for the next plans (not this one)

- `satellite-services` will consume this stack's `RawBucketName` output (the collector writes there) and stand up the collector behind an ALB; its scheme + auth is the open security decision from the spec.
- The Lakerunner-account poller role (in `lakerunner-infra-base`) is the principal passed as `LakerunnerPrincipal` here, and needs `sts:AssumeRole` on this stack's `LakerunnerAccessRoleArn`.
