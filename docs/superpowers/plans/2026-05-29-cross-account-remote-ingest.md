# Cross-account remote ingest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a second AWS account's otel collector feed telemetry into an existing lakerunner install by writing (via an assumed role) to an S3 bucket the main account owns, which notifies lakerunner's SQS queue.

**Architecture:** A new standalone main-account template (`cardinal-remote-ingest.yaml`) creates the bucket, a cross-account writer IAM role, and the bucket->SQS notification. A new standalone remote-account template (`cardinal-remote-collector.yaml`) creates an ALB-fronted otel collector whose `awss3` exporter assumes the writer role. Small edits to `cardinal_infrastructure.py` broaden the SQS queue policy to accept `cardinal-remote-ingest-*` buckets and add an `AdditionalStorageProfilesYaml` parameter for registering the new org.

**Tech Stack:** Python 3, troposphere, cloud-radar (template tests), pytest, cfn-lint.

**Spec:** `docs/superpowers/specs/2026-05-29-cross-account-remote-ingest-design.md`

---

## File structure

```
cardinal-remote-otel-config.yaml             (new) otel config with role_arn per awss3 exporter
src/cardinal_cfn/defaults.py                 (modify) add load_remote_otel_default_config()
src/cardinal_cfn/cardinal_infrastructure.py  (modify) queue policy 2nd statement + AdditionalStorageProfilesYaml
src/cardinal_cfn/remote_ingest.py            (new) cardinal-remote-ingest.yaml generator
src/cardinal_cfn/remote_collector.py         (new) cardinal-remote-collector.yaml generator
build.sh                                     (modify) generate + lint the two new templates
Makefile                                     (modify) lint target
tests/unit/test_remote_otel_config.py        (new)
tests/templates/test_cardinal_infrastructure.py (modify) queue policy + new param
tests/templates/test_remote_ingest.py        (new)
tests/templates/test_remote_collector.py     (new)
```

---

## Task 1: Remote otel config file + loader

**Files:**
- Create: `cardinal-remote-otel-config.yaml`
- Modify: `src/cardinal_cfn/defaults.py`
- Test: `tests/unit/test_remote_otel_config.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_remote_otel_config.py
"""The remote collector config must carry role_arn on every awss3 exporter."""

import yaml

from cardinal_cfn.defaults import load_remote_otel_default_config


def test_loads_nonempty_string():
    cfg = load_remote_otel_default_config()
    assert isinstance(cfg, str) and cfg.strip()


def test_every_awss3_exporter_has_role_arn():
    cfg = yaml.safe_load(load_remote_otel_default_config())
    exporters = cfg["exporters"]
    awss3 = {k: v for k, v in exporters.items() if k.startswith("awss3")}
    assert awss3, "expected at least one awss3 exporter"
    for name, ex in awss3.items():
        assert ex.get("role_arn") == "${env:LRDB_S3_ROLE_ARN}", (
            f"{name} missing role_arn assume-role hook"
        )


def test_keeps_health_check_extension():
    cfg = yaml.safe_load(load_remote_otel_default_config())
    assert "health_check" in cfg["extensions"]
    assert cfg["extensions"]["health_check"]["endpoint"] == "0.0.0.0:13133"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_remote_otel_config.py -v`
Expected: FAIL — `ImportError: cannot import name 'load_remote_otel_default_config'`

- [ ] **Step 3: Create the config file**

Copy `cardinal-otel-config.yaml` to `cardinal-remote-otel-config.yaml`, change the header comment to describe the remote/assume-role variant, and add `role_arn: ${env:LRDB_S3_ROLE_ARN}` to each of the three `awss3/*` exporters (sibling of `s3uploader` and `marshaler`). Full file:

```yaml
# Remote-account cardinalhq-otel-collector config. Identical to
# cardinal-otel-config.yaml except each awss3 exporter assumes the
# cross-account writer role (LRDB_S3_ROLE_ARN) before writing to the
# main-account ingest bucket. Used by cardinal-remote-collector.yaml.
#
# Env-var contract (set by the remote collector stack):
#   LRDB_S3_BUCKET    - main-account remote-ingest bucket
#   LRDB_S3_REGION    - bucket region (the main/lakerunner region, NOT the
#                       remote account's region)
#   LRDB_S3_ROLE_ARN  - writer role ARN to assume for the cross-account write
#   ORG               - organization UUID for the s3_prefix path
#   COLLECTOR         - collector name (defaults to "lakerunner")

receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317
      http:
        endpoint: 0.0.0.0:4318

processors:
  memory_limiter:
    limit_mib: 1024
    check_interval: 1s
  batch:
    timeout: 10s
    send_batch_max_size: 2000
    send_batch_size: 1000

exporters:
  awss3/logs:
    s3uploader:
      region: ${env:LRDB_S3_REGION}
      s3_bucket: ${env:LRDB_S3_BUCKET}
      s3_prefix: otel-raw/${env:ORG}/${env:COLLECTOR}
      s3_force_path_style: true
      s3_partition_format: 'year=%Y/month=%m/day=%d/hour=%H/minute=%M'
      compression: gzip
    role_arn: ${env:LRDB_S3_ROLE_ARN}
    marshaler: otlp_proto

  awss3/metrics:
    s3uploader:
      region: ${env:LRDB_S3_REGION}
      s3_bucket: ${env:LRDB_S3_BUCKET}
      s3_prefix: otel-raw/${env:ORG}/${env:COLLECTOR}
      s3_force_path_style: true
      s3_partition_format: 'year=%Y/month=%m/day=%d/hour=%H/minute=%M'
      compression: gzip
    role_arn: ${env:LRDB_S3_ROLE_ARN}
    marshaler: otlp_proto

  awss3/traces:
    s3uploader:
      region: ${env:LRDB_S3_REGION}
      s3_bucket: ${env:LRDB_S3_BUCKET}
      s3_prefix: otel-raw/${env:ORG}/${env:COLLECTOR}
      s3_force_path_style: true
      s3_partition_format: 'year=%Y/month=%m/day=%d/hour=%H/minute=%M'
      compression: gzip
    role_arn: ${env:LRDB_S3_ROLE_ARN}
    marshaler: otlp_proto

extensions:
  health_check:
    endpoint: 0.0.0.0:13133

service:
  extensions:
    - health_check
  pipelines:
    logs:
      receivers: [otlp]
      processors: [memory_limiter, batch]
      exporters: [awss3/logs]
    metrics:
      receivers: [otlp]
      processors: [memory_limiter, batch]
      exporters: [awss3/metrics]
    traces:
      receivers: [otlp]
      processors: [memory_limiter, batch]
      exporters: [awss3/traces]
```

- [ ] **Step 4: Add the loader to `defaults.py`**

Add the remote-config path constant next to `_OTEL_CONFIG_PATH`:

```python
_REMOTE_OTEL_CONFIG_PATH = os.path.join(_REPO_ROOT, "cardinal-remote-otel-config.yaml")
```

Add the function after `load_otel_default_config`:

```python
def load_remote_otel_default_config() -> str:
    """Return cardinal-remote-otel-config.yaml as a string.

    Same shape as load_otel_default_config but with role_arn on each awss3
    exporter so the remote collector assumes the cross-account writer role.
    """
    with open(_REMOTE_OTEL_CONFIG_PATH, "r") as f:
        return f.read()
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_remote_otel_config.py -v`
Expected: PASS (3 passed)

- [ ] **Step 6: Commit**

```bash
git add cardinal-remote-otel-config.yaml src/cardinal_cfn/defaults.py tests/unit/test_remote_otel_config.py
git commit -m "feat: remote otel config with cross-account assume-role"
```

---

## Task 2: Infra queue policy + AdditionalStorageProfilesYaml

**Files:**
- Modify: `src/cardinal_cfn/cardinal_infrastructure.py` (queue policy ~360-388; storage profiles param ~209 and ~573-601; param-group metadata ~288/316)
- Test: `tests/templates/test_cardinal_infrastructure.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/templates/test_cardinal_infrastructure.py` (it already has a `td` fixture loading `cardinal_infrastructure.build().to_json()`; if not, add one mirroring `test_otel.py`):

```python
def test_queue_policy_allows_remote_ingest_buckets(td):
    """A second statement lets cardinal-remote-ingest-* buckets in this account
    notify the queue, without naming each bucket."""
    qp = next(
        r for r in td["Resources"].values()
        if r["Type"] == "AWS::SQS::QueuePolicy"
    )
    stmts = qp["Properties"]["PolicyDocument"]["Statement"]
    arnlikes = [
        s["Condition"]["ArnLike"]["aws:SourceArn"]
        for s in stmts
        if "Condition" in s and "ArnLike" in s["Condition"]
    ]
    assert {"Fn::Sub": "arn:${AWS::Partition}:s3:::cardinal-remote-ingest-*"} in arnlikes
    for s in stmts:
        assert s["Condition"]["StringEquals"]["aws:SourceAccount"] == {"Ref": "AWS::AccountId"}


def test_additional_storage_profiles_parameter(td):
    assert "AdditionalStorageProfilesYaml" in td["Parameters"]
    assert td["Parameters"]["AdditionalStorageProfilesYaml"]["Default"] == ""


def test_storage_profiles_value_appends_additional(td):
    ssm = next(
        r for r in td["Resources"].values()
        if r["Type"] == "AWS::SSM::Parameter" and r["Properties"].get("Name", {})
    )
    # Find the storage-profiles SSM param specifically.
    ssm_params = [
        r for r in td["Resources"].values()
        if r["Type"] == "AWS::SSM::Parameter"
    ]
    sp = next(
        r for r in ssm_params
        if "storage_profiles" in json.dumps(r["Properties"]["Value"])
        or "organization_id" in json.dumps(r["Properties"]["Value"])
    )
    assert "AdditionalStorageProfilesYaml" in json.dumps(sp["Properties"]["Value"])
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/templates/test_cardinal_infrastructure.py -k "remote_ingest or additional_storage or appends_additional" -v`
Expected: FAIL — the assertions don't find the new statement/param.

- [ ] **Step 3: Add the second queue-policy statement**

In `cardinal_infrastructure.py`, the `IngestQueuePolicy` resource (~357-388) currently has one statement in `PolicyDocument["Statement"]`. Add a second statement to that list, after the existing one:

```python
                    {
                        "Effect": "Allow",
                        "Principal": {"Service": "s3.amazonaws.com"},
                        "Action": [
                            "sqs:SendMessage",
                            "sqs:GetQueueAttributes",
                            "sqs:GetQueueUrl",
                        ],
                        "Resource": GetAtt(ingest_queue, "Arn"),
                        "Condition": {
                            "StringEquals": {
                                "aws:SourceAccount": Ref("AWS::AccountId")
                            },
                            "ArnLike": {
                                "aws:SourceArn": Sub(
                                    "arn:${AWS::Partition}:s3:::cardinal-remote-ingest-*"
                                )
                            },
                        },
                    },
```

- [ ] **Step 4: Add the `AdditionalStorageProfilesYaml` parameter**

Near the other parameters (after `storage_profiles_param_name`, ~209-230), add:

```python
    additional_storage_profiles = t.add_parameter(
        Parameter(
            "AdditionalStorageProfilesYaml",
            Type="String",
            Default="",
            Description=(
                "Extra storage-profile YAML list items appended after the seeded "
                "profile (e.g. cardinal-remote-ingest StorageProfileSnippet "
                "outputs). Leave blank for none."
            ),
        )
    )
```

- [ ] **Step 5: Append the parameter to the storage-profiles SSM value**

In the `StorageProfilesParam` resource (~573-601), change the `Value=Sub(...)` so the additional YAML is appended after the seeded profile. The seeded template string ends with `"  use_path_style: true\n"`; append `"${Additional}"` and pass it:

```python
                Value=Sub(
                    "- organization_id: ${OrgId}\n"
                    "  instance_num: 1\n"
                    "  collector_name: lakerunner\n"
                    "  cloud_provider: aws\n"
                    "  region: ${AWS::Region}\n"
                    "  bucket: ${BucketName}\n"
                    "  insecure_tls: false\n"
                    "  use_path_style: true\n"
                    "${Additional}",
                    OrgId=Ref(organization_id),
                    BucketName=bucket_name_value,
                    Additional=Ref(additional_storage_profiles),
                ),
```

- [ ] **Step 6: Add the parameter to the console parameter-group metadata**

Find the parameter-group list (~288) that contains `"IngestBucketName"`, `"IngestBucketLifecycleDays"` and add `"AdditionalStorageProfilesYaml"` to that group's `parameters` list. Add a label in the labels dict (~316) if one exists for the group:

```python
        "AdditionalStorageProfilesYaml": "Additional storage-profile YAML (remote buckets)",
```

- [ ] **Step 7: Run to verify pass**

Run: `.venv/bin/pytest tests/templates/test_cardinal_infrastructure.py -v`
Expected: PASS (all, including the three new tests)

- [ ] **Step 8: Commit**

```bash
git add src/cardinal_cfn/cardinal_infrastructure.py tests/templates/test_cardinal_infrastructure.py
git commit -m "feat: infra accepts remote-ingest bucket notifications + extra storage profiles"
```

---

## Task 3: `cardinal-remote-ingest.yaml` generator

**Files:**
- Create: `src/cardinal_cfn/remote_ingest.py`
- Test: `tests/templates/test_remote_ingest.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/templates/test_remote_ingest.py
"""Tests for the cardinal-remote-ingest standalone template."""

import json

import pytest

from cardinal_cfn import remote_ingest


@pytest.fixture
def td():
    return json.loads(remote_ingest.build().to_json())


def test_parameters(td):
    for n in ("RemoteAccountId", "OrgId", "QueueArn", "BucketName",
              "CollectorName", "RemoteOtelRoleNamePattern",
              "IngestBucketLifecycleDays"):
        assert n in td["Parameters"], f"missing parameter: {n}"


def test_remote_account_id_is_12_digits(td):
    assert td["Parameters"]["RemoteAccountId"]["AllowedPattern"] == r"^[0-9]{12}$"


def test_creates_one_bucket_retained_owner_enforced(td):
    buckets = [r for r in td["Resources"].values() if r["Type"] == "AWS::S3::Bucket"]
    assert len(buckets) == 1
    b = buckets[0]
    assert b["DeletionPolicy"] == "Retain"
    assert b["UpdateReplacePolicy"] == "Retain"
    rule = b["Properties"]["OwnershipControls"]["Rules"][0]
    assert rule["ObjectOwnership"] == "BucketOwnerEnforced"
    pab = b["Properties"]["PublicAccessBlockConfiguration"]
    assert all(pab[k] is True for k in (
        "BlockPublicAcls", "BlockPublicPolicy", "IgnorePublicAcls", "RestrictPublicBuckets"
    ))


def test_bucket_notifies_queue(td):
    b = next(r for r in td["Resources"].values() if r["Type"] == "AWS::S3::Bucket")
    qc = b["Properties"]["NotificationConfiguration"]["QueueConfigurations"][0]
    assert qc["Event"] == "s3:ObjectCreated:*"
    assert qc["Queue"] == {"Ref": "QueueArn"}


def test_writer_role_trusts_remote_account_root_with_name_condition(td):
    role = next(r for r in td["Resources"].values() if r["Type"] == "AWS::IAM::Role")
    stmt = role["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]
    assert stmt["Principal"]["AWS"] == {
        "Fn::Sub": "arn:${AWS::Partition}:iam::${RemoteAccountId}:root"
    }
    assert stmt["Action"] == "sts:AssumeRole"
    assert stmt["Condition"]["ArnLike"]["aws:PrincipalArn"] == {
        "Fn::Sub": "arn:${AWS::Partition}:iam::${RemoteAccountId}:role/${RemoteOtelRoleNamePattern}"
    }


def test_writer_role_can_put_to_bucket(td):
    role = next(r for r in td["Resources"].values() if r["Type"] == "AWS::IAM::Role")
    doc = role["Properties"]["Policies"][0]["PolicyDocument"]
    actions = doc["Statement"][0]["Action"]
    assert "s3:PutObject" in actions
    assert "s3:AbortMultipartUpload" in actions


def test_outputs(td):
    for n in ("BucketName", "BucketArn", "BucketRegion", "WriterRoleArn",
              "StorageProfileSnippet"):
        assert n in td["Outputs"], f"missing output: {n}"


def test_storage_profile_snippet_uses_region_and_bucket(td):
    snippet = json.dumps(td["Outputs"]["StorageProfileSnippet"]["Value"])
    assert "organization_id: ${OrgId}" in snippet
    assert "use_path_style: true" in snippet


def test_no_ecs_or_sqs_resources(td):
    """This template only owns the bucket + writer role; the queue lives in infra."""
    for r in td["Resources"].values():
        assert r["Type"] not in ("AWS::SQS::Queue", "AWS::ECS::Service")
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/templates/test_remote_ingest.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'cardinal_cfn.remote_ingest'`

- [ ] **Step 3: Write the generator**

```python
# src/cardinal_cfn/remote_ingest.py
"""cardinal-remote-ingest.yaml: cross-account remote ingest bucket (main account).

Standalone root template, one stack instance per remote bucket/account. Creates
an S3 bucket in the main (lakerunner) account that a remote account's otel
collector writes to (by assuming the WriterRole this stack creates), wires the
bucket's s3:ObjectCreated notifications to the main SQS ingest queue, and emits
a storage-profile snippet for the operator to register with lakerunner.

Design: docs/superpowers/specs/2026-05-29-cross-account-remote-ingest-design.md
"""

from troposphere import (
    Equals,
    GetAtt,
    If,
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
    LifecycleConfiguration,
    LifecycleRule,
    NotificationConfiguration,
    OwnershipControls,
    OwnershipControlsRule,
    PublicAccessBlockConfiguration,
    QueueConfigurations,
)

from cardinal_cfn.policies import apply_policy


def _tags(*, component: str) -> Tags:
    return Tags(
        Name=Sub(f"cardinal-remote-ingest-{component}-${{RemoteAccountId}}"),
        Project="cardinal",
        Application="cardinal-lakerunner",
        Component=component,
        ManagedBy="cardinal-cfn",
    )


def build() -> Template:
    t = Template()
    t.set_description(
        "Cardinal remote ingest: an S3 bucket in the main account that a remote "
        "account's otel collector writes to (via an assumed writer role), wired "
        "to the main lakerunner SQS ingest queue. One stack per remote bucket."
    )

    remote_account_id = t.add_parameter(Parameter(
        "RemoteAccountId",
        Type="String",
        AllowedPattern=r"^[0-9]{12}$",
        Description="The second (remote) AWS account ID whose otel collector writes to this bucket.",
    ))
    org_id = t.add_parameter(Parameter(
        "OrgId",
        Type="String",
        MinLength=1,
        Description="Lakerunner organization_id this bucket's telemetry is attributed to.",
    ))
    queue_arn = t.add_parameter(Parameter(
        "QueueArn",
        Type="String",
        MinLength=1,
        Description="ARN of the main lakerunner SQS ingest queue (infra IngestQueueArn output).",
    ))
    bucket_name = t.add_parameter(Parameter(
        "BucketName",
        Type="String",
        Default="",
        AllowedPattern=r"^$|^cardinal-remote-ingest-[a-z0-9.-]{1,40}$",
        Description=(
            "Bucket name. Blank = cardinal-remote-ingest-<RemoteAccountId>. Any "
            "override MUST keep the cardinal-remote-ingest- prefix so the infra "
            "queue policy grants the notification."
        ),
    ))
    collector_name = t.add_parameter(Parameter(
        "CollectorName",
        Type="String",
        Default="lakerunner",
        Description="Collector name for the storage profile and otel s3_prefix.",
    ))
    role_name_pattern = t.add_parameter(Parameter(
        "RemoteOtelRoleNamePattern",
        Type="String",
        Default="cardinal-remote-otel-*",
        Description="Remote task-role name pattern allowed to assume the writer role.",
    ))
    lifecycle_days = t.add_parameter(Parameter(
        "IngestBucketLifecycleDays",
        Type="Number",
        Default=7,
        MinValue=1,
        Description="Days after which objects in the bucket expire (GC backstop).",
    ))

    t.add_condition("UseDefaultBucketName", Equals(Ref(bucket_name), ""))
    bucket_name_value = If(
        "UseDefaultBucketName",
        Sub("cardinal-remote-ingest-${RemoteAccountId}"),
        Ref(bucket_name),
    )

    writer_role = t.add_resource(Role(
        "WriterRole",
        AssumeRolePolicyDocument={
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Principal": {"AWS": Sub("arn:${AWS::Partition}:iam::${RemoteAccountId}:root")},
                "Action": "sts:AssumeRole",
                "Condition": {
                    "ArnLike": {
                        "aws:PrincipalArn": Sub(
                            "arn:${AWS::Partition}:iam::${RemoteAccountId}:role/${RemoteOtelRoleNamePattern}"
                        )
                    }
                },
            }],
        },
        Policies=[Policy(
            PolicyName="cardinal-remote-writer",
            PolicyDocument={
                "Version": "2012-10-17",
                "Statement": [{
                    "Effect": "Allow",
                    "Action": [
                        "s3:PutObject",
                        "s3:AbortMultipartUpload",
                        "s3:ListMultipartUploadParts",
                    ],
                    "Resource": Sub(
                        "arn:${AWS::Partition}:s3:::${BucketName}/*",
                        BucketName=bucket_name_value,
                    ),
                }],
            },
        )],
        Tags=_tags(component="writer-role"),
    ))

    bucket = t.add_resource(Bucket(
        "RemoteIngestBucket",
        BucketName=bucket_name_value,
        OwnershipControls=OwnershipControls(
            Rules=[OwnershipControlsRule(ObjectOwnership="BucketOwnerEnforced")]
        ),
        PublicAccessBlockConfiguration=PublicAccessBlockConfiguration(
            BlockPublicAcls=True,
            BlockPublicPolicy=True,
            IgnorePublicAcls=True,
            RestrictPublicBuckets=True,
        ),
        LifecycleConfiguration=LifecycleConfiguration(Rules=[
            LifecycleRule(
                Id="cardinal-remote-ingest-expire",
                Status="Enabled",
                Prefix="",
                ExpirationInDays=Ref(lifecycle_days),
                AbortIncompleteMultipartUpload=AbortIncompleteMultipartUpload(
                    DaysAfterInitiation=1
                ),
            )
        ]),
        NotificationConfiguration=NotificationConfiguration(
            QueueConfigurations=[
                QueueConfigurations(Event="s3:ObjectCreated:*", Queue=Ref(queue_arn))
            ]
        ),
        Tags=_tags(component="bucket"),
    ))
    apply_policy(bucket, "s3-ingest-bucket")

    t.add_output(Output("BucketName", Description="Remote ingest bucket name.", Value=bucket_name_value))
    t.add_output(Output("BucketArn", Description="Remote ingest bucket ARN.", Value=GetAtt(bucket, "Arn")))
    t.add_output(Output(
        "BucketRegion",
        Description="Bucket region (the main/lakerunner region). Feed to the remote collector's BucketRegion.",
        Value=Ref("AWS::Region"),
    ))
    t.add_output(Output(
        "WriterRoleArn",
        Description="Role ARN the remote collector assumes to write. Feed to the remote collector's WriterRoleArn.",
        Value=GetAtt(writer_role, "Arn"),
    ))
    t.add_output(Output(
        "StorageProfileSnippet",
        Description="YAML list item to append to the infra stack's AdditionalStorageProfilesYaml, then re-run the migrator.",
        Value=Sub(
            "- organization_id: ${OrgId}\n"
            "  instance_num: 1\n"
            "  collector_name: ${CollectorName}\n"
            "  cloud_provider: aws\n"
            "  region: ${AWS::Region}\n"
            "  bucket: ${BucketName}\n"
            "  insecure_tls: false\n"
            "  use_path_style: true\n",
            BucketName=bucket_name_value,
        ),
    ))

    return t


if __name__ == "__main__":
    print(build().to_yaml(), end="")
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/templates/test_remote_ingest.py -v`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add src/cardinal_cfn/remote_ingest.py tests/templates/test_remote_ingest.py
git commit -m "feat: cardinal-remote-ingest template (bucket + cross-account writer role)"
```

---

## Task 4: `cardinal-remote-collector.yaml` generator

**Files:**
- Create: `src/cardinal_cfn/remote_collector.py`
- Test: `tests/templates/test_remote_collector.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/templates/test_remote_collector.py
"""Tests for the cardinal-remote-collector standalone template (remote account)."""

import json

import pytest

from cardinal_cfn import remote_collector


@pytest.fixture
def td():
    return json.loads(remote_collector.build().to_json())


def test_customer_supplied_parameters(td):
    for n in ("VpcId", "PrivateSubnetsCsv", "ClusterArn", "WriterRoleArn",
              "BucketName", "BucketRegion", "OrgId", "CollectorName",
              "OtlpIngressCidr"):
        assert n in td["Parameters"], f"missing parameter: {n}"


def test_no_license_secret_anywhere(td):
    """The remote collector runs receive->S3 only; it needs no license."""
    blob = json.dumps(td)
    assert "LICENSE_DATA" not in blob
    assert "LicenseSecretArn" not in td["Parameters"]
    task_def = next(r for r in td["Resources"].values() if r["Type"] == "AWS::ECS::TaskDefinition")
    container = task_def["Properties"]["ContainerDefinitions"][0]
    assert "Secrets" not in container or container["Secrets"] == []


def test_creates_internal_alb_on_4318(td):
    albs = [r for r in td["Resources"].values()
            if r["Type"] == "AWS::ElasticLoadBalancingV2::LoadBalancer"]
    assert len(albs) == 1
    assert albs[0]["Properties"]["Scheme"] == "internal"
    listeners = [r for r in td["Resources"].values()
                 if r["Type"] == "AWS::ElasticLoadBalancingV2::Listener"]
    assert len(listeners) == 1
    assert listeners[0]["Properties"]["Port"] == 4318
    assert listeners[0]["Properties"]["Protocol"] == "HTTP"


def test_creates_two_security_groups(td):
    sgs = [r for r in td["Resources"].values() if r["Type"] == "AWS::EC2::SecurityGroup"]
    assert len(sgs) == 2


def test_task_role_name_matches_writer_trust_pattern(td):
    """Task role name must start with cardinal-remote-otel- so the main writer
    role's trust condition matches it."""
    roles = [r for r in td["Resources"].values() if r["Type"] == "AWS::IAM::Role"]
    task_roles = [
        r for r in roles
        if any(
            "sts:AssumeRole" in json.dumps(p["PolicyDocument"])
            and "WriterRoleArn" in json.dumps(p["PolicyDocument"])
            for p in r["Properties"].get("Policies", [])
        )
    ]
    assert len(task_roles) == 1
    name = task_roles[0]["Properties"]["RoleName"]
    assert name == {"Fn::Sub": "cardinal-remote-otel-${AWS::Region}"}


def test_task_role_can_assume_writer_role(td):
    roles = [r for r in td["Resources"].values() if r["Type"] == "AWS::IAM::Role"]
    found = False
    for r in roles:
        for p in r["Properties"].get("Policies", []):
            doc = json.dumps(p["PolicyDocument"])
            if "sts:AssumeRole" in doc and "WriterRoleArn" in doc:
                found = True
    assert found, "no role grants sts:AssumeRole on WriterRoleArn"


def test_collector_env_uses_bucket_region_and_role(td):
    task_def = next(r for r in td["Resources"].values() if r["Type"] == "AWS::ECS::TaskDefinition")
    container = task_def["Properties"]["ContainerDefinitions"][0]
    env = {e["Name"]: e["Value"] for e in container["Environment"]}
    assert env["LRDB_S3_BUCKET"] == {"Ref": "BucketName"}
    assert env["LRDB_S3_REGION"] == {"Ref": "BucketRegion"}
    assert env["LRDB_S3_ROLE_ARN"] == {"Ref": "WriterRoleArn"}
    assert env["ORG"] == {"Ref": "OrgId"}
    assert "CHQ_COLLECTOR_CONFIG_YAML" in env


def test_service_disables_public_ip(td):
    svc = next(r for r in td["Resources"].values() if r["Type"] == "AWS::ECS::Service")
    awsvpc = svc["Properties"]["NetworkConfiguration"]["AwsvpcConfiguration"]
    assert awsvpc["AssignPublicIp"] == "DISABLED"


def test_no_cloud_map_registration(td):
    """Self-telemetry discovery is a main-account concern; not here."""
    assert not [r for r in td["Resources"].values()
                if r["Type"] == "AWS::ServiceDiscovery::Service"]


def test_outputs(td):
    for n in ("OtelAlbDnsName", "OtelExternalUrl"):
        assert n in td["Outputs"], f"missing output: {n}"
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/templates/test_remote_collector.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'cardinal_cfn.remote_collector'`

- [ ] **Step 3: Write the generator**

```python
# src/cardinal_cfn/remote_collector.py
"""cardinal-remote-collector.yaml: otel collector in a remote account.

Standalone root template deployed via the AWS console in the second account.
Receives OTLP, assumes the main-account writer role, and writes telemetry to the
main-account remote-ingest bucket. The customer brings VpcId, PrivateSubnetsCsv,
and ClusterArn; this stack creates the ALB, security groups, roles, log group,
and the otel ECS service.

Design: docs/superpowers/specs/2026-05-29-cross-account-remote-ingest-design.md
"""

from troposphere import (
    GetAtt,
    Output,
    Parameter,
    Ref,
    Split,
    Sub,
    Tags,
    Template,
)
from troposphere.ec2 import SecurityGroup, SecurityGroupRule
from troposphere.ecs import (
    AwsvpcConfiguration,
    ContainerDefinition,
    DeploymentCircuitBreaker,
    DeploymentConfiguration,
    Environment,
    LoadBalancer as EcsLoadBalancer,
    LogConfiguration,
    NetworkConfiguration,
    PortMapping,
    Service,
    TaskDefinition,
)
from troposphere.elasticloadbalancingv2 import (
    Action,
    Listener,
    LoadBalancer,
    Matcher,
    TargetGroup,
)
from troposphere.iam import Policy, Role
from troposphere.logs import LogGroup

from cardinal_cfn.defaults import load_defaults, load_remote_otel_default_config
from cardinal_cfn.images import add_image_override

_SERVICE_KEY = "otel-grpc"
_OTLP_HTTP_PORT = 4318
_HEALTH_PORT = 13133


def _tags(*, component: str) -> Tags:
    return Tags(
        Name=f"cardinal-remote-collector-{component}",
        Project="cardinal",
        Application="cardinal-lakerunner",
        Component=component,
        ManagedBy="cardinal-cfn",
    )


def build() -> Template:
    t = Template()
    t.set_description(
        "Cardinal remote collector: an ALB-fronted cardinalhq-otel-collector in a "
        "remote account that assumes a main-account writer role to write telemetry "
        "to the main-account remote-ingest bucket."
    )

    defaults = load_defaults()
    otel_cfg = defaults["otel"]["otel-gateway"]

    # Customer-supplied
    t.add_parameter(Parameter("VpcId", Type="AWS::EC2::VPC::Id", Description="Customer VPC ID."))
    t.add_parameter(Parameter(
        "PrivateSubnetsCsv",
        Type="String",
        Description="Comma-separated private subnet IDs (>=2 AZs) for the internal ALB and the collector ENIs.",
    ))
    t.add_parameter(Parameter("ClusterArn", Type="String", Description="Customer ECS cluster ARN."))

    # From the main-account cardinal-remote-ingest stack outputs
    t.add_parameter(Parameter("WriterRoleArn", Type="String", Description="Writer role ARN to assume (remote-ingest WriterRoleArn output)."))
    t.add_parameter(Parameter("BucketName", Type="String", Description="Main-account remote-ingest bucket name."))
    t.add_parameter(Parameter(
        "BucketRegion",
        Type="String",
        Description="Bucket region (the main/lakerunner region). NOT the remote account's region.",
    ))
    t.add_parameter(Parameter("OrgId", Type="String", Description="Lakerunner organization_id (match the remote-ingest OrgId)."))
    t.add_parameter(Parameter("CollectorName", Type="String", Default="lakerunner", Description="Collector name (match the remote-ingest CollectorName)."))
    t.add_parameter(Parameter(
        "OtlpIngressCidr",
        Type="String",
        Default="10.0.0.0/8",
        Description="Source CIDR allowed to reach the internal ALB on 4318. Narrow to your sender/VPC CIDR.",
    ))

    image_ref = add_image_override(
        t,
        name="OtelImage",
        default=defaults["images"]["otel"],
        description="Container image for the cardinalhq-otel-collector service.",
    )
    t.add_parameter(Parameter("OtelReplicas", Type="Number", Default=str(otel_cfg["replicas"]), Description="Desired replicas."))
    t.add_parameter(Parameter("OtelCpu", Type="String", Default=str(otel_cfg["cpu"]), Description="Fargate CPU units."))
    t.add_parameter(Parameter("OtelMemory", Type="String", Default=str(otel_cfg["memory_mib"]), Description="Fargate memory (MiB)."))

    # ------------------------------------------------------------------ SGs
    alb_sg = t.add_resource(SecurityGroup(
        "AlbSecurityGroup",
        GroupDescription="cardinal remote collector ALB; OTLP/HTTP 4318 ingress.",
        VpcId=Ref("VpcId"),
        SecurityGroupIngress=[SecurityGroupRule(
            IpProtocol="tcp", FromPort=_OTLP_HTTP_PORT, ToPort=_OTLP_HTTP_PORT,
            CidrIp=Ref("OtlpIngressCidr"),
            Description="OTLP/HTTP from senders",
        )],
        SecurityGroupEgress=[SecurityGroupRule(
            IpProtocol="-1", CidrIp="0.0.0.0/0", Description="All egress",
        )],
        Tags=_tags(component="alb-sg"),
    ))
    task_sg = t.add_resource(SecurityGroup(
        "TaskSecurityGroup",
        GroupDescription="cardinal remote collector tasks; 4318 from ALB only.",
        VpcId=Ref("VpcId"),
        SecurityGroupIngress=[SecurityGroupRule(
            IpProtocol="tcp", FromPort=_OTLP_HTTP_PORT, ToPort=_OTLP_HTTP_PORT,
            SourceSecurityGroupId=Ref(alb_sg),
            Description="OTLP/HTTP from the ALB",
        )],
        SecurityGroupEgress=[SecurityGroupRule(
            IpProtocol="-1", CidrIp="0.0.0.0/0", Description="All egress",
        )],
        Tags=_tags(component="task-sg"),
    ))

    # ------------------------------------------------------------------ ALB
    alb = t.add_resource(LoadBalancer(
        "Alb",
        Scheme="internal",
        Type="application",
        Subnets=Split(",", Ref("PrivateSubnetsCsv")),
        SecurityGroups=[Ref(alb_sg)],
        Tags=_tags(component="alb"),
    ))
    target_group = t.add_resource(TargetGroup(
        "OtelTargetGroup",
        Port=_OTLP_HTTP_PORT,
        Protocol="HTTP",
        TargetType="ip",
        VpcId=Ref("VpcId"),
        HealthCheckPath="/",
        HealthCheckPort=str(_HEALTH_PORT),
        HealthCheckProtocol="HTTP",
        Matcher=Matcher(HttpCode="200"),
        Tags=_tags(component="otel-tg"),
    ))
    listener = t.add_resource(Listener(
        "OtelHttpListener",
        LoadBalancerArn=Ref(alb),
        Port=_OTLP_HTTP_PORT,
        Protocol="HTTP",
        DefaultActions=[Action(Type="forward", TargetGroupArn=Ref(target_group))],
    ))

    # ------------------------------------------------------------------ Roles
    exec_role = t.add_resource(Role(
        "ExecutionRole",
        AssumeRolePolicyDocument={
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }],
        },
        ManagedPolicyArns=[
            "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy",
        ],
        Tags=_tags(component="exec-role"),
    ))
    task_role = t.add_resource(Role(
        "TaskRole",
        RoleName=Sub("cardinal-remote-otel-${AWS::Region}"),
        AssumeRolePolicyDocument={
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }],
        },
        Policies=[Policy(
            PolicyName="cardinal-remote-otel-assume-writer",
            PolicyDocument={
                "Version": "2012-10-17",
                "Statement": [{
                    "Effect": "Allow",
                    "Action": "sts:AssumeRole",
                    "Resource": Ref("WriterRoleArn"),
                }],
            },
        )],
        Tags=_tags(component="task-role"),
    ))

    log_group = t.add_resource(LogGroup(
        "OtelLogGroup",
        LogGroupName="/cardinal/otel-grpc",
        RetentionInDays=14,
        DeletionPolicy="Delete",
        UpdateReplacePolicy="Delete",
        Tags=_tags(component="otel-logs"),
    ))

    # ------------------------------------------------------------ Task def
    env = [
        Environment(Name="CHQ_COLLECTOR_CONFIG_YAML", Value=load_remote_otel_default_config()),
        Environment(Name="LRDB_S3_BUCKET", Value=Ref("BucketName")),
        Environment(Name="LRDB_S3_REGION", Value=Ref("BucketRegion")),
        Environment(Name="LRDB_S3_ROLE_ARN", Value=Ref("WriterRoleArn")),
        Environment(Name="ORG", Value=Ref("OrgId")),
        Environment(Name="COLLECTOR", Value=Ref("CollectorName")),
    ] + [Environment(Name=k, Value=str(v)) for k, v in (otel_cfg.get("environment") or {}).items()]

    task_def = t.add_resource(TaskDefinition(
        "OtelTaskDef",
        RequiresCompatibilities=["FARGATE"],
        NetworkMode="awsvpc",
        Cpu=Ref("OtelCpu"),
        Memory=Ref("OtelMemory"),
        ExecutionRoleArn=GetAtt(exec_role, "Arn"),
        TaskRoleArn=GetAtt(task_role, "Arn"),
        ContainerDefinitions=[ContainerDefinition(
            Name=_SERVICE_KEY,
            Image=image_ref,
            Essential=True,
            Command=otel_cfg.get("command"),
            Environment=env,
            PortMappings=[PortMapping(ContainerPort=_OTLP_HTTP_PORT, Protocol="tcp")],
            LogConfiguration=LogConfiguration(
                LogDriver="awslogs",
                Options={
                    "awslogs-group": Ref(log_group),
                    "awslogs-region": Ref("AWS::Region"),
                    "awslogs-stream-prefix": _SERVICE_KEY,
                },
            ),
        )],
        Tags=_tags(component="otel-taskdef"),
    ))

    # ------------------------------------------------------------- Service
    service = t.add_resource(Service(
        "OtelService",
        Cluster=Ref("ClusterArn"),
        LaunchType="FARGATE",
        DesiredCount=Ref("OtelReplicas"),
        TaskDefinition=Ref(task_def),
        DependsOn=[listener.title],
        NetworkConfiguration=NetworkConfiguration(
            AwsvpcConfiguration=AwsvpcConfiguration(
                Subnets=Split(",", Ref("PrivateSubnetsCsv")),
                SecurityGroups=[Ref(task_sg)],
                AssignPublicIp="DISABLED",
            )
        ),
        DeploymentConfiguration=DeploymentConfiguration(
            MinimumHealthyPercent=50,
            MaximumPercent=200,
            DeploymentCircuitBreaker=DeploymentCircuitBreaker(Enable=True, Rollback=True),
        ),
        LoadBalancers=[EcsLoadBalancer(
            ContainerName=_SERVICE_KEY,
            ContainerPort=_OTLP_HTTP_PORT,
            TargetGroupArn=Ref(target_group),
        )],
        Tags=_tags(component="otel-service"),
    ))

    t.add_output(Output("OtelAlbDnsName", Value=GetAtt(alb, "DNSName")))
    t.add_output(Output("OtelExternalUrl", Value=Sub(f"http://${{Dns}}:{_OTLP_HTTP_PORT}", Dns=GetAtt(alb, "DNSName"))))

    return t


if __name__ == "__main__":
    print(build().to_yaml(), end="")
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/templates/test_remote_collector.py -v`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add src/cardinal_cfn/remote_collector.py tests/templates/test_remote_collector.py
git commit -m "feat: cardinal-remote-collector template (ALB-fronted otel, assumes writer role)"
```

---

## Task 5: Build + lint wiring

**Files:**
- Modify: `build.sh`
- Modify: `Makefile`

- [ ] **Step 1: Add generation lines to `build.sh`**

After the `cardinal-cleanup.yaml` generation block (~31), add:

```sh
echo "Generating cardinal-remote-ingest.yaml..."
python3 -m cardinal_cfn.remote_ingest > generated-templates/cardinal-remote-ingest.yaml

echo "Generating cardinal-remote-collector.yaml..."
python3 -m cardinal_cfn.remote_collector > generated-templates/cardinal-remote-collector.yaml
```

Add both to the `cfn-lint` invocation (~50-55):

```sh
         generated-templates/cardinal-remote-ingest.yaml \
         generated-templates/cardinal-remote-collector.yaml \
```

- [ ] **Step 2: Add both to the `Makefile` lint target**

In the `lint:` target (~38-44), add the two lines before `generated-templates/cardinal-lakerunner.yaml`:

```make
	  generated-templates/cardinal-remote-ingest.yaml \
	  generated-templates/cardinal-remote-collector.yaml \
```

- [ ] **Step 3: Build and lint everything**

Run: `make build`
Expected: generates all templates including the two new ones; cfn-lint passes (warnings tolerable, no errors). If cfn-lint flags an error on either new template, fix the template and re-run.

- [ ] **Step 4: Run the full test suite**

Run: `make test`
Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add build.sh Makefile
git commit -m "build: generate and lint the remote-ingest and remote-collector templates"
```

---

## Task 6: Operator docs

**Files:**
- Modify or create: a short operator runbook section (e.g. `docs/operations/remote-ingest.md`)

- [ ] **Step 1: Write the runbook**

Document the operator workflow from the spec's "Operator workflow" section: deploy `cardinal-remote-ingest` in the main account, record outputs, deploy `cardinal-remote-collector` in the second account (CAPABILITY_NAMED_IAM), append the `StorageProfileSnippet` to the infra `AdditionalStorageProfilesYaml`, redeploy infra, re-run the migrator. Note the deploy-ordering prerequisite (infra must carry the broadened queue policy first) and the same-region bucket constraint.

- [ ] **Step 2: Commit**

```bash
git add docs/operations/remote-ingest.md
git commit -m "docs: remote-ingest operator runbook"
```

---

## Self-review notes

- **Spec coverage:** bucket+writer role (Task 3), remote collector ALB+otel+assume-role (Task 4), queue-policy widening + AdditionalStorageProfilesYaml (Task 2), remote otel config role_arn (Task 1), build/lint (Task 5), operator docs (Task 6). All spec sections mapped.
- **Type/name consistency:** `WriterRoleArn`, `BucketName`, `BucketRegion`, `OrgId`, `CollectorName` parameter names are identical between the remote-ingest outputs and the remote-collector inputs. The task-role name `cardinal-remote-otel-${AWS::Region}` matches the writer-role trust default pattern `cardinal-remote-otel-*`. `_SERVICE_KEY="otel-grpc"` matches the log group `/cardinal/otel-grpc`.
- **Verify-at-implementation:** the awss3 exporter `role_arn` field name and `${env:...}` expansion must be confirmed against the cardinalhq-otel-collector image (template tests do not run the collector). If the field differs, fix `cardinal-remote-otel-config.yaml` only.
