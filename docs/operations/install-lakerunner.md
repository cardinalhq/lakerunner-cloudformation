# Install part 2: lakerunner (the application)

Second of two install steps. Creates the stateless application: ALB,
twelve ECS services + their target groups + listener rules, and the
migration / cert-import custom resources. The ECS cluster, Cloud Map
namespace, IAM roles, and security groups are all consumed as
parameters -- the lakerunner stack creates none of them.

**Pre-req:** [`install-infrastructure.md`](install-infrastructure.md)
must have completed and you must have `/tmp/infra-outputs.json` from
its step 3.

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
  `CertificateChain`) -- ship raw PEMs; `cert.yaml` creates an
  `AWS::IAM::ServerCertificate` from them and the ALB listener uses its
  ARN. No Lambda, no out-of-band step. Useful for air-gapped customers
  and self-signed test certs. (No `CertLambdaRoleArn` parameter.)

## Step 2: build the params file

The lakerunner stack has many parameters. They split into three
groups: infra-setup outputs (group A), IT-supplied IAM/SG identifiers
(group B), and operator-supplied network + TLS + DEX values (group C).

Use the helper below to build a single combined JSON file. The
infra-setup script's JSON output keys are identical to the lakerunner
parameter keys so group A is mechanical.

```sh
python3 - <<'PY' > /tmp/lakerunner-params.json
import json, pathlib
infra = json.loads(pathlib.Path("/tmp/infra-outputs.json").read_text())

# Group A: pass-through infra-setup outputs (keys match 1:1).
infra_keys = [
    "DbEndpoint", "DbPort", "DbName", "DbMasterSecretArn",
    "IngestBucketName", "IngestQueueUrl",
    "IngestQueueArn", "LicenseSecretArn", "AdminKeySecretArn",
    "StorageProfilesParamName", "ApiKeysParamName",
    "ClusterName", "ClusterArn",
    "ServiceNamespaceId", "ServiceNamespaceName",
]
params = [{"ParameterKey": k, "ParameterValue": infra[k]} for k in infra_keys]

# Group B (IT) + Group C (operator). Edit these for the install.
account = "<ACCOUNT>"
hash_text = pathlib.Path("/tmp/dex-hash.txt").read_text().strip()
params += [
    # B: IT roles + SGs
    {"ParameterKey": "TaskRoleArn",            "ParameterValue": f"arn:aws:iam::{account}:role/cardinal-task-role"},
    {"ParameterKey": "ExecutionRoleArn",       "ParameterValue": f"arn:aws:iam::{account}:role/cardinal-execution-role"},
    {"ParameterKey": "TaskSgId",               "ParameterValue": "sg-..."},
    {"ParameterKey": "AlbSgId",                "ParameterValue": "sg-..."},
    # C: network + TLS + DEX
    {"ParameterKey": "VpcId",                  "ParameterValue": "vpc-..."},
    {"ParameterKey": "PrivateSubnets",         "ParameterValue": "subnet-aaaa,subnet-bbbb,subnet-cccc"},
    {"ParameterKey": "CertificateArn",         "ParameterValue": "arn:aws:acm:..."},
    {"ParameterKey": "DexAdminEmail",          "ParameterValue": "admin@example.com"},
    {"ParameterKey": "DexAdminPasswordHash",   "ParameterValue": hash_text},
    {"ParameterKey": "OidcSuperadminEmails",   "ParameterValue": "admin@example.com"},
    {"ParameterKey": "TemplateBaseUrl",        "ParameterValue": "https://cardinal-cfn-us-east-1.s3.us-east-1.amazonaws.com/lakerunner/<VERSION>/cardinal-lakerunner/"},
]
print(json.dumps(params, indent=2))
PY
```

If you are supplying PEMs instead of an `CertificateArn`, replace the
`CertificateArn` entry with `CertificateBody`, `CertificatePrivateKey`,
and (optionally) `CertificateChain` -- passed as raw multi-line strings
in the JSON params file. The stack builds an `AWS::IAM::ServerCertificate`
from them; nothing else is needed.

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
    --template-url https://cardinal-cfn-us-east-1.s3.us-east-1.amazonaws.com/lakerunner/<VERSION>/cardinal-lakerunner.yaml \
    --parameters file:///tmp/lakerunner-params.json

aws cloudformation wait stack-create-complete \
    --region <REGION> --stack-name cardinal-lakerunner
```

No `CAPABILITY_*` flag is needed. (The stack creates no IAM roles or
policies; the PEM cert path creates an `AWS::IAM::ServerCertificate`,
which is not a capability-gated resource type.) Total install time is
typically 10-15 minutes. The `migration` nested stack
comes up first -- it runs the migrator as an ECS task against the
existing RDS and only reports complete once that task succeeds -- and
the service-tier stacks (which `DependsOn` it) come up after.

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
    --template-url https://cardinal-cfn-us-east-1.s3.us-east-1.amazonaws.com/lakerunner/<NEW_VERSION>/cardinal-lakerunner.yaml \
    --parameters file:///tmp/lakerunner-params.json
```

For updates that touch IAM-policy-bearing resources -- there are none
in the application stack today, but a future release might -- deploy
via a CloudFormation service role to keep operator IAM permissions
out of the rollback path. See [`deploying.md`](deploying.md).

## Tearing down

The lakerunner stack owns no `Retain` or `Snapshot` resources. A plain
`aws cloudformation delete-stack cardinal-lakerunner` removes
everything it created; the infra layer (created by `data-setup.sh`)
survives by design. See [`tearing-down.md`](tearing-down.md) for the
full layered procedure.
