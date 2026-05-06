# Installing Cardinal lakerunner

This is the install runbook for the Cardinal lakerunner CloudFormation
distribution. Spec:
`docs/superpowers/specs/2026-05-06-cardinal-cfn-prereqs-split-design.md`.

## Status

Phase 1 of the install layout pivot ships the data-setup Lambda + its
CFN wrapper + the customer-facing required-roles cookbook. Customers
can use these today to provision the data layer.

The lakerunner application stack continues to ship in its existing
shape (`cardinal-lakerunner.yaml` root + 12 nested children) and is
created/updated using the existing `scripts/deploy-lakerunner.sh`.
**Phase 2** (a follow-up PR) refactors the lakerunner children to
take the customer-supplied role ARNs / SG IDs / data-layer outputs as
parameters; until then the lakerunner stack still creates its own
roles internally.

The transitional install path:

1. Customer's IT pre-creates the IAM roles documented in
   `required-roles.md` (only the `DataSetupLambdaRoleArn` is needed
   for Phase 1; the lakerunner stack continues to create its own
   roles in Phase 1).
2. Customer creates the `cardinal-data-setup` stack (this PR).
3. Customer harvests the data-setup stack outputs.
4. Customer runs the existing lakerunner deploy script with the
   harvested values plumbed into the existing parameter surface
   (`DbEndpoint`, `DbSecretArn`, `BucketName`, `QueueUrl`, etc. --
   parameters that already exist on `cardinal-lakerunner.yaml`).

## Steps

### 1. IT pre-creates the data-setup Lambda role

Generate the cookbook locally:

```sh
make build
less docs/operations/required-roles.md
```

The customer's IT team creates a single IAM role named e.g.
`cardinal-data-setup-lambda-role` with the trust policy and inline
policy from the `cardinal-data-setup-lambda-role` section of the
cookbook. Hand the resulting role ARN back to the operator.

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

Or with jq for scripted reuse:

```sh
aws cloudformation describe-stacks \
    --stack-name cardinal-data-setup \
    --query 'Stacks[0].Outputs[*].[OutputKey,OutputValue]' --output text \
    > cardinal-data-setup-outputs.tsv
```

The outputs match the Lambda's response keys: `DbEndpoint`, `DbPort`,
`DbName`, `DbMasterSecretArn`, `MaestroDbSecretArn`,
`IngestBucketName`, `IngestQueueUrl`, `IngestQueueArn`,
`LicenseSecretArn`, `InternalKeysSecretArn`, `AdminKeySecretArn`,
`StorageProfilesParamName`, `ApiKeysParamName`.

### 4. Deploy the lakerunner application stack

Use the existing deploy script with the harvested values plumbed into
the existing `cardinal-lakerunner.yaml` parameters. See
`docs/operations/deploying.md` for the existing script's CLI surface.

(Phase 2 simplifies this step: the lakerunner stack will accept all
data-setup outputs as parameters with matching names, so the harvest
becomes a single `--parameter-overrides file://outputs.json`.)

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

## Phase 2 -- planned

- The lakerunner stack stops creating IAM roles + SGs, takes them as
  parameters.
- The lakerunner stack's database, storage, and config nested children
  are removed; their outputs become parameters threaded through the
  root from the data-setup stack's outputs.
- The deploy / teardown / Jenkins-related operator scripts are
  rewritten or retired.

See the spec for details.
