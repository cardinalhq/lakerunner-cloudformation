# End-to-end test plan: install, verify, tear down, re-install

This is the manual / semi-automated acceptance test for a release candidate of the `cardinal-lakerunner` CloudFormation stack. It is constrained to **only the parameter set** the `jenkins/Jenkinsfile.lakerunner` job exposes, and it uses **no CloudFormation deployer service role** -- the Jenkins AWS Credentials plugin's bound IAM identity drives every API call.

A `params.json` file is permitted as a convenience input format for the test driver (the script accepts file-path flags for multi-line content like license JSON and PEMs; bulk values can be staged in a single file). The hard rule: every key in `params.json` must correspond to a parameter the Jenkins job exposes. If the test discovers a need for a parameter the Jenkinsfile does not yet expose, **first add it to the Jenkinsfile** (and to this spec) in a separate PR, then resume the trial. This keeps `params.json` and the Jenkins job in lock-step.

The test exercises a full first install, runtime convergence, browser-level OIDC login, tear-down, and a clean re-install in the same account / VPC.

## Scope

In scope:

- The Jenkins job `jenkins/Jenkinsfile.lakerunner` end-to-end, including the install (CREATE) path and the upgrade (UPDATE) path on re-run.
- All twelve nested children of `cardinal-lakerunner.yaml`.
- DEX OIDC bring-up (admin login + maestro `/api/me`).
- Self-signed TLS cert generation and PEM-based import via the Jenkinsfile credentials.
- License acceptance.
- `scripts/teardown-lakerunner.sh` and the retained-resource cleanup.

Out of scope (one-time setup, persists across trials):

- VPC + subnets. Pre-existing in the test account; either deployed once from `cardinal-vpc.yaml` via Console / `aws cloudformation create-stack`, or a customer-supplied VPC. Customers will already have a working VPC -- this test mirrors that assumption.
- Region: **us-east-1** in the test account. The published template bucket lives in us-east-2; cross-region template fetch is fine, leave `TemplateBaseUrl` at its default.

Explicitly NOT in scope (intentional simplifications because there is no public DNS available for the test environment):

- Real DNS / Route 53 record pointing at the ALB. We hit the ALB DNS directly with `curl -k`. Browsers will warn for two reasons: self-signed cert, and (probably) hostname mismatch. Both are accepted noise for this test.

## Pre-flight (run once per test account; persists across trials)

### A. AWS identity and connectivity

```sh
aws sts get-caller-identity --output table
```

Capture the account ID and the IAM identity (user or assumed role) the Jenkins AWS Credentials binding will use. This identity needs every IAM, EC2, ECS, ELB, RDS, S3, SQS, Secrets Manager, SSM, Lambda, Logs, KMS, and CloudFormation permission the stack touches. **No deployer service role is used.** If a permission is missing, install will fail; capture the error and either grant the perm or open a PR to document the minimum required policy (see "Expected follow-up PRs" at the end).

### B. VPC

Either:

- **Use an existing VPC** in the account. Capture VpcId and at least two private subnet IDs in distinct AZs. The application stack runs entirely in private subnets behind an internal ALB; public subnets are not used by this stack.
- **Or deploy the Cardinal VPC stack once** via the AWS Console or CLI (this stack survives all subsequent trials):

    ```sh
    aws cloudformation create-stack \
        --region us-east-1 \
        --stack-name cardinal-vpc \
        --template-url https://cardinal-cfn.s3.us-east-2.amazonaws.com/lakerunner/<version>/cardinal-vpc.yaml \
        --capabilities CAPABILITY_NAMED_IAM
    aws cloudformation wait stack-create-complete \
        --region us-east-1 --stack-name cardinal-vpc
    aws cloudformation describe-stacks \
        --region us-east-1 --stack-name cardinal-vpc \
        --query 'Stacks[0].Outputs' --output table
    ```

    Note `VpcId` and `PrivateSubnetsCsv` from the outputs. (`PublicSubnetsCsv` is also exposed but is not used by the lakerunner application stack.)

### C. ALB reachability

The lakerunner ALB is always **internal** -- the application stack does not provision or attach to public subnets. To reach the ALB from outside the VPC during testing you need a route in: SSM Session Manager into an EC2 instance in a private subnet, a bastion host, a VPN, Direct Connect, or VPC peering. The simplest path for a one-off test account is `aws ssm start-session` into a small Amazon Linux EC2 in one of the private subnets and `curl -k https://<alb-dns>/...` from there.

### D. Self-signed TLS cert

Generate once on the operator's laptop, store as Jenkins **Secret File** credentials.

```sh
mkdir -p /tmp/cardinal-cert && cd /tmp/cardinal-cert

openssl req -x509 -newkey rsa:2048 -nodes \
    -keyout private.key \
    -out cert.pem \
    -days 365 \
    -subj "/CN=cardinal.test.local" \
    -addext "subjectAltName=DNS:cardinal.test.local,DNS:*.cardinal.test.local"
```

The CN does not need to match anything reachable in DNS -- browsers and curl will warn about both self-signed and (probably) hostname mismatch. We will use `curl -k` and click through browser warnings. That is the entire point of the "no DNS" simplification.

In Jenkins -> **Manage Credentials** -> add two **Secret File** credentials:

- ID `cardinal-test-cert-body` -> upload `cert.pem`
- ID `cardinal-test-cert-key`  -> upload `private.key`

There is no chain file for a self-signed cert.

### E. DEX admin password and bcrypt hash

Generate once. The maestro Go services accept `$2a` / `$2b`; htpasswd emits `$2y` which must be rewritten:

```sh
PASSWORD='choose-a-strong-one'
htpasswd -bnBC 12 "" "$PASSWORD" | tr -d ':\n' | sed 's/^\$2y/\$2a/'
# -> $2a$12$.... (a 60-char bcrypt hash)
```

Store as a Jenkins **Secret Text** credential, ID `cardinal-test-dex-hash`. Keep the plaintext password somewhere secure -- you will type it during browser login in Phase 3.

The DEX admin email is decided by you (e.g. `admin@cardinal.test`). Default in the Jenkinsfile is `admin@cardinal.local`.

### F. License JSON

Stage the license content somewhere copy-pasteable. It will be pasted into the Jenkins job UI on each trial -- visible by design.

### G. Jenkins job

Copy `jenkins/Jenkinsfile.lakerunner` into a Jenkins **Pipeline** job (inline or "Pipeline script from SCM"). Edit defaults at the top to match the test account:

| Param | Value for this test |
|---|---|
| `StackName` | `cardinal-lakerunner-test` |
| `Region` | `us-east-1` |
| `TemplateBaseUrl` | leave default (`https://cardinal-cfn.s3.us-east-2.amazonaws.com/lakerunner`) |
| `Version` | the explicit release tag under test (e.g. `v0.0.38`). There is no `latest` -- every deploy is to a versioned tag |
| `DeployerRoleArn` | **leave empty** -- intentional for this test |
| `AwsCredentialsId` | the binding for the test account |
| `VpcId` | from B |
| `PrivateSubnets` | comma-separated IDs from B |
| `CertificateArn` | leave empty (we are importing PEMs) |
| `LicenseData` | paste the JSON from F |
| `DexAdminEmail` | from E (e.g. `admin@cardinal.test`) |
| `OidcSuperadminEmails` | same as `DexAdminEmail`, or comma-separated allowlist |
| `DexAdminPasswordHashCredentialId` | `cardinal-test-dex-hash` (from E) |
| `CertificateBodyCredentialId` | `cardinal-test-cert-body` (from D) |
| `CertificatePrivateKeyCredentialId` | `cardinal-test-cert-key` (from D) |
| `CertificateChainCredentialId` | empty (self-signed -- no chain) |

Keep `AutoApprove=false` for trial runs so you can read the change-set summary before applying.

## Trial structure

A trial is one full pass: **Install -> Verify -> Tear-down**. After tear-down, the VPC + Jenkins credentials + DEX hash + cert files all persist; only the lakerunner stack itself is removed. The next trial starts again at "Install" with the existing Jenkins job re-run.

The first trial below is documented in detail. Subsequent trials follow the same steps; capture only deltas.

## Trial 1 / Phase 1: install via Jenkins

### 1a. Run with `AutoApprove=false`

Click "Build with Parameters" on the Jenkins job. Confirm the values match the table above. Build.

### 1b. Inspect the Plan stage

Pipeline pauses after the `cardinal-deploy-<timestamp>` change set is described. Read the change-set table in the build log.

Pass criteria:

- A long list of `Add` rows -- one per resource the stack creates. Expect IAM roles, IAM policies, Lambda functions, Secrets Manager secrets, SSM parameters, RDS instance + subnet group, S3 bucket, SQS queue, ECS cluster, ECS task definitions, ECS services, ALB + listener + target groups + listener rules, CloudWatch log groups, the cert-import custom resource, the migration custom resource, and the twelve nested-stack resources.
- **Zero `Remove` rows.**
- **Zero `Replacement: True` rows on stateful resources** (RDS, S3, persistent EBS). On install, everything is `Add`, so this should be trivially satisfied.

Capture the change set summary in the test log.

### 1c. Approve

Click "Apply" on the input prompt. Pipeline executes the change set and waits for `CREATE_COMPLETE`.

Pass criteria:

- Build status: green.
- Final stack status: `CREATE_COMPLETE`.
- Total wall time recorded (typical: 15-25 minutes; the RDS instance is the long pole).

If the build fails, capture the failing stack event:

```sh
aws cloudformation describe-stack-events \
    --region us-east-1 --stack-name cardinal-lakerunner-test \
    --query 'StackEvents[?contains(ResourceStatus, `FAILED`)].[LogicalResourceId,ResourceStatus,ResourceStatusReason]' \
    --output table
```

Most likely failure modes for the no-deployer-role install:

- **`AccessDenied` on iam:CreateRole / iam:PassRole / iam:AttachRolePolicy** -- the bound IAM identity needs IAM-write perms. Either grant them or open a PR adding a "test user" policy template.
- **`AccessDenied` on lambda:CreateFunction** -- both the cert-import and migration custom resources are Lambda-backed.
- **Cert import Lambda timeout** -- the cert-import custom resource imports the PEMs via ACM API. If it hangs, the most likely cause is malformed PEMs (CRLF line endings, missing trailing newline, encrypted key). See `docs/operations/certificates.md` "Format gotchas".

## Trial 1 / Phase 2: post-install discovery

### 2a. Capture stack outputs

```sh
aws cloudformation describe-stacks \
    --region us-east-1 --stack-name cardinal-lakerunner-test \
    --query 'Stacks[0].Outputs' --output table
```

Note the ALB DNS name (an output of the root stack -- look for the `AlbDnsName` or similarly named output). This is what we will hit with curl. Example: `cardinal-lakerunner-test-Alb-XYZ-1234567890.us-east-1.elb.amazonaws.com`.

### 2b. Capture nested stack identifiers

```sh
aws cloudformation describe-stack-resources \
    --region us-east-1 --stack-name cardinal-lakerunner-test \
    --query 'StackResources[?ResourceType==`AWS::CloudFormation::Stack`].[LogicalResourceId,PhysicalResourceId]' \
    --output table
```

Note the physical IDs of `MigrationStack`, `ServicesQueryStack`, `ServicesProcessStack`, `ServicesControlStack`, `MaestroStack`, `OtelStack`. These let you scope `describe-stack-resources` calls during debugging.

### 2c. Capture the InstallIdLong

```sh
aws cloudformation describe-stacks \
    --region us-east-1 --stack-name cardinal-lakerunner-test \
    --query 'Stacks[0].Outputs[?OutputKey==`InstallIdLong`].OutputValue' \
    --output text
```

(If no such output exists, derive it from the stack ID per the rule in CLAUDE.md.) This shows up in the names of the ingest bucket, secrets, and log groups, and is the key for tear-down later.

## Trial 1 / Phase 3: runtime verification

Run each check; record pass / fail / observed value. **Do not skip checks because earlier ones passed** -- some failures only surface downstream.

### 3a. Migration custom resource succeeded

```sh
aws cloudformation describe-stack-resource \
    --region us-east-1 --stack-name cardinal-lakerunner-test \
    --logical-resource-id MigrationStack \
    --query 'StackResourceDetail.PhysicalResourceId' --output text
# Then for that nested stack:
aws cloudformation describe-stack-events \
    --region us-east-1 --stack-name <migration-nested-physical-id> \
    --query 'StackEvents[?ResourceType==`AWS::CloudFormation::CustomResource`]' \
    --output table
```

Pass: the custom resource shows `CREATE_COMPLETE` with no failure events. Tail its CloudWatch log group (`/aws/lambda/cardinal-migration-<InstallIdLong>`) -- it should show the migrator container exit-0.

### 3b. ECS service convergence

For each service in the cluster:

```sh
CLUSTER=$(aws cloudformation describe-stack-resource \
    --region us-east-1 --stack-name cardinal-lakerunner-test \
    --logical-resource-id ClusterStack \
    --query 'StackResourceDetail.PhysicalResourceId' --output text \
    | xargs -I{} aws cloudformation describe-stack-resource \
        --region us-east-1 --stack-name {} \
        --logical-resource-id Cluster \
        --query 'StackResourceDetail.PhysicalResourceId' --output text)

aws ecs list-services --cluster "$CLUSTER" --region us-east-1 --output text \
    --query 'serviceArns' \
| tr '\t' '\n' \
| while read svc; do
    aws ecs describe-services --cluster "$CLUSTER" --services "$svc" --region us-east-1 \
        --query 'services[0].[serviceName,desiredCount,runningCount,pendingCount,deployments[0].rolloutState]' \
        --output text
done
```

Pass: every service shows `runningCount == desiredCount`, `pendingCount == 0`, `rolloutState == COMPLETED`. If a service is stuck in `IN_PROGRESS` or has `runningCount < desiredCount`, the deployment circuit breaker has not fired yet -- wait up to 10 minutes; if still wrong, dump the latest stopped task:

```sh
aws ecs list-tasks --cluster "$CLUSTER" --service-name <svc> \
    --desired-status STOPPED --region us-east-1 --query 'taskArns[0]' --output text \
| xargs -I{} aws ecs describe-tasks --cluster "$CLUSTER" --tasks {} --region us-east-1 \
    --query 'tasks[0].containers[*].[name,exitCode,reason]' --output table
```

### 3c. ALB target groups healthy

```sh
ALB_TGS=$(aws elbv2 describe-target-groups --region us-east-1 \
    --query 'TargetGroups[?contains(TargetGroupName,`cardinal`) || contains(LoadBalancerArns[0],`cardinal-lakerunner-test`)].TargetGroupArn' \
    --output text)
for tg in $ALB_TGS; do
    aws elbv2 describe-target-health --region us-east-1 --target-group-arn "$tg" \
        --query 'TargetHealthDescriptions[*].[Target.Id,TargetHealth.State,TargetHealth.Reason]' \
        --output table
done
```

Pass: every target shows `State: healthy`. `unhealthy` targets are usually a healthcheck path mismatch or a service that never came up -- correlate with 3b.

### 3d. ALB HTTPS listener responds

```sh
ALB_DNS=<from 2a>

# Confirm TLS handshake succeeds (will warn about self-signed -- expected):
curl -kv "https://${ALB_DNS}/" 2>&1 | grep -E "(SSL connection|HTTP/|Server certificate)"

# Confirm we see the cert we generated:
echo | openssl s_client -connect "${ALB_DNS}:443" -servername cardinal.test.local 2>/dev/null \
    | openssl x509 -noout -subject -issuer -dates
```

Pass: TLS handshake completes; the served cert's subject matches `CN=cardinal.test.local` (what we generated in pre-flight D); the dates show the 365-day validity.

### 3e. Dex OIDC discovery endpoint

```sh
curl -ksS "https://${ALB_DNS}/dex/.well-known/openid-configuration" | jq .
```

Pass: returns a JSON document with a non-empty `issuer`, `authorization_endpoint`, `token_endpoint`, `jwks_uri`. The issuer should reflect the configured maestro hostname.

### 3f. Browser login flow

This step is manual (browser), with a `--resolve` workaround if the maestro UI insists on a specific hostname:

```sh
# Option 1: just hit the ALB and click through the warning.
open "https://${ALB_DNS}/"

# Option 2: pin a hostname locally so the maestro UI's same-origin checks pass
# (no /etc/hosts mutation required if you stay in the test browser session):
chrome --host-resolver-rules="MAP cardinal.test.local ${ALB_DNS}" \
    "https://cardinal.test.local/"
```

In the browser:

1. Click through the self-signed warning.
2. You should land on the maestro login page (or be redirected to `/dex/auth/...`).
3. Click "Login with Cardinal" (or whichever tile is wired to the local DEX connector).
4. Enter the DEX admin email + plaintext password from pre-flight E.
5. You should land back on the maestro UI, logged in.

Pass: login succeeds without 4xx/5xx; you can see the maestro home page; the user menu shows your admin email.

### 3g. maestro `/api/me` returns 200 with superadmin

While logged in, in the browser dev tools or via curl with the session cookie:

```sh
# After logging in via browser, copy the session cookie from dev tools, then:
curl -ksS "https://${ALB_DNS}/api/me" \
    -H "Cookie: <session-cookie>" | jq .
```

Pass: HTTP 200, JSON includes the admin email and `role` / `roles` indicating superadmin.

### 3h. License acceptance

Confirm via the maestro admin UI (organization / settings page) that the license is recognized -- the customer name / seat count / expiry from the JSON you pasted in pre-flight F should be visible. If maestro shows "no license" or "evaluation mode," the license param did not flow through correctly -- check the `cardinal/<InstallIdLong>/license` secret in Secrets Manager and confirm the secret value matches what you pasted into the Jenkins job.

### 3i. Optional smoke: end-to-end signal

Send a sample log line via the OTEL collector endpoint (also fronted by the ALB), wait ~60s, query it back via the maestro query UI. Pass: the line appears.

## Trial 1 / Phase 4: tear down

`scripts/teardown-lakerunner.sh` is run **without** the deployer role flag (matches the no-role constraint of this test).

```sh
sh scripts/teardown-lakerunner.sh \
    --stack-name cardinal-lakerunner-test \
    --region us-east-1 \
    --yes
```

The script handles: deleting the stack, draining the ingest bucket so it can be removed, deleting the retained `license` / `admin-api-key` / `db-master` secrets, and the RDS final snapshot.

Pass criteria:

- Script exits 0.
- `aws cloudformation describe-stacks --stack-name cardinal-lakerunner-test` returns `does not exist`.
- The VPC stack is **untouched** -- still exists, still healthy.
- Tag-based discovery finds zero `cardinal-*` resources tagged for this install:

    ```sh
    aws resourcegroupstaggingapi get-resources --region us-east-1 \
        --tag-filters Key=cardinal:install,Values=<InstallIdLong> \
        --query 'ResourceTagMappingList[*].ResourceARN' --output table
    ```

If the tag query returns rows, capture them and document. Most likely culprits historically: orphaned ENIs from the cert-import Lambda or the migration ECS task; orphaned target groups whose ALB listener was already deleted.

## Trial 2: re-install on the same VPC

Re-run the Jenkins job with the same parameters. Mode auto-detects to CREATE again (the previous stack is gone). Run the same Phase 1 -> Phase 4 steps; record any deltas from Trial 1.

Pass: Trial 2 reaches `CREATE_COMPLETE` and passes the same Phase 3 verification as Trial 1, with no manual fixup between trials.

If Trial 2 fails where Trial 1 passed, the suspect list is:

- Tear-down left state behind (orphaned resource still bound to a name the new install wants).
- The retained `license` secret has stale state that conflicts with the new install -- but secrets are keyed on `InstallIdLong`, which is freshly derived per install, so this should not happen. If it does, that is a CLAUDE.md-violating bug in the install-id derivation -- file a PR.

## Final cleanup pass (optional, end of test session)

After all trials are done and you want a truly clean account:

```sh
# Tear down the lakerunner stack from the most recent trial (if still present).
sh scripts/teardown-lakerunner.sh --stack-name cardinal-lakerunner-test --region us-east-1 --yes

# Tear down the VPC stack (only if you deployed it for this test session;
# otherwise leave it for next time).
aws cloudformation delete-stack --stack-name cardinal-vpc --region us-east-1
aws cloudformation wait stack-delete-complete --stack-name cardinal-vpc --region us-east-1

# Delete Jenkins credentials staged in pre-flight D + E if no longer needed.
```

## Reporting

For each trial, capture:

- Trial number, date, version (git SHA + tag), region.
- Wall time per phase (1, 2, 3, 4).
- Pass / fail per check in Phase 3 (3a through 3i).
- Any failure logs, stack-event captures, or test diffs.
- A final summary: `PASS` / `FAIL` / `PASS WITH FOLLOW-UPS` and a list of opened PRs.

Archive the trial log to `docs/operations/test-runs/<date>-<version>.md` so future runs have a baseline to compare to.

## Expected follow-up PRs

Discovered as the test runs; not all will hit. Track each as a separate PR.

- **Minimum IAM policy doc** for the no-deployer-role install path. Likely outcome: a new section in `docs/operations/deploying.md` enumerating every action the stack needs the bound identity to have, plus an optional `cardinal-test-user-policy.json` for copy-paste into a test IAM user.
- **Jenkinsfile param polish** -- if the Jenkins version in the test environment does not support `text(...)` parameter type, fall back to `string()` with a JSON-on-one-line constraint and a doc note.
- **Cert-import Lambda robustness** -- if the self-signed PEMs from the openssl one-liner trip a format check, fix the importer to accept what openssl emits or document the workaround.
- **Healthcheck path tuning** -- if any service shows 3b green but 3c (target group health) red, the service's healthcheck path may not match what the container actually serves; service-stack PR to fix.
- **Tear-down survivor cleanup** -- if Phase 4 leaves anything tagged `cardinal:install=<id>` behind, extend `scripts/teardown-lakerunner.sh` to clean it.
- **Stack output additions** -- if Phase 2 has to derive `InstallIdLong` or the ALB DNS via `Fn::Split` instead of reading a stack output, add the missing outputs to the root template.

The intent of this test is to drive these out by running it. Each fix is its own PR; the test plan stays stable across them.
