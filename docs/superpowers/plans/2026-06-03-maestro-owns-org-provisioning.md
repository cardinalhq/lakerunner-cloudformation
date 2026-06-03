# Maestro Sole Owner of Org Provisioning — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Lakerunner install in "just the admin key exists" mode by removing CloudFormation's three org-content writers, leaving Maestro (via `/api/v1/provision`) as the sole owner of the org, its storage line, and its ingest key.

**Architecture:** Pure removal on the CFN generators. Today three things seed org content into `configdb`: (1) infra-base SSM `StorageProfilesParam`/`ApiKeysParam` imported by the migrator, (2) the migration child's `ensure-storage-profile` sidecar that upserts the org bucket row, (3) Maestro's provisioning worker. We delete (1) and (2). `ADMIN_INITIAL_API_KEY` (admin-key-only auth) is already wired in `services_control.py:350` — unchanged. `OrganizationId` stays only on the services root (feeds `MAESTRO_BOOTSTRAP_ORG_ID`); it is removed from infra-base and the migration child.

**Tech Stack:** Python + troposphere generators; cloud-radar template tests; cfn-lint; bash deploy drivers assembled from `scripts-src/` by `build.sh`.

**Verification gate for every task:** `make build` (generate + cfn-lint) and the relevant `pytest` must pass before commit.

---

### Task 1: Drop org-content seeding from `lakerunner-infra-base`

**Files:**
- Modify: `src/cardinal_cfn/lakerunner_infra_base.py`
- Test: `tests/templates/test_lakerunner_infra_base.py`

- [ ] **Step 1: Update the test to the new contract (failing first).**

In `tests/templates/test_lakerunner_infra_base.py`:
- Remove `StorageProfilesParamName`, `ApiKeysParamName`, `OrganizationId`, `InitialIngestApiKey` from the expected-parameters set (around lines 31-35) and from the outputs set (around lines 337-338).
- Remove `test`-the-org-default body that reads `td["Parameters"]["OrganizationId"]` (lines ~41-65) and the `StorageProfilesParam` / `ApiKeysParam` resource assertions (lines ~305-311).
- Add a negative test:

```python
def test_no_org_content_params_or_resources(template_dict):
    """infra-base seeds NO org content; Maestro owns it via /api/v1/provision."""
    params = template_dict["Parameters"]
    for gone in ("OrganizationId", "InitialIngestApiKey",
                 "StorageProfilesParamName", "ApiKeysParamName"):
        assert gone not in params, f"{gone} should be removed from infra-base"
    resources = template_dict["Resources"]
    assert "StorageProfilesParam" not in resources
    assert "ApiKeysParam" not in resources
    outputs = template_dict.get("Outputs", {})
    assert "StorageProfilesParamName" not in outputs
    assert "ApiKeysParamName" not in outputs
```

- [ ] **Step 2: Run the test, expect failure.**

Run: `.venv/bin/python -m pytest tests/templates/test_lakerunner_infra_base.py -q`
Expected: FAIL (params/resources still present).

- [ ] **Step 3: Edit `lakerunner_infra_base.py` — remove the parameters.**

Delete the `Parameter` blocks for `StorageProfilesParamName` (lines ~206-215), `ApiKeysParamName` (~216-225), `OrganizationId` (~226-239), and `InitialIngestApiKey` (~250-258). Keep `license_secret_name`, `admin_key_secret_name`, `license_data`.

- [ ] **Step 4: Edit parameter-group metadata.**

In `add_parameter_group_metadata` (lines ~260-309): delete the `"Organization"` group entry (`["OrganizationId", "InitialIngestApiKey"]`), drop `StorageProfilesParamName`/`ApiKeysParamName` from the `"Names (advanced)"` group, and remove the `OrganizationId`/`InitialIngestApiKey` entries from the `labels=` dict.

- [ ] **Step 5: Remove the `HasInitialIngestApiKey` condition** (lines ~322-325).

- [ ] **Step 6: Remove the SSM `_stmt_ssm_read([...])` calls** from all five tier roles (migration ~600-603, query ~622-625, process ~658-661, control ~707-710, maestro ~762-765). Also remove the exec role's `ResolveCardinalSsm` statement (lines ~568-579) and the now-unused `_stmt_ssm_read` helper (lines ~991-1007). No SSM parameters remain to read.

- [ ] **Step 7: Remove the SSM resources** `StorageProfilesParam` (lines ~853-881) and `ApiKeysParam` (~883-910), plus the `SSMParameter` import (line 60) and the two `_emit(...ParamName...)` outputs (~944-947).

- [ ] **Step 8: Update the module docstring** (lines 17, 31-33) to drop the storage-profiles/api-keys SSM mentions; note the stack creates the admin-key + license secrets only.

- [ ] **Step 9: Run tests + build.**

Run: `.venv/bin/python -m pytest tests/templates/test_lakerunner_infra_base.py -q && make build`
Expected: PASS; cfn-lint clean.

- [ ] **Step 10: Commit.**

```bash
git add src/cardinal_cfn/lakerunner_infra_base.py tests/templates/test_lakerunner_infra_base.py
git commit -m "infra-base: stop seeding org content (no OrganizationId/SSM); Maestro owns it"
```

---

### Task 2: Strip the migrator's SSM import and the `ensure-storage-profile` sidecar

**Files:**
- Modify: `src/cardinal_cfn/children/migration.py`
- Test: `tests/templates/test_migration.py`

- [ ] **Step 1: Update tests to new contract (failing first).**

In `tests/templates/test_migration.py`:
- Remove `StorageProfilesParamName`, `ApiKeysParamName`, `OrgId`, `IngestBucketName` from the expected-params tuple (lines ~43-44).
- Delete `test_migrator_seeds_configdb_from_ssm` (~48-65).
- Change the container-name expectation (line ~104) to `["configdb-init", "migrator", "keepalive"]`.
- Delete `test_ensure_storage_profile_*` tests (~131-162) and the `ensure-storage-profile` entries in the dependsOn test; set keepalive's expected `DependsOn` to `[{"ContainerName": "migrator", "Condition": "SUCCESS"}]` (~126-127).
- Add:

```python
def test_migrator_does_not_import_ssm(template_dict):
    """Org content is Maestro-owned; the migrator seeds nothing from SSM."""
    migrator = _containers(template_dict)["migrator"]
    env = {e["Name"]: e["Value"] for e in migrator["Environment"]}
    assert "STORAGE_PROFILE_FILE" not in env
    assert "API_KEYS_FILE" not in env
    secrets = {s["Name"] for s in migrator.get("Secrets", [])}
    assert "STORAGE_PROFILES_YAML" not in secrets
    assert "API_KEYS_YAML" not in secrets


def test_no_ensure_storage_profile_container(template_dict):
    assert "ensure-storage-profile" not in _containers(template_dict)
```

- [ ] **Step 2: Run tests, expect failure.**

Run: `.venv/bin/python -m pytest tests/templates/test_migration.py -q`
Expected: FAIL.

- [ ] **Step 3: Remove the four parameters** `StorageProfilesParamName`, `ApiKeysParamName` (lines ~91-104), `OrgId` (~110-123), `IngestBucketName` (~124-130) and update the SSM-seed comment block (~83-90).

- [ ] **Step 4: Strip the migrator container's SSM wiring.** In `migrator_container`, delete the `STORAGE_PROFILE_FILE` and `API_KEYS_FILE` `Environment` entries (~219-220) and the `STORAGE_PROFILES_YAML` / `API_KEYS_YAML` `Secret` entries (~229-240).

- [ ] **Step 5: Delete the `ensure_sp_container` definition** entirely (~245-322).

- [ ] **Step 6: Rewire keepalive.** Change `keepalive_container`'s `DependsOn` to `ContainerName="migrator", Condition="SUCCESS"` (~332), and update its comment (~328-331). In `TaskDefinition.ContainerDefinitions` (~352-357) remove `ensure_sp_container`, leaving `[configdb_init_container, migrator_container, keepalive_container]`.

- [ ] **Step 7: Update the module docstring** (lines 4-30) to describe three containers (configdb-init → migrator → keepalive) and drop the ensure-storage-profile narrative.

- [ ] **Step 8: Run tests + build.**

Run: `.venv/bin/python -m pytest tests/templates/test_migration.py -q && make build`
Expected: PASS; cfn-lint clean.

- [ ] **Step 9: Commit.**

```bash
git add src/cardinal_cfn/children/migration.py tests/templates/test_migration.py
git commit -m "migration: drop SSM import + ensure-storage-profile sidecar (Maestro owns org content)"
```

---

### Task 3: Unwire the removed params from the services root

**Files:**
- Modify: `src/cardinal_cfn/lakerunner_services.py`
- Test: `tests/templates/test_lakerunner_services.py`

- [ ] **Step 1: Update tests (failing first).**

In `tests/templates/test_lakerunner_services.py`:
- Remove `StorageProfilesParamName`, `ApiKeysParamName` from the expected param/forwarding sets (~76-77).
- Change the `IngestBucketName`/migration assertions (~81, ~144) so the Migration child is passed NEITHER `IngestBucketName`, `OrgId`, `StorageProfilesParamName`, nor `ApiKeysParamName`:

```python
def test_migration_child_gets_no_org_content_params(template_dict):
    migration = _migration_child_params(template_dict)  # existing helper or inline
    for gone in ("StorageProfilesParamName", "ApiKeysParamName",
                 "OrgId", "IngestBucketName"):
        assert gone not in migration
```

- Keep the `OrganizationId` test (it still exists on the services root and feeds Maestro) but update any assertion text that says it is "seeded into storage-profiles / api-keys."

- [ ] **Step 2: Run tests, expect failure.**

Run: `.venv/bin/python -m pytest tests/templates/test_lakerunner_services.py -q`
Expected: FAIL.

- [ ] **Step 3: Remove the two infra-setup params.** In `_INFRA_SETUP_PARAMS` (lines ~178-181) delete the `StorageProfilesParamName` and `ApiKeysParamName` tuples.

- [ ] **Step 4: Trim the Migration child params.** In the `migration_stack = _add_child(...)` call (lines ~590-593) delete `"StorageProfilesParamName"`, `"ApiKeysParamName"`, `"OrgId"`, `"IngestBucketName"`.

- [ ] **Step 5: Update `OrganizationId` parameter description** (lines ~376-381) to drop "seeded into storage-profiles / api-keys" — it now reads only: the org Maestro pre-populates and provisions into Lakerunner; must match every satellite's `OrganizationId`.

- [ ] **Step 6: Run tests + build.**

Run: `.venv/bin/python -m pytest tests/templates/test_lakerunner_services.py -q && make build`
Expected: PASS; cfn-lint clean.

- [ ] **Step 7: Commit.**

```bash
git add src/cardinal_cfn/lakerunner_services.py tests/templates/test_lakerunner_services.py
git commit -m "services-root: stop threading SSM-param + OrgId/IngestBucket into migration child"
```

---

### Task 4: Update the infra-base deploy driver

**Files:**
- Modify: `scripts-src/parts/deploy-lakerunner-infra-base.sh`
- Generated: `scripts/deploy-lakerunner-infra-base.sh` (regenerated by `build.sh`)
- Test: `tests/unit/test_deploy_stack_lint.py`

- [ ] **Step 1: Edit the driver part.** In `scripts-src/parts/deploy-lakerunner-infra-base.sh`:
  - Remove the `ORGANIZATION_ID` required-input doc + validation (lines ~33-36, ~73) and the `OrganizationId=$ORGANIZATION_ID` param line (~102).
  - Remove `INITIAL_INGEST_API_KEY` doc + the `InitialIngestApiKey=...` block (~45, ~111-112).
  - Remove `API_KEYS_PARAM_NAME` / `STORAGE_PROFILES_PARAM_NAME` docs + the `ApiKeysParamName=` / `StorageProfilesParamName=` param lines (~52-53, ~122-124).

  Note: `ORGANIZATION_ID` REMAINS a required input on the **services** driver (`deploy-lakerunner-services.sh`) — do not touch that one.

- [ ] **Step 2: Regenerate the scripts.**

Run: `./build.sh` (or `make build` if it assembles scripts; verify which regenerates `scripts/`).
Expected: `scripts/deploy-lakerunner-infra-base.sh` updated, no `OrganizationId`/SSM-param references.

- [ ] **Step 3: Verify.**

Run: `grep -n "ORGANIZATION_ID\|StorageProfilesParamName\|ApiKeysParamName\|InitialIngestApiKey" scripts/deploy-lakerunner-infra-base.sh`
Expected: no matches.
Run: `.venv/bin/python -m pytest tests/unit/test_deploy_stack_lint.py -q`
Expected: PASS.

- [ ] **Step 4: Commit.**

```bash
git add scripts-src/parts/deploy-lakerunner-infra-base.sh scripts/deploy-lakerunner-infra-base.sh
git commit -m "deploy-infra-base: drop ORGANIZATION_ID + org-content SSM inputs"
```

---

### Task 5: Docs — CHANGELOG, CLAUDE.md, full test sweep

**Files:**
- Modify: `CHANGELOG.md`, `CLAUDE.md`
- Test: full suite

- [ ] **Step 1: Add a CHANGELOG entry** (newest first; bump from the current top version). Operator-facing content:
  - infra-base no longer takes `OrganizationId` / `InitialIngestApiKey` and no longer creates the `/cardinal/storage-profiles` + `/cardinal/api-keys` SSM params; the migrator no longer imports them and the `ensure-storage-profile` sidecar is removed. Lakerunner installs admin-key-only.
  - `OrganizationId` is now only on `lakerunner-services` (drives Maestro's `MAESTRO_BOOTSTRAP_ORG_ID`); Maestro provisions the org + storage line + ingest key via `/api/v1/provision`.
  - **Upgrade action:** none for existing installs (configdb already populated; the migrator only seeds empty tables). For the infra-base driver, stop passing `ORGANIZATION_ID` / `INITIAL_INGEST_API_KEY` / `*_PARAM_NAME`. Operators who relied on a deterministic ingest key now create it in the Maestro UI.

- [ ] **Step 2: Fix CLAUDE.md.** The "Repository overview" / data-setup section says `data-setup.sh` creates "the two SSM parameters." Update it: there are no longer org-content SSM parameters; Lakerunner is provisioned admin-key-only and Maestro owns org content. Remove the `storage-profiles`/`api-keys` SSM bullet wherever it appears.

- [ ] **Step 3: Full build + test.**

Run: `make build && make test`
Expected: all PASS; cfn-lint clean.

- [ ] **Step 4: Commit.**

```bash
git add CHANGELOG.md CLAUDE.md
git commit -m "docs: changelog + CLAUDE.md for admin-key-only install / Maestro-owned org content"
```

---

### Task 6 (verification, no code): confirm Maestro provisions post-boot

Not a CFN change — a runtime check the design flagged. After a fresh deploy (or against the existing test account), confirm Maestro's provisioning worker calls `/api/v1/provision` shortly after boot WITHOUT waiting on collector activity, so the org's storage line exists for `otel-raw/` ingestion. If it only fires on activity, open a conductor follow-up to run the shared-install reconcile on a short timer at startup.

- [ ] Capture maestro logs showing `Bootstrap complete` then a `provision_org` job success; confirm `configdb.lrconfig_organization_buckets` has the central bucket row. Record findings in the PR description. (If unreachable in this environment, note it as the one manual post-merge check.)

---

## Self-Review

- **Spec coverage:** retire SSM seeds (Task 1), migrator import (Task 2), `ensure-storage-profile` third writer (Task 2), `OrganizationId` off infra-base (Tasks 1/4) but kept on services→maestro (Task 3), admin-key-only already wired (no task needed — noted), driver reconcile (Task 4), data-setup/CLAUDE.md SSM-ownership reconciliation (Task 5), CHANGELOG (Task 5), post-boot provision verification (Task 6). All covered.
- **Placeholder scan:** none — every step names exact files/lines and the assertions/edits.
- **Type consistency:** parameter/output/container names match across tasks (`StorageProfilesParamName`, `ApiKeysParamName`, `OrgId`, `IngestBucketName`, `ensure-storage-profile`, `OrganizationId`, `ADMIN_INITIAL_API_KEY`).
- Note: line numbers are from the current files and will drift as edits land; locate by symbol, not line.
