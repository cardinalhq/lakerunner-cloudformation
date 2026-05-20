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

The stack always creates its own resources. There is no mode for adopting
resources that `data-setup.sh` already created; an account that wants the
CloudFormation-managed data plane runs this stack instead of `data-setup.sh`,
not on top of it.

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
    "ParameterKey": "PrivateSubnets",
    "ParameterValue": "subnet-aaaaaaaa,subnet-bbbbbbbb"
  },
  {
    "ParameterKey": "DBSecurityGroupId",
    "ParameterValue": "sg-0123456789abcdef0"
  },
  {
    "ParameterKey": "LicenseData",
    "ParameterValue": "<license-token>"
  }
]
```

`PrivateSubnets` must contain two or more private subnet IDs in distinct AZs,
as a single comma-separated string. Do not include a `VpcId`; this stack does
not have a `VpcId` parameter.

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

RDS, the DB subnet group, the SQS queue, and the `cardinal-db-master` /
`cardinal-maestro-db` secrets are CloudFormation-generated names. The ingest
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
    "ParameterKey": "PrivateSubnets",
    "ParameterValue": "subnet-aaaaaaaa,subnet-bbbbbbbb"
  },
  {
    "ParameterKey": "DBSecurityGroupId",
    "ParameterValue": "sg-0123456789abcdef0"
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

- [`install-infrastructure.md`](install-infrastructure.md): the
  `data-setup.sh` shell path and prerequisite infrastructure.
- [`install-lakerunner.md`](install-lakerunner.md): the application stack that
  consumes this stack's outputs.
- `src/cardinal_cfn/cardinal_infrastructure.py`: the template generator.
