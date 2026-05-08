# Deploying the lakerunner stack from Jenkins

`jenkins/Jenkinsfile.lakerunner` is a declarative pipeline that creates **or** upgrades a single `cardinal-lakerunner` CloudFormation stack. Mode is auto-detected by `scripts/deploy-lakerunner.sh`: a missing stack triggers an install (CREATE change set), an existing stack triggers an upgrade (UPDATE change set). All deploy behaviour lives in the shell script; the Jenkinsfile is a thin wrapper.

One Jenkins job per install. Multi-install operators copy the Jenkinsfile into a separate job per stack and edit the parameter defaults at the top to match.

## What the pipeline does

1. **Pre-flight** — the script verifies `aws`, `jq`, and `curl` are on `PATH`. Missing tools fail fast with an actionable message naming the missing tool.
2. **Mode detect** — `aws cloudformation describe-stacks` decides CREATE vs UPDATE.
3. **Resolve parameters** — for each parameter declared by the new template:
    - `TemplateBaseUrl` is always set to `<--template-base-url>/<--version>/cardinal-lakerunner/`. It encodes the version path nested children load from, so it must track the deploy target — never carry forward, even when overridden for an air-gapped mirror.
    - **UPDATE only:** `*Image` parameters with a default in the new template take that default (refresh the image set on every upgrade). Toggle off via `RefreshImageDefaults=false`.
    - **UPDATE only:** parameters present in the running stack carry forward via `UsePreviousValue=true`.
    - **CREATE only:** parameters with a CLI flag value (job parameter) take that value.
    - Parameters new to the template that have a default take the default.
    - Parameters with no default and no value (carry-forward on UPDATE, CLI flag on CREATE) fail the deploy and name the missing parameter so the operator can supply it.
4. **Plan** — create a CloudFormation change set named `cardinal-deploy-<unix-timestamp>`, wait for it to settle, print the resource changes (adds / modifies / replacements / removes) to the build log.
5. **Approval** — only when `AutoApprove=false`: the pipeline pauses on a Jenkins `input` step.
6. **Apply** — execute the change set and wait for `CREATE_COMPLETE` (install) or `UPDATE_COMPLETE` (upgrade). Any other terminal state fails the build.
7. **Cleanup** — best-effort `delete-change-set` on any `cardinal-deploy-*` change set still pending on the stack.

The script's exit code is the build's exit code. Non-zero leaves the build red.

## Prerequisites

On the Jenkins agent that will run the job:

- POSIX shell (`/bin/sh`)
- AWS CLI v2
- `jq` (1.6+)
- `curl`
- The Jenkins **AWS Credentials** plugin
- For installs: the **Credentials Binding** plugin (used to bind secret-text and secret-file credentials)

The script will refuse to start if `aws`, `jq`, or `curl` is missing, with a one-liner naming the missing tool. Common install commands:

| OS | Install |
|---|---|
| Debian / Ubuntu | `sudo apt-get install -y awscli jq curl` |
| Amazon Linux | `sudo yum install -y aws-cli jq curl` |
| Alpine | `sudo apk add aws-cli jq curl` |
| macOS (Homebrew) | `brew install awscli jq curl` |

In your AWS account: a CloudFormation deployer role created from `cardinal-deployer-role.yaml` (see `docs/operations/deploying.md`). Strongly recommended; the deploy pipeline passes its ARN via `--role-arn` and avoids the `UPDATE_ROLLBACK_FAILED` wedge.

## Per-install setup

1. Copy `jenkins/Jenkinsfile.lakerunner` into a Jenkins **Pipeline** job (either inline, or pointed at this repo via "Pipeline script from SCM").
2. Edit the `parameters {}` defaults near the top of the file:
    - `StackName` — the install's stack name.
    - `Region` — the install's AWS region.
    - `AwsCredentialsId` — the Jenkins credential binding ID with permission to run CloudFormation against this account.
    - `DeployerRoleArn` — the ARN of the deployer role for this account.
3. **For new installs**, also fill in:
    - `VpcId`, `PrivateSubnets` (the application stack runs entirely in private subnets behind an internal ALB)
    - `LicenseData` — paste the license JSON. Visible in the build UI on purpose so you can verify it before running.
    - `DexAdminEmail`, `OidcSuperadminEmails`
    - `DexAdminPasswordHashCredentialId` — the ID of a Jenkins **Secret Text** credential containing the bcrypt hash. **Required.**
    - **TLS cert** — either `CertificateArn` (existing ACM cert) or the cert PEM credential IDs (`CertificateBodyCredentialId`, `CertificatePrivateKeyCredentialId`, optional `CertificateChainCredentialId`). See `docs/operations/certificates.md` for how to obtain a cert via ACM, bring an existing one, or generate a self-signed cert for dev.
4. Set `Version` to the release tag you intend to deploy (e.g. `v0.0.38`). **Required** -- there is no `latest` tag in the published bucket, every deploy is to an explicit version.
5. Save the job. Run.

For multiple installs, repeat for each — one job per install. After install, the install-only parameters are ignored on subsequent upgrade runs (their values are stored in the stack and carried forward via `UsePreviousValue`).

## Job parameters

### Common (both install and upgrade)

| Parameter | Default | Notes |
|---|---|---|
| `StackName` | `cardinal-lakerunner` | Edit per install |
| `Region` | `us-east-2` | Edit per install |
| `TemplateBaseUrl` | `https://cardinal-cfn-us-east-1.s3.us-east-1.amazonaws.com/lakerunner` | Override for air-gapped customers |
| `Version` | (empty -- required) | Published template tag, e.g. `v0.0.38` |
| `DeployerRoleArn` | (empty) | Strongly recommended; see `docs/operations/deploying.md` |
| `AutoApprove` | `true` | When `false`, the pipeline pauses for manual approval after the change set is described |
| `RefreshImageDefaults` | `true` | UPGRADE only: when `false`, `*Image` parameters carry forward instead of taking new template defaults |
| `AwsCredentialsId` | `aws-cardinal-deploy` | Jenkins AWS Credentials plugin binding ID |

### Install-only (ignored on upgrade)

| Parameter | Notes |
|---|---|
| `VpcId` | Required for install |
| `PrivateSubnets` | Required for install. Comma-separated subnet IDs. The ALB is internal-only and lives in these subnets |
| `CertificateArn` | Existing ACM cert. Alternative to PEM import below. See `docs/operations/certificates.md` for how to obtain one |
| `LicenseData` | License JSON. **Visible** in the build UI for verification |
| `DexAdminEmail` | DEX admin login email |
| `OidcSuperadminEmails` | Comma-separated maestro superadmin allowlist |
| `DexAdminPasswordHashCredentialId` | Jenkins **Secret Text** credential ID with the bcrypt hash. **Required for install** |
| `CertificateBodyCredentialId` | Jenkins **Secret File** credential ID with PEM cert (used when `CertificateArn` is empty) |
| `CertificatePrivateKeyCredentialId` | Jenkins **Secret File** credential ID with PEM private key |
| `CertificateChainCredentialId` | Jenkins **Secret File** credential ID with the intermediate chain PEM |

## AWS auth alternatives

The default uses the Jenkins AWS Credentials plugin (recommended). Two alternatives:

- **IAM instance profile on the Jenkins agent.** Set `AwsCredentialsId` to a binding that returns no credentials, and ensure the agent's instance profile has the required permissions. The AWS CLI will pick up the instance role automatically.
- **Assume role from the agent.** Wrap the script invocation in `aws sts assume-role` + `eval "$(aws sts ... | jq ... )"` to export `AWS_*` env vars before calling the script. Keep this in the Jenkinsfile rather than the script — the script reads `AWS_*` env vars directly and stays auth-agnostic.

## Air-gapped variant

Mirror `https://cardinal-cfn-us-east-1.s3.us-east-1.amazonaws.com/lakerunner/<version>/` to a private bucket (or any HTTPS-reachable location). Set the `TemplateBaseUrl` parameter on the Jenkins job to point at your mirror. Override per-image parameters via your stack defaults so customer images come from your private registry rather than `public.ecr.aws/...`.

## Reading the change set summary

The Plan stage prints a table per resource change:

```
| Action     | Type                          | Logical              | Replacement |
| ---------- | ----------------------------- | -------------------- | ----------- |
| Modify     | AWS::ECS::Service             | QueryApi             | False       |
| Modify     | AWS::ECS::TaskDefinition      | QueryApiTaskDef      | True        |
| Add        | AWS::IAM::Policy              | NewServicePolicy     | -           |
```

What to look for:

- **`Replacement: True`** on a stateful resource (RDS, S3, persistent EBS): stop and investigate. Image-tag bumps should never replace storage.
- **`Action: Remove`** on something you didn't expect: stop and investigate. The published templates do not remove resources during an in-place upgrade.
- A flood of `Modify` rows across all services on a published image bump is normal.
- For installs, expect a long list of `Add` rows — every resource the stack creates.

When `AutoApprove=false`, the pipeline pauses here and waits for an operator to click "Apply".

## Recovery

If the deploy lands the stack in `UPDATE_FAILED`, `UPDATE_ROLLBACK_COMPLETE`, `CREATE_FAILED`, or `ROLLBACK_COMPLETE`, the build is red. For upgrades, the stack has either applied nothing or fully rolled back automatically. Re-run the job once the underlying issue is fixed:

- **Bad image tag** → publish a fixed image, re-run.
- **Parameter without default required** → the script prints the parameter name; supply it on the next run (currently via the script's flags or by adding it as a job parameter).
- **AWS API throttling / transient error** → re-run.
- **Install rolled back (`ROLLBACK_COMPLETE`)** → just re-run the job. The script auto-deletes the empty `ROLLBACK_COMPLETE` stack and re-enters CREATE mode (same recovery path as `REVIEW_IN_PROGRESS`).

If the stack lands in `UPDATE_ROLLBACK_FAILED`, you are in the IAM-rollback wedge described in `docs/operations/deploying.md`. The fix is to ensure all upgrades use a deployer role; see that document for one-time recovery steps.

## Versioning

Every deploy targets an explicit published tag (`v0.0.38`, `v0.0.39`, ...). There is no `latest` rolling tag in the published bucket -- it would defeat reproducibility and audit trails.

Bumping the install means changing the `Version` parameter on the Jenkins job and re-running. Rollback is the same operation with the previous `Version`.

## What the pipeline does *not* do

- Does not orchestrate multiple installs in sequence. One job per install.
- Does not rotate sensitive parameters (`LicenseData`, `ApiKeysOverride`, `StorageProfilesOverride`) on upgrade. Those carry forward via `UsePreviousValue=true`. Rotation is a separate operation.
- Does not perform application-level health probes after the deploy. The success signal is "CloudFormation reached `CREATE_COMPLETE` / `UPDATE_COMPLETE`". Anything richer (HTTP probes, target group health, etc.) is a follow-on responsibility.
- Does not fall back to retries on its own. CloudFormation's automatic rollback handles the bad-update case; the per-service ECS deployment circuit breaker (already configured in the stack) handles bad image tags.

## Running the script standalone

The script is self-contained and runnable outside Jenkins for emergency deploys or dry runs:

```sh
export AWS_REGION=us-east-2
export AWS_PROFILE=cardinal-prod  # or AWS_ACCESS_KEY_ID, etc.

# Upgrade
sh scripts/deploy-lakerunner.sh \
    --stack-name cardinal-lakerunner \
    --region us-east-2 \
    --version v1.20.0 \
    --deployer-role-arn arn:aws:iam::123456789012:role/cardinal-cfn-deployer \
    --no-execute

# Install
sh scripts/deploy-lakerunner.sh \
    --stack-name cardinal-lakerunner \
    --region us-east-2 \
    --version v1.20.0 \
    --deployer-role-arn arn:aws:iam::123456789012:role/cardinal-cfn-deployer \
    --vpc-id vpc-0abc... \
    --private-subnets subnet-1,subnet-2 \
    --license-data-file ./license.json \
    --dex-admin-password-hash "$(cat ./dex-hash.txt)" \
    --no-execute
```

`--no-execute` plans only; the change set is left in place for manual inspection. Drop the flag to apply.
