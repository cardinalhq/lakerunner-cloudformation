# Run the `cardinal-infrastructure` stack

Use this runbook for the standalone `cardinal-infrastructure` CloudFormation
stack:

```text
https://cardinal-cfn.s3.<region>.amazonaws.com/lakerunner/<version>/cardinal-infrastructure.yaml
```

The stack creates the data-plane resources consumed by the
`cardinal-lakerunner` application stack: RDS, S3 ingest bucket, SQS ingest
queue, the four `cardinal-*` secrets, and the two `/cardinal/*` SSM
parameters.

The stack always creates its own resources. It is the single supported
infra path -- there is no shell-script alternative.

Replace these placeholders in the examples:

| Placeholder | Value |
|---|---|
| `<region>` | AWS region, for example `us-east-2` |
| `<version>` | Published lakerunner template version |
| `<account-id>` | AWS account ID |
| `<license-token>` | Cardinal license token, a single-line string beginning with `z64:` |

## Usage

Create this file as `infrastructure-parameters.json`:

```json
[
  {
    "ParameterKey": "VpcId",
    "ParameterValue": "vpc-0123456789abcdef0"
  },
  {
    "ParameterKey": "PrivateSubnets",
    "ParameterValue": "subnet-aaaaaaaa,subnet-bbbbbbbb"
  },
  {
    "ParameterKey": "LicenseData",
    "ParameterValue": "<license-token>"
  }
]
```

`PrivateSubnets` must contain two or more private subnet IDs in distinct
AZs, as a single comma-separated string. `VpcId` must match the VPC the
subnets live in; the stack creates the RDS security group in this VPC.

Run the stack:

```sh
aws cloudformation create-stack \
  --region <region> \
  --stack-name cardinal-infrastructure \
  --template-url https://cardinal-cfn.s3.<region>.amazonaws.com/lakerunner/<version>/cardinal-infrastructure.yaml \
  --parameters file://infrastructure-parameters.json

aws cloudformation wait stack-create-complete \
  --region <region> \
  --stack-name cardinal-infrastructure
```

RDS, the DB subnet group, the SQS queue, and the `cardinal-db-master`
secret are CloudFormation-generated names. The ingest
bucket is the one named resource that is predictable: leaving `IngestBucketName`
out makes the template create `cardinal-ingest-<account-id>-<region>`.

## Optional tuning

Add these keys to `infrastructure-parameters.json` only when you want values
other than the defaults:

| Parameter | Default |
|---|---|
| `DBEngineVersion` | `18.3` |
| `DBInstanceClass` | `db.t3.medium` |
| `DBAllocatedStorage` | `100` |
| `IngestBucketLifecycleDays` | `7` |

Example:

```json
[
  {
    "ParameterKey": "VpcId",
    "ParameterValue": "vpc-0123456789abcdef0"
  },
  {
    "ParameterKey": "PrivateSubnets",
    "ParameterValue": "subnet-aaaaaaaa,subnet-bbbbbbbb"
  },
  {
    "ParameterKey": "LicenseData",
    "ParameterValue": "<license-token>"
  },
  {
    "ParameterKey": "DBInstanceClass",
    "ParameterValue": "db.t3.large"
  },
  {
    "ParameterKey": "DBAllocatedStorage",
    "ParameterValue": "200"
  }
]
```

## Recovery-only overrides

Leave these parameters out during a normal create:

| Parameter | Default |
|---|---|
| `IngestBucketName` | blank, which computes `cardinal-ingest-<account-id>-<region>` |
| `LicenseSecretName` | `cardinal-license` |
| `AdminKeySecretName` | `cardinal-admin-key` |
| `StorageProfilesParamName` | `/cardinal/storage-profiles` |
| `ApiKeysParamName` | `/cardinal/api-keys` |

Set them only when recovering from a failed create that retained an orphaned
named resource and you intentionally want the retry to use a different name.

## See also

- [`install-infrastructure.md`](install-infrastructure.md): the prerequisite
  infrastructure overview.
- [`install-lakerunner.md`](install-lakerunner.md): the application stack that
  consumes this stack's outputs.
- `src/cardinal_cfn/cardinal_infrastructure.py`: the template generator.
