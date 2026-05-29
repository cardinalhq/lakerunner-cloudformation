# Cross-account remote ingest design

Status: approved (brainstorming, 2026-05-29)

## Problem

A Cardinal lakerunner install lives in one AWS account (the "main" account).
We need telemetry produced in a **second AWS account** to land in lakerunner's
ingest pipeline. The pipeline is: an otel collector writes telemetry blobs to an
S3 bucket; the bucket emits `s3:ObjectCreated` notifications to lakerunner's SQS
queue; lakerunner's `pubsub-sqs` service fans those out to the process workers,
which read each object back from S3 and ingest it.

Getting a second account's telemetry into that pipeline requires crossing an
account boundary somewhere. The question is *where*, and how to grant the
minimum cross-account permission.

## Decision: bucket in the main account, remote collector assumes a writer role

The remote-ingest bucket lives in the **main account** (option 1). Consequence:

- **Write** (remote collector -> bucket) crosses the account boundary.
- **Notification** (bucket -> SQS) is same-account, same-region.
- **Read** (lakerunner -> bucket) is same-account.

Only the write crosses the boundary. The alternative (bucket in the remote
account) would cross the boundary three times -- notification, read, and the
read is the painful one (lakerunner reading objects owned by a foreign account).

The write crosses via **STS assume-role**, not a direct bucket-policy grant:

- The main account creates a **writer role**. Its trust policy allows the remote
  account *root* to assume it, narrowed by a condition on the assuming
  principal's name (`aws:PrincipalArn ArnLike .../role/cardinal-remote-otel-*`).
  Trusting the account root (not a specific role ARN) is what removes the
  circular dependency -- the writer role can be created before the remote role
  exists, so the main stack is self-contained and just outputs the role ARN.
- The remote collector's otel task role is granted `sts:AssumeRole` on that
  writer-role ARN.
- The collector's `awss3` exporter is configured with `role_arn` set to the
  writer-role ARN. The collector assumes the role (using its task-role creds as
  the STS source) before each S3 write.
- Because the collector writes *as the assumed main-account role*, the objects
  are natively owned by the main account, so lakerunner reads them with no ACL
  or ownership fuss. The bucket is also set to `BucketOwnerEnforced` (ACLs off)
  for hygiene.

Why assume-role over a direct cross-account bucket-policy grant:

1. Clean ordering / no circularity (trust the account root + name condition).
2. Native main-account object ownership (lakerunner reads with zero ACL work).
3. Tight scope: only the named role pattern can write, gated by the trust policy.

The `awss3` exporter's `role_arn` field is confirmed against the
opentelemetry-collector-contrib README (top-level field, sibling of
`s3uploader`). The `cardinalhq-otel-collector` image is a custom build; the
exact field name and env-expansion behaviour must be re-confirmed against that
image during implementation (template tests do not run the collector).

## Data flow

```
remote app --OTLP--> remote ALB :4318 --> remote otel collector
   --(assume main writer role via STS)--> PutObject  main-account bucket
       (bucket is in the main account, in lakerunner's region)
   --> S3 ObjectCreated --> main SQS (same-account, same-region)
   --> lakerunner pubsub-sqs --> process-{logs,metrics,traces}
   --> read object from bucket (same-account) --> ingest under the bucket's org
```

## Components

### 1. New main-account template: `cardinal-remote-ingest.yaml`

Generator: `src/cardinal_cfn/remote_ingest.py` (standalone root template, in the
style of `lrdev_baseinfra.py`). One **stack instance per remote bucket/account**.
Published to S3 alongside the other `cardinal-*` templates.

Parameters:

- `RemoteAccountId` (String, 12 digits, `AllowedPattern ^[0-9]{12}$`) -- the
  second account, used in the writer role's trust policy.
- `OrgId` (String, required) -- the lakerunner organization this bucket's
  telemetry is attributed to. One org per remote bucket.
- `QueueArn` (String) -- the main SQS ingest queue ARN (from the infra stack's
  `IngestQueueArn` output). Target of the bucket notification.
- `BucketName` (String, default `cardinal-remote-ingest-${RemoteAccountId}` via
  a rendered default; `AllowedPattern` enforces the `cardinal-remote-ingest-`
  prefix). The prefix is mandatory because the infra queue policy grants the
  notification by prefix match (see component 3).
- `CollectorName` (String, default `lakerunner`) -- collector name for the
  storage-profile and the otel `s3_prefix`.
- `RemoteOtelRoleNamePattern` (String, default `cardinal-remote-otel-*`) -- the
  remote task-role name pattern allowed to assume the writer role. Default
  matches the name the remote collector template assigns its task role.
- `IngestBucketLifecycleDays` (Number, default 7) -- mirrors the primary ingest
  bucket's GC backstop.

Resources:

- **S3 bucket** (`Retain`): explicit `BucketName`; `BucketOwnerEnforced`
  ownership; `PublicAccessBlockConfiguration` all-true; lifecycle expiration
  (`IngestBucketLifecycleDays`) + abort-incomplete-multipart; and a
  `NotificationConfiguration` -> `QueueConfigurations` with `Event
  s3:ObjectCreated:*`, `Queue=Ref(QueueArn)`. No cross-stack `DependsOn` is
  possible; the infra queue policy must already permit this bucket's prefix
  (see component 3) -- documented as a deploy-ordering prerequisite.
- **Writer IAM role** (`cardinal-remote-writer-...`, CFN-generated name fine):
  - Trust: `Principal AWS: arn:${AWS::Partition}:iam::${RemoteAccountId}:root`,
    `Action sts:AssumeRole`, `Condition ArnLike aws:PrincipalArn
    arn:${AWS::Partition}:iam::${RemoteAccountId}:role/${RemoteOtelRoleNamePattern}`.
  - Permissions: `s3:PutObject`, `s3:AbortMultipartUpload`,
    `s3:ListMultipartUploadParts` on `${BucketArn}/*`. (The s3manager uploader
    used by the exporter may multipart large blobs.)

Outputs:

- `BucketName`, `BucketArn`
- `BucketRegion` (`Ref AWS::Region` -- this stack runs in the main/lakerunner
  region, which is the bucket's region).
- `WriterRoleArn`
- `StorageProfileSnippet` -- a ready-to-paste YAML list item for the operator to
  append to the infra stack's `AdditionalStorageProfilesYaml` (see component 3),
  in the same shape the infra stack seeds:

  ```yaml
  - organization_id: <OrgId>
    instance_num: 1
    collector_name: <CollectorName>
    cloud_provider: aws
    region: <BucketRegion>
    bucket: <BucketName>
    insecure_tls: false
    use_path_style: true
  ```

### 2. New remote-account template: `cardinal-remote-collector.yaml`

Generator: `src/cardinal_cfn/remote_collector.py` (standalone root template).
Deployed by the operator via the AWS console in the **second account**. Customer
brings VPC, subnets, and an ECS cluster; this stack creates everything else
(ALB, SGs, roles, log group, otel service) -- mirroring how the main product
creates its own ALB while consuming a customer SG.

Parameters:

- `VpcId` (`AWS::EC2::VPC::Id`) -- customer-supplied.
- `PrivateSubnetsCsv` (String) -- customer-supplied; used for the internal ALB
  and the ECS service ENIs.
- `ClusterArn` (String) -- customer-supplied ECS cluster.
- `WriterRoleArn` (String) -- from the main stack's `WriterRoleArn` output.
- `BucketName` (String) -- from the main stack's `BucketName` output.
- `BucketRegion` (String) -- from the main stack's `BucketRegion` output. NOTE:
  this is the *bucket's* region (lakerunner's), which may differ from the remote
  account's region. The otel `LRDB_S3_REGION` env var is set from this, **not**
  from `AWS::Region`.
- `OrgId`, `CollectorName` -- match the main stack's values (drive the
  `s3_prefix`).
- `OtlpIngressCidr` (String, default `10.0.0.0/8`) -- source CIDR allowed to
  reach the internal ALB on 4318. Operator narrows to the VPC/sender CIDR.
- `OtelImage` -- image override (default from `cardinal-defaults.yaml`).
- `OtelReplicas` / `OtelCpu` / `OtelMemory` -- tunables (defaults from
  `cardinal-defaults.yaml` `otel.otel-gateway`).

Resources:

- **ALB security group**: ingress tcp/4318 from `OtlpIngressCidr`; egress all.
- **Task security group**: ingress tcp/4318 from the ALB SG; egress all.
- **Internal ALB** + HTTP listener on 4318 whose default action forwards to the
  collector target group (no path-pattern rules needed -- single backend).
- **Target group** (HTTP, ip target, port 4318, health check on 13133 `/`),
  reusing `services_common.build_target_group`.
- **Log group** `/cardinal/otel-grpc` via `services_common.build_log_group`.
- **Execution role**: ECR pull + CloudWatch Logs. No secrets (no license).
- **Task role** named with the `cardinal-remote-otel-` prefix (so it matches the
  writer role's trust condition): granted `sts:AssumeRole` on `WriterRoleArn`
  plus CloudWatch Logs. No SQS, no DB, no SSM. The explicit role name is a
  sanctioned exception to the CFN-generated-name rule (it is externally
  referenced by the main account's writer-role trust condition, like SSM param
  names and the ingest bucket name are externally-referenced exceptions). It
  forces `CAPABILITY_NAMED_IAM` at deploy time -- the console handles this.
  Cross-account assume-role requires both sides to allow: the writer role's
  trust policy (resource side, account-root + name condition) and this role's
  `sts:AssumeRole` identity policy (principal side). The name condition is
  therefore defense-in-depth on top of the principal-side scoping.
- **Task definition** via `services_common.build_task_definition`, with env:
  - `CHQ_COLLECTOR_CONFIG_YAML` = the remote otel config (see below).
  - `LRDB_S3_BUCKET` = `Ref(BucketName)`.
  - `LRDB_S3_REGION` = `Ref(BucketRegion)`  (not `AWS::Region`).
  - `LRDB_S3_ROLE_ARN` = `Ref(WriterRoleArn)`.
  - `ORG` = `Ref(OrgId)`, `COLLECTOR` = `Ref(CollectorName)`.
  - No `LICENSE_DATA` secret.
- **ECS service** attached to the target group (inline, like `otel.py`, so the
  `LoadBalancers` block references the always-present target group). No Cloud Map
  registration (self-telemetry discovery is a main-account concern).

Outputs: `OtelAlbDnsName`, `OtelExternalUrl` (`http://<alb>:4318`).

New config file: `cardinal-remote-otel-config.yaml` -- a copy of
`cardinal-otel-config.yaml` with `role_arn: ${env:LRDB_S3_ROLE_ARN}` added to
each `awss3/*` exporter (top-level, sibling of `s3uploader` and `marshaler`).
A separate file (not a shared one) because an empty `role_arn` is not safely
ignored by the exporter, so the main collector cannot share it.

`defaults.py` gains `load_remote_otel_default_config()` (or
`load_otel_default_config` is generalized to take a filename) to read it.

### 3. Edits to `cardinal_infrastructure.py`

- **Queue policy**: keep the existing statement scoped to the primary ingest
  bucket ARN exactly as-is. **Add a second statement** allowing
  `s3.amazonaws.com` `sqs:SendMessage` with `Condition StringEquals
  aws:SourceAccount = ${AWS::AccountId}` and `ArnLike aws:SourceArn =
  arn:${AWS::Partition}:s3:::cardinal-remote-ingest-*`. This lets each new
  remote-ingest bucket notify the queue without per-bucket edits to the
  infra-owned policy. The distinct `cardinal-remote-ingest-` prefix does not
  overlap the primary `cardinal-ingest-` prefix.
- **Storage profiles**: add an `AdditionalStorageProfilesYaml` parameter
  (String, default `""`) and append it to the `StorageProfilesParam` value after
  the seeded primary profile. Empty default is harmless (appends nothing). The
  operator pastes one or more `cardinal-remote-ingest.yaml` `StorageProfileSnippet`
  outputs here.

## Operator workflow (manual, AWS console)

No cross-account automation (no StackSets, no spanning script).

1. `cardinal-infrastructure` + `cardinal-lakerunner` already deployed. Infra
   must be on a template version that includes the broadened queue policy
   (component 3) before any remote-ingest stack is deployed.
2. **Main account**: deploy `cardinal-remote-ingest.yaml` with `RemoteAccountId`,
   `OrgId`, and `QueueArn` (the infra `IngestQueueArn` output). Record its
   outputs.
3. **Second account**: deploy `cardinal-remote-collector.yaml` via the console,
   pasting `WriterRoleArn`, `BucketName`, `BucketRegion`, `OrgId`,
   `CollectorName`, plus the customer's `VpcId`, `PrivateSubnetsCsv`,
   `ClusterArn`, and an `OtlpIngressCidr`.
4. **Register the storage profile**: append the remote-ingest stack's
   `StorageProfileSnippet` to the infra stack's `AdditionalStorageProfilesYaml`
   parameter and redeploy infra (updates the SSM param).
5. **Re-run the migrator** so it re-imports the storage profiles into `configdb`
   (bump `LakerunnerImage`, or cycle the migrator service desired-count). The
   migrator is idempotent. This re-import is the main ergonomic cost; automatic
   registration is an explicit non-goal for v1.

## File layout

```
src/cardinal_cfn/remote_ingest.py        -> generated-templates/cardinal-remote-ingest.yaml
src/cardinal_cfn/remote_collector.py     -> generated-templates/cardinal-remote-collector.yaml
cardinal-remote-otel-config.yaml         (new; remote collector config with role_arn)
src/cardinal_cfn/cardinal_infrastructure.py  (edits: queue policy + AdditionalStorageProfilesYaml)
src/cardinal_cfn/defaults.py             (edit: load remote otel config)
build.sh                                 (edit: generate the two new templates)
Makefile lint target                     (edit: lint the two new templates)
tests/templates/test_remote_ingest.py    (new)
tests/templates/test_remote_collector.py (new)
tests/templates/...                      (edit existing infra test for queue policy + new param)
```

These are `cardinal-*` product templates (customer/operator-facing, published to
S3), not `lrdev-*` scaffolding.

## Testing

- `cloud-radar` per-template assertions for both new templates: writer-role
  trust + permissions shape, bucket ownership/notification, the assume-role
  env wiring, `LRDB_S3_REGION` sourced from `BucketRegion` (not `AWS::Region`),
  no `LICENSE_DATA` secret on the remote collector, and the storage-profile
  snippet shape.
- An assertion on the infra queue policy's second statement
  (`cardinal-remote-ingest-*` ArnLike) and the `AdditionalStorageProfilesYaml`
  append.
- `cfn-lint` clean on both new templates (via `build.sh` / `make lint`).
- End-to-end cross-account verification needs a second AWS account and is a
  documented manual procedure; it cannot run in the single test account
  (493746473138). Not automated.

## Flags / non-goals

- **Cross-region write cost**: the bucket is pinned to lakerunner's region
  (S3 -> SQS must be same-region). If the remote account is in another region,
  the collector writes cross-region (egress cost). Accepted.
- **KMS**: buckets are SSE-S3 today; if a bucket is later switched to SSE-KMS,
  the writer role needs `kms:GenerateDataKey` on the key. Out of scope now.
- **License**: the remote collector runs a pure OTLP-receive -> S3-write
  pipeline and needs no `LICENSE_DATA`. If the image refuses to boot without a
  license, that surfaces in testing and the design is revisited.
- **Automatic storage-profile registration / migrator re-run**: explicit
  non-goal for v1; documented manual step instead.
- **Multiple buckets per remote account**: supported by overriding `BucketName`
  (keeping the required prefix) and deploying additional stack instances; the
  default name encodes one bucket per remote account.
