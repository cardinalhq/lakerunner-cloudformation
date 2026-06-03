# Maestro as Sole Owner of Lakerunner Org Provisioning — Design

Date: 2026-06-03
Status: Approved

## Goal

A fresh install where the **only** Lakerunner-specific setup step is "an admin
key exists." Everything else — the bootstrap organization, its storage line (the
central ingest bucket, `otel-raw/` prefix), and the org's ingest API key — is
provisioned by Maestro through Lakerunner's admin API
(`POST /api/v1/provision`), the same "normal provisioning" path Maestro uses for
every other org.

This collapses the configuration surface to a single place. Instead of two
writers seeding org content into Lakerunner's `configdb` (CloudFormation via an
SSM → migrator import, *and* Maestro via the admin API), Maestro becomes the
**sole owner** of org/table content. CloudFormation (and `data-setup.sh`) create
only schema-bearing and identity-bearing prerequisites: the database, the
buckets, the queues, and the admin-key secret.

A direct benefit: self-registering collectors "just work." Pubsub
auto-registration refuses to create orgs (it only adopts buckets for an
*existing* org); because Maestro now guarantees the org exists, satellite
collectors that self-register against the bucket succeed.

## Background: why this is doable today

Verified against the live code in `../lakerunner` (Go services) and
`../conductor` (Maestro). The architecture already supports admin-key-only
operation; the work is to stop CloudFormation from also seeding org content.

Lakerunner runtime (`../lakerunner`):

- `POST /api/v1/provision` atomically writes the org row
  (`lrconfig_organizations`), the storage line
  (`lrconfig_bucket_configurations` + `lrconfig_organization_buckets`), and org
  API keys (`lrconfig_organization_api_keys` +
  `lrconfig_organization_api_key_mappings`) in one transaction. Idempotent
  (409 = already provisioned = success).
- Storage profiles and API keys are read from `configdb` **at runtime — never
  from SSM**. The SSM path is a one-time install seed only.
- The admin key validates against `lrconfig_admin_api_keys` **or** an
  `ADMIN_INITIAL_API_KEY` env fallback (constant-time compare). The env fallback
  needs no DB seeding.
- Services (`admin-api`, `query-api`, `process-*`, `pubsub-sqs`, migrator) boot
  healthy with an empty `configdb`. There is no startup validation requiring an
  org or a storage profile to exist. Failures only occur at request time when a
  specific missing profile is queried.
- The migrator seeds `configdb` only when the tables are empty
  (`initializeIfNeededFunc`), and gracefully skips when no config file is
  supplied. An empty seed is a no-op.
- Pubsub auto-registration validates that the org **already exists** before
  adopting a bucket; it does not create orgs.

Conductor / Maestro (`../conductor/packages/maestro`):

- `services/bootstrap.ts` (issue #784) is env-gated on `MAESTRO_BOOTSTRAP_*`.
  On every boot it find-or-creates: the owner user, the org (PK ==
  `MAESTRO_BOOTSTRAP_ORG_ID`), the owner membership, and a `shared_cardinal`
  managed Lakerunner instance with `auto_add_to_all_orgs=true` carrying the
  bucket columns from `MAESTRO_BOOTSTRAP_BUCKET_*`. It is idempotent and adds
  **no** startup dependency on the admin-api being reachable.
- The shared-install reconciler maps each org onto the shared instance and
  enqueues a provision job; `services/lakerunner-provisioning.ts` builds the
  `DesiredBucket[]` from the instance's bucket columns, calls
  `provisionOrganization` (→ `/api/v1/provision`), and `importApiKey` to create
  the org's ingest key. The async worker retries, so admin-api need not be up at
  Maestro boot.

CloudFormation maestro child (`src/cardinal_cfn/children/maestro.py`):

- Already wires the complete `MAESTRO_BOOTSTRAP_*` env: `ORG_ID`
  (`OrganizationId`), `ORG_NAME`, `OWNER_EMAIL` (`DexAdminEmail`),
  `LAKERUNNER_QUERY_API_URL`, `LAKERUNNER_ADMIN_API_URL`,
  `LAKERUNNER_ADMIN_API_KEY` (from the `cardinal-admin-key` secret), and the
  bucket group (`BUCKET_NAME` = central ingest bucket, `BUCKET_REGION`,
  `BUCKET_CLOUD_PROVIDER=aws`, `BUCKET_COLLECTOR_NAME=lakerunner`).

The only reason the desired world does not already hold is that CloudFormation
**also** seeds org content into `configdb` at install time (via
`OrganizationId` + `InitialIngestApiKey` → SSM `StorageProfilesParam` /
`ApiKeysParam` → migrator import). That second writer is what this design
removes.

## Decisions

- **Ownership:** Maestro is the *sole* owner of org content. CloudFormation
  seeds no org, no storage profiles, no org API keys.
- **Admin key:** `admin-api` receives the key via an `ADMIN_INITIAL_API_KEY`
  env, sourced from the existing `cardinal-admin-key` Secrets Manager secret.
  The key is validated via the env fallback and is never persisted to
  `configdb`. Maestro uses the same secret value to authenticate `/provision`.
- **Org-content SSM seeds:** *retired entirely* (removed, not emptied) — the
  `StorageProfilesParam` / `ApiKeysParam` SSM parameters, their seeding inputs,
  IAM read grants, and the migrator import.
- **Provision trigger:** Maestro's existing boot-time `MAESTRO_BOOTSTRAP_*`
  path. No new one-shot job or "set this up" command.
- **`OrganizationId` location:** lives only on the services / maestro side
  (feeding `MAESTRO_BOOTSTRAP_ORG_ID`). Removed from `lakerunner-infra-base`.

## Scope of change (CloudFormation, mostly deletion)

1. **`src/cardinal_cfn/lakerunner_infra_base.py`**
   - Remove the `OrganizationId` and `InitialIngestApiKey` parameters.
   - Remove the `StorageProfilesParam` / `ApiKeysParam` SSM resources, their
     seeding logic (`HasInitialIngestApiKey` condition, the YAML bodies), the
     `*ParamName` parameters, and the `StorageProfilesParamName` /
     `ApiKeysParamName` outputs.
   - Remove the `ssm:GetParameter*` IAM grants tied to those params.
   - Keep the `cardinal-admin-key` secret and continue exporting its ARN.

2. **`src/cardinal_cfn/children/migration.py`**
   - Drop the `STORAGE_PROFILE_FILE` / `API_KEYS_FILE` env, the
     `STORAGE_PROFILES_YAML` / `API_KEYS_YAML` SSM-backed secrets, the
     `*ParamName` parameters, and the `ssm:GetParameter` grant. The migrator
     runs schema-only and seeds nothing (graceful empty-config skip).

3. **`src/cardinal_cfn/children/services_control.py` (admin-api)**
   - Add `ADMIN_INITIAL_API_KEY` as a Secrets-Manager-backed env from the
     `cardinal-admin-key` secret ARN.

4. **Dead-env cleanup (service children)**
   - Remove `StorageProfilesParamName` / `ApiKeysParamName` env from every
     service container that still carries them. Runtime is DB-driven; these are
     inert.

5. **`src/cardinal_cfn/root.py` and `src/cardinal_cfn/lakerunner_services.py`**
   - Stop threading `OrganizationId` and the `*ParamName` outputs from
     `lakerunner-infra-base`. `OrganizationId` remains a services-side parameter
     feeding the maestro child.

6. **`scripts/data-setup.sh`**
   - Stop creating/seeding the org-content SSM parameters. Continue creating the
     `cardinal-admin-key` secret. (Audit for any other org-content seeding it
     performs and remove it.)
   - **Reconcile SSM ownership first:** `CLAUDE.md` describes `data-setup.sh` as
     the creator of the two SSM parameters, while `lakerunner_infra_base.py`
     creates `StorageProfilesParam` / `ApiKeysParam` as CFN resources. The plan
     must determine which path actually creates them in the current deploy (or
     both) and remove the org-content seeding wherever it lives, then update
     `CLAUDE.md` to match.

7. **`CHANGELOG.md` + parameter docs**
   - Operator-facing entry: this changes the **fresh-install** contract.
     Lakerunner installs admin-key-only; Maestro provisions the org. Note no
     upgrade action for existing installs (their `configdb` already holds
     content; the migrator's empty seed is a no-op there).

8. **Conductor / Maestro:** no code changes anticipated. A verification task
   confirms the deployed maestro image carries `bootstrap.ts` (#784) and the
   provisioning worker, and that a fresh deploy provisions the org + central
   bucket so `otel-raw/` ingestion works end to end.

## Resulting flow (the staged sequence)

1. Lakerunner installs with only `ADMIN_INITIAL_API_KEY` set; `configdb` empty.
2. Maestro boots with no orgs.
3. Bootstrap creates the `shared_cardinal` instance (admin key + central
   bucket), then the org, maps the bucket onto it, and the async worker calls
   `/api/v1/provision` → writes the org row + storage line + org ingest key into
   `configdb`.
4. `otel-raw/` ingestion works; self-registering collectors find the org
   present; satellite buckets continue to be adopted via pubsub
   auto-registration (which required the org to exist — now guaranteed).

## Edge cases and non-goals

- **Eventual consistency:** if collector data lands before Maestro's first
  successful provision, pubsub auto-registration rejects and retries until the
  org exists. Acceptable; no ordering guarantee is added.
- **Admin key not in DB:** validated purely via the env fallback. Consistent
  with the single-install posture. Both `admin-api` and Maestro reference the
  same `cardinal-admin-key` secret.
- **The "write bucket":** the central org reads and writes to the same instance
  (default `instance_num`); no `writes_to` redirect is needed for it. Satellite
  raw buckets continue to redirect cooked output via pubsub auto-registration's
  `PubsubAutoRegisterWritesToInstance` — unchanged by this design, and now
  reliable because the org exists.
- **Existing installs:** non-destructive. The migrator only seeds empty tables,
  so a live `configdb` with content is untouched. This design changes the
  fresh-install contract, not running installs.
- **Non-goal:** a separate one-shot provision command/container. Rejected in
  favor of the existing, tested boot-time bootstrap.
