# Changelog

This file records **operational and system-level changes** to the Cardinal
Lakerunner CloudFormation stacks — what an operator updating an existing install
needs to know: new or changed parameters, changed defaults, new manual steps,
image bumps, IAM and security-group changes, and resource replacements. It is
not an exhaustive code log; see the git history and linked PRs for that.

Versions are the published Git tags / S3 prefixes (`v0.0.NNN`). To bring an
install up to date, read every entry from the version you are on up to your
target version and apply the noted upgrade actions. Earliest recorded version is
v0.0.114.

## v1.4.0

- **lakerunner image bumped to `v1.61.1`** (from `v1.60.0`). The single
  `LakerunnerImage` drives every lakerunner task and the DB migrator, so the
  update redeploys `MigratorService` (reruns the idempotent migrator) before the
  service tiers roll. No parameter, IAM, or resource changes.
- **maestro image bumped to `v1.66.5`** (from `v1.66.0`). Default `MaestroImage`
  bump (digest-pinned multi-arch manifest); the Maestro service rolls to the new
  task definition on redeploy. No parameter, IAM, or resource changes.
- Upgrade action: deploy v1.4.0; no manual steps.

## v1.3.0

- **lakerunner image bumped to `v1.60.0`** (from `v1.57.1`). The single
  `LakerunnerImage` drives every lakerunner task and the DB migrator, so the
  update redeploys `MigratorService` (reruns the idempotent migrator) before the
  service tiers roll. As of this release the `process-{logs,metrics,traces}`
  workers no longer use DuckDB in-process — the heavy lifting moved to the `lkrn
  pack` subprocess. This stack never set the old DuckDB tuning env vars
  (`LAKERUNNER_DUCKDB_MEMORY_LIMIT`, `LAKERUNNER_DUCKDB_TEMP_DIRECTORY`,
  `MALLOC_ARENA_MAX`) and relies on binary defaults, so there is no parameter,
  env, or resource change. No IAM changes.
- **maestro image bumped to `v1.66.0`** (from `v1.62.10`). Default `MaestroImage`
  bump (digest-pinned multi-arch manifest); the Maestro service rolls to the new
  task definition on redeploy. No parameter, IAM, or resource changes.
- Upgrade action: deploy v1.3.0; no manual steps.

## v1.2.2

- **lakerunner image bumped to `v1.57.1`** (from `v1.54.0`). The single
  `LakerunnerImage` drives every lakerunner task and the DB migrator, so the
  update redeploys `MigratorService` (reruns the idempotent migrator) before the
  service tiers roll. No parameter or IAM changes. Upgrade action: deploy v1.2.2;
  no manual steps.

## v1.2.1

- **maestro image bumped to `v1.62.10`** (from `v1.62.4`). Default `MaestroImage`
  bump (digest-pinned multi-arch manifest). On redeploy the Maestro service rolls
  to the new task definition; no parameter, IAM, or resource changes. Upgrade
  action: deploy v1.2.1; no manual steps.

## v1.2.0

- **lakerunner image bumped to `v1.54.0`** (from `v1.51.5`). The single
  `LakerunnerImage` drives every lakerunner task and the DB migrator, so the
  update redeploys `MigratorService` (reruns the idempotent migrator) before the
  service tiers roll. No parameter or IAM changes. Upgrade action: deploy v1.2.0;
  no manual steps.

## v1.1.9

- **process-{logs,metrics,traces} now autoscale on CPU via native ECS
  Application Auto Scaling.** Each service gets a scalable target (min 1, max
  `Process*Replicas`) and a target-tracking policy on
  `ECSServiceAverageCPUUtilization` at 90% -- mirroring the Kubernetes HPA. The
  monitoring container no longer scales them (its `LAKERUNNER_AUTOSCALER_*` env
  is removed), and the control task role no longer carries
  `ecs:UpdateService` / `ecs:DescribeServices`. No customer-facing parameter
  changes; the `Process*Replicas` parameters keep their meaning (the autoscaler
  ceiling).
- **Deployer IAM:** the deploy principal now needs `application-autoscaling:*`
  (register/deregister scalable target, put/delete scaling policy, describe) and
  `cloudwatch` alarm actions (`PutMetricAlarm` / `DeleteAlarms` /
  `DescribeAlarms`) for the target-tracking alarms. First registration also
  creates the `AWSServiceRoleForApplicationAutoScaling_ECSService` service-linked
  role automatically; if your account lacks it, grant
  `iam:CreateServiceLinkedRole` for `ecs.application-autoscaling.amazonaws.com`.
  Upgrade action: extend your deployer policy before updating.

## v1.1.8

- **`deploy-lakerunner-services.sh` gains `PROCESS_LOGS_MEMORY` /
  `PROCESS_METRICS_MEMORY` / `PROCESS_TRACES_MEMORY`.** Optional env vars that
  set the Fargate task memory (MiB) for the process-{logs,metrics,traces}
  services. Each must be a valid Fargate CPU/memory combo (at 1 vCPU:
  2048-8192). Passed to the stack only when set, so an existing install's value
  carries forward on update unless you override it -- note this means a bumped
  template default (e.g. the v1.1.7 process-logs 4096) does NOT reach an existing
  install through a plain upgrade; set the matching env var on the services
  driver to apply a new size in place. No upgrade action if you are happy with
  your current sizing.

## v1.1.7

- **process-logs memory default `2048` -> `4096` MiB.** `ProcessLogsMemory`
  default doubled (still 1 vCPU; 4096 MiB is a valid Fargate combo). On redeploy
  the process-logs task definition revises and the service rolls; each
  process-logs task now reserves 4 GiB. If you pin `ProcessLogsMemory` via a
  parameter, it is unchanged. Note the autoscaler ceiling is unchanged
  (`ProcessLogsReplicas`, default 10), so peak reservation rises accordingly.
- **Image bumps: lakerunner `v1.51.3` -> `v1.51.5`, maestro `v1.60.3` ->
  `v1.62.4`.** Default `LakerunnerImage` and `MaestroImage` bumps (digest-pinned
  multi-arch manifests). On redeploy the DB migrator reruns (idempotent) before
  the service-tier stacks update. No new parameters or resource replacements;
  upgrade action is none if you use the defaults. If you pin the images via
  parameters, set them to the new versions explicitly.

## v1.1.6

- **Image bump: lakerunner `v1.51.1` -> `v1.51.3`.** Default `LakerunnerImage`
  bump (digest-pinned multi-arch manifest); this is the version that honors
  `LAKERUNNER_LOG_TRACKED_FIELDS` (below). On redeploy the DB migrator reruns
  (idempotent) before the service-tier stacks update. No new parameters or
  resource replacements; upgrade action is none if you use the defaults. If you
  pin `LakerunnerImage` via a parameter, set it to the new version explicitly.
- **process-logs now sets `LAKERUNNER_LOG_TRACKED_FIELDS`.** The process-logs
  task hardcodes the tracked-field set
  `service_name,environment_type,installation,proc_name,partition_id` — the log
  fields whose distinct values are rolled up into the fast tag-value lookup
  table at ingest, overriding lakerunner's compiled-in default
  (`k8s_cluster_name,k8s_namespace_name,service_name`). Requires a lakerunner
  image that honors this env var. No new parameters; on redeploy the
  process-logs task definition revises and the service rolls. Temporary until
  tracked fields get a Maestro UI; per-org admin-API config still overrides it.

## v1.1.5

- **Image bumps: lakerunner `v1.41.6` -> `v1.51.1`, maestro `v1.53.1` ->
  `v1.60.3`.** Default `LakerunnerImage` and `MaestroImage` bumps (digest-pinned
  multi-arch manifests). On redeploy the DB migrator reruns (idempotent) before
  the service-tier stacks update. No new parameters or resource replacements;
  upgrade action is none if you use the defaults. If you pin `LakerunnerImage` /
  `MaestroImage` via parameters, set them to the new versions explicitly.
- **Default `QueryWorkerReplicas` lowered from `8` to `4`.** On redeploy the
  lakerunner-query-worker service scales down to 4 tasks. If you rely on the
  previous count, set `QueryWorkerReplicas=8` explicitly.

## v1.1.4

- **Deploy drivers now reject non-ASCII input with a precise error.** Every
  operator-supplied value — environment variables / flags and the contents of
  input files (license token, DEX extra users JSON, certificate PEMs, policy
  JSON) — is validated before any AWS call. A value containing smart quotes,
  no-break spaces, or other non-ASCII or control characters now fails fast
  (exit 2) with a message naming the parameter, the offending character (e.g.
  `left double curly quote (U+201C)`), its line and byte position, and the
  plain-ASCII replacement to use. Previously such characters — typically
  introduced by pasting from a word processor, browser, or chat tool —
  flowed into the stack parameters and surfaced much later as confusing
  JSON or template errors. No template changes, no upgrade action; if a
  redeploy now fails the new check, the flagged value was already corrupt —
  fix it as the message says.

Script changes:

- `deploy-lakerunner-infra-base.sh`, `deploy-lakerunner-infra-rds.sh`,
  `deploy-lakerunner-services.sh`, `deploy-satellite-infra-base.sh`,
  `deploy-satellite-services.sh` (shared engine), `deploy-lakerunner.sh`

## v1.1.3

- **New optional parameter `PublicDnsName`** (default empty) on the
  lakerunner-services stack. Sets the DNS name the install is reached at
  (e.g. `lakerunner.example.com`) — typically a CNAME the operator points at
  the ALB (the stack's `AlbDnsName` output). When set, the Maestro/Dex OIDC
  issuer and redirect URLs and the `QueryApiUrl` output are derived from it
  instead of the raw `*.elb.amazonaws.com` name, so browser logins work
  through the vanity name; the supplied certificate
  (`CERTIFICATE_ARN`/`CERTIFICATE_BODY`) must match it. The deploy driver
  accepts it as `PUBLIC_DNS_NAME`. Leave it unset for the existing
  ALB-DNS-name behavior — no upgrade action, no resource replacement. Setting
  it on an existing install redeploys the Maestro service (new issuer env
  vars) and invalidates sessions issued under the old hostname.

Template changes:

- `cardinal-lakerunner-services.yaml`
- `cardinal-lakerunner/maestro.yaml` (parameter description only)

Script changes:

- `deploy-lakerunner-services.sh`

## v1.1.2

- **New optional parameter `NameSuffix`** (default empty) on the
  satellite-infra-base and satellite-services stacks, so multiple satellite
  stacks (e.g. one collector for prod and one for dev) can coexist in a single
  AWS account. When set, it is appended to the stacks' fixed physical names:
  the `cardinal-satellite-access` IAM role (account-global, the previous hard
  collision), the default raw bucket name
  `cardinal-otel-raw-<account>-<region>`, and the `/cardinal/otel-grpc` log
  group. Max 16 chars (lowercase alphanumeric and hyphens) to keep the default
  bucket name within S3's 63-char limit. The suffixed role still matches the
  central install's existing `cardinal-satellite-access*` assume-role grant,
  so no change is needed on the lakerunner side. The deploy drivers accept it
  as `NAME_SUFFIX`. Leave it unset on existing stacks: all names resolve to
  exactly their previous values — no upgrade action, no resource replacement.

## v1.1.1

- **Deploy-driver fix: inline `DEX_EXTRA_USERS` works and accepts multi-line
  JSON.** In v1.1.0 the driver's newline guard rejected every inline
  `DEX_EXTRA_USERS` value with a "contains a newline" error; the only working
  path was `DEX_EXTRA_USERS_FILE`. The guard is gone — the driver now flattens
  the JSON (newlines are insignificant whitespace) before passing it as the
  `DexExtraUsers` stack param, so multi-line values pasted into an env var or
  Jenkins param work. Driver-only change; no template changes, no upgrade
  action, no resource replacement.

## v1.1.0

- **New optional parameter `DexExtraUsers`** (NoEcho, default empty) on the
  lakerunner-services stack. Adds bundled-DEX login accounts beyond the admin:
  a JSON array of `{"email":...,"hash":"$2y$..."}` objects (optional
  `username`/`userID`). The deploy driver accepts it as `DEX_EXTRA_USERS`
  (inline, single-line JSON) or `DEX_EXTRA_USERS_FILE` (path to a JSON file).
  Leave it empty for the existing admin-only behavior — no upgrade action.
  Make any of the extra users a superadmin by also adding their email to
  `OIDC_SUPERADMIN_EMAILS`; otherwise an admin invites them to an org in the
  Maestro UI after their first login. Requires the bundled DEX image that
  supports `DEX_EXTRA_USERS` (shipped with this release's `DexImage` default);
  an empty value works on any prior image. No resource replacement.

## v1.0.1

- **lakerunner `v1.40.4` -> `v1.41.6`.** Default `LakerunnerImage` bump. On
  redeploy the DB migrator reruns (idempotent) before the service-tier stacks
  update. No new parameters or resource replacements.

## v1.0.0

First 1.0 release. No new parameters or resource replacements vs `v0.0.136`;
this tags the consolidated, production-ready state. Operator-facing changes
accumulated since `v0.0.136`:

- **Docs consolidated to two paths.** Production installs follow
  `docs/operations/production-deploy.md`; dev/test environments follow
  `docs/operations/dev-environment.md`. The legacy monolithic-model runbooks
  were retired (the per-stack model is the only supported one).
- **Releases now attach version-baked deploy drivers.** Each release attaches
  `deploy-*.sh` with `STACK_VERSION` baked in (matching the S3
  `lakerunner/<version>/scripts/` copies); the committed `scripts/*.sh` remain
  `dev`. Production should use the release-pinned drivers — see
  `scripts/README.md`.
- **Per-stack teardown driver added.** `dev-scripts/teardown-cardinal.sh`
  deletes the five `cardinal-*` stacks in reverse order and wipes the retained
  fixed-name survivors, leaving the VPC and ECS cluster intact (gated behind
  `CONFIRM=DELETE`).

No upgrade action beyond the usual redeploy.

## v0.0.136

- **maestro `v1.53.0` -> `v1.53.1`.** Picks up the fix for the fresh-install
  provisioning cold-start race (conductor #998): the Lakerunner provisioning
  worker now retries transient admin-api failures for ~29 min (capped backoff)
  instead of giving up after ~31s, so on a fresh install the org reliably lands
  in configdb without a manual maestro restart. **Upgrade action:** redeploy
  `lakerunner-services`.

## v0.0.135

- **cleanup task aws-cli image pinned + plumbed through defaults.** The
  `cardinal-cleanup` teardown task used a hardcoded
  `public.ecr.aws/aws-cli/aws-cli:latest`. It is now sourced from
  `cardinal-defaults.yaml` (`images.aws_cli`), pinned by digest
  (`2.34.63@sha256:c95a…`), and exposed as an `AwsCliImage` parameter on the
  cleanup stack (like the other image overrides). `dev-scripts/sweep-stranded-
  resources.sh` default and a new `cleanup-images.txt` manifest track the same
  pin. No upgrade action — affects only teardown, never a running install. Every
  image the project references is now a digest-pinned `public.ecr.aws` image.

## v0.0.134

- **Deploy driver now always sets `DbInitImage` from the baked, pinned default**
  (like `LakerunnerImage`/`MaestroImage`/`DexImage`), composed from
  `IMAGE_REGISTRY` + the locked `db_init` suffix. Previously `DbInitImage` was
  only passed when `DB_INIT_IMAGE` was set, so on a stack **update** it carried
  `UsePreviousValue` — meaning the v0.0.133 db-init image change (and any future
  bump) did **not** take effect on a plain redeploy. **Upgrade action:** redeploy
  `lakerunner-services` with the v0.0.134 driver; db-init moves to the pinned
  `postgres:18-alpine` automatically (no `DB_INIT_IMAGE` needed). `DB_INIT_IMAGE`
  remains a full-URI escape hatch. This stack is always on `public.ecr.aws`, so
  db-init follows `IMAGE_REGISTRY` like the other images.

## v0.0.133

- **db-init image: `ghcr.io/cardinalhq/initcontainer-grafana:latest` ->
  `public.ecr.aws/docker/library/postgres:18-alpine` (digest-pinned).** The
  maestro `db-init` container only ever used the image to run a one-line
  `psql ... CREATE DATABASE maestro`; it overrode the entrypoint and used none
  of the grafana tooling. Switch it to the official Postgres image (Docker
  Official Images, AWS-mirrored on public ECR; psql 18 matches the RDS major
  version), which is leaner, from a trusted publisher, digest-pinned (no more
  mutable `:latest`), and removes the only `ghcr.io` pull. **Upgrade action:**
  redeploy `lakerunner-services`. The `DbInitImage` default changes; if you
  pinned `DB_INIT_IMAGE` in a saved config, update it. No DB/data change
  (db-init is idempotent: `CREATE DATABASE ... || true`).

## v0.0.132

- **Image bumps: maestro `v1.50.0` -> `v1.53.0`, dex-customization `v0.2.0` ->
  `v0.3.0`.** Redeploy `lakerunner-services` to pick them up. The maestro task
  redeploys (brief blip on the singleton maestro/dex service); no data-bearing
  resource is replaced.
- **`dex-init` sidecar removed; `DexInitImage` parameter removed.** dex
  v0.3.0 renders its own `/etc/dex/config.yaml` at startup (gomplate over a
  baked template, from the same `DexClientId` / `DexAdminEmail` /
  `DexAdminPasswordHash` inputs as before), so the busybox `dex-init` init
  container is gone. The maestro task drops from six containers to five, and
  the `busybox` image is no longer pulled. **Upgrade action:** redeploy
  `lakerunner-services`. If you set the `DexInitImage` stack parameter or the
  `DEX_INIT_IMAGE` deploy-driver env var in a saved config, remove it — both
  no longer exist. No change to the DEX admin login or OIDC behavior.
- **dex container now runs with a writable root filesystem (was read-only),
  still as nonroot.** dex v0.3.0's entrypoint writes the rendered config to
  `/tmp`; on Fargate a read-only rootfs would require a writable `/tmp` volume,
  but empty Fargate volumes mount `0755 root:root` and the nonroot dex user
  can't write them. Rather than run dex as root or add an init container, the
  dex container leaves `ReadOnlyRootFilesystem` unset and uses the image's
  `1777` `/tmp` on the task's ephemeral storage. No upgrade action.

## v0.0.131

- **otel collector: added the `service_graph` connector** to
  `cardinal-otel-config.yaml` (generates service-graph metrics from spans).
  **Upgrade action:** redeploy `satellite-services` so the collector picks up
  the new config. No parameter or resource changes.

## v0.0.130

- **Internet-facing collector ALB is now actually reachable (ingest pipeline
  fix).** When `satellite-services` is deployed with `AlbScheme=internet-facing`,
  the collector ALB security group now adds a `0.0.0.0/0` ingress on the OTLP
  port (4318), mirroring the app ALB in `lakerunner-infra-base`. Previously the
  collector ALB only ever allowed `IngestSourceCidr` (default `10.0.0.0/8`)
  regardless of scheme, so an internet-facing collector sat in public subnets
  but its SG rejected everything outside RFC1918. This silently broke ingest on
  internet-facing installs: the lakerunner tier's self-telemetry (and any
  in-VPC sender) egresses via the VPC NAT gateway and arrives with a public
  source IP, so OTLP POSTs were dropped, nothing reached the raw bucket/queue,
  and the UI stayed empty even though every ECS service was healthy. **Upgrade
  action:** redeploy `satellite-services`; if it is internet-facing, the ALB SG
  gains a `0.0.0.0/0` rule on 4318 (it now accepts unauthenticated OTLP from any
  host — restrict at the network edge if that is a concern, or keep the ALB
  internal). Internal ALBs are unchanged (`IngestSourceCidr` only). No resource
  replacement.

## v0.0.129

- **Maestro UI now boots (dex theme fix).** The bundled DEX image moves to
  `dex-customization:v0.2.0`, which embeds the Cardinal login theme in the
  binary; the dex-init config drops the now-removed `frontend.dir:
  /srv/dex-cardinal/web` (a stale `frontend.dir` makes v0.2.0 fail to boot:
  `failed to load web static: open robots.txt: no such file or directory`).
  This is what made the Maestro service trip its deployment circuit breaker on
  fresh installs. **Upgrade action:** redeploy `lakerunner-services`; the dex
  container is replaced (new image + new config). No parameter changes.
- **`lakerunner-services` driver auto-generates its self-signed cert on a
  rolled-back recreate, not just when the stack is absent.** When no
  `CERTIFICATE_ARN`/PEM is supplied, the driver decides whether to generate a
  cert by stack status, mirroring the engine's recreate states: it generates
  when the stack is absent, `REVIEW_IN_PROGRESS`, or `ROLLBACK_COMPLETE` (all of
  which the engine creates fresh), and keeps the existing cert on a true
  in-place update (no ALB listener churn). Previously a bare existence check
  skipped generation for a `ROLLBACK_COMPLETE` stack the engine then recreated,
  leaving `CertificateArn` empty and failing the ALB HTTPS listeners with
  `Certificate ARN '' is not valid` — wedging any re-run after a failed first
  deploy. **Upgrade action:** none — driver-only behavior; no template or
  parameter changes.

## v0.0.128

- **Execution roles can take customer-supplied extra permissions.** The
  execution role in `lakerunner-infra-base` and the collector execution role in
  `satellite-services` gain an `ExecutionRoleExtraPolicyArns` parameter (CSV of
  managed-policy ARNs) appended to `ManagedPolicyArns` alongside
  `AmazonECSTaskExecutionRolePolicy`. Use this for air-gapped ECR pull-through
  first-pull (`ecr:BatchImportUpstreamImage` + repo auto-create), cross-account
  ECR, or KMS-encrypted repos (standard private-ECR pulls are already covered by
  the base policy).
- **Two driver inputs feed it** on `deploy-lakerunner-infra-base.sh` and
  `deploy-satellite-services.sh`: `EXECUTION_ROLE_POLICY_ARNS` (ready-made
  managed-policy ARNs) and `EXECUTION_ROLE_POLICY_JSON` / `_FILE` (a pasted IAM
  policy the driver flattens and turns into a customer-managed policy named
  `<STACK_NAME>-exec-extra`, then attaches by ARN — CFN can't inline a string
  policy without Lambda). The JSON path needs `jq` and IAM write permissions for
  the deployer. See `docs/air-gapped-images.md`.
- **Upgrade action:** none — both inputs are optional and default to today's
  behavior (base policy only). No resource replacement.

## v0.0.127

- **The locked-image + registry-prefix model now covers all stacks.** Following
  the satellite work in v0.0.126, `deploy-lakerunner-services.sh` no longer takes
  per-image `LAKERUNNER_IMAGE` / `MAESTRO_IMAGE` / `DEX_IMAGE` / `OTEL_IMAGE`
  overrides. The lakerunner, maestro, and dex images (our public ECR) are now
  baked (repo path + pinned digest) into the driver; set `IMAGE_REGISTRY` to
  your registry/pull-through-cache root to redirect all three with one knob
  (default `public.ecr.aws`). **Upgrade action:** anyone setting those per-image
  vars switches to `IMAGE_REGISTRY`.
- **External/utility images stay as explicit overrides.** busybox (dex-init) and
  the ghcr `initcontainer-grafana` (db-init) are not on our public ECR and are
  not governed by `IMAGE_REGISTRY`; mirror them via `DEX_INIT_IMAGE` /
  `DB_INIT_IMAGE` as before.
- **`VERSION` is now optional (`STACK_VERSION`) on every deploy driver**
  (`infra-base`, `infra-rds`, `services`, `satellite-infra-base`,
  `satellite-services`). Each published driver defaults to the version baked
  into it at publish time, so it deploys its own matching templates. `VERSION`
  remains a legacy alias. **Upgrade action:** none required.
- **lakerunner, maestro, dex are digest-pinned** in `cardinal-defaults.yaml`
  (multi-arch index digests), flowing into the template defaults. A new
  `lakerunner-images.txt` (in `generated-templates/`) lists the full
  mirror/scan surface for the application stack. No resource replacement.

## v0.0.126

- **Satellite collector image is now locked in the deploy driver; operators
  supply only a registry prefix.** `deploy-satellite-services.sh` no longer
  takes a full `OTEL_IMAGE` URI (the v0.0.125 input). Instead set `IMAGE_REGISTRY`
  to your registry/pull-through-cache root (default `public.ecr.aws`); the
  collector repo path and pinned tag/digest are baked into the published driver
  and only the prefix is operator-supplied. Example:
  `IMAGE_REGISTRY=<acct>.dkr.ecr.<region>.amazonaws.com/aws-public`. See
  `docs/air-gapped-images.md` (includes the first-pull IAM note for ECR
  pull-through). **Upgrade action:** anyone who set `OTEL_IMAGE` (only available
  in v0.0.125) must switch to `IMAGE_REGISTRY`.
- **`VERSION` is now optional, renamed `STACK_VERSION`.** The published driver
  defaults to the version baked into it at publish time, so it deploys its own
  matching templates. Set `STACK_VERSION` to target a different published
  version. `VERSION` is still accepted as a legacy alias, so existing automation
  keeps working. **Upgrade action:** none required.
- **otel collector image is digest-pinned.** `cardinal-defaults.yaml` now pins
  `cardinalhq-otel-collector:v1.8.0@sha256:9906…` (multi-arch index digest),
  which flows into the template default and `satellite-images.txt`. No resource
  replacement.

## v0.0.125

- **Air-gapped image mirroring for the satellite collector.** A generated
  `satellite-images.txt` (in `generated-templates/`) lists the upstream
  image(s) the `cardinal-satellite-services` stack runs, so they can be
  mirrored and scanned. To deploy from a private mirror, set `OTEL_IMAGE` to
  the full mirrored image URI when running `deploy-satellite-services.sh`; the
  script passes it as the literal `OtelImage` parameter. Unset preserves the
  template's public default. See `docs/air-gapped-images.md`. No upgrade action
  unless mirroring; no resource replacement.

## v0.0.124

- **Lakerunner now installs admin-key-only; Maestro is the sole owner of org
  content.** CloudFormation no longer seeds the organization, its storage line,
  or its ingest key into `configdb`. Three writers were removed: the
  `lakerunner-infra-base` `StorageProfilesParam` / `ApiKeysParam` SSM parameters
  (and the migrator import that consumed them), and the `migration` child's
  `ensure-storage-profile` sidecar. The org, its storage line (the central
  bucket, `otel-raw/`), and its ingest key are now provisioned at runtime by
  Maestro through Lakerunner's `/api/v1/provision` admin API. Admin-api auth is
  unchanged — it already seeds its first key from `cardinal-admin-key` via
  `ADMIN_INITIAL_API_KEY`.
  - **Parameters removed:** `lakerunner-infra-base` drops `OrganizationId`,
    `InitialIngestApiKey`, `StorageProfilesParamName`, `ApiKeysParamName` (and
    its `StorageProfilesParamName` / `ApiKeysParamName` outputs). The
    `migration` child drops `StorageProfilesParamName`, `ApiKeysParamName`,
    `OrgId`, `IngestBucketName`. `lakerunner-services` no longer threads the
    `*ParamName` outputs. `OrganizationId` now lives **only** on
    `lakerunner-services` (it feeds Maestro's `MAESTRO_BOOTSTRAP_ORG_ID`).
  - **Driver change:** `deploy-lakerunner-infra-base.sh` no longer requires
    `ORGANIZATION_ID` and no longer accepts `INITIAL_INGEST_API_KEY`,
    `API_KEYS_PARAM_NAME`, or `STORAGE_PROFILES_PARAM_NAME`.
    `deploy-lakerunner-services.sh` still requires `ORGANIZATION_ID`.
  - **Upgrade action:** none for existing installs — `configdb` is already
    populated and the migrator only seeds empty tables, so this is
    non-destructive to running stacks (it changes the fresh-install contract).
    On the infra-base driver, stop passing `ORGANIZATION_ID` /
    `INITIAL_INGEST_API_KEY` / `*_PARAM_NAME`. Operators who relied on a
    deterministic ingest key now create it in the Maestro UI rather than via
    `InitialIngestApiKey`.
- **Traces route to the cooked bucket; satellite raw-bucket grant narrowed.**
  Default `LakerunnerImage` bumped to `v1.40.4` (was `v1.40.0`): v1.40.4 fixes the
  trace ingest worklane to honor the read/write storage-profile split, so cooked
  traces redirect to the cooked bucket via `writes_to_instance_num` (like logs and
  metrics) instead of being written back to the satellite source bucket. The
  migrator shares this image, so the bump retriggers migrations on update.
  Accordingly the `cardinal-satellite-access` role (in `satellite-infra-base`) no
  longer grants `s3:PutObject` on the raw bucket — its statement Sid changes from
  `RawBucketReadWriteDelete` to `RawBucketReadDelete` (`s3:DeleteObject` stays for
  the poller's `delete_sources` cleanup). This also removes the prior risk of
  cooked trace segments aging out under the raw bucket's lifecycle expiry.
  - **Upgrade action:** redeploy `satellite-infra-base` (each satellite
    account/region) and `lakerunner-services`. The narrowed grant requires
    lakerunner **>= v1.40.4**; if you override `LakerunnerImage` below v1.40.4,
    trace ingest fails with `AccessDenied` on `s3:PutObject` against the raw
    bucket — stay on v1.40.4+.

## v0.0.123

- **`OrganizationId` is now a required, operator-chosen parameter (no default).**
  The canonical `12340000-...` default is removed from `lakerunner-infra-base`,
  `lakerunner-services`, and the nested `maestro`/`migration` children; each now
  requires a UUID (validated by `AllowedPattern`). This makes the bootstrap org
  predictable by choice so it can match a satellite deployed before the central
  install. (#188)
  - The org-onboarding flow: pick a UUID up front; deploy the satellite with it
    (`ORGANIZATION_ID` on `satellite-services`); install lakerunner with the
    **same** UUID. More orgs can be added later via the DB / Maestro.
  - **Upgrade action (required):** set `ORGANIZATION_ID` on the
    `deploy-lakerunner-infra-base.sh` (Job 1) and `deploy-lakerunner-services.sh`
    (Job 5) drivers — both now require it. Existing installs that used the
    canonical org must pass `ORGANIZATION_ID=12340000-0000-4000-8000-000000000000`
    explicitly to keep the same value (a different value would re-seed the
    config-source storage profile for a different org).
  - The org stays on **both** infra-base (seeds the authoritative config-source
    `storage_profiles` + `api_keys` SSM) and services (migration sidecar +
    Maestro). Use the same UUID in both.

## v0.0.122

- **`deploy-satellite-services.sh` now echoes the inputs it can see.** Before
  validating, the driver prints every required and optional input visible to the
  process (value, or `<unset>`) to stderr, so a `missing required: ...` failure
  is easy to diagnose. The usual cause is a value set as a plain shell variable
  but not exported, so the driver — a separate process — never receives it and
  it shows as `<unset>`. No behavior or parameter change.
  - Upgrade action: none.

## v0.0.121

- **`PubsubAutoRegister` now defaults to `true`.** New `lakerunner-services`
  deploys auto-register unseen satellite raw-bucket orgs and route their cooked
  output to `PubsubAutoRegisterWritesToInstance` (default `1`) without an extra
  flag. (#186)
  - Upgrade action: none to keep it on. To preserve the old off-by-default
    behavior, pass `PUBSUB_AUTOREGISTER=false` (driver) / set
    `PubsubAutoRegister=false`. Existing stacks keep whatever value they were
    deployed with until you re-apply.
- **pubsub-sqs can now consume multiple satellite SQS queues.** Beyond the
  primary (group-0) `QueueUrl`/`QueueRoleArn`, `lakerunner-services` adds
  numbered queue groups 1..10: `QueueUrl<n>` / `QueueRegion<n>` / `QueueRoleArn<n>`
  params, emitted as `SQS_QUEUE_URL_<n>` / `SQS_REGION_<n>` / `SQS_ROLE_ARN_<n>`
  on the pubsub-sqs container only when set. Each group carries its own region
  and assume-role, so the central poller reaches satellite queues in other
  accounts/regions. Driver env: `QUEUE_URL_<n>` / `QUEUE_REGION_<n>` /
  `QUEUE_ROLE_ARN_<n>` (`QUEUE_REGION_<n>` defaults to `REGION`).
  - Upgrade action: none. Set the numbered env vars to add satellites; the
    ceiling is 10 (bump `MAX_ADDITIONAL_QUEUES` to raise it).

## v0.0.120

- **Satellite installs are fully decoupled from the central account.** A
  satellite (`satellite-infra-base` + `satellite-services`, deployed as a
  same-account/region pair) may now live in the **same or a different AWS
  account** than the central lakerunner install — no satellite driver reads the
  central `lakerunner-infra-base` stack (a cross-account `describe-stacks` that
  could never have worked). (#185)
  - **Collector no longer uses a license.** `satellite-services` drops the
    `LicenseSecretArn` parameter, the collector exec-role
    `secretsmanager:GetSecretValue`, and the `LICENSE_DATA` container secret —
    the otel-collector image needs no license.
  - **Central principal is supplied directly.** `deploy-satellite-infra-base.sh`
    now takes `LAKERUNNER_PRINCIPAL` (the central `ProcessRoleArn`, read once out
    of band) instead of mapping it from the `lakerunner-infra-base` stack, and no
    longer requires `INFRA_BASE_STACK`.
  - `deploy-satellite-services.sh` no longer requires `INFRA_BASE_STACK` (it only
    needed it for the license); it still pulls `RawBucketName` from the
    satellite's own paired `satellite-infra-base` in the same account.
  - Upgrade action: when redeploying a satellite, set `LAKERUNNER_PRINCIPAL` on
    the infra-base driver and drop `INFRA_BASE_STACK` from both satellite drivers.
    The collector's own task/exec roles are unchanged (always self-contained). No
    data-bearing resource is replaced.

## v0.0.119

- **Satellite self-telemetry traces now process (IAM widening).** The
  `cardinal-satellite-access` role gains `s3:PutObject` on the satellite raw
  bucket (Sid `RawBucketReadDelete` -> `RawBucketReadWriteDelete`) in
  `satellite-infra-base`. Previously satellite trace processing failed with
  `s3:PutObject AccessDenied` while logs and metrics succeeded — the lakerunner
  trace worklane writes cooked segments back to the raw bucket instead of
  following the cooked-bucket redirect that logs/metrics use. (#183)
  - Upgrade action: update the `satellite-infra-base` stack to apply the widened
    role. Only relevant if you run the satellite / self-telemetry path.
  - Caveat: until the lakerunner code splits read vs. write for the trace
    worklane, cooked trace parquet lands in the raw bucket (subject to its
    lifecycle). The grant is removable once that lands.

## v0.0.118

- **Internal cleanup, no template change.** Removed dead per-service
  `cpu`/`memory_mib`/`replicas` fields and a dead `monitoring.ingress` block from
  the merged control tier in `cardinal-defaults.yaml`. The generated
  `services-control` template is byte-identical to v0.0.117. No upgrade action.
  (#182)

## v0.0.117

- **Container image bump.** Default images updated: `lakerunner`
  v1.33.0 -> **v1.40.0**, `maestro` v1.46.4 -> **v1.50.0**. `otel` (v1.8.0) and
  `dex` (v0.1.0) unchanged. (#181)
  - The `lakerunner` image is shared by the service tasks and the DB migrator, so
    updating reruns the migrator (the `MigrationStack`) before the service-tier
    stacks update; the service tiers only deploy after migrations succeed, and a
    failed migration rolls the update back.
  - Upgrade action: none if you use the defaults. If you pin `LakerunnerImage` /
    `MaestroImage` via parameters, set them to the new versions explicitly.

## v0.0.116

- **Security groups no longer specify inline egress (SCP compatibility).** All
  security groups (application ALB and task SGs, RDS, satellite ALB/task, and the
  lrdev VPC-endpoint SG) drop their inline all-allow `SecurityGroupEgress` and
  keep AWS's auto-created default all-allow egress instead. Network behavior is
  identical. (#180)
  - Why it matters: with an inline egress rule, CloudFormation calls
    `ec2:RevokeSecurityGroupEgress` during SG creation to swap the default rule —
    even when the rule is byte-for-byte the default. Accounts whose SCP denies
    that revoke (e.g. Control Tower "deny VPC-destructive" guardrails) previously
    hit `CREATE_FAILED` on `infra-base`. This unblocks them.
  - Upgrade action: none for existing installs; relevant to new installs in
    SCP-restricted accounts.
- **New teardown tool (dev only).** `dev-scripts/sweep-stranded-resources.sh`
  deletes resources left in `DELETE_FAILED` / `DELETE_SKIPPED` after a failed
  stack delete, via a one-shot privileged Fargate task (requires a
  caller-supplied superadmin task role). Not published to customers. (#179)

## v0.0.115

- **S3 bucket Public Access Block is now opt-in (default off).** New parameter
  **`ConfigureBucketPublicAccessBlock`** (default `false`) on
  `lakerunner-infra-base` (cooked bucket) and `satellite-infra-base` (raw
  bucket). By default the `PublicAccessBlockConfiguration` property is omitted
  entirely; set the parameter to `true` to restore the explicit block. Driver
  passthrough: `CONFIGURE_BUCKET_PUBLIC_ACCESS_BLOCK`. (#178)
  - Why it matters: setting the block requires `s3:PutBucketPublicAccessBlock`,
    which some deployer roles lack — those deploys previously failed. Buckets stay
    non-public via AWS account/bucket default BPA; neither bucket carries any
    public policy or ACL.
  - Upgrade action: none required. If your org mandates an explicit per-bucket
    block and your deployer role holds the permission, set
    `ConfigureBucketPublicAccessBlock=true`.

## v0.0.114

- **Database engine reverted from Aurora back to a single RDS instance.**
  `cardinal-lakerunner-infra-rds` is once again an `AWS::RDS::DBInstance`
  (PostgreSQL, `db.r7g.large`, gp3, `DBAllocatedStorage` parameter), reverting
  the Aurora PostgreSQL cluster that shipped in v0.0.113. (#175)
  - **CRITICAL upgrade action — only if you deployed v0.0.113 (Aurora):**
    updating from v0.0.113 to v0.0.114 or later changes the database from an
    Aurora `DBCluster` to an RDS `DBInstance`. These are different resource types,
    so CloudFormation **removes the Aurora cluster and creates a new, empty RDS
    instance** — data does not migrate automatically. The Aurora cluster
    snapshots on removal per its deletion policy, but the new instance starts
    empty. Plan a data migration (snapshot/restore or dump/load) before updating.
  - Installs on v0.0.112 or earlier (already RDS) are unaffected — for them this
    is a no-op.
- **Deploy drivers accept license and cert material as direct strings (files now
  optional).** The chained per-stack drivers take secrets inline; the `_FILE`
  path variants remain as fallbacks (additive, non-breaking). (#177)
  - `infra-base`: `LICENSE_DATA` (the `z64:...` token as a string, primary) or
    `LICENSE_DATA_FILE` (path fallback) — one is required.
  - `services` (cert): `CERTIFICATE_BODY` / `CERTIFICATE_PRIVATE_KEY` /
    `CERTIFICATE_CHAIN` as PEM strings, with `CERTIFICATE_*_FILE` path fallbacks
    (the string wins when both are set). The create-only self-signed fallback is
    unchanged.
- **Script layout: `scripts/` is now customer-facing drivers only.** Lifecycle
  and dev tooling moved to `dev-scripts/` — including `cleanup-lakerunner.sh` and
  `teardown-lakerunner.sh` (the former `deploy-scripts/` was renamed to
  `dev-scripts/`). (#176)
  - Upgrade action: if you referenced `scripts/cleanup-lakerunner.sh` or
    `scripts/teardown-lakerunner.sh`, repoint to `dev-scripts/`.
