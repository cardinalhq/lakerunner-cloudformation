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

There are only two supported scenarios:

1. **Brand new usage**: no existing `data-setup.sh` resources; let this stack
   create the infrastructure.
2. **Adopting existing infrastructure**: `data-setup.sh` already created the
   resources; import those live resources into this stack.

Replace these placeholders in the examples:

| Placeholder | Value |
|---|---|
| `<region>` | AWS region, for example `us-east-2` |
| `<version>` | Published lakerunner template version |
| `<account-id>` | AWS account ID |
| `<license-token>` | Cardinal license token, a single-line string beginning with `z64:` |

## Scenario 1: brand new usage

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

For brand new usage, omit all import-only name parameters:

```text
DBInstanceIdentifier
DBSubnetGroupName
IngestQueueName
DBMasterSecretName
MaestroDBSecretName
```

Leaving those out lets CloudFormation use generated names where the template is
designed to do so. The ingest bucket is the exception: leaving
`IngestBucketName` out makes the template create
`cardinal-ingest-<account-id>-<region>`.

## Scenario 2: adopting existing infrastructure

Use this path only when `scripts/data-setup.sh` has already created the
resources. Adoption is a CloudFormation resource import, so you need:

1. a parameter file with `ImportMode=Yes`;
2. a resources-to-import file listing the live resources;
3. a follow-up stack update with `ImportMode=No`.

### 2.1 Capture live values

Get the live RDS tuning values and use them in the import parameter file:

```sh
aws rds describe-db-instances \
  --region <region> \
  --db-instance-identifier cardinal-db \
  --query 'DBInstances[0].{EngineVersion:EngineVersion,DBInstanceClass:DBInstanceClass,AllocatedStorage:AllocatedStorage,SubnetGroup:DBSubnetGroup.DBSubnetGroupName,SecurityGroups:VpcSecurityGroups[].VpcSecurityGroupId,SubnetGroupSubnets:DBSubnetGroup.Subnets[].SubnetIdentifier}' \
  --output table
```

If you cannot easily run arbitrary AWS CLI commands from Jenkins, get these
values from the AWS Console instead. In RDS, open the `cardinal-db` database
and copy the engine version, DB instance class, and allocated storage from the
Configuration tab.

Also confirm the live ingest-bucket lifecycle expiration (it is `7` unless the
operator ran `data-setup.sh` with a non-default `BUCKET_LIFECYCLE_DAYS`):

```sh
aws s3api get-bucket-lifecycle-configuration \
  --bucket cardinal-ingest-<account-id>-<region> \
  --query 'Rules[?ID==`cardinal-ingest-expire`].Expiration.Days' \
  --output text
```

Use the actual values returned by AWS for `DBEngineVersion`, `DBInstanceClass`,
`DBAllocatedStorage`, and `IngestBucketLifecycleDays`. The `PrivateSubnets` and
`DBSecurityGroupId` you pass must also equal the live DB subnet group's subnets
and the live instance's security group, respectively. If any of these do not
match the imported resource, CloudFormation reports drift and may try to modify
the resource on the next stack update.

### 2.2 Create the import parameter file

Create this file as `infrastructure-import-parameters.json`:

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
    "ParameterKey": "ImportMode",
    "ParameterValue": "Yes"
  },
  {
    "ParameterKey": "DBInstanceIdentifier",
    "ParameterValue": "cardinal-db"
  },
  {
    "ParameterKey": "DBSubnetGroupName",
    "ParameterValue": "cardinal-db-subnet-group"
  },
  {
    "ParameterKey": "IngestQueueName",
    "ParameterValue": "cardinal-ingest"
  },
  {
    "ParameterKey": "DBMasterSecretName",
    "ParameterValue": "cardinal-db-master"
  },
  {
    "ParameterKey": "MaestroDBSecretName",
    "ParameterValue": "cardinal-maestro-db"
  },
  {
    "ParameterKey": "DBEngineVersion",
    "ParameterValue": "<live-engine-version>"
  },
  {
    "ParameterKey": "DBInstanceClass",
    "ParameterValue": "<live-db-instance-class>"
  },
  {
    "ParameterKey": "DBAllocatedStorage",
    "ParameterValue": "<live-allocated-storage-gib>"
  },
  {
    "ParameterKey": "IngestBucketLifecycleDays",
    "ParameterValue": "7"
  }
]
```

For an unmodified `data-setup.sh` install, the fixed names above are the names
to use. Do not change them after import; they are how CloudFormation continues
to identify the imported resources.

Pass the **same** `LicenseData` token that is already stored in the live
`cardinal-license` secret. The template sets that secret's value from this
parameter, so the follow-up update in step 2.5 will overwrite the live secret
if you supply a different token.

### 2.3 Create the resources-to-import file

Create this file as `infrastructure-resources-to-import.json`:

```json
[
  {
    "ResourceType": "AWS::SQS::Queue",
    "LogicalResourceId": "IngestQueue",
    "ResourceIdentifier": {
      "QueueUrl": "https://sqs.<region>.amazonaws.com/<account-id>/cardinal-ingest"
    }
  },
  {
    "ResourceType": "AWS::S3::Bucket",
    "LogicalResourceId": "IngestBucket",
    "ResourceIdentifier": {
      "BucketName": "cardinal-ingest-<account-id>-<region>"
    }
  },
  {
    "ResourceType": "AWS::RDS::DBSubnetGroup",
    "LogicalResourceId": "DBSubnetGroup",
    "ResourceIdentifier": {
      "DBSubnetGroupName": "cardinal-db-subnet-group"
    }
  },
  {
    "ResourceType": "AWS::SecretsManager::Secret",
    "LogicalResourceId": "DBMasterSecret",
    "ResourceIdentifier": {
      "Id": "arn:aws:secretsmanager:<region>:<account-id>:secret:cardinal-db-master-<suffix>"
    }
  },
  {
    "ResourceType": "AWS::RDS::DBInstance",
    "LogicalResourceId": "DBInstance",
    "ResourceIdentifier": {
      "DBInstanceIdentifier": "cardinal-db"
    }
  },
  {
    "ResourceType": "AWS::SecretsManager::Secret",
    "LogicalResourceId": "LicenseSecret",
    "ResourceIdentifier": {
      "Id": "arn:aws:secretsmanager:<region>:<account-id>:secret:cardinal-license-<suffix>"
    }
  },
  {
    "ResourceType": "AWS::SecretsManager::Secret",
    "LogicalResourceId": "AdminKeySecret",
    "ResourceIdentifier": {
      "Id": "arn:aws:secretsmanager:<region>:<account-id>:secret:cardinal-admin-key-<suffix>"
    }
  },
  {
    "ResourceType": "AWS::SecretsManager::Secret",
    "LogicalResourceId": "MaestroDBSecret",
    "ResourceIdentifier": {
      "Id": "arn:aws:secretsmanager:<region>:<account-id>:secret:cardinal-maestro-db-<suffix>"
    }
  },
  {
    "ResourceType": "AWS::SSM::Parameter",
    "LogicalResourceId": "StorageProfilesParam",
    "ResourceIdentifier": {
      "Name": "/cardinal/storage-profiles"
    }
  },
  {
    "ResourceType": "AWS::SSM::Parameter",
    "LogicalResourceId": "ApiKeysParam",
    "ResourceIdentifier": {
      "Name": "/cardinal/api-keys"
    }
  }
]
```

The SQS queue is identified by its **URL**, not its name. Get the exact URL
with:

```sh
aws sqs get-queue-url \
  --region <region> \
  --queue-name cardinal-ingest \
  --query QueueUrl \
  --output text
```

For Secrets Manager resources, use the full secret ARN, including the random
suffix AWS appends to the name. In the AWS Console, open Secrets Manager,
choose each secret, and copy its ARN from the secret details page. If you can
run AWS CLI discovery commands, get the exact ARNs with:

```sh
aws secretsmanager describe-secret \
  --region <region> \
  --secret-id cardinal-db-master \
  --query ARN \
  --output text
```

Repeat that command for:

```text
cardinal-license
cardinal-admin-key
cardinal-maestro-db
```

### 2.4 Create and execute the import change set

Create the import change set:

```sh
aws cloudformation create-change-set \
  --region <region> \
  --stack-name cardinal-infrastructure \
  --change-set-name import-cardinal-infrastructure \
  --change-set-type IMPORT \
  --template-url https://cardinal-cfn.s3.<region>.amazonaws.com/lakerunner/<version>/cardinal-infrastructure.yaml \
  --parameters file://infrastructure-import-parameters.json \
  --resources-to-import file://infrastructure-resources-to-import.json
```

Wait for CloudFormation to finish preparing it:

```sh
aws cloudformation wait change-set-create-complete \
  --region <region> \
  --stack-name cardinal-infrastructure \
  --change-set-name import-cardinal-infrastructure
```

Review it:

```sh
aws cloudformation describe-change-set \
  --region <region> \
  --stack-name cardinal-infrastructure \
  --change-set-name import-cardinal-infrastructure
```

Execute it:

```sh
aws cloudformation execute-change-set \
  --region <region> \
  --stack-name cardinal-infrastructure \
  --change-set-name import-cardinal-infrastructure

aws cloudformation wait stack-import-complete \
  --region <region> \
  --stack-name cardinal-infrastructure
```

`ImportMode=Yes` intentionally skips two CloudFormation-only resources:
`IngestQueuePolicy` and `DBMasterSecretAttachment`. Add them back in the next
step.

### 2.5 Turn import mode off

Create this file as `infrastructure-adopted-parameters.json`. It is the same
as `infrastructure-import-parameters.json`, except `ImportMode` is `No`:

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
    "ParameterKey": "ImportMode",
    "ParameterValue": "No"
  },
  {
    "ParameterKey": "DBInstanceIdentifier",
    "ParameterValue": "cardinal-db"
  },
  {
    "ParameterKey": "DBSubnetGroupName",
    "ParameterValue": "cardinal-db-subnet-group"
  },
  {
    "ParameterKey": "IngestQueueName",
    "ParameterValue": "cardinal-ingest"
  },
  {
    "ParameterKey": "DBMasterSecretName",
    "ParameterValue": "cardinal-db-master"
  },
  {
    "ParameterKey": "MaestroDBSecretName",
    "ParameterValue": "cardinal-maestro-db"
  },
  {
    "ParameterKey": "DBEngineVersion",
    "ParameterValue": "<live-engine-version>"
  },
  {
    "ParameterKey": "DBInstanceClass",
    "ParameterValue": "<live-db-instance-class>"
  },
  {
    "ParameterKey": "DBAllocatedStorage",
    "ParameterValue": "<live-allocated-storage-gib>"
  },
  {
    "ParameterKey": "IngestBucketLifecycleDays",
    "ParameterValue": "7"
  }
]
```

Run the follow-up update:

```sh
aws cloudformation update-stack \
  --region <region> \
  --stack-name cardinal-infrastructure \
  --template-url https://cardinal-cfn.s3.<region>.amazonaws.com/lakerunner/<version>/cardinal-infrastructure.yaml \
  --parameters file://infrastructure-adopted-parameters.json

aws cloudformation wait stack-update-complete \
  --region <region> \
  --stack-name cardinal-infrastructure
```

After this update, the stack owns the imported resources and the two skipped
CloudFormation-only resources are present in the template again.

## Optional tuning for brand new usage

For a brand new stack, add these keys to `infrastructure-parameters.json` only
when you want values other than the defaults:

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

Leave these parameters out during normal create and adoption:

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
