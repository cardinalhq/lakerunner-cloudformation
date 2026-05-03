# Upgrading the lakerunner stack from Jenkins

`jenkins/Jenkinsfile.upgrade-lakerunner` is a declarative pipeline that upgrades a single existing `cardinal-lakerunner` CloudFormation stack to a newer published template version. All upgrade behaviour lives in `scripts/upgrade-lakerunner.sh`; the Jenkinsfile is a thin wrapper.

One Jenkins job per install. Multi-install operators copy the Jenkinsfile into a separate job per stack and edit the parameter defaults at the top to match.

## What the pipeline does

1. **Pre-flight** — the script verifies `aws`, `jq`, and `curl` are on `PATH`. Missing tools fail fast with an actionable message naming the missing tool.
2. **Resolve parameters** — for each parameter declared by the new template:
    - `TemplateBaseUrl` is always set to `<--template-base-url>/<--version>/cardinal-lakerunner/`. This parameter encodes the version path nested children load from, so it must track the upgrade target — never carry forward, even if the customer overrode `--template-base-url` for an air-gapped mirror.
    - `*Image` parameters with a default in the new template take that default (refresh the image set on every upgrade).
    - Parameters present in the running stack carry forward via `UsePreviousValue=true`.
    - Parameters new in the new template that have a default take the default.
    - Parameters new in the new template with no default fail the upgrade and name the missing parameter so the operator can supply it.
3. **Plan** — create a CloudFormation change set named `cardinal-upgrade-<unix-timestamp>`, wait for it to settle, print the resource changes (adds / modifies / replacements / removes) to the build log.
4. **Approval** — only when `AutoApprove=false`: the pipeline pauses on a Jenkins `input` step.
5. **Apply** — execute the change set and wait for `UPDATE_COMPLETE`. Any other terminal state fails the build.
6. **Cleanup** — best-effort `delete-change-set` on any `cardinal-upgrade-*` change set still pending on the stack.

The script's exit code is the build's exit code. Non-zero leaves the build red.

## Prerequisites

On the Jenkins agent that will run the job:

- POSIX shell (`/bin/sh`)
- AWS CLI v2
- `jq` (1.6+)
- `curl`
- The Jenkins **AWS Credentials** plugin

The script will refuse to start if `aws`, `jq`, or `curl` is missing, with a one-liner naming the missing tool. Common install commands:

| OS | Install |
|---|---|
| Debian / Ubuntu | `sudo apt-get install -y awscli jq curl` |
| Amazon Linux | `sudo yum install -y aws-cli jq curl` |
| Alpine | `sudo apk add aws-cli jq curl` |
| macOS (Homebrew) | `brew install awscli jq curl` |

In your AWS account: a CloudFormation deployer role created from `cardinal-deployer-role.yaml` (see `docs/operations/deploying.md`). Strongly recommended; the upgrade pipeline passes its ARN via `--role-arn` and avoids the `UPDATE_ROLLBACK_FAILED` wedge.

## Per-install setup

1. Copy `jenkins/Jenkinsfile.upgrade-lakerunner` into a Jenkins **Pipeline** job (either inline, or pointed at this repo via "Pipeline script from SCM").
2. Edit the `parameters {}` defaults near the top of the file:
    - `StackName` — the install's stack name.
    - `Region` — the install's AWS region.
    - `AwsCredentialsId` — the Jenkins credential binding ID with permission to run CloudFormation against this account.
    - `DeployerRoleArn` — the ARN of the deployer role for this account.
3. Optionally pin `Version` to a specific release (e.g. `v1.20.0`) instead of `latest`.
4. Save the job. Run.

For multiple installs, repeat for each — one job per install.

## Job parameters

| Parameter | Default | Notes |
|---|---|---|
| `StackName` | `cardinal-lakerunner` | Edit per install |
| `Region` | `us-east-2` | Edit per install |
| `TemplateBaseUrl` | `https://cardinal-cfn.s3.us-east-2.amazonaws.com/lakerunner` | Override for air-gapped customers |
| `Version` | `latest` | Pin to a release for reproducibility |
| `DeployerRoleArn` | (empty) | Strongly recommended; see `docs/operations/deploying.md` |
| `AutoApprove` | `true` | When `false`, the pipeline pauses for manual approval after the change set is described |
| `RefreshImageDefaults` | `true` | When `false`, `*Image` parameters carry forward instead of taking new template defaults |
| `AwsCredentialsId` | `aws-cardinal-deploy` | Jenkins AWS Credentials plugin binding ID |

## AWS auth alternatives

The default uses the Jenkins AWS Credentials plugin (recommended). Two alternatives:

- **IAM instance profile on the Jenkins agent.** Set `AwsCredentialsId` to a binding that returns no credentials, and ensure the agent's instance profile has the required permissions. The AWS CLI will pick up the instance role automatically.
- **Assume role from the agent.** Wrap the script invocation in `aws sts assume-role` + `eval "$(aws sts ... | jq ... )"` to export `AWS_*` env vars before calling the script. Keep this in the Jenkinsfile rather than the script — the script reads `AWS_*` env vars directly and stays auth-agnostic.

## Air-gapped variant

Mirror `https://cardinal-cfn.s3.us-east-2.amazonaws.com/lakerunner/<version>/` to a private bucket (or any HTTPS-reachable location). Set the `TemplateBaseUrl` parameter on the Jenkins job to point at your mirror. Override per-image parameters via your stack defaults so customer images come from your private registry rather than `public.ecr.aws/...`.

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

When `AutoApprove=false`, the pipeline pauses here and waits for an operator to click "Apply".

## Recovery

If the upgrade lands the stack in `UPDATE_FAILED` or `UPDATE_ROLLBACK_COMPLETE`, the build is red and the stack has either applied nothing or fully rolled back automatically. Re-run the job once the underlying issue is fixed:

- **Bad image tag** → publish a fixed image, re-run.
- **Parameter without default required** → the script prints the parameter name; supply it on the next run (currently via the script's flags or by adding it as a job parameter).
- **AWS API throttling / transient error** → re-run.

If the stack lands in `UPDATE_ROLLBACK_FAILED`, you are in the IAM-rollback wedge described in `docs/operations/deploying.md`. The fix is to ensure all upgrades use a deployer role; see that document for one-time recovery steps.

## Pinning vs. tracking `latest`

Use `Version=latest` for environments where you want to follow the published rolling tag (development, internal staging). Pin to an explicit version (`v1.20.0`) for environments where you want reproducible upgrades — production, regulated installs, anything an audit trail must explain.

When pinning, bumping the install means changing the `Version` parameter on the Jenkins job and re-running. This pattern also makes a rollback trivial: re-run with the previous `Version`.

## What the pipeline does *not* do

- Does not orchestrate multiple installs in sequence. One job per install.
- Does not rotate sensitive parameters (`LicenseData`, `ApiKeysOverride`, `StorageProfilesOverride`). Those carry forward via `UsePreviousValue=true`. Rotation is a separate operation.
- Does not perform application-level health probes after the upgrade. The success signal is "CloudFormation reached `UPDATE_COMPLETE`". Anything richer (HTTP probes, target group health, etc.) is a follow-on responsibility.
- Does not fall back to retries on its own. CloudFormation's automatic rollback on update failure handles the bad-update case; the per-service ECS deployment circuit breaker (already configured in the stack) handles bad image tags.

## Running the script standalone

The script is self-contained and runnable outside Jenkins for emergency upgrades or dry runs:

```sh
export AWS_REGION=us-east-2
export AWS_PROFILE=cardinal-prod  # or AWS_ACCESS_KEY_ID, etc.

sh scripts/upgrade-lakerunner.sh \
    --stack-name cardinal-lakerunner \
    --region us-east-2 \
    --version v1.20.0 \
    --deployer-role-arn arn:aws:iam::123456789012:role/cardinal-cfn-deployer \
    --no-execute
```

`--no-execute` plans only; the change set is left in place for manual inspection. Drop the flag to apply.
