# Installing Cardinal lakerunner

End-to-end install runbook for the Cardinal lakerunner CloudFormation
distribution. Two CloudFormation stacks, in order:

1. `cardinal-data-setup` -- a thin wrapper that deploys a Lambda and
   invokes it once. The Lambda creates the data layer (RDS, S3 ingest
   bucket, SQS queue, secrets, SSM parameters) and returns their
   identifiers as stack outputs.
2. `cardinal-lakerunner` -- the application stack. It consumes the
   `cardinal-data-setup` outputs as inputs, plus the customer-supplied
   IAM role and security-group identifiers. It creates only stateless
   application resources (ECS cluster, ALB, services, listeners,
   target groups, custom-resource Lambdas).

The `cardinal-vpc` stack is **not part of the install path** -- it
exists only as a convenience for ephemeral test environments.
Customers run the application in their own VPC and pass `VpcId` +
`PrivateSubnets` directly.

The whole install is parameter-driven. There is no manual step
between the two stacks beyond piping outputs into inputs; this
document shows exactly how.

## Layer ownership

| Layer | Owned by | What it does |
|---|---|---|
| IAM roles + security groups | Customer's IT (one-time, out of band) | Pre-creates the four IAM roles and three SGs documented in `docs/operations/required-roles.md`. Hands ARNs and SG IDs to the operator. |
| `cardinal-data-setup` stack | Operator | Deploys + invokes the data-setup Lambda. Outputs the data-resource identifiers. |
| `cardinal-lakerunner` stack | Operator | Application stack. Consumes the data-setup outputs and the IT-supplied ARNs/IDs. |

## Prerequisites

Before deploying, the operator must have:

- AWS CLI configured for the target account / region.
- A VPC ID and at least two private-subnet IDs in distinct AZs.
- The IAM role ARNs and security-group IDs from
  `docs/operations/required-roles.md`. Required role ARNs:
  - `DataSetupLambdaRoleArn` (data-setup stack only)
  - `TaskRoleArn`, `ExecutionRoleArn`, `MigrationLambdaRoleArn`
    (lakerunner stack)
  - `CertLambdaRoleArn` (lakerunner stack, only if importing PEMs --
    skip if `CertificateArn` is supplied).
- Required SG IDs: `TaskSgId`, `AlbSgId`, `DbSgId`.
- A Cardinal license. The license file is a single line that begins
  with `z64:` followed by base64-encoded data. Copy its **raw**
  contents into the `LicenseData` parameter on the data-setup stack.
- A bcrypt hash of the DEX admin password (see "DEX admin password
  hash" below).
- Either an existing ACM certificate ARN (`CertificateArn`) or a PEM
  bundle (`CertificateBody` + `CertificatePrivateKey` + optional
  `CertificateChain`) to pass to the lakerunner stack.

### DEX admin password hash

The maestro Go services accept bcrypt `$2a` / `$2b` prefixes;
`htpasswd` emits `$2y` which must be rewritten:

```sh
PASSWORD='choose-a-strong-one'
htpasswd -bnBC 12 "" "$PASSWORD" | tr -d ':\n' | sed 's/^\$2y/\$2a/' > dex-hash.txt
# dex-hash.txt now contains a 60-char $2a$12$... bcrypt hash
```

Keep the plaintext password somewhere secure -- it is what the admin
will type at the maestro login screen. The bcrypt hash is what the
stack stores.

### Published template URL

Templates are published per release tag at:

```
https://cardinal-cfn.s3.us-east-2.amazonaws.com/lakerunner/<VERSION>/<template>.yaml
```

Replace `<VERSION>` with the explicit release tag under deployment
(e.g. `v0.0.38`). There is no `latest` tag -- every install is to a
named version.

The same `<VERSION>` is used for both stacks. The lakerunner root
defaults `TemplateBaseUrl` to the `dev` channel
(`.../lakerunner/dev/cardinal-lakerunner/`). For a release install,
override `TemplateBaseUrl` to point at the matching version directory:

```
TemplateBaseUrl=https://cardinal-cfn.s3.us-east-2.amazonaws.com/lakerunner/<VERSION>/cardinal-lakerunner/
```

(Trailing slash is required.)

## Step 1: deploy the data-setup stack

The `cardinal-data-setup.yaml` template takes these parameters:

| Parameter | Source | Notes |
|---|---|---|
| `DataSetupLambdaRoleArn` | IT | Pre-created Lambda execution role. |
| `VpcId` | Operator | Target VPC for the RDS subnet group. |
| `PrivateSubnets` | Operator | `List<AWS::EC2::Subnet::Id>` (CSV in JSON params). |
| `DbSgId` | IT | DB security group ID. |
| `LicenseData` | Operator | Raw license-file contents (`z64:...`). NoEcho. |
| `BucketLifecycleDays` | Optional | S3 ingest object expiry (default 7). |
| `DbInstanceClass` | Optional | RDS class (default `db.t3.medium`). |
| `DbAllocatedStorage` | Optional | RDS GiB (default 100). |
| `LambdaCodeS3Bucket` | Optional | Default `cardinal-cfn`. |
| `LambdaCodeS3Key` | Optional | Default `lakerunner/dev/cardinal-data-setup-lambda.zip`; override to match `<VERSION>`. |

There are **no DEX or OIDC parameters** on the data-setup stack --
those flow only into the lakerunner stack.

Stage parameters in a JSON file rather than inline so the multi-line
license content is easy to handle:

```sh
LICENSE_DATA="$(cat ~/Downloads/cop-rpi.license-2.token)"   # or whichever .token file is freshest

cat > /tmp/data-setup-params.json <<EOF
[
  {"ParameterKey": "DataSetupLambdaRoleArn", "ParameterValue": "arn:aws:iam::<ACCOUNT>:role/cardinal-data-setup-lambda-role"},
  {"ParameterKey": "VpcId",                  "ParameterValue": "vpc-..."},
  {"ParameterKey": "PrivateSubnets",         "ParameterValue": "subnet-aaaa,subnet-bbbb,subnet-cccc"},
  {"ParameterKey": "DbSgId",                 "ParameterValue": "sg-..."},
  {"ParameterKey": "LicenseData",            "ParameterValue": "${LICENSE_DATA}"},
  {"ParameterKey": "LambdaCodeS3Key",        "ParameterValue": "lakerunner/<VERSION>/cardinal-data-setup-lambda.zip"}
]
EOF

aws cloudformation create-stack \
    --region <REGION> \
    --stack-name cardinal-data-setup \
    --template-url https://cardinal-cfn.s3.us-east-2.amazonaws.com/lakerunner/<VERSION>/cardinal-data-setup.yaml \
    --parameters file:///tmp/data-setup-params.json

aws cloudformation wait stack-create-complete \
    --region <REGION> --stack-name cardinal-data-setup
```

The stack does not create IAM; no `CAPABILITY_*` flag is needed.
Stack create completes when the Lambda finishes its first invocation
(10-20 minutes on a cold install -- RDS provisioning dominates).

The Lambda is idempotent: every step does describe-then-act on a
deterministic name. If the stack ends up in `ROLLBACK_COMPLETE`,
delete it and re-run -- the data resources the Lambda already created
survive (S3 bucket, RDS instance, secrets) and the next run picks up
where the previous one left off.

## Step 2: harvest data-setup outputs

```sh
aws cloudformation describe-stacks \
    --region <REGION> --stack-name cardinal-data-setup \
    --query 'Stacks[0].Outputs' --output json > /tmp/data-setup-outputs.json
```

The data-setup outputs are 1:1 with the lakerunner data-layer
parameters. Mapping (left = data-setup output key, right = lakerunner
parameter key):

| `cardinal-data-setup` output | -> | `cardinal-lakerunner` parameter |
|---|---|---|
| `DbEndpoint` | -> | `DbEndpoint` |
| `DbPort` | -> | `DbPort` |
| `DbName` | -> | `DbName` |
| `DbMasterSecretArn` | -> | `DbMasterSecretArn` |
| `MaestroDbSecretArn` | -> | `MaestroDbSecretArn` |
| `IngestBucketName` | -> | `IngestBucketName` |
| `IngestQueueUrl` | -> | `IngestQueueUrl` |
| `IngestQueueArn` | -> | `IngestQueueArn` |
| `LicenseSecretArn` | -> | `LicenseSecretArn` |
| `InternalKeysSecretArn` | -> | `InternalKeysSecretArn` |
| `AdminKeySecretArn` | -> | `AdminKeySecretArn` |
| `StorageProfilesParamName` | -> | `StorageProfilesParamName` |
| `ApiKeysParamName` | -> | `ApiKeysParamName` |

The names are intentionally identical so the only operator action
between stacks is "rename `OutputKey` -> `ParameterKey`."

## Step 3: deploy the lakerunner stack

The lakerunner stack has many parameters. The required ones split
into three groups:

**A. From the data-setup stack outputs** (table above).

**B. Customer-supplied (IT)** -- IAM role ARNs and SG IDs:

- `TaskRoleArn`, `ExecutionRoleArn`, `MigrationLambdaRoleArn`
- `CertLambdaRoleArn` (only if importing PEMs)
- `TaskSgId`, `AlbSgId`

**C. Operator-supplied** -- network + TLS + DEX:

- `VpcId`, `PrivateSubnets` (same values used in step 1)
- One of:
  - `CertificateArn` (existing ACM cert), or
  - `CertificateBody` + `CertificatePrivateKey`
    (+ optional `CertificateChain`) for PEM import
- `DexAdminEmail` (e.g. `admin@example.com`)
- `DexAdminPasswordHash` (the `$2a$12$...` from "DEX admin password
  hash" above)
- `OidcSuperadminEmails` (comma-separated allowlist; default matches
  `DexAdminEmail`)
- `TemplateBaseUrl` (override away from the `dev` default to the
  release-tag path -- see "Published template URL")

Build a single JSON params file by combining the three groups. A
short Python helper makes the data-setup substitution mechanical:

```sh
python3 - <<'PY' > /tmp/lakerunner-params.json
import json, pathlib
ds = {o["OutputKey"]: o["OutputValue"] for o in
      json.loads(pathlib.Path("/tmp/data-setup-outputs.json").read_text())}

# Pass-through data-setup outputs (keys are identical on both sides)
data_setup_keys = [
    "DbEndpoint", "DbPort", "DbName", "DbMasterSecretArn",
    "MaestroDbSecretArn", "IngestBucketName", "IngestQueueUrl",
    "IngestQueueArn", "LicenseSecretArn", "InternalKeysSecretArn",
    "AdminKeySecretArn", "StorageProfilesParamName", "ApiKeysParamName",
]
params = [{"ParameterKey": k, "ParameterValue": ds[k]} for k in data_setup_keys]

# Customer-supplied + operator-supplied. Edit these for the install.
account = "<ACCOUNT>"
hash_text = pathlib.Path("dex-hash.txt").read_text().strip()
params += [
    {"ParameterKey": "VpcId",                 "ParameterValue": "vpc-..."},
    {"ParameterKey": "PrivateSubnets",        "ParameterValue": "subnet-aaaa,subnet-bbbb,subnet-cccc"},
    {"ParameterKey": "TaskRoleArn",           "ParameterValue": f"arn:aws:iam::{account}:role/cardinal-task-role"},
    {"ParameterKey": "ExecutionRoleArn",      "ParameterValue": f"arn:aws:iam::{account}:role/cardinal-execution-role"},
    {"ParameterKey": "MigrationLambdaRoleArn","ParameterValue": f"arn:aws:iam::{account}:role/cardinal-migration-lambda-role"},
    {"ParameterKey": "TaskSgId",              "ParameterValue": "sg-..."},
    {"ParameterKey": "AlbSgId",               "ParameterValue": "sg-..."},
    {"ParameterKey": "CertificateArn",        "ParameterValue": "arn:aws:acm:..."},
    {"ParameterKey": "DexAdminEmail",         "ParameterValue": "admin@example.com"},
    {"ParameterKey": "DexAdminPasswordHash",  "ParameterValue": hash_text},
    {"ParameterKey": "OidcSuperadminEmails",  "ParameterValue": "admin@example.com"},
    {"ParameterKey": "TemplateBaseUrl",       "ParameterValue": "https://cardinal-cfn.s3.us-east-2.amazonaws.com/lakerunner/<VERSION>/cardinal-lakerunner/"},
]
print(json.dumps(params, indent=2))
PY

aws cloudformation create-stack \
    --region <REGION> \
    --stack-name cardinal-lakerunner \
    --template-url https://cardinal-cfn.s3.us-east-2.amazonaws.com/lakerunner/<VERSION>/cardinal-lakerunner.yaml \
    --parameters file:///tmp/lakerunner-params.json

aws cloudformation wait stack-create-complete \
    --region <REGION> --stack-name cardinal-lakerunner
```

If you are importing PEMs instead of using `CertificateArn`, replace
the `CertificateArn` entry with `CertificateBody`,
`CertificatePrivateKey`, and (optionally) `CertificateChain`, and add
`CertLambdaRoleArn`. The PEMs must be passed as raw multi-line
strings -- stage them in the JSON params file the same way as
`LicenseData` in step 1.

Optional sizing parameters (replicas, CPU, memory per service) all
have sensible defaults; override only the ones you need to change.

The lakerunner stack does not create IAM, so no `CAPABILITY_*` flag
is needed. Total install time is typically 15-25 minutes.

## Step 4: post-install discovery

```sh
aws cloudformation describe-stacks \
    --region <REGION> --stack-name cardinal-lakerunner \
    --query 'Stacks[0].Outputs' --output table
```

The `AlbDnsName` output is the entry point for the maestro UI, the
query API, and the OTEL collector. End-to-end verification (target
group health, OIDC discovery, browser login, license acceptance) is
covered in `docs/operations/end-to-end-test-plan.md` Phase 3.

## Updates

To roll out a new release tag, update both stacks in order:

```sh
# 1. data-setup -- only matters if a new release ships a new Lambda zip.
aws cloudformation update-stack \
    --region <REGION> --stack-name cardinal-data-setup \
    --template-url https://cardinal-cfn.s3.us-east-2.amazonaws.com/lakerunner/<NEW_VERSION>/cardinal-data-setup.yaml \
    --parameters \
        ParameterKey=LambdaCodeS3Key,ParameterValue=lakerunner/<NEW_VERSION>/cardinal-data-setup-lambda.zip \
        $(jq -r '.[] | select(.ParameterKey != "LambdaCodeS3Key") | "ParameterKey=\(.ParameterKey),UsePreviousValue=true"' /tmp/data-setup-params.json | tr '\n' ' ')

# 2. lakerunner -- bump TemplateBaseUrl + image tags. Re-run the Python
#    block in step 3 with the new version, then update-stack.
aws cloudformation update-stack \
    --region <REGION> --stack-name cardinal-lakerunner \
    --template-url https://cardinal-cfn.s3.us-east-2.amazonaws.com/lakerunner/<NEW_VERSION>/cardinal-lakerunner.yaml \
    --parameters file:///tmp/lakerunner-params.json
```

For updates that touch IAM-policy-bearing resources -- there are
none in the application stack today, but a future release might --
deploy via a CloudFormation service role to keep operator IAM
permissions out of the rollback path. See `docs/operations/deploying.md`.

## Failure recovery

The data-setup Lambda is idempotent: each `ensure_*` step does
describe-then-act on a deterministic name, so re-invocation after a
partial failure converges. The Lambda's execution role grants update
+ delete on every resource it manages, so the Lambda recovers from
partial state on its own -- no IT break-glass involvement required.

Common failure modes:

- **VPC / subnets do not exist or are in the wrong region.** Lambda
  fails fast on `CreateDBSubnetGroup`. Fix the parameter, redeploy.
- **`DbSgId` does not exist.** Same shape; Lambda fails fast on
  `CreateDBInstance`.
- **`LicenseData` is malformed.** The Lambda creates the secret with
  the raw string; lakerunner services fail at runtime with a parse
  error. Overwrite the secret with the correct content via
  `aws secretsmanager put-secret-value` and restart the affected
  services.

If the data-setup stack ends up in `CREATE_FAILED` /
`ROLLBACK_FAILED` and the Lambda's logs do not explain it:

1. Delete the failed stack. Lambda-managed resources live outside
   the stack and survive.
2. Re-run from step 1; the Lambda will reconcile.

## Tearing down

The data-setup Lambda is a no-op on `RequestType=Delete` by default,
so `aws cloudformation delete-stack` removes the Lambda function and
the custom-resource record but **leaves all data resources intact**
(RDS, S3 ingest bucket, secrets, SSM params survive). This is
intentional -- those resources hold customer data.

The lakerunner stack is freely deletable: it owns no IAM, no SGs, no
RDS, and no S3 ingest, so nothing in it carries customer data. See
`docs/operations/tearing-down.md` for the survivor cleanup pass when
a real teardown (not just an upgrade-in-place) is required.
