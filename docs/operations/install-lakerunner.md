# Install part 2: lakerunner (the application)

Second of two install steps. Creates the stateless application: ALB,
twelve ECS services + their target groups + listener rules, the
migration / cert-import resources, and every security group + IAM role
the application needs. The customer's contributions narrow to: ECS
cluster + VPC + private subnets + a TLS cert (or PEMs).

**Pre-req:** [`install-infrastructure.md`](install-infrastructure.md)
must have completed. Note its stack outputs -- you will thread them in
as `cardinal-lakerunner` parameters.

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
  `AWS::IAM::ServerCertificate` from them and the ALB listener uses
  its ARN. Useful for air-gapped customers and self-signed test certs.

## Step 2: build the params file

Three input groups:

1. **Infrastructure-stack outputs** -- pulled from
   `aws cloudformation describe-stacks --stack-name cardinal-infrastructure`.
2. **Customer-supplied identifiers** -- ECS cluster + VPC + private
   subnets (and the TLS cert).
3. **Operator decisions** -- DEX admin email + bcrypt hash, optional
   ALB inbound CIDR overrides, sizing knobs.

Build the params file by reading the infra stack outputs:

```sh
aws cloudformation describe-stacks \
    --region <REGION> --stack-name cardinal-infrastructure \
    --query 'Stacks[0].Outputs' --output json > /tmp/infra-outputs.json

python3 - <<'PY' > /tmp/lakerunner-params.json
import json, pathlib

raw = json.loads(pathlib.Path("/tmp/infra-outputs.json").read_text())
infra = {o["OutputKey"]: o["OutputValue"] for o in raw}

# Group 1: pass-through infra-stack outputs (keys match 1:1).
infra_keys = [
    "DbEndpoint", "DbPort", "DbName", "DbMasterSecretArn",
    "RdsSecurityGroupId",
    "IngestBucketName", "IngestQueueUrl", "IngestQueueArn",
    "LicenseSecretArn", "AdminKeySecretArn",
    "StorageProfilesParamName", "ApiKeysParamName",
]
params = [{"ParameterKey": k, "ParameterValue": infra[k]} for k in infra_keys]

# Group 2: customer-supplied. Edit these per install.
hash_text = pathlib.Path("/tmp/dex-hash.txt").read_text().strip()
params += [
    {"ParameterKey": "ClusterName",            "ParameterValue": "<ECS-CLUSTER-NAME>"},
    {"ParameterKey": "ClusterArn",             "ParameterValue": "arn:aws:ecs:<REGION>:<ACCOUNT>:cluster/<ECS-CLUSTER-NAME>"},
    {"ParameterKey": "VpcId",                  "ParameterValue": "vpc-..."},
    {"ParameterKey": "PrivateSubnets",         "ParameterValue": "subnet-aaaa,subnet-bbbb,subnet-cccc"},
    {"ParameterKey": "CertificateArn",         "ParameterValue": "arn:aws:acm:..."},
    # Group 3: operator decisions
    {"ParameterKey": "DexAdminEmail",          "ParameterValue": "admin@example.com"},
    {"ParameterKey": "DexAdminPasswordHash",   "ParameterValue": hash_text},
    {"ParameterKey": "OidcSuperadminEmails",   "ParameterValue": "admin@example.com"},
    {"ParameterKey": "TemplateBaseUrl",        "ParameterValue": "https://cardinal-cfn-us-east-1.s3.us-east-1.amazonaws.com/lakerunner/<VERSION>/cardinal-lakerunner/"},
]
print(json.dumps(params, indent=2))
PY
```

If you are supplying PEMs instead of a `CertificateArn`, replace the
`CertificateArn` entry with `CertificateBody`, `CertificatePrivateKey`,
and (optionally) `CertificateChain` -- passed as raw multi-line strings
in the JSON params file. The stack builds an
`AWS::IAM::ServerCertificate` from them; nothing else is needed.

`TemplateBaseUrl` defaults to the `dev` channel; release installs must
override it to the matching version directory (trailing slash
required).

### ALB inbound CIDRs (optional)

The ALB is internal-scheme and lives in the private subnets. Inbound
HTTPS (443) / admin-HTTPS (9443) / OTLP-HTTP (4318) defaults allow
all RFC1918 ranges (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`).
To tighten, override one or more of `AlbAllowedCidr1` /
`AlbAllowedCidr2` / `AlbAllowedCidr3`; set the unused slots to the
empty string to drop their ingress rules entirely.

### Cloud Map namespace (optional)

The lakerunner stack creates a private DNS namespace in the VPC for
in-cluster service discovery (default `cardinal.local`). Override
`ServiceNamespaceName` if that clashes with an existing namespace in
the VPC.

### Sizing parameters (optional)

Replicas / CPU / memory per service all have sensible defaults;
override only the ones you need to change.

## Step 3: deploy

```sh
aws cloudformation create-stack \
    --region <REGION> \
    --stack-name cardinal-lakerunner \
    --template-url https://cardinal-cfn-us-east-1.s3.us-east-1.amazonaws.com/lakerunner/<VERSION>/cardinal-lakerunner.yaml \
    --parameters file:///tmp/lakerunner-params.json \
    --capabilities CAPABILITY_IAM

aws cloudformation wait stack-create-complete \
    --region <REGION> --stack-name cardinal-lakerunner
```

`CAPABILITY_IAM` is required because the `Security` nested child
creates the per-tier task roles + the shared execution role. Total
install time is typically 10-15 minutes. The `Security` nested stack
comes up first, followed by `Migration` (runs the migrator as an ECS
task against the RDS) which reports complete only after the migration
task succeeds, and finally the service-tier stacks which `DependsOn`
it.

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
mechanical -- the infrastructure-stack outputs do not change unless
you also update the infrastructure stack) and run `update-stack`:

```sh
aws cloudformation update-stack \
    --region <REGION> --stack-name cardinal-lakerunner \
    --template-url https://cardinal-cfn-us-east-1.s3.us-east-1.amazonaws.com/lakerunner/<NEW_VERSION>/cardinal-lakerunner.yaml \
    --parameters file:///tmp/lakerunner-params.json \
    --capabilities CAPABILITY_IAM
```

For updates that touch IAM-policy-bearing resources, deploy via a
CloudFormation service role to keep operator IAM permissions out of
the rollback path. See [`deploying.md`](deploying.md).

## Tearing down

The lakerunner stack owns no `Retain` or `Snapshot` resources. A plain
`aws cloudformation delete-stack cardinal-lakerunner` removes
everything it created; the infrastructure layer survives by design
(it carries `Retain` / `Snapshot` policies). See
[`tearing-down.md`](tearing-down.md) for the full layered procedure.
