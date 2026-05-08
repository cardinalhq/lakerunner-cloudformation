# Install part 1: infrastructure (the data layer)

This is the first of two install steps. It creates the resources
Cardinal needs to hold customer data: an RDS Postgres instance, an S3
ingest bucket, an SQS ingest queue, the Secrets Manager secrets that
hold the license / admin / DB credentials, and two SSM parameters.

After this stack reaches `CREATE_COMPLETE`, the data layer exists
**outside** any CloudFormation stack -- the data-setup Lambda created
the resources directly. Continue with
[`install-lakerunner.md`](install-lakerunner.md) to deploy the
application.

## Layer ownership at a glance

| Layer | Owned by | Where it lives |
|---|---|---|
| IAM roles + security groups | Customer's IT (out of band) | Pre-created using the cookbook in [`required-roles.md`](required-roles.md). |
| Data layer (RDS, S3, SQS, secrets, SSM) | `cardinal-data-setup` stack -> Lambda | Resources live outside CFN; only the Lambda is in the stack. **This document.** |
| Application layer (ECS, ALB, services) | `cardinal-lakerunner` stack | Stateless. Owned end-to-end by CFN. **Next document.** |

## Step 0: IT prereqs (one-time, out of band)

The customer's IT team must pre-create:

- **Five IAM roles** -- `cardinal-task-role`, `cardinal-execution-role`,
  `cardinal-migration-lambda-role`, `cardinal-data-setup-lambda-role`,
  and (optional) `cardinal-cert-lambda-role`. Trust principals + inline
  policies are documented in
  [`required-roles.md`](required-roles.md), generated from
  `src/cardinal_cfn/iam_policies.py` so they never drift from what the
  templates expect.
- **Three security groups** in the target VPC -- `TaskSgId`, `AlbSgId`,
  `DbSgId`. Required ingress is in `required-roles.md` under
  "Required security groups".

Customers free to use a single shared role with the union of every
inline policy; the role's trust must allow both
`ecs-tasks.amazonaws.com` and `lambda.amazonaws.com`.

This step is identical for both stacks; do it once.

## Step 1: collect the parameters

The `cardinal-data-setup.yaml` template takes:

| Parameter | Source | Notes |
|---|---|---|
| `DataSetupLambdaRoleArn` | IT (step 0) | Pre-created Lambda execution role. |
| `VpcId` | Operator | Target VPC for the RDS subnet group. |
| `PrivateSubnets` | Operator | `List<AWS::EC2::Subnet::Id>` (CSV when staged in JSON params). At least two subnets in distinct AZs. |
| `DbSgId` | IT (step 0) | DB security group ID. |
| `LicenseData` | Operator | Raw single-line `z64:...` license file content. NoEcho. |
| `BucketLifecycleDays` | Optional | S3 ingest object expiry (default `7`). |
| `DbInstanceClass` | Optional | RDS class (default `db.t3.medium`). |
| `DbAllocatedStorage` | Optional | RDS GiB (default `100`). |
| `LambdaCodeS3Url` | Optional | Full `s3://<bucket>/<prefix>/<version>/<file>.zip` URL of the data-setup Lambda zip. Must point at a bucket in the **same region** as this stack. Default targets us-east-2; override for any other region. See "Lambda code URL by region" below. |

There are **no DEX or OIDC parameters** on this stack -- those flow
only into the lakerunner stack.

### Lambda code URL by region

The data-setup Lambda's code zip must live in an S3 bucket in the
same region as the Lambda function. The published artifact is
mirrored to one regional bucket per supported region:

| Region | Default `LambdaCodeS3Url` |
|---|---|
| `us-east-1` | `s3://cardinal-cfn-us-east-1/lakerunner/<VERSION>/cardinal-data-setup-lambda.zip` |
| `us-east-2` | `s3://cardinal-cfn-us-east-2/lakerunner/<VERSION>/cardinal-data-setup-lambda.zip` (template default) |

Air-gapped mirrors must preserve the same key shape -- exactly three
slash-separated segments after the bucket -- because the template
parses `LambdaCodeS3Url` with a fixed-depth `Fn::Split`.

## Step 2: published template URL

Templates are published per release tag at:

```
https://cardinal-cfn.s3.us-east-2.amazonaws.com/lakerunner/<VERSION>/<template>.yaml
```

Replace `<VERSION>` with the explicit release tag (e.g. `v0.0.41`).
There is no `latest` tag -- every install is to a named version. The
same `<VERSION>` is used for both stacks.

## Step 3: deploy

Stage parameters in a JSON file rather than inline so the multi-line
license content is easy to handle:

```sh
LICENSE_DATA="$(cat ~/path/to/license.token)"   # the file beginning with "z64:"

cat > /tmp/data-setup-params.json <<EOF
[
  {"ParameterKey": "DataSetupLambdaRoleArn", "ParameterValue": "arn:aws:iam::<ACCOUNT>:role/cardinal-data-setup-lambda-role"},
  {"ParameterKey": "VpcId",                  "ParameterValue": "vpc-..."},
  {"ParameterKey": "PrivateSubnets",         "ParameterValue": "subnet-aaaa,subnet-bbbb,subnet-cccc"},
  {"ParameterKey": "DbSgId",                 "ParameterValue": "sg-..."},
  {"ParameterKey": "LicenseData",            "ParameterValue": "${LICENSE_DATA}"},
  {"ParameterKey": "LambdaCodeS3Url",        "ParameterValue": "s3://cardinal-cfn-<REGION>/lakerunner/<VERSION>/cardinal-data-setup-lambda.zip"}
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

The stack does not create IAM; no `CAPABILITY_*` flag is needed. Stack
create completes when the Lambda finishes its first invocation (10-20
minutes on a cold install -- RDS provisioning dominates).

## Alternative: skip the Lambda, run the steps directly

`scripts/data-setup.sh` is a POSIX-shell, AWS-CLI-only driver that is
functionally equivalent to the data-setup Lambda. Use it when you want
to drive the data layer from Jenkins (or any CI runner) without
deploying the wrapper CFN stack at all. Parameters are at the top of
the script as env-overridable variables; outputs land on stdout as a
JSON document with the same 13 keys the data-setup stack emits.

```sh
REGION=us-east-2 \
VPC_ID=vpc-... \
PRIVATE_SUBNETS=subnet-aaaa,subnet-bbbb \
DB_SG_ID=sg-... \
LICENSE_DATA_FILE=/path/to/license.token \
    scripts/data-setup.sh > /tmp/data-setup-outputs.json
```

The script is idempotent (each step does describe-then-act on the same
deterministic resource names the Lambda uses) so re-running after a
partial failure converges. The caller's identity needs the same
permissions the data-setup Lambda's role has -- see
[`required-roles.md`](required-roles.md), `cardinal-data-setup-lambda-role`.

If you use this path, skip step 3 above. Continue to step 4.

## Step 4: harvest outputs

These outputs are 1:1 with the `cardinal-lakerunner` data-layer
parameters; the next install step uses them as direct inputs.

```sh
aws cloudformation describe-stacks \
    --region <REGION> --stack-name cardinal-data-setup \
    --query 'Stacks[0].Outputs' --output json > /tmp/data-setup-outputs.json
```

Output keys (all identical to the corresponding `cardinal-lakerunner`
parameter keys):

`DbEndpoint`, `DbPort`, `DbName`, `DbMasterSecretArn`,
`MaestroDbSecretArn`, `IngestBucketName`, `IngestQueueUrl`,
`IngestQueueArn`, `LicenseSecretArn`, `InternalKeysSecretArn`,
`AdminKeySecretArn`, `StorageProfilesParamName`, `ApiKeysParamName`.

**Next:** [`install-lakerunner.md`](install-lakerunner.md).

## Failure recovery

The data-setup Lambda is idempotent: each `ensure_*` step does
describe-then-act on a deterministic name, so re-invocation after a
partial failure converges. The Lambda's execution role grants update +
delete on every resource it manages, so the Lambda recovers from
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
`ROLLBACK_FAILED` and the Lambda's logs do not explain it, delete the
failed stack and re-run from step 3. Lambda-managed resources live
outside the stack and survive; the next run reconciles to the
desired state.

## Tearing down

The data-setup Lambda is a no-op on `RequestType=Delete` by design.
`aws cloudformation delete-stack cardinal-data-setup` removes only the
Lambda function and the custom-resource record; **the data layer
survives** (RDS, S3 bucket, secrets, SSM params, SQS queue all
remain). This is intentional -- those resources hold customer data.

To wipe the data layer for a real decommission, see the "Layer 2"
procedure in [`tearing-down.md`](tearing-down.md). For a redeploy
against the existing data, just delete the application stack and
re-run [`install-lakerunner.md`](install-lakerunner.md).
