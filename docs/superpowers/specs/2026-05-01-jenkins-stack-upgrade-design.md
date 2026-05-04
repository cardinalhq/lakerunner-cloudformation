# Jenkins lakerunner stack upgrade — design

Date: 2026-05-01

> **Historical note (2026-05-04):** The pipeline grew to handle initial install
> in addition to upgrade. Current names are `scripts/deploy-lakerunner.sh`,
> `jenkins/Jenkinsfile.lakerunner`, and `docs/operations/jenkins-deploy.md`.
> Mode is auto-detected from `describe-stacks`. The parameter-resolution rules
> below still describe the UPDATE path; CREATE-mode resolution is documented
> in the operator doc.

## Goal

Ship a Jenkins job template plus an extracted driving script that customers (and Cardinal itself) use to safely upgrade an existing `cardinal-lakerunner` CloudFormation stack to a newer published template version. Design favors the common case — published templates ship with refreshed image defaults — while leaving every other parameter untouched.

Multiple installs are managed by copying the Jenkinsfile per install and editing the parameter defaults at the top. There is one Jenkins job per install.

## Deliverables

- `scripts/upgrade-lakerunner.sh` — POSIX shell driving script that does the actual deploy and reports success or failure via exit code.
- `jenkins/Jenkinsfile.upgrade-lakerunner` — declarative Jenkins pipeline that binds AWS credentials and invokes the script.
- `docs/operations/jenkins-upgrade.md` — operator documentation.
- `tests/unit/test_upgrade_lakerunner.py` — pytest unit tests that drive the script's `--internal-resolve-params` mode against fixture inputs to validate parameter resolution and no-op detection.

## Non-goals

- Multi-install orchestration (sequential dev → staging → prod rollout). Out of scope; each install is its own job.
- Slack / email / chat notifications.
- Multi-region simultaneous upgrade.
- Rotation of sensitive parameters (`LicenseData`, `ApiKeysOverride`, `StorageProfilesOverride`). They carry forward via `UsePreviousValue=true`.
- Drift detection.
- Application-level health probes after the upgrade. Confirmation is "did CloudFormation reach `UPDATE_COMPLETE`" — nothing more.
- A standalone Cardinal-internal pipeline. The customer-facing template is the canonical artifact and Cardinal uses it directly.
- Any runtime dependency on the `cardinal_cfn` Python package or other contents of this repo. The script is self-contained — at runtime it sees only the published `cardinal-lakerunner.yaml` in S3 and the AWS CLI.

## Runtime dependencies on the Jenkins worker

- POSIX shell (`/bin/sh`).
- `aws` CLI v2.
- `jq` (1.6 or later — for building and inspecting JSON).
- `curl` (for the HTTP HEAD probe of the template URL).

No Python, no `boto3`, no `pyyaml`. The script does not parse the template YAML directly — it uses `aws cloudformation get-template-summary` to discover the new template's parameter schema, which returns names, types, defaults, and `NoEcho` flags as structured JSON.

## Driving script — `scripts/upgrade-lakerunner.sh`

The script is the single source of truth for upgrade behavior. The Jenkinsfile is a thin wrapper around it. It is also runnable standalone from an operator laptop or any other CI system, so customers can use it outside Jenkins for emergency upgrades or dry runs.

### Inputs

Every input is provided as a long-form flag. Flags that are also exposed as Jenkins job parameters share names (kebab-case for flags, PascalCase in Jenkins) and meaning.

| Flag | Required | Default | Purpose |
|---|---|---|---|
| `--stack-name` | yes | — | Existing stack to upgrade |
| `--region` | yes | — | AWS region of the stack |
| `--template-base-url` | no | `https://cardinal-cfn.s3.us-east-2.amazonaws.com/lakerunner` | Published template prefix; override for air-gapped mirrors |
| `--version` | no | `latest` | Path segment between the base URL and `cardinal-lakerunner.yaml` |
| `--deployer-role-arn` | no | (empty) | When set, passed via `--role-arn` to all CloudFormation calls |
| `--refresh-image-defaults` / `--no-refresh-image-defaults` | no | refresh on | When on, parameters whose name ends in `Image` take the new template's defaults; when off, they carry forward |
| `--no-execute` | no | (off) | Create and describe the change set, then stop without executing. Leaves the change set in place for inspection |

The script reads `AWS_*` environment variables for credentials. It does not touch `~/.aws/credentials`.

### Behavior

The script runs these stages in order. Any non-zero result from a stage aborts the script with a clear error pointing at the failed stage.

1. **Pre-flight tool check.** The script's first action, before parsing flags or making AWS calls. It runs `aws --version`, `jq --version`, and `curl --version` (each piped to `>/dev/null 2>&1`) and on any failure exits code 2 with a single-line message naming the missing tool and a hint at how to install it on the common runner OSes (`apt-get install jq`, `yum install jq`, `apk add jq`, etc.). This is the bulkhead that turns "missing tool" into an obvious operator error rather than a confusing failure halfway through the upgrade.

2. **Validate inputs.** Confirm required flags are present. Confirm `--stack-name` exists in `--region` via `describe-stacks`. If the stack doesn't exist, exit code 2 — this script does upgrades, not initial creates.

3. **Resolve template URL.** `${template_base_url}/${version}/cardinal-lakerunner.yaml`. Probe with an HTTP HEAD; abort on 404.

4. **Discover new template parameters.** `aws cloudformation get-template-summary --template-url <resolved url> --query 'Parameters'` returns the new template's parameter schema as JSON: name, type, default (when present), `NoEcho` flag. No local YAML parsing.

5. **Resolve parameters.** Walk the new template's parameter list. For each parameter, choose exactly one of these outcomes:

   1. **Refresh image default.** Name ends in `Image`, `--refresh-image-defaults` is on, and the new template declares a default. Result: `{ParameterKey, ParameterValue=<new template default>}`.
   2. **Carry forward.** The parameter exists in the current stack's parameter set (per `describe-stacks`). Result: `{ParameterKey, UsePreviousValue=true}`.
   3. **Take new default.** The parameter is new in the new template (didn't exist in the current stack) and the new template declares a default. Result: `{ParameterKey, ParameterValue=<new template default>}`.
   4. **Hard fail.** None of the above. Exit non-zero with a message naming the parameter and explaining the operator must supply a value.

   The script never sets `--use-previous-parameters`; every decision is explicit and visible in the resolved `parameters.json`.

6. **Create change set.** `aws cloudformation create-change-set --change-set-type UPDATE` with the resolved parameters. Change set name is `cardinal-upgrade-<unix-timestamp>` so cleanup logic can identify and remove change sets created by this script. Pass `--role-arn` when set. Wait for the change set to reach a terminal state.

   - Terminal `CREATE_COMPLETE`: proceed.
   - Terminal `FAILED` whose `StatusReason` contains the no-op marker (AWS uses several phrasings; the script matches a small set including `didn't contain changes` and `The submitted information didn't contain changes`): delete the change set and exit success. No-op upgrades are normal and quiet.
   - Any other terminal state: delete the change set and exit non-zero with the AWS-supplied reason.

7. **Describe and print change set summary.** Always print the resource changes — additions, modifications, replacements, removals — to stdout via `aws cloudformation describe-change-set`. Always printed regardless of `--no-execute`.

8. **Stop here on `--no-execute`.** Do not delete the change set. Print its name and ARN so the operator can inspect or execute it manually. Exit success.

9. **Execute change set.** `aws cloudformation execute-change-set`, then `aws cloudformation wait stack-update-complete`. The waiter exits non-zero if the stack lands in any state other than `UPDATE_COMPLETE`.

10. **Cleanup on abort.** If any stage between 6 and 9 fails or the script is interrupted, attempt a best-effort `delete-change-set`. Failure to delete the change set does not change the script's exit code.

11. **Print stack outputs** on success — `AlbDnsName`, `MaestroUrl`, `QueryApiUrl`, etc.

### Exit codes

- `0` — change set executed and stack reached `UPDATE_COMPLETE`, or change set was a no-op, or `--no-execute` completed.
- `1` — generic error (AWS API failure, change set failed, stack ended in a non-`UPDATE_COMPLETE` state).
- `2` — pre-flight or input validation failure (missing tool, missing flag, stack not found, parameter has no resolvable value).

CloudFormation does its own automatic rollback on update failure; the per-service deployment circuit breaker (already configured in the stack) handles bad image deploys at the ECS level. The script does not implement its own rollback.

## Jenkinsfile — `jenkins/Jenkinsfile.upgrade-lakerunner`

A declarative pipeline. Customers copy this file once per install and edit the `parameters {}` defaults at the top to match that install's stack name, region, and credentials binding ID.

### Job parameters

| Parameter | Type | Default | Notes |
|---|---|---|---|
| `StackName` | string | `cardinal-lakerunner` | Edit per install |
| `Region` | string | `us-east-2` | Edit per install |
| `TemplateBaseUrl` | string | `https://cardinal-cfn.s3.us-east-2.amazonaws.com/lakerunner` | Edit for air-gapped customers |
| `Version` | string | `latest` | Use a pinned version (`v1.20.0`) for reproducibility |
| `DeployerRoleArn` | string | (empty) | Strongly recommended; see `docs/operations/deploying.md` |
| `AutoApprove` | bool | `true` | When `false`, the pipeline pauses at a Jenkins `input` step after the change set is described |
| `RefreshImageDefaults` | bool | `true` | Pass through to the script |
| `AwsCredentialsId` | string | `aws-cardinal-deploy` | Jenkins AWS Credentials plugin binding ID |

### Stages

1. **Setup** — `withCredentials([[$class: 'AmazonWebServicesCredentialsBinding', credentialsId: params.AwsCredentialsId]])`. The script's pre-flight check handles tool verification; the Jenkinsfile does not duplicate it.
2. **Plan** — invoke `sh scripts/upgrade-lakerunner.sh --no-execute ...`. The build log captures the change set summary. Always runs.
3. **Approval** — only when `params.AutoApprove == false`: a Jenkins `input` step. Skipped entirely when `true` (no human required).
4. **Apply** — invoke the script again *without* `--no-execute`. The script's no-op detector ensures this second run is cheap when the Plan stage already created an equivalent change set, and the operator (or auto-approval) is committing to applying whatever the live state requires at apply time.
5. **Post (always)** — best-effort cleanup pass: list change sets on the stack and `delete-change-set` any whose name starts with `cardinal-upgrade-` and is still in a pending (non-executed) state.

### Why two full script invocations rather than create-then-execute the same change set?

Reusing a change set across stages requires the Jenkinsfile to track the change set ARN and pass it to a separate `execute-change-set` step. That spreads the upgrade logic across the script and the Jenkinsfile. Re-running the script keeps each invocation self-contained and the Jenkinsfile trivial. The cost is a few extra seconds for the second `create-change-set`; the benefit is that the script remains the single source of truth for upgrade behavior. Customers who need stronger plan-vs-apply equivalence pin `Version` so the template body cannot drift between the two runs.

### Failure surfacing

The script's exit code is the build's exit code. Jenkins shows the build red on any non-zero exit. The script's stderr is captured in the build log.

## Operator documentation — `docs/operations/jenkins-upgrade.md`

Covers:

- Pre-reqs (AWS Credentials plugin, deployer role, Jenkins agent with `aws` CLI v2, `jq`, and `curl` available).
- Per-install setup: copy the Jenkinsfile, set the parameter defaults at the top, point a Jenkins job at it.
- AWS auth alternatives: AWS Credentials plugin (default), IAM instance profile on the worker, assume-role from the worker.
- Air-gapped variant: customer-mirrored `TemplateBaseUrl` plus image override parameters.
- How to read a change set summary in the build log.
- Recovery if the upgrade lands in `UPDATE_FAILED` or `UPDATE_ROLLBACK_COMPLETE` (re-run with the same or fixed parameters; CFN's automatic rollback restores the previous state on most failures).
- Pinning a version vs. tracking `latest`.

## Testing

To make the shell script testable in isolation, it exposes two internal-only flags used by tests:

- `--internal-resolve-params <new-template-summary.json> <current-stack-params.json>` — given two pre-fetched JSON blobs (instead of calling AWS), runs the parameter-resolution function and prints the resulting `parameters.json` to stdout. No AWS calls. No side effects. Pure data transform.
- `--internal-classify-changeset-status <status> <status-reason>` — classifies a change set status into `success`, `noop`, or `failure`. No AWS calls.

These flags are documented as test hooks only and are not part of the public CLI surface.

Tests:

- **Unit, parameter resolution** (`tests/unit/test_upgrade_lakerunner.py`). Each pytest case writes JSON fixtures to a temp dir, invokes the script via `subprocess` with `--internal-resolve-params`, captures stdout, and asserts on the JSON. Coverage: all four resolution rules (image refresh, carry forward, take new default, hard fail) plus the `--no-refresh-image-defaults` variant.
- **Unit, no-op detection.** Same harness, calling `--internal-classify-changeset-status` with each documented AWS phrasing for empty change sets, plus a real failure phrasing.
- **Lint, shell.** `shellcheck scripts/upgrade-lakerunner.sh` run from a pytest test that soft-skips if `shellcheck` is not on PATH (so contributor environments without shellcheck still pass).
- **Lint, Jenkinsfile.** A pytest test that runs `groovy -e` against the file if Groovy is available; soft-skips when not.
- **No live AWS test.** The `aws cloudformation` surface is well-trusted; integration tests against real stacks are out of scope.

The Makefile gains a `make test-jenkins` target that runs the new tests in isolation, and `make check` (the pre-push gate) includes them via the existing test runner.

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| New template adds a required parameter with no default | Resolution rule 4 fails with a clear error naming the parameter; operator either pins to a previous version, supplies it via a future job parameter, or runs the underlying `aws cloudformation update-stack` once with the new value |
| Change set creation succeeds but execution races with another in-flight update | The `wait stack-update-complete` waiter surfaces this as a non-zero exit; operator re-runs after the other update settles |
| Operator runs the job against the wrong account | `DeployerRoleArn` is account-scoped; `StackName` and `Region` are explicit per job; the script prints the resolved AWS account ID before any mutation |
| `latest` template silently changes between Plan and Apply | Customers who need stronger guarantees pin `Version`. The risk is small in practice because publishing a new `latest` mid-upgrade is a Cardinal-side operational rarity |
| Bad image tag bricks a service | ECS deployment circuit breaker (already in the stack) rolls the failed service back; the stack lands in `UPDATE_ROLLBACK_COMPLETE`; script exits non-zero; build is red |
| Pending change sets accumulate over time | The Post stage of the Jenkinsfile deletes pending change sets on the stack whose name starts with `cardinal-upgrade-` |

## Out of scope but worth noting

A future per-install `params.json` checked into a separate config repo, or a future multi-install orchestrator that calls this job in sequence per install, both layer cleanly on top of this design without changing the script's interface.
