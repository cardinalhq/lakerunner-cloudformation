# Satellite Mapping — Maestro (Conductor) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Maestro reconcile a deploy-supplied satellite-mapping JSON into lakerunner: parse `MAESTRO_SATELLITE_CONFIG`, build a per-org `DesiredBucket[]` (with `mode` / `sqs_queue_url` / `managed_by`), and provision it through the existing admin client on a periodic reconcile tick.

**Architecture:** Maestro already owns lakerunner provisioning via a sync-job worker that POSTs `DesiredBucket[]` to `/api/v1/provision`. Today it sends 0-or-1 bucket from env/instance config. This plan adds a stateless, env-driven satellite config (mirroring the existing `MAESTRO_BOOTSTRAP_BUCKET_*` / `DEFAULT_BUCKET_*` patterns), extends the wire type, and adds a periodic per-org reconcile that sends the full satellite list scoped by `managed_by="external_json_config"`. The lakerunner foundation (separate PR) makes the provision call a `managed_by`-scoped declarative replace and makes empty `api_keys` a no-op, so a bucket-only reconcile neither clobbers other-origin buckets nor wipes ingest keys.

**Tech Stack:** TypeScript, conductor monorepo `packages/maestro`, vitest (threads pool), zod (already a dependency) for JSON validation.

**Repo:** `/Users/mgraff/git/github/cardinalhq/conductor` — work in the existing worktree at `.worktrees/satellite-mapping-maestro` (branch `feat/satellite-mapping-maestro`, off `origin/main`). The main checkouts are in flux; stay in the worktree. Run commands from `packages/maestro`.

## Global Constraints

- The lakerunner provision wire contract (from the foundation PR) for each bucket: snake_case keys `bucket_name`, `collector_name`, `cloud_provider`, `region`, optional `endpoint`, `role`, `use_path_style`, `insecure_tls`, and the NEW `mode` (`normal`|`read-only`|`satellite`), `sqs_queue_url`, `managed_by`.
- `managed_by` for JSON-sourced rows is exactly the string `external_json_config`.
- `mode` default is `normal`; each org must have exactly one `normal` collector (lakerunner enforces it, but Maestro validates early with a clear error).
- The satellite JSON shape (operator-declared, delivered by CFN as the `MAESTRO_SATELLITE_CONFIG` env var — a JSON string):

  ```json
  {
    "organizations": {
      "<org-uuid>": {
        "collectors": {
          "<collector-name>": {
            "bucket": "string",
            "sqsurl": "string",
            "region": "string",
            "role": "string (optional)",
            "mode": "normal|read-only|satellite (optional, default normal)",
            "cloud_provider": "string (optional, default \"aws\")"
          }
        }
      }
    }
  }
  ```
- Reconcile is declarative and idempotent: it sends the full per-org satellite `DesiredBucket[]` and an empty `api_keys` array (a no-op on the lakerunner side). It must NOT enqueue or duplicate other provisioning concerns.
- Follow the existing `buildDesiredBucket` "base keys first in literal order, refinements only when set" rule for byte-stable serialization.
- Tests: vitest, `*.test.ts` under `__tests__/`, threads pool. Run `pnpm --filter @cardinalhq/maestro test:unit` (adjust to the package's actual name/scripts). Stub deps with `vi.fn()`; do not require a real DB.
- No emoji; short commit messages.

---

### Task 1: Extend `DesiredBucket` wire type + `buildDesiredBucket`

**Files:**
- Modify: `packages/maestro/src/lib/lakerunner-admin-client.ts` (`DesiredBucket` interface, lines ~18-27)
- Modify: `packages/maestro/src/services/lakerunner-provisioning.ts` (`buildDesiredBucket`, lines ~81-102)
- Test: `packages/maestro/src/services/__tests__/lakerunner-provisioning.test.ts` (extend existing)

**Interfaces:**
- Produces: `DesiredBucket` gains optional `mode?: "normal" | "read-only" | "satellite"`, `sqs_queue_url?: string`, `managed_by?: string`. `buildDesiredBucket` accepts and forwards them, appended after the existing refinement keys, only when set (mode only when non-empty; managed_by only when non-empty; sqs_queue_url only when non-empty).

- [ ] **Step 1: Write the failing test**

In `lakerunner-provisioning.test.ts`:

```ts
import { describe, it, expect } from "vitest";
// buildDesiredBucket is module-private; if not exported, export it for testing
// or assert via the public path that serializes it. Prefer exporting it.
import { buildDesiredBucket } from "../lakerunner-provisioning.js";

describe("buildDesiredBucket satellite fields", () => {
  it("includes mode, sqs_queue_url, managed_by when set", () => {
    const b = buildDesiredBucket({
      bucket_name: "eu-raw", collector_name: "eu", cloud_provider: "aws", region: "eu-west-1",
      role: "arn:aws:iam::222:role/x",
      mode: "read-only", sqs_queue_url: "https://sqs/eu", managed_by: "external_json_config",
    });
    expect(b).toMatchObject({
      bucket_name: "eu-raw", collector_name: "eu", cloud_provider: "aws", region: "eu-west-1",
      role: "arn:aws:iam::222:role/x",
      mode: "read-only", sqs_queue_url: "https://sqs/eu", managed_by: "external_json_config",
    });
  });

  it("omits satellite fields when unset", () => {
    const b = buildDesiredBucket({
      bucket_name: "c", collector_name: "c", cloud_provider: "aws", region: "r",
    });
    expect("mode" in b).toBe(false);
    expect("sqs_queue_url" in b).toBe(false);
    expect("managed_by" in b).toBe(false);
  });
});
```

- [ ] **Step 2: Run to verify failure**

Run: `pnpm --filter @cardinalhq/maestro test:unit -- lakerunner-provisioning`
Expected: FAIL — `buildDesiredBucket` not exported / fields not forwarded.

- [ ] **Step 3: Extend the interface**

In `lakerunner-admin-client.ts`, add to `DesiredBucket`:

```ts
  mode?: "normal" | "read-only" | "satellite";
  sqs_queue_url?: string;
  managed_by?: string;
```

- [ ] **Step 4: Extend `buildDesiredBucket`**

Add the parameter fields to the input type and append after the existing refinements (export the function if it isn't already):

```ts
  if (b.mode != null && b.mode !== "") out.mode = b.mode;
  if (b.sqs_queue_url != null && b.sqs_queue_url !== "") out.sqs_queue_url = b.sqs_queue_url;
  if (b.managed_by != null && b.managed_by !== "") out.managed_by = b.managed_by;
```

- [ ] **Step 5: Run to verify pass**

Run: `pnpm --filter @cardinalhq/maestro test:unit -- lakerunner-provisioning`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add packages/maestro/src/lib/lakerunner-admin-client.ts packages/maestro/src/services/lakerunner-provisioning.ts packages/maestro/src/services/__tests__/lakerunner-provisioning.test.ts
git commit -m "maestro: DesiredBucket gains mode, sqs_queue_url, managed_by"
```

---

### Task 2: Satellite config — parse, validate, build per-org `DesiredBucket[]`

**Files:**
- Create: `packages/maestro/src/services/satellite-config.ts`
- Test: `packages/maestro/src/services/__tests__/satellite-config.test.ts`

**Interfaces:**
- Consumes: `DesiredBucket` + `buildDesiredBucket` from Task 1.
- Produces:
  - `parseSatelliteConfig(raw: string | undefined): SatelliteConfig | undefined` — `undefined`/empty → `undefined` (feature off). Throws `SatelliteConfigError` (a custom Error subclass) on malformed JSON, schema violation, unknown mode, or an org without exactly one `normal` collector.
  - `type SatelliteConfig = { organizations: Record<string, { collectors: Record<string, Collector> }> }` where `Collector = { bucket: string; sqsurl: string; region: string; role?: string; mode: "normal"|"read-only"|"satellite"; cloud_provider: string }` (mode/cloud_provider defaulted during parse).
  - `buildOrgBuckets(org: { collectors: Record<string, Collector> }): DesiredBucket[]` — maps each collector to a `DesiredBucket` via `buildDesiredBucket`, setting `managed_by: "external_json_config"`, `collector_name` = the map key, `bucket_name` = `bucket`, `sqs_queue_url` = `sqsurl`.

- [ ] **Step 1: Write the failing tests**

Create `satellite-config.test.ts`:

```ts
import { describe, it, expect } from "vitest";
import { parseSatelliteConfig, buildOrgBuckets, SatelliteConfigError } from "../satellite-config.js";

const ORG = "12340000-0000-4000-8000-000000000000";
const valid = JSON.stringify({
  organizations: {
    [ORG]: {
      collectors: {
        central: { bucket: "central", sqsurl: "https://sqs/central", region: "us-east-1" },
        eu: { bucket: "eu-raw", sqsurl: "https://sqs/eu", region: "eu-west-1", role: "arn:x", mode: "read-only" },
      },
    },
  },
});

describe("parseSatelliteConfig", () => {
  it("returns undefined when unset/empty", () => {
    expect(parseSatelliteConfig(undefined)).toBeUndefined();
    expect(parseSatelliteConfig("")).toBeUndefined();
  });

  it("parses a valid config and defaults mode/cloud_provider", () => {
    const cfg = parseSatelliteConfig(valid)!;
    const c = cfg.organizations[ORG].collectors;
    expect(c.central.mode).toBe("normal");
    expect(c.central.cloud_provider).toBe("aws");
    expect(c.eu.mode).toBe("read-only");
  });

  it("throws on malformed JSON", () => {
    expect(() => parseSatelliteConfig("{not json")).toThrow(SatelliteConfigError);
  });

  it("throws on unknown mode", () => {
    const bad = JSON.stringify({ organizations: { [ORG]: { collectors: {
      central: { bucket: "b", sqsurl: "s", region: "r", mode: "weird" } } } } });
    expect(() => parseSatelliteConfig(bad)).toThrow(SatelliteConfigError);
  });

  it("throws when an org has zero normal collectors", () => {
    const bad = JSON.stringify({ organizations: { [ORG]: { collectors: {
      eu: { bucket: "b", sqsurl: "s", region: "r", mode: "read-only" } } } } });
    expect(() => parseSatelliteConfig(bad)).toThrow(/exactly one normal/i);
  });

  it("throws when an org has two normal collectors", () => {
    const bad = JSON.stringify({ organizations: { [ORG]: { collectors: {
      a: { bucket: "a", sqsurl: "s", region: "r" },
      b: { bucket: "b", sqsurl: "s", region: "r" } } } } });
    expect(() => parseSatelliteConfig(bad)).toThrow(/exactly one normal/i);
  });
});

describe("buildOrgBuckets", () => {
  it("maps collectors to DesiredBucket[] with managed_by", () => {
    const cfg = parseSatelliteConfig(valid)!;
    const buckets = buildOrgBuckets(cfg.organizations[ORG]);
    const byCollector = Object.fromEntries(buckets.map((b) => [b.collector_name, b]));
    expect(byCollector.central).toMatchObject({
      bucket_name: "central", sqs_queue_url: "https://sqs/central",
      mode: "normal", managed_by: "external_json_config",
    });
    expect(byCollector.eu).toMatchObject({
      bucket_name: "eu-raw", role: "arn:x", mode: "read-only", managed_by: "external_json_config",
    });
  });
});
```

- [ ] **Step 2: Run to verify failure**

Run: `pnpm --filter @cardinalhq/maestro test:unit -- satellite-config`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement `satellite-config.ts`**

```ts
import { z } from "zod";
import { type DesiredBucket } from "../lib/lakerunner-admin-client.js";
import { buildDesiredBucket } from "./lakerunner-provisioning.js";

export class SatelliteConfigError extends Error {}

const collectorSchema = z.object({
  bucket: z.string().min(1),
  sqsurl: z.string().min(1),
  region: z.string().min(1),
  role: z.string().optional(),
  mode: z.enum(["normal", "read-only", "satellite"]).default("normal"),
  cloud_provider: z.string().min(1).default("aws"),
});

const schema = z.object({
  organizations: z.record(
    z.string().uuid(),
    z.object({ collectors: z.record(z.string().min(1), collectorSchema) }),
  ),
});

export type Collector = z.infer<typeof collectorSchema>;
export type SatelliteConfig = z.infer<typeof schema>;

export function parseSatelliteConfig(raw: string | undefined): SatelliteConfig | undefined {
  if (raw == null || raw.trim() === "") return undefined;
  let json: unknown;
  try {
    json = JSON.parse(raw);
  } catch (err) {
    throw new SatelliteConfigError(`MAESTRO_SATELLITE_CONFIG is not valid JSON: ${String(err)}`);
  }
  const parsed = schema.safeParse(json);
  if (!parsed.success) {
    throw new SatelliteConfigError(`MAESTRO_SATELLITE_CONFIG schema error: ${parsed.error.message}`);
  }
  for (const [orgId, org] of Object.entries(parsed.data.organizations)) {
    const normals = Object.values(org.collectors).filter((c) => c.mode === "normal");
    if (normals.length !== 1) {
      throw new SatelliteConfigError(
        `org ${orgId} must have exactly one normal collector, got ${normals.length}`,
      );
    }
  }
  return parsed.data;
}

export function buildOrgBuckets(org: { collectors: Record<string, Collector> }): DesiredBucket[] {
  return Object.entries(org.collectors).map(([collectorName, c]) =>
    buildDesiredBucket({
      bucket_name: c.bucket,
      collector_name: collectorName,
      cloud_provider: c.cloud_provider,
      region: c.region,
      role: c.role,
      sqs_queue_url: c.sqsurl,
      mode: c.mode,
      managed_by: "external_json_config",
    }),
  );
}
```

- [ ] **Step 4: Run to verify pass**

Run: `pnpm --filter @cardinalhq/maestro test:unit -- satellite-config`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/maestro/src/services/satellite-config.ts packages/maestro/src/services/__tests__/satellite-config.test.ts
git commit -m "maestro: satellite-config parse/validate + per-org DesiredBucket build"
```

---

### Task 3: Inject satellite buckets into provisioning + startup reconcile

**Design (revised after grounding):** Provisioning is per-deployment — `executeProvisionOrg` (in `lakerunner-provisioning.ts`) resolves the org's deployment via `managedInstances.getById(job.deploymentId)`, builds that deployment's admin client, and already calls `provisionOrganization(orgId, orgName, buckets, [])` (empty api_keys; keys are managed separately via `importApiKey`). So we do NOT build a standalone admin client. Instead: (a) inject a pre-built `satelliteBuckets: Record<orgId, DesiredBucket[]>` into the provisioning service and, in `executeProvisionOrg`, use it to REPLACE the instance-derived buckets for any org present in the satellite config (the satellite JSON is authoritative — its single `normal` collector is the org's central bucket, so the legacy instance/bootstrap bucket is suppressed for that org, avoiding a double-`normal`); (b) at startup, parse `MAESTRO_SATELLITE_CONFIG`, pre-build the per-org bucket lists with `buildOrgBuckets`, and call the existing `sharedInstallReconciler.reconcileForOrg(orgId, actor)` for each satellite org so a provision is enqueued on every boot ("every deploy re-applies"). Pre-building in `index.ts` avoids a circular import (`satellite-config.ts` imports `buildDesiredBucket` from `lakerunner-provisioning.ts`; the service must not import back from `satellite-config.ts`).

**Files:**
- Modify: `packages/maestro/src/services/lakerunner-provisioning.ts` (`LakerunnerProvisioningDeps` — add `satelliteBuckets?`; `executeProvisionOrg` bucket-building branch, ~lines 360-455)
- Modify: `packages/maestro/src/index.ts` (parse env near `defaultBucketConfig` ~line 328; pre-build buckets; pass dep into the provisioning service; enqueue `reconcileForOrg` per satellite org near the existing startup reconcile ~line 2348)
- Test: `packages/maestro/src/services/__tests__/lakerunner-provisioning.test.ts` (extend)

**Interfaces:**
- Consumes: `buildOrgBuckets` + `SatelliteConfig` (Task 2); `DesiredBucket` (Task 1); the existing `LakerunnerSharedInstallReconciler.reconcileForOrg(orgId, actor)`.
- Produces: `LakerunnerProvisioningDeps` gains optional `satelliteBuckets?: Record<string, DesiredBucket[]>` (orgId → pre-built buckets). When `satelliteBuckets[job.orgId]` is set, `executeProvisionOrg` sends exactly those buckets (and the existing empty-api_keys call is unchanged); when absent, the existing instance-column logic runs untouched.

- [ ] **Step 1: Write the failing test**

In `lakerunner-provisioning.test.ts`, add a describe block that constructs the provisioning service with a stub `managedInstances.getById` returning a shared deployment (with `adminApiUrl`/`adminApiKey`), a captured `provisionOrganization` (via mocking `createLakerunnerAdminClient`, mirroring the existing test's admin-client mock), and `satelliteBuckets: { [ORG]: [ {bucket_name:"central", collector_name:"central", cloud_provider:"aws", region:"us-east-1", mode:"normal", managed_by:"external_json_config"}, {bucket_name:"eu", collector_name:"eu", cloud_provider:"aws", region:"eu-west-1", mode:"read-only", sqs_queue_url:"https://sqs/eu", managed_by:"external_json_config"} ] }`. Drive `executeProvisionOrg` for a `provision_org` job with `orgId === ORG` and assert:

```ts
const [, , buckets, apiKeys] = provisionOrganization.mock.calls[0];
expect(apiKeys).toEqual([]);
expect(buckets.map((b: any) => b.collector_name).sort()).toEqual(["central", "eu"]);
expect(buckets.every((b: any) => b.managed_by === "external_json_config")).toBe(true);
// exactly one normal in the payload
expect(buckets.filter((b: any) => (b.mode ?? "normal") === "normal")).toHaveLength(1);
```

Add a second test: with the SAME service but a job for an org NOT in `satelliteBuckets`, assert the instance-column bucket path is used (the bucket reflects the deployment's `bucketName`, not the satellite list) — i.e. satellite injection does not affect non-satellite orgs.

(Match the existing test file's mocking of `createLakerunnerAdminClient` / `managedInstances`; if the existing suite already has a harness for `executeProvisionOrg`, reuse it.)

- [ ] **Step 2: Run to verify failure**

Run: `pnpm --filter @cardinalhq/maestro test:unit -- lakerunner-provisioning`
Expected: FAIL — `satelliteBuckets` not in deps / not consumed.

- [ ] **Step 3: Add the dep + branch**

In `lakerunner-provisioning.ts`, add to `LakerunnerProvisioningDeps`:

```ts
  satelliteBuckets?: Record<string, import("../lib/lakerunner-admin-client.js").DesiredBucket[]>;
```

In `executeProvisionOrg`, replace the bucket-building section's start so the satellite list wins when present:

```ts
const satellite = this.deps.satelliteBuckets?.[job.orgId];
let buckets: DesiredBucket[];
if (satellite && satellite.length > 0) {
  buckets = satellite; // satellite JSON authoritative: suppress instance/bootstrap bucket
} else {
  buckets = [];
  // ... existing instance-column / defaultBucket logic, unchanged ...
}
```

Keep the existing `provisionOrganization(job.orgId, orgName, buckets, [])` call and the existing "shared instance with zero buckets throws" guard (satellite lists are non-empty, so satellite orgs pass it).

- [ ] **Step 4: Run to verify pass**

Run: `pnpm --filter @cardinalhq/maestro test:unit -- lakerunner-provisioning`
Expected: PASS.

- [ ] **Step 5: Startup wiring in `index.ts`**

Near `defaultBucketConfig` (~line 328), parse + pre-build (fail-safe — a bad config disables the feature, never crashes):

```ts
import { parseSatelliteConfig } from "./services/satellite-config.js";
import { buildOrgBuckets } from "./services/satellite-config.js";

let satelliteBuckets: Record<string, import("./lib/lakerunner-admin-client.js").DesiredBucket[]> | undefined;
try {
  const satCfg = parseSatelliteConfig(process.env.MAESTRO_SATELLITE_CONFIG);
  if (satCfg) {
    satelliteBuckets = Object.fromEntries(
      Object.entries(satCfg.organizations).map(([orgId, org]) => [orgId, buildOrgBuckets(org)]),
    );
  }
} catch (err) {
  logger.error("Invalid MAESTRO_SATELLITE_CONFIG; satellite mappings disabled", { error: String(err) });
  satelliteBuckets = undefined;
}
```

Pass `satelliteBuckets` into the provisioning service deps (wherever `LakerunnerProvisioningService` / the worker is constructed — it already receives `defaultBucket`). Then, after the existing startup bootstrap reconcile (~line 2348), enqueue an initial reconcile per satellite org so boot re-applies the config:

```ts
if (satelliteBuckets) {
  for (const orgId of Object.keys(satelliteBuckets)) {
    try {
      await sharedInstallReconciler.reconcileForOrg(orgId, {
        userId: orgId, role: "system", reason: "satellite_config_reconcile",
      });
    } catch (err) {
      logger.error("Satellite startup reconcile failed for org", { orgId, error: String(err) });
    }
  }
}
```

(Confirm the actor shape from the existing `reconcileForOrg` call at index.ts ~2348 and match it.)

- [ ] **Step 6: Typecheck + unit suite**

Run: `pnpm --filter @cardinalhq/maestro typecheck` and `pnpm --filter @cardinalhq/maestro test:unit`
Expected: no new type errors; tests pass.

- [ ] **Step 7: Commit**

```bash
git add packages/maestro/src/services/lakerunner-provisioning.ts packages/maestro/src/index.ts packages/maestro/src/services/__tests__/lakerunner-provisioning.test.ts
git commit -m "maestro: provision satellite buckets per org + startup reconcile"
```

> **Note — periodic drift reconcile is deferred.** Startup re-enqueues on every boot (a config change ships via a redeploy that restarts Maestro), and satellites are re-sent on every subsequent `provision_org` for the org. A periodic in-process tick (mirroring the alerting-reconcile cron) is a future enhancement, out of scope here.

## Self-Review

**Spec coverage:**
- "Extend DesiredBucket + admin payload with mode/sqs_queue_url/managed_by" → Task 1.
- "Read the satellite JSON and build per-org DesiredBucket[]" → Task 2.
- "Declarative reconcile, managed_by=external_json_config, empty api_keys" → Task 3: `executeProvisionOrg` sends the per-org satellite buckets (all `external_json_config`) with empty api_keys, relying on the foundation's managed_by-scoped replace + empty-api_keys no-op.
- "Deploy-supplied (env), Maestro syncs" → Task 3 startup wiring (`MAESTRO_SATELLITE_CONFIG` parsed + pre-built + `reconcileForOrg` per org).

**Decisions baked in (note for reviewers):**
- Stateless env-driven (no new Maestro DB table) — matches the existing `MAESTRO_BOOTSTRAP_BUCKET_*` / `DEFAULT_BUCKET_*` pattern and the spec's "configdb is the source of truth, Maestro is the sync engine."
- zod for JSON validation (already a dependency; nested shape benefits from it) rather than the hand-rolled `bootstrap.ts` style.
- `cloud_provider` is an optional per-collector field defaulting to `"aws"` (the satellite JSON spec is AWS-centric; lakerunner's `DesiredBucket` requires it).
- Bad `MAESTRO_SATELLITE_CONFIG` disables the feature with a logged error rather than crashing Maestro (fail-safe).
- **Satellite JSON is authoritative per org:** for any org in the config, `executeProvisionOrg` sends ONLY the satellite buckets (incl. the single `normal`/central), suppressing the legacy instance/bootstrap bucket — so there is never a double-`normal`. Reuses the per-deployment admin client `executeProvisionOrg` already builds; no standalone client.
- Reconcile is triggered via the existing `sharedInstallReconciler.reconcileForOrg` (enqueues `provision_org`); periodic drift reconcile is deferred (startup + every subsequent provision covers the deploy-driven case).

**Cross-repo dependency:** requires the lakerunner foundation PR (managed_by-scoped replace + empty-api_keys no-op) to be merged/available in the lakerunner image Maestro provisions against. Until then the reconcile would clobber other-origin rows / keys.

## Handoff to the CFN plan

CFN must deliver the satellite JSON to the Maestro container as the env var `MAESTRO_SATELLITE_CONFIG` (a JSON string, the document in Global Constraints), sourced from SSM `/cardinal/satellites`. Because the satellite JSON is authoritative per org (its `normal` collector IS the central bucket), CFN should express the central bucket as the JSON's `normal` collector for satellite-managed orgs rather than relying on `MAESTRO_BOOTSTRAP_BUCKET_*` for them.
