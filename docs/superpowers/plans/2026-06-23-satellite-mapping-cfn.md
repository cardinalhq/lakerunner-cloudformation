# Satellite Mapping — CFN Driver Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the numbered `QUEUE_URL_<n>` env hack and `PUBSUB_AUTOREGISTER*` with a single operator JSON, delivered to Maestro as `MAESTRO_SATELLITE_CONFIG` via SSM `/cardinal/satellites`. The deploy driver auto-synthesizes the central `normal` collector (collector_name `lakerunner`) from the install's bucket/queue/region/org and merges operator-supplied satellites. `pubsub-sqs` no longer receives any SQS env (it reads queues from configdb on the new lakerunner image).

**Architecture:** This repo's lakerunner stack creates no infra; the driver (`deploy-lakerunner-services.sh`) provisions and threads identifiers in. The driver composes the satellite JSON with `jq`, writes it to SSM `/cardinal/satellites` (`aws ssm put-parameter`), and passes the param name to the stack; `maestro.py` injects it as an ECS `Secret`-sourced env `MAESTRO_SATELLITE_CONFIG`. The numbered-queue params, the `_additional_queue_env()` machinery, the primary `QueueUrl`/`QueueRoleArn` pubsub env, and the autoregister params are all removed from `services_process.py`.

**Tech Stack:** Python + troposphere (generators), bash + aws-cli + jq (driver), cloud-radar/cfn-lint (tests).

**Repo:** `/Users/mgraff/git/github/cardinalhq/lakerunner-cloudformation` (this repo; the CFN repo is the working repo, not in flux — no worktree needed). Build/test via the Makefile (`make build`, `make test`, `make lint`).

## Global Constraints

- The central collector synthesized into the JSON MUST use `collector_name = "lakerunner"` (the legacy value at `maestro.py:562`). This is load-bearing for upgrades: lakerunner's `UpsertOrganizationBucketManaged` is `ON CONFLICT (organization_id, collector_name) DO UPDATE … managed_by = EXCLUDED.managed_by`, so reusing the name converts an existing install's *unmanaged* central bucket row to `external_json_config` in place (no duplicate `normal`). Expose it as a driver knob `CENTRAL_COLLECTOR_NAME` (default `lakerunner`) and document that on upgrade it must match the install's existing collector name.
- The satellite JSON shape delivered to Maestro (must match the Maestro plan's parser exactly):

  ```json
  { "organizations": { "<org-uuid>": { "collectors": {
    "<name>": { "bucket": "...", "sqsurl": "...", "region": "...", "role": "(optional)", "mode": "normal|read-only|satellite (optional)", "cloud_provider": "(optional, default aws)" } } } } }
  ```
- The central collector entry: `{ bucket: <RawBucketName>, sqsurl: <RawQueueUrl>, region: <REGION>, mode: "normal" }` under `organizations[<ORGANIZATION_ID>].collectors["lakerunner"]`. Operator satellites (read-only/satellite) merge in under the same/other orgs; the merge MUST reject a duplicate `normal` collector for the install org (the operator's JSON must not also declare a normal for it).
- `pubsub-sqs` receives NO SQS env after this change (queues come from configdb).
- The Maestro execution role must be able to resolve the SSM secret: `ssm:GetParameters` on `/cardinal/satellites` (mirror the retired `/cardinal/storage-profiles` grant).
- cfn-lint must pass with no errors (`make lint`). Follow `CLAUDE.md` coding style; CHANGELOG entry required before any release tag.
- This is an UPGRADE-SAFE change: existing installs must keep ingesting. See the Upgrade section.

---

### Task 1: Remove queue + autoregister surface from `services_process.py`

**Files:**
- Modify: `src/cardinal_cfn/children/services_process.py`
- Test: `tests/templates/test_services_process.py` (adjust assertions)

**Interfaces:**
- Produces: the `services-process` template no longer declares `QueueUrl`, `QueueRoleArn`, `QueueUrl<n>`, `QueueRegion<n>`, `QueueRoleArn<n>`, `MAX_ADDITIONAL_QUEUES`, `HasQueue<n>`/`HasQueueRegion<n>` conditions, `PubsubAutoRegister`, `PubsubAutoRegisterWritesToInstance`; the `pubsub-sqs` container has no `SQS_*` / `LAKERUNNER_PUBSUB_AUTOREGISTER*` env; `_additional_queue_env()` is deleted.

- [ ] **Step 1: Update the failing template test**

In `tests/templates/test_services_process.py`, replace any assertion that these params/env exist with assertions that they are ABSENT. Add:

```python
def test_no_queue_or_autoregister_params(services_process_template):
    params = services_process_template.get("Parameters", {})
    for k in ("QueueUrl", "QueueRoleArn", "QueueUrl1", "QueueRegion1", "QueueRoleArn1",
              "PubsubAutoRegister", "PubsubAutoRegisterWritesToInstance"):
        assert k not in params, f"{k} must be removed"

def test_pubsub_sqs_has_no_sqs_env(services_process_template):
    # locate the pubsub-sqs container env; assert no SQS_* / AUTOREGISTER vars
    env_names = _pubsub_sqs_env_names(services_process_template)  # helper: collect Environment Names
    assert not any(n.startswith("SQS_") or "AUTOREGISTER" in n for n in env_names)
```

(Write `_pubsub_sqs_env_names` to walk the generated task definition for the `pubsub-sqs` container, matching how the existing tests locate containers.)

- [ ] **Step 2: Run to verify failure**

Run: `make build && python3 -m pytest tests/templates/test_services_process.py -k "queue or autoregister or sqs_env" -v`
Expected: FAIL (params/env still present).

- [ ] **Step 3: Delete the declarations + env + helper**

In `src/cardinal_cfn/children/services_process.py` remove: the `QueueUrl`/`QueueRoleArn` parameters (~106-127); the `for n in range(1, MAX_ADDITIONAL_QUEUES+1)` loop and `MAX_ADDITIONAL_QUEUES` (~49, ~133-161); the `PubsubAutoRegister*` parameters (~294-317); the `SQS_QUEUE_URL`/`SQS_REGION`/`SQS_ROLE_ARN`/`LAKERUNNER_PUBSUB_AUTOREGISTER*` Environment entries and the `*_additional_queue_env()` splat in the pubsub-sqs `extra_env` (~462-472); and the `_additional_queue_env()` function (~644-684). Remove now-unused imports (`Not`, `Equals`, `If`, `Ref` if unused, etc.) flagged by lint.

- [ ] **Step 4: Run to verify pass**

Run: `make build && python3 -m pytest tests/templates/test_services_process.py -v && make lint`
Expected: PASS, cfn-lint clean.

- [ ] **Step 5: Commit**

```bash
git add src/cardinal_cfn/children/services_process.py tests/templates/test_services_process.py
git commit -m "services-process: drop SQS queue env + autoregister (pubsub reads configdb)"
```

---

### Task 2: `maestro.py` — inject `MAESTRO_SATELLITE_CONFIG` from SSM; retire bootstrap bucket env

**Files:**
- Modify: `src/cardinal_cfn/children/maestro.py`
- Test: `tests/templates/test_maestro.py`

**Interfaces:**
- Consumes: a new `SatellitesParamName` String parameter (default `/cardinal/satellites`).
- Produces: an ECS `Secret(Name="MAESTRO_SATELLITE_CONFIG", ValueFrom=Sub("arn:${AWS::Partition}:ssm:${AWS::Region}:${AWS::AccountId}:parameter${SatellitesParamName}"))` on the maestro container. The `MAESTRO_BOOTSTRAP_BUCKET_NAME/REGION/CLOUD_PROVIDER/COLLECTOR_NAME` Environment entries (~559-562) and the `BucketName` parameter (~143-155) are removed (the central bucket now arrives via the JSON). `MAESTRO_BOOTSTRAP_ORG_ID/ORG_NAME/OWNER_EMAIL` stay.

- [ ] **Step 1: Write the failing test**

In `tests/templates/test_maestro.py`:

```python
def test_satellite_config_secret_present(maestro_template):
    secrets = _maestro_container_secret_names(maestro_template)  # helper
    assert "MAESTRO_SATELLITE_CONFIG" in secrets

def test_bootstrap_bucket_env_removed(maestro_template):
    env = _maestro_container_env_names(maestro_template)
    for k in ("MAESTRO_BOOTSTRAP_BUCKET_NAME", "MAESTRO_BOOTSTRAP_BUCKET_REGION",
              "MAESTRO_BOOTSTRAP_BUCKET_CLOUD_PROVIDER", "MAESTRO_BOOTSTRAP_BUCKET_COLLECTOR_NAME"):
        assert k not in env
    assert "BucketName" not in maestro_template.get("Parameters", {})
```

- [ ] **Step 2: Run to verify failure**

Run: `make build && python3 -m pytest tests/templates/test_maestro.py -k "satellite or bootstrap_bucket" -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

In `maestro.py`: remove the `BucketName` parameter and the four `MAESTRO_BOOTSTRAP_BUCKET_*` Environment lines. Add the `SatellitesParamName` parameter:

```python
t.add_parameter(Parameter(
    "SatellitesParamName",
    Type="String",
    Default="/cardinal/satellites",
    Description=("SSM parameter holding the satellite-mapping JSON. Injected as the "
                 "MAESTRO_SATELLITE_CONFIG env var; Maestro's provisioning worker "
                 "reconciles it into lakerunner's configdb (sole writer)."),
))
```

Add to the maestro container `Secrets=[...]` (next to the admin-key secret):

```python
Secret(
    Name="MAESTRO_SATELLITE_CONFIG",
    ValueFrom=Sub("arn:${AWS::Partition}:ssm:${AWS::Region}:${AWS::AccountId}:parameter${SatellitesParamName}"),
),
```

- [ ] **Step 4: Run to verify pass**

Run: `make build && python3 -m pytest tests/templates/test_maestro.py -v && make lint`
Expected: PASS, cfn-lint clean.

- [ ] **Step 5: Commit**

```bash
git add src/cardinal_cfn/children/maestro.py tests/templates/test_maestro.py
git commit -m "maestro: inject MAESTRO_SATELLITE_CONFIG from SSM; retire bootstrap bucket env"
```

---

### Task 3: Root wiring (`lakerunner_services.py`)

**Files:**
- Modify: `src/cardinal_cfn/lakerunner_services.py`
- Test: `tests/templates/test_root.py` (or the root template test)

**Interfaces:**
- Consumes: Task 1 (services-process no longer takes queue/autoregister params), Task 2 (maestro takes `SatellitesParamName`, no `BucketName`).
- Produces: the root declares a `SatellitesParamName` parameter (default `/cardinal/satellites`), passes it to the maestro child, stops passing `BucketName` to maestro, and stops passing `QueueUrl`/`QueueRoleArn`/`QueueUrl<n>`/`QueueRegion<n>`/`QueueRoleArn<n>`/`PubsubAutoRegister*` to the services-process child.

- [ ] **Step 1: Write the failing test**

```python
def test_root_passes_satellites_param_to_maestro(root_template):
    maestro = root_template["Resources"]["MaestroStack"]["Properties"]["Parameters"]
    assert "SatellitesParamName" in maestro
    assert "BucketName" not in maestro

def test_root_drops_queue_params_to_process(root_template):
    proc = root_template["Resources"]["ServicesProcessStack"]["Properties"]["Parameters"]
    for k in ("QueueUrl", "QueueRoleArn", "QueueUrl1", "PubsubAutoRegister"):
        assert k not in proc
```

(Match the real nested-stack logical IDs from the generated root.)

- [ ] **Step 2: Run to verify failure**

Run: `make build && python3 -m pytest tests/templates/test_root.py -k "satellites or queue_params" -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

In `lakerunner_services.py`: add a `SatellitesParamName` root parameter; in the `_add_child` call for the maestro child, add `"SatellitesParamName": Ref("SatellitesParamName")` and remove `"BucketName": ...`; in the services-process child params dict, remove the `QueueUrl`/`QueueRoleArn`/numbered/`PubsubAutoRegister*` entries. Remove the now-unused root parameters (`QueueUrl`, `QueueRoleArn`, `QueueUrl<n>`, etc., `PubsubAutoRegister*`) if they were declared at the root.

- [ ] **Step 4: Run to verify pass**

Run: `make build && python3 -m pytest tests/templates/test_root.py -v && make lint`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/cardinal_cfn/lakerunner_services.py tests/templates/test_root.py
git commit -m "root: wire SatellitesParamName to maestro; drop queue/autoregister + BucketName"
```

---

### Task 4: Driver — synthesize central + merge satellites + write SSM (`deploy-lakerunner-services.sh`)

**Files:**
- Modify: `scripts/deploy-lakerunner-services.sh`

**Interfaces:**
- Consumes: the satellite-infra-base stack outputs `RawQueueUrl` + `RawBucketName` (already read for `RawQueueUrl` at ~208-221; add `RawBucketName`), `REGION`, `ORGANIZATION_ID`.
- Produces: composes `MAESTRO_SATELLITE_CONFIG` JSON (central `lakerunner` collector + operator `SATELLITE_CONFIG` satellites), writes it to SSM `/cardinal/satellites` (`aws ssm put-parameter --type String --tier Advanced --overwrite`), passes `SatellitesParamName=/cardinal/satellites` to the stack. New env knobs: `SATELLITE_CONFIG` / `SATELLITE_CONFIG_FILE` (operator satellites JSON: the `organizations` document WITHOUT the install org's central — or with only read-only/satellite collectors), `CENTRAL_COLLECTOR_NAME` (default `lakerunner`), `SATELLITES_PARAM_NAME` (default `/cardinal/satellites`). Removes: the numbered-queue loop (~294-312), the `PubsubAutoRegister*` param assembly (~443-446), and the primary `QueueUrl=`/`QueueRoleArn=` param lines (~253-254).

- [ ] **Step 1: Add the central-collector synthesis + merge (jq)**

After reading the satellite stack outputs (extend the existing `jq` block to also extract `RawBucketName`), build the central collector and merge operator satellites:

```bash
raw_bucket=$(printf '%s' "$sat_outputs" | jq -r '(.[] | select(.OutputKey=="RawBucketName") | .OutputValue) // ""')
central_collector="${CENTRAL_COLLECTOR_NAME:-lakerunner}"

# Operator satellites: a JSON doc { "organizations": { ... } } with read-only/satellite
# collectors only (no normal for the install org). Empty when unset.
operator_json="${SATELLITE_CONFIG:-}"
if [ -z "$operator_json" ] && [ -n "${SATELLITE_CONFIG_FILE:-}" ]; then
    operator_json=$(cat "$SATELLITE_CONFIG_FILE")
fi
operator_json="${operator_json:-{\"organizations\":{}}}"

# Synthesize the central normal collector for the install org and deep-merge.
central_json=$(jq -n \
    --arg org "$ORGANIZATION_ID" --arg coll "$central_collector" \
    --arg bucket "$raw_bucket" --arg sqs "$queue_url" --arg region "$REGION" \
    '{organizations: {($org): {collectors: {($coll): {bucket:$bucket, sqsurl:$sqs, region:$region, mode:"normal"}}}}}')

satellites_json=$(printf '%s' "$operator_json" | jq --argjson c "$central_json" '
    # reject a duplicate normal for the install org
    . as $op
    | ($c.organizations | keys[0]) as $org
    | (($op.organizations[$org].collectors // {}) | to_entries | map(select((.value.mode // "normal")=="normal")) | length) as $opNormals
    | if $opNormals > 0 then error("operator SATELLITE_CONFIG must not declare a normal collector for the install org \($org)") else . end
    | $c * $op  # deep-ish merge; for collectors, combine
    ' 2>/dev/null) || { echo "[deploy] ERROR composing satellite config" >&2; exit 2; }
```

(The merge must UNION the collectors maps, not replace — if the simple `*` does not union nested collector maps the way you need, use an explicit `reduce` over `$op.organizations` merging `.collectors`. Verify the composed JSON has exactly one `normal` per org before writing.)

- [ ] **Step 2: Validate + write to SSM**

```bash
# Validate: each org exactly one normal collector.
bad=$(printf '%s' "$satellites_json" | jq -r '
    [.organizations | to_entries[] | {org:.key, normals: ([.value.collectors[] | select((.mode//"normal")=="normal")] | length)}
     | select(.normals != 1) | "\(.org):\(.normals)"] | join(", ")')
[ -n "$bad" ] && { echo "[deploy] ERROR: orgs without exactly one normal collector: $bad" >&2; exit 2; }

sat_param="${SATELLITES_PARAM_NAME:-/cardinal/satellites}"
aws ssm put-parameter --name "$sat_param" --type String --tier Advanced \
    --value "$satellites_json" --overwrite --region "$REGION" >/dev/null
echo "[deploy] wrote satellite config to SSM $sat_param" >&2
```

- [ ] **Step 3: Update the params blob**

Remove the `QueueUrl=`/`QueueRoleArn=` lines (~253-254), the numbered-queue loop (~294-312), and the `PubsubAutoRegister*` assembly (~443-446). Add:

```bash
params="$params
SatellitesParamName=$sat_param"
```

- [ ] **Step 4: Update usage/help text**

Update the script's `usage()` to drop `QUEUE_URL_<n>` / `QUEUE_REGION_<n>` / `QUEUE_ROLE_ARN_<n>` / `PUBSUB_AUTOREGISTER*` and document `SATELLITE_CONFIG` / `SATELLITE_CONFIG_FILE` / `CENTRAL_COLLECTOR_NAME` / `SATELLITES_PARAM_NAME`, including the upgrade note (operators move old numbered queues into `SATELLITE_CONFIG`).

- [ ] **Step 5: Verify the driver parses + composes**

Run a dry compose (no AWS) to confirm jq logic. Provide a sample `SATELLITE_CONFIG` with one read-only satellite and assert the composed JSON has the central `lakerunner` normal + the satellite, exactly one normal for the org. (Add a small shell test under `tests/` if the repo has a script-test harness; otherwise document the manual check in the commit.)

- [ ] **Step 6: Commit**

```bash
git add scripts/deploy-lakerunner-services.sh
git commit -m "driver: synthesize central collector + merge satellites -> SSM /cardinal/satellites"
```

---

### Task 5: Permissions, docs, defaults, CHANGELOG (incl. upgrade)

**Files:**
- Modify: `docs/operations/permissions-lakerunner.md`, `docs/operations/iam-roles.md` (maestro execution role: `ssm:GetParameters` on `/cardinal/satellites`; drop `/cardinal/storage-profiles` + `/cardinal/api-keys` references)
- Modify: `docs/operations/production-deploy.md` (replace numbered-queue / autoregister guidance with `SATELLITE_CONFIG`)
- Modify: `scripts/README.md` (driver env reference)
- Modify: `cardinal-defaults.yaml` (remove the retired `storage_profiles:` block if unused)
- Modify: `CHANGELOG.md` (new version entry with upgrade actions)

**Interfaces:** documentation only; no template change.

- [ ] **Step 1: Update IAM/permissions docs**

Grant the maestro EXECUTION role `ssm:GetParameters` on `arn:${Partition}:ssm:${Region}:${Account}:parameter/cardinal/satellites` (ECS resolves the `Secret` at task start). Remove the now-retired `/cardinal/storage-profiles` and `/cardinal/api-keys` rows.

- [ ] **Step 2: Update operator docs + README**

In `production-deploy.md` and `scripts/README.md`: remove `QUEUE_URL_<n>` / `PUBSUB_AUTOREGISTER*`; document `SATELLITE_CONFIG` (operator read-only/satellite collectors), `CENTRAL_COLLECTOR_NAME`, and that the central collector is auto-synthesized.

- [ ] **Step 3: Remove retired defaults**

If `cardinal-defaults.yaml`'s `storage_profiles:` block is no longer read by any generator (confirm with grep), remove it.

- [ ] **Step 4: CHANGELOG (upgrade-focused)**

Add a `## vX.Y.Z` entry. Required content:
- New: satellite mappings via `SATELLITE_CONFIG` JSON → SSM `/cardinal/satellites` → Maestro; central collector auto-synthesized as `lakerunner`.
- Removed params: `QUEUE_URL_<n>` / `QUEUE_REGION_<n>` / `QUEUE_ROLE_ARN_<n>` and `PUBSUB_AUTOREGISTER` / `PUBSUB_AUTOREGISTER_WRITES_TO_INSTANCE`. **Upgrade action:** operators using numbered satellite queues must move each into `SATELLITE_CONFIG` (as a `read-only` or `satellite` collector with `bucket`/`sqsurl`/`region`/`role`) before deploying; otherwise those satellites stop being polled.
- Requires the new lakerunner image (pubsub-sqs reads queues from configdb) and the Maestro image that consumes `MAESTRO_SATELLITE_CONFIG`. Note the bundled image pins.
- Note the brief ingestion gap: on first deploy, pubsub-sqs polls nothing until Maestro's startup reconcile writes `sqs_queue_url` into configdb (minutes).
- Note: the central bucket's existing *unmanaged* configdb row is converted in place to `external_json_config` (because `CENTRAL_COLLECTOR_NAME` defaults to the legacy `lakerunner`). Operators who customized the collector name must set `CENTRAL_COLLECTOR_NAME` to match, or they will get a duplicate `normal` (rejected). Lingering autoregister-created satellite rows become inert (no `sqs_queue_url`) and may be cleaned up.
- IAM: maestro execution role now needs `ssm:GetParameters` on `/cardinal/satellites`.

- [ ] **Step 5: Build + full test + lint**

Run: `make build && make test && make lint`
Expected: all green, cfn-lint clean.

- [ ] **Step 6: Commit**

```bash
git add docs/ scripts/README.md cardinal-defaults.yaml CHANGELOG.md
git commit -m "docs: satellite config + upgrade notes; retire storage-profiles refs"
```

---

## Upgrade (cross-cutting — verify before release)

Existing installs MUST keep ingesting across the upgrade:

1. **Central bucket in-place takeover.** With `CENTRAL_COLLECTOR_NAME=lakerunner` (default, matching `maestro.py:562`'s legacy value), the first Maestro reconcile converts the existing unmanaged central `organization_buckets` row to `external_json_config` and populates `bucket_configurations.sqs_queue_url` — via the `ON CONFLICT (organization_id, collector_name) DO UPDATE` upsert. No duplicate `normal`, no manual data migration.
2. **Numbered-queue satellites → `SATELLITE_CONFIG`.** These move from removed env params into the JSON. Until the operator does this, those satellites are not polled (documented in CHANGELOG).
3. **Ordering / brief gap.** New lakerunner image makes pubsub-sqs configdb-sourced; until Maestro's startup reconcile lands `sqs_queue_url`, pubsub polls nothing. Eventual-consistent within minutes; acceptable.
4. **Inert leftovers.** Autoregister-created unmanaged satellite rows remain but have no `sqs_queue_url` (never polled); operators may delete them.

## Self-Review

**Spec coverage:** numbered-hack + autoregister removal (Tasks 1, 3, 4); SATELLITE_CONFIG → SSM → Maestro env (Tasks 2, 4); central auto-synthesis with legacy collector name for upgrade (Task 4, Global Constraints, Upgrade); IAM + docs + CHANGELOG (Task 5).

**Cross-repo dependencies:** requires the lakerunner foundation image (pubsub-from-configdb, mode/managed_by contract, ON CONFLICT migration) and the Maestro image (`MAESTRO_SATELLITE_CONFIG` parser + per-org reconcile). The bundled image pins in `cardinal-defaults.yaml` must be bumped to versions that include both before release.

**Open items for execution:** confirm the exact jq collector-map UNION semantics (Task 4 Step 1) against a couple of fixtures; confirm whether `BucketName` is referenced by any other child before removing the root→maestro pass (Task 3); confirm SSM Advanced-tier 8 KB ceiling is adequate or escalate the carrier to S3.
