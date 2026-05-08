# Installing Cardinal lakerunner

Install runbook for the Cardinal lakerunner CloudFormation distribution.
Spec:
`docs/superpowers/specs/2026-05-06-cardinal-cfn-prereqs-split-design.md`.

## Architecture summary

The install splits cleanly into three layers, each owned by a different
party:

1. **Customer's IT** pre-creates the IAM roles and security groups
   Cardinal needs and hands the operator a list of ARNs / IDs. The role
   trust + policy contents come from
   `docs/operations/required-roles.md` (generated from
   `src/cardinal_cfn/required_roles_doc.py`, which assembles the policy
   fragments in `src/cardinal_cfn/iam_policies.py` so the doc never drifts
   from what Cardinal actually requires).
2. **Operator** deploys the `cardinal-data-setup` stack. That stack's
   only job is to deploy a Python Lambda and invoke it once. The Lambda
   creates the data layer (RDS, S3 ingest bucket, SQS queue, secrets,
   SSM params) and returns a JSON document with their identifiers as
   stack outputs.
3. **Operator** deploys the `cardinal-lakerunner` stack with the
   data-setup outputs and the customer-supplied role/SG ARNs/IDs as
   parameters. The lakerunner stack creates only stateless application
   resources (ECS cluster, ALB, ECS services, listeners, target
   groups). It never creates IAM, security groups, RDS, S3 ingest, or
   secrets -- everything comes in as parameters.

## Steps

### 1. IT pre-creates the required IAM roles and security groups

Generate the cookbook locally:

```sh
make build
less docs/operations/required-roles.md
```

The customer's IT team:

- Creates the IAM roles documented in `required-roles.md` (a single
  shared role works as long as it is granted the union of every inline
  policy in the cookbook). Hand the resulting role ARNs back.
- Creates the three security groups documented in the cookbook
  (`TaskSgId`, `AlbSgId`, `DbSgId`). Hand back the SG IDs.

### 2. Operator deploys the data-setup stack

```sh
aws cloudformation create-stack \
    --stack-name cardinal-data-setup \
    --template-url https://cardinal-cfn.s3.us-east-2.amazonaws.com/lakerunner/<VERSION>/cardinal-data-setup.yaml \
    --parameters \
        ParameterKey=DataSetupLambdaRoleArn,ParameterValue=arn:aws:iam::<ACCOUNT>:role/cardinal-data-setup-lambda-role \
        ParameterKey=VpcId,ParameterValue=vpc-... \
        ParameterKey=PrivateSubnets,ParameterValue=subnet-a\,subnet-b\,subnet-c \
        ParameterKey=DbSgId,ParameterValue=sg-... \
        ParameterKey=LicenseData,ParameterValue=file:///path/to/license.json \
        ParameterKey=DexAdminEmail,ParameterValue=admin@example.com \
        ParameterKey=DexAdminPasswordHash,ParameterValue=file:///path/to/hash.txt \
        ParameterKey=OidcSuperadminEmails,ParameterValue=alice@example.com\,bob@example.com \
    --capabilities CAPABILITY_NAMED_IAM
```

The stack deploys the data-setup Lambda and triggers it once. Stack
create completes when the Lambda finishes (10-20 min on cold installs
because of RDS). Stack outputs carry the Lambda's response.

Alternative for environments where customers cannot run CFN: deploy
the Lambda manually (`aws lambda create-function ...` from
`cardinal-data-setup-lambda.zip`) and invoke it directly:

```sh
aws lambda invoke \
    --function-name cardinal-data-setup \
    --payload "$(jq -n --arg vpc "$VPC_ID" --arg subnets "$PRIVATE_SUBNETS" --arg dbsg "$DB_SG_ID" \
                       --rawfile license license.json \
                       --rawfile hash hash.txt \
                       --arg email "$DEX_ADMIN_EMAIL" \
                       --arg superadmins "$OIDC_SUPERADMIN_EMAILS" \
                '{Region: env.AWS_REGION, VpcId: $vpc, PrivateSubnets: $subnets, DbSgId: $dbsg,
                  LicenseData: $license, DexAdminEmail: $email, DexAdminPasswordHash: $hash,
                  OidcSuperadminEmails: $superadmins}')" \
    response.json
```

### 3. Harvest stack outputs

```sh
aws cloudformation describe-stacks \
    --stack-name cardinal-data-setup \
    --query 'Stacks[0].Outputs' --output table
```

The data-setup outputs are 1:1 with `cardinal-lakerunner.yaml`'s
data-setup parameter group: `DbEndpoint`, `DbPort`, `DbName`,
`DbMasterSecretArn`, `MaestroDbSecretArn`, `IngestBucketName`,
`IngestQueueUrl`, `IngestQueueArn`, `LicenseSecretArn`,
`InternalKeysSecretArn`, `AdminKeySecretArn`,
`StorageProfilesParamName`, `ApiKeysParamName`.

### 4. Deploy the lakerunner application stack

```sh
aws cloudformation create-stack \
    --stack-name cardinal-lakerunner \
    --template-url https://cardinal-cfn.s3.us-east-2.amazonaws.com/lakerunner/<VERSION>/cardinal-lakerunner.yaml \
    --parameters \
        ParameterKey=VpcId,ParameterValue=vpc-... \
        ParameterKey=PrivateSubnets,ParameterValue=subnet-a\,subnet-b\,subnet-c \
        ParameterKey=CertificateArn,ParameterValue=arn:aws:acm:... \
        ParameterKey=TaskRoleArn,ParameterValue=arn:aws:iam::<ACCOUNT>:role/cardinal-task-role \
        ParameterKey=ExecutionRoleArn,ParameterValue=arn:aws:iam::<ACCOUNT>:role/cardinal-execution-role \
        ParameterKey=MigrationLambdaRoleArn,ParameterValue=arn:aws:iam::<ACCOUNT>:role/cardinal-migration-lambda-role \
        ParameterKey=TaskSgId,ParameterValue=sg-... \
        ParameterKey=AlbSgId,ParameterValue=sg-... \
        ParameterKey=DbEndpoint,ParameterValue=<from data-setup output> \
        ParameterKey=DbMasterSecretArn,ParameterValue=<from data-setup output> \
        ParameterKey=MaestroDbSecretArn,ParameterValue=<from data-setup output> \
        ParameterKey=IngestBucketName,ParameterValue=<from data-setup output> \
        ParameterKey=IngestQueueUrl,ParameterValue=<from data-setup output> \
        ParameterKey=IngestQueueArn,ParameterValue=<from data-setup output> \
        ParameterKey=LicenseSecretArn,ParameterValue=<from data-setup output> \
        ParameterKey=InternalKeysSecretArn,ParameterValue=<from data-setup output> \
        ParameterKey=AdminKeySecretArn,ParameterValue=<from data-setup output> \
        ParameterKey=StorageProfilesParamName,ParameterValue=<from data-setup output> \
        ParameterKey=ApiKeysParamName,ParameterValue=<from data-setup output> \
        ParameterKey=DexAdminPasswordHash,ParameterValue=file:///path/to/hash.txt
```

Optional: pass `CertLambdaRoleArn` (only required if `CertificateArn`
is empty, i.e. the PEM-import path is in use). All sizing parameters
have sensible defaults; override only the ones you need to change.

## Failure recovery

The data-setup Lambda is idempotent: each `ensure_*` step does
describe-then-act, so re-invocation after a partial failure converges.
The Lambda's execution role grants update + delete on every resource
it manages, so the Lambda recovers from partial state on its own --
no IT break-glass involvement required.

Common failure modes:

- **VPC subnet does not exist / is not in the right region.** Lambda
  fails fast on `CreateDBSubnetGroup`. Fix the parameter, redeploy.
- **DB SG ID does not exist.** Same shape; Lambda fails fast on
  `CreateDBInstance`.
- **License JSON is invalid.** Lambda creates the secret with the raw
  string; the lakerunner services fail at runtime with a parse error.
  Operator overwrites the secret with the correct JSON via
  `aws secretsmanager put-secret-value`.

If the data-setup stack ends up in a `CREATE_FAILED` /
`ROLLBACK_FAILED` state and the Lambda's logs do not explain it, the
operator can:

1. Delete the failed stack with retain-resources for any persistent
   resources the Lambda created (none of the Lambda's resources are
   in the stack -- they exist outside CFN, owned by the Lambda).
2. Re-run from step 2.

## Tearing down

The data-setup Lambda is a no-op on `RequestType=Delete` by default,
so `aws cloudformation delete-stack` removes the Lambda function and
the custom resource record but **leaves all data resources intact**
(RDS, S3 bucket, secrets, SSM params survive).

To actually destroy the data resources, the operator either:

- Asks IT to flip the Lambda's policy to allow Delete and re-invokes
  the Lambda with `RequestType=Delete` (a future enhancement -- not
  in this PR), or
- Manually deletes the resources using the customer's break-glass
  identity.

The lakerunner stack is freely deletable by the operator's own role:
it owns no IAM, no SGs, no RDS, and no S3 ingest, so nothing in the
stack carries customer data.
