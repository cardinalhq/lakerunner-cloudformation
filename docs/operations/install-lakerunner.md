# Install part 2: lakerunner (the application)

Second of two install steps. Creates the stateless application
infrastructure: ECS cluster, ALB, twelve services + their target
groups + listener rules, and the migration / cert-import custom
resources.

**Pre-req:** [`install-infrastructure.md`](install-infrastructure.md)
must be at `CREATE_COMPLETE` and you must have
`/tmp/data-setup-outputs.json` from its step 4.

## Step 1: prep secondary inputs

### DEX admin password hash

The maestro Go services accept bcrypt `$2a` / `$2b` prefixes; `htpasswd`
emits `$2y` which must be rewritten:

```sh
PASSWORD='choose-a-strong-one'
htpasswd -bnBC 12 "" "$PASSWORD" | tr -d ':\n' | sed 's/^\$2y/\$2a/' > /tmp/dex-hash.txt
# /tmp/dex-hash.txt contains a 60-char $2a$12$... bcrypt hash
```

Keep the plaintext password somewhere secure -- it is what the admin
will type at the maestro login. Only the bcrypt hash is stored on the
stack.

### TLS certificate

Choose one of:

- **`CertificateArn`** -- pass an existing ACM cert ARN. Recommended for
  production. See [`certificates.md`](certificates.md) for issuance
  guidance.
- **`CertificateBody` + `CertificatePrivateKey`** (+ optional
  `CertificateChain`) -- ship raw PEMs and let the cert-import Lambda
  push them into ACM. Required for air-gapped customers; useful for
  testing with self-signed certs. Requires the `CertLambdaRoleArn`
  parameter (the IT-side role from
  [`required-roles.md`](required-roles.md)).

## Step 2: build the params file

The lakerunner stack has many parameters. They split into three
groups: data-setup outputs (group A), IT-supplied IAM/SG identifiers
(group B), and operator-supplied network + TLS + DEX values (group C).

Use the helper below to build a single combined JSON file. The data-
setup output keys are identical to the lakerunner parameter keys so
group A is mechanical.

```sh
python3 - <<'PY' > /tmp/lakerunner-params.json
import json, pathlib
ds = {o["OutputKey"]: o["OutputValue"] for o in
      json.loads(pathlib.Path("/tmp/data-setup-outputs.json").read_text())}

# Group A: pass-through data-setup outputs (keys match 1:1).
data_setup_keys = [
    "DbEndpoint", "DbPort", "DbName", "DbMasterSecretArn",
    "MaestroDbSecretArn", "IngestBucketName", "IngestQueueUrl",
    "IngestQueueArn", "LicenseSecretArn", "InternalKeysSecretArn",
    "AdminKeySecretArn", "StorageProfilesParamName", "ApiKeysParamName",
]
params = [{"ParameterKey": k, "ParameterValue": ds[k]} for k in data_setup_keys]

# Group B (IT) + Group C (operator). Edit these for the install.
account = "<ACCOUNT>"
hash_text = pathlib.Path("/tmp/dex-hash.txt").read_text().strip()
params += [
    # B: IT roles + SGs
    {"ParameterKey": "TaskRoleArn",            "ParameterValue": f"arn:aws:iam::{account}:role/cardinal-task-role"},
    {"ParameterKey": "ExecutionRoleArn",       "ParameterValue": f"arn:aws:iam::{account}:role/cardinal-execution-role"},
    {"ParameterKey": "MigrationLambdaRoleArn", "ParameterValue": f"arn:aws:iam::{account}:role/cardinal-migration-lambda-role"},
    {"ParameterKey": "TaskSgId",               "ParameterValue": "sg-..."},
    {"ParameterKey": "AlbSgId",                "ParameterValue": "sg-..."},
    # C: network + TLS + DEX
    {"ParameterKey": "VpcId",                  "ParameterValue": "vpc-..."},
    {"ParameterKey": "PrivateSubnets",         "ParameterValue": "subnet-aaaa,subnet-bbbb,subnet-cccc"},
    {"ParameterKey": "CertificateArn",         "ParameterValue": "arn:aws:acm:..."},
    {"ParameterKey": "DexAdminEmail",          "ParameterValue": "admin@example.com"},
    {"ParameterKey": "DexAdminPasswordHash",   "ParameterValue": hash_text},
    {"ParameterKey": "OidcSuperadminEmails",   "ParameterValue": "admin@example.com"},
    {"ParameterKey": "TemplateBaseUrl",        "ParameterValue": "https://cardinal-cfn.s3.us-east-2.amazonaws.com/lakerunner/<VERSION>/cardinal-lakerunner/"},
]
print(json.dumps(params, indent=2))
PY
```

If you are importing PEMs instead of using `CertificateArn`, replace
the `CertificateArn` entry with `CertificateBody`,
`CertificatePrivateKey`, and (optionally) `CertificateChain`, and add
`CertLambdaRoleArn`. PEMs are passed as raw multi-line strings in the
JSON params file, the same way `LicenseData` was in the infrastructure
step.

`TemplateBaseUrl` defaults to the `dev` channel; release installs must
override it to the matching version directory (trailing slash
required).

Optional sizing parameters (replicas, CPU, memory per service) all
have sensible defaults; override only the ones you need to change.

## Step 3: deploy

```sh
aws cloudformation create-stack \
    --region <REGION> \
    --stack-name cardinal-lakerunner \
    --template-url https://cardinal-cfn.s3.us-east-2.amazonaws.com/lakerunner/<VERSION>/cardinal-lakerunner.yaml \
    --parameters file:///tmp/lakerunner-params.json

aws cloudformation wait stack-create-complete \
    --region <REGION> --stack-name cardinal-lakerunner
```

The stack creates no IAM, so no `CAPABILITY_*` flag is needed. Total
install time is typically 10-15 minutes (12 ECS services come up in
parallel; the migration custom resource runs against the existing
RDS).

## Step 4: post-install discovery

```sh
aws cloudformation describe-stacks \
    --region <REGION> --stack-name cardinal-lakerunner \
    --query 'Stacks[0].Outputs' --output table
```

Outputs of note:

- `AlbDnsName` -- internal DNS name of the shared ALB. The ALB is
  internal-scheme; reach it from inside the VPC via SSM Session
  Manager, a bastion, a VPN, Direct Connect, or Route 53 + private
  hosted zone.
- `MaestroUrl`, `QueryApiUrl` -- baseline URLs that hang off the ALB
  DNS.
- `InstallIdLong`, `InstallIdShort` -- per-install identifiers used in
  log group names and (legacy) secret names.

For end-to-end verification (target group health, OIDC discovery,
browser login, license acceptance) follow Phase 3 of
[`end-to-end-test-plan.md`](end-to-end-test-plan.md).

## Updates

To roll out a new release tag, rebuild the params file (mostly
mechanical -- the data-setup outputs do not change unless you also
update the infrastructure stack) and run `update-stack`:

```sh
aws cloudformation update-stack \
    --region <REGION> --stack-name cardinal-lakerunner \
    --template-url https://cardinal-cfn.s3.us-east-2.amazonaws.com/lakerunner/<NEW_VERSION>/cardinal-lakerunner.yaml \
    --parameters file:///tmp/lakerunner-params.json
```

For updates that touch IAM-policy-bearing resources -- there are none
in the application stack today, but a future release might -- deploy
via a CloudFormation service role to keep operator IAM permissions
out of the rollback path. See [`deploying.md`](deploying.md).

## Tearing down

The lakerunner stack owns no `Retain` or `Snapshot` resources. A plain
`aws cloudformation delete-stack cardinal-lakerunner` removes
everything it created; the data layer (created by the data-setup
Lambda) survives by design. See [`tearing-down.md`](tearing-down.md)
for the full layered procedure.
