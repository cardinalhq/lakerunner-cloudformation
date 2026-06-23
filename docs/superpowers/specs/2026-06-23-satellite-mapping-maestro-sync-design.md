# Satellite mapping as Maestro-synced JSON — design

Status: draft (brainstormed 2026-06-23)

## Problem

Satellite ingest sources (cross-account/region raw buckets + their SQS queues)
are configured today through a bash "hack" in `scripts/deploy-lakerunner-services.sh`:
flat numbered env groups `QUEUE_URL_<n>` / `QUEUE_REGION_<n>` / `QUEUE_ROLE_ARN_<n>`
(n = 1..10), walked with `eval "q_url=\${QUEUE_URL_$n:-}"` and fanned into the
matching `QueueUrl<n>` CFN params (`src/cardinal_cfn/children/services_process.py`,
`MAX_ADDITIONAL_QUEUES = 10`). Those env vars become `SQS_QUEUE_URL_<n>` on the
`pubsub-sqs` container; the poller consumes each satellite's queue via its own
assume-role.

This surface has three problems:

- The numbered-env convention is opaque and capped at 10.
- It only configures the *queue poll list*. The org→bucket *mapping* (which org
  owns which bucket, where cooked output lands) lives separately in configdb,
  written by Maestro's provisioning worker, and partly auto-created at runtime by
  `pubsub-sqs` autoregister (`PUBSUB_AUTOREGISTER` / `PUBSUB_AUTOREGISTER_WRITES_TO_INSTANCE`).
- There is therefore no single source of truth: configdb is mutated by Maestro,
  by autoregister, and seeded (historically) at install. A satellite is defined
  in two unrelated places (CFN env for the queue, configdb for the mapping).

## Goal

A single operator-declared JSON document is the source of truth for satellite
ingest: per org, the set of collectors (raw inputs + the one writable cooked
destination), each with bucket, SQS queue, region, optional assume-role, and a
`readonly` flag. Maestro owns reconciling that document into configdb. Both the
bucket mapping and the queue poll list derive from it.

## Key facts grounding the design (verified 2026-06-23)

- Maestro is already the **sole writer** of `organization_buckets`. Its in-process
  Lakerunner provisioning worker POSTs a `DesiredBucket[]` to lakerunner's admin
  API (`/api/v1/provision`); `src/cardinal_cfn/children/maestro.py:144-153` calls
  it out as "the sole writer of that row." The migrator no longer seeds storage
  profiles (`migration.py:159`: "org content is owned by Maestro ... not seeded
  here"); configdb tables start empty.
- Maestro's admin client already models `DesiredBucket` (bucket_name,
  collector_name, cloud_provider, region, endpoint, role, use_path_style,
  insecure_tls) and its `provisionOrganization` already accepts `buckets?:
  DesiredBucket[]` — it just carries one entry today
  (`conductor: packages/maestro/src/lib/lakerunner-admin-client.ts`,
  `packages/maestro/src/services/lakerunner-provisioning.ts`).
- The provisioning worker is a long-running reconcile loop with a sync-job queue,
  bounded retry/backoff (`conductor: packages/maestro/src/services/lakerunner-provisioning-worker.ts`).
- There is **no** post-install bucket-mapping editor in Maestro UI. Bucket config
  is declarative: `site-config-builder.ts` parses `bootstrap.bucket`;
  `managed-object-store-defaults.ts` seeds one storage-profile entry. The only
  admin surfaces are a list page (`packages/ui-pages/.../managed-lakerunner-instances`)
  and the install wizard. So making the JSON authoritative removes no editing
  feature — it extends an existing declarative-reconcile path from 1 to N rows.

## Decisions

1. **JSON is the source of truth, continuously reconciled.** Every deploy
   re-applies it; configdb converges to match.
2. **Autoregister is removed.** With every satellite enumerated in the JSON there
   are no "unseen" buckets to auto-register. `PUBSUB_AUTOREGISTER` /
   `PUBSUB_AUTOREGISTER_WRITES_TO_INSTANCE` go away.
3. **Maestro owns the config and the sync.** The deploy layer hands Maestro the
   JSON; Maestro reconciles configdb. This CFN repo gets out of the
   bucket-mapping business.
4. **Operator declares the JSON at deploy time** (not Maestro-managed UI state).
   CFN delivers it to Maestro. A superadmin editing UI may be added later; see
   Decision 9.
5. **`pubsub-sqs` sources its queue poll list from configdb** (written by Maestro),
   not from ECS env. This is the only version where the JSON is genuinely the
   single source. Requires a lakerunner-binary change.
6. **One writable collector per org = the cooked destination = group 0 (primary).**
   Exactly one collector per org has `readonly: false`; all `readonly: true`
   satellites in that org route their cooked output to it. That writable collector
   *is* the primary (group-0) queue + bucket — there is no separate primary-queue
   concept. `pubsub-sqs` treats it as just another configdb-sourced queue entry.
   Reconcile rejects an org with zero or more than one writable collector.
7. **Delivery via SSM `/cardinal/satellites`.** CFN writes the JSON verbatim to
   this SSM parameter and injects it into the Maestro container as an env var,
   reusing the machinery the migrator already uses for `STORAGE_PROFILES_YAML`
   (Maestro's role already has `ssm:GetParameter` on `/cardinal/*`; no new IAM).
   Caveat: SSM advanced tier caps at 8 KB — adequate for tens of satellites. If
   it outgrows that, move the blob to an S3 object Maestro reads. The data is not
   secret (bucket names, queue URLs, role ARNs, regions).
8. **Reconcile is fully declarative, scoped by provenance.** A `managed_by` column
   tags each mapping/queue row with its origin. JSON-sync sets
   `managed_by = external_json_config`. Reconcile upserts declared rows and
   **deletes only rows where `managed_by = external_json_config`** that the JSON no
   longer mentions. Rows from other origins (a future UI) are never touched.
9. **`managed_by` future-proofs a UI.** A later superadmin editor writes rows with
   a different `managed_by` value; the UI surfaces the column (label "External
   JSON Config" for JSON-owned rows) and can mark those read-only. JSON and UI
   coexist without clobbering.

## JSON schema

```json
{
  "organizations": {
    "12340000-0000-4000-8000-000000000000": {
      "collectors": {
        "central": {
          "bucket": "cardinal-ingest-acct1",
          "sqsurl": "https://sqs.us-east-1.amazonaws.com/111.../central",
          "region": "us-east-1"
        },
        "satellite-eu": {
          "readonly": true,
          "bucket": "eu-raw",
          "sqsurl": "https://sqs.eu-west-1.amazonaws.com/222.../eu",
          "region": "eu-west-1",
          "role": "arn:aws:iam::222...:role/cardinal-satellite-eu"
        }
      }
    }
  }
}
```

Per-collector fields:

- `bucket` (required) — bucket name.
- `sqsurl` (required) — the queue `pubsub-sqs` polls for this collector.
- `region` (required) — region for both the bucket and the queue.
- `role` (optional) — assume-role ARN for cross-account access; omit to use the
  task's own credentials.
- `readonly` (optional, default `false`) — `true` marks a raw-only input whose
  cooked output is written to the org's single writable collector.

Collector key = `collector_name`. Validation: each org has exactly one collector
with `readonly` false/absent; org keys are UUIDs; required fields present.

## Data flow

```
deploy-lakerunner-services.sh   (operator sets SATELLITE_CONFIG / SATELLITE_CONFIG_FILE)
   │  validates JSON, writes verbatim to SSM /cardinal/satellites
   ▼
maestro.py   (SSM value injected into the Maestro container as an env var)
   ▼
Maestro provisioning worker   (reads JSON, reconciles)
   │  upsert declared, delete stale managed_by=external_json_config rows
   ▼
configdb   organization_buckets (mapping + readonly + cooked routing + managed_by)
           + queue list (url / region / role + managed_by)
   ▲                                    ▲
   │ query / process read mapping       │ pubsub-sqs reads its poll list
```

## Per-repo work split

This is a cross-repo system change. Boundaries:

### This CFN repo (`lakerunner-cloudformation`)

- `scripts/deploy-lakerunner-services.sh`: add `SATELLITE_CONFIG` /
  `SATELLITE_CONFIG_FILE` input; validate JSON; write to SSM `/cardinal/satellites`.
  **Delete** the numbered `QUEUE_URL_<n>` / `QUEUE_REGION_<n>` / `QUEUE_ROLE_ARN_<n>`
  loop and the `PUBSUB_AUTOREGISTER*` inputs.
- `src/cardinal_cfn/children/services_process.py`: **delete** `QueueUrl<n>` /
  `QueueRegion<n>` / `QueueRoleArn<n>` params, `MAX_ADDITIONAL_QUEUES`, the
  `HasQueue<n>` conditions, and the per-group env emission. The primary
  `QueueUrl` / `QueueRoleArn` (group 0) also goes away — group 0 is the org's
  writable collector in the JSON and is configdb-sourced like every other queue.
  Remove `PUBSUB_AUTOREGISTER*` params.
- `src/cardinal_cfn/children/maestro.py`: widen the bootstrap-bucket input from a
  single bucket to the satellite JSON; inject `/cardinal/satellites` as an env
  var.
- `scripts/data-setup.sh`: create the `/cardinal/satellites` SSM parameter (empty
  default) if it provisions the other `/cardinal/*` params.
- Docs: `scripts/README.md`, `docs/operations/*`, `cardinal-defaults.yaml`.

### Conductor / Maestro repo

- Extend the `DesiredBucket` contract + admin-API payload with `readonly` and the
  cooked-instance routing, plus `managed_by`.
- Read the satellite JSON (from the injected env / SSM) and build the
  `DesiredBucket[]` for each org from it (today it builds one from
  `bootstrap.bucket`).
- Make the provisioning worker's reconcile fully declarative for
  `managed_by = external_json_config` rows: upsert declared, delete stale.
- Write the queue poll list (url/region/role + managed_by) into configdb.
- (Later, out of scope here) superadmin UI editing, surfacing `managed_by`.

### Lakerunner binary repo

- Remove autoregister.
- Make `pubsub-sqs` source its queue poll list from configdb instead of
  `SQS_QUEUE_URL_<n>` env.
- configdb schema: add `managed_by` to the mapping table; add a queue-list table
  (or columns) if not already present; readonly + cooked-instance routing
  columns as needed.

## Error handling

- **Invalid JSON / failed validation** (bad schema, zero or >1 writable collector
  per org): the deploy driver fails before writing SSM, with a specific message.
  Maestro re-validates and refuses to reconcile a malformed document, leaving the
  prior good state in place.
- **Provision API unreachable on fresh install:** the worker already ramps/backs
  off (~29 min budget, `MAX_ATTEMPTS = 35`); unchanged.
- **Reconcile delete safety:** deletes are scoped to
  `managed_by = external_json_config`; UI/other-origin rows can never be removed
  by JSON sync.

## Testing

- CFN: cloud-radar template assertions that the numbered queue params and
  `PUBSUB_AUTOREGISTER*` are gone and the Maestro container receives the
  `/cardinal/satellites` env; unit tests for the driver's JSON validation.
- Maestro: unit tests for JSON→`DesiredBucket[]` mapping, the
  one-writable-per-org rule, and declarative reconcile (upsert + scoped delete).
- Lakerunner: pubsub-sqs sourcing queues from configdb; autoregister removal does
  not regress single-bucket installs.

## Out of scope

- The superadmin editing UI (deferred; `managed_by` reserves the seam).
- Multiple writable cooked destinations per org (single writable collector only).
- Non-AWS cloud providers beyond what `DesiredBucket.cloud_provider` already
  supports.

## Open items for planning

- Exact configdb schema for the queue list and routing columns (owned by the
  lakerunner repo).
- SSM 8 KB ceiling vs S3 delivery trigger point.
