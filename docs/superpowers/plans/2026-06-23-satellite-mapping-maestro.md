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

### Task 3: Reconcile tick + worker/startup wiring

**Files:**
- Create: `packages/maestro/src/services/satellite-reconcile.ts`
- Modify: `packages/maestro/src/services/lakerunner-provisioning-worker.ts` (add the tick to the poll loop, mirroring the alerting-reconcile cron at ~lines 129-157; add deps)
- Modify: `packages/maestro/src/index.ts` (parse `MAESTRO_SATELLITE_CONFIG`, pass into the worker deps, ~lines 327-340 + 2044-2058)
- Test: `packages/maestro/src/services/__tests__/satellite-reconcile.test.ts`

**Interfaces:**
- Consumes: `SatelliteConfig`, `buildOrgBuckets` (Task 2); the admin client's `provisionOrganization(orgId, orgName, buckets, apiKeys)` (Task 1).
- Produces: `runSatelliteReconcileTick(deps: { config: SatelliteConfig; orgs: OrgNameResolver; adminClient: LakerunnerAdminClient; logger: Logger }): Promise<void>` where `OrgNameResolver` is the minimal interface `{ findById(orgId: string): Promise<{ name: string } | null> }`. For each org in `config.organizations`: resolve the name (skip + warn if not found), build `buildOrgBuckets`, and call `adminClient.provisionOrganization(orgId, name, buckets, [])`. Errors per org are logged and do not abort the other orgs.

- [ ] **Step 1: Write the failing test**

Create `satellite-reconcile.test.ts`:

```ts
import { describe, it, expect, vi } from "vitest";
import { runSatelliteReconcileTick } from "../satellite-reconcile.js";
import { parseSatelliteConfig } from "../satellite-config.js";

const ORG = "12340000-0000-4000-8000-000000000000";
const cfg = parseSatelliteConfig(JSON.stringify({
  organizations: { [ORG]: { collectors: {
    central: { bucket: "central", sqsurl: "https://sqs/central", region: "us-east-1" },
    eu: { bucket: "eu-raw", sqsurl: "https://sqs/eu", region: "eu-west-1", mode: "read-only" },
  } } },
}))!;

const logger = { info: vi.fn(), warn: vi.fn(), error: vi.fn() };

describe("runSatelliteReconcileTick", () => {
  it("provisions each org's full satellite list with empty api_keys", async () => {
    const provisionOrganization = vi.fn().mockResolvedValue(undefined);
    const orgs = { findById: vi.fn().mockResolvedValue({ name: "Acme" }) };
    await runSatelliteReconcileTick({ config: cfg, orgs, adminClient: { provisionOrganization } as any, logger } as any);

    expect(provisionOrganization).toHaveBeenCalledTimes(1);
    const [orgId, orgName, buckets, apiKeys] = provisionOrganization.mock.calls[0];
    expect(orgId).toBe(ORG);
    expect(orgName).toBe("Acme");
    expect(apiKeys).toEqual([]);
    expect(buckets.map((b: any) => b.collector_name).sort()).toEqual(["central", "eu"]);
    expect(buckets.every((b: any) => b.managed_by === "external_json_config")).toBe(true);
  });

  it("skips an org that does not exist in Maestro and continues", async () => {
    const provisionOrganization = vi.fn().mockResolvedValue(undefined);
    const orgs = { findById: vi.fn().mockResolvedValue(null) };
    await runSatelliteReconcileTick({ config: cfg, orgs, adminClient: { provisionOrganization } as any, logger } as any);
    expect(provisionOrganization).not.toHaveBeenCalled();
    expect(logger.warn).toHaveBeenCalled();
  });

  it("isolates a per-org failure", async () => {
    const provisionOrganization = vi.fn().mockRejectedValue(new Error("boom"));
    const orgs = { findById: vi.fn().mockResolvedValue({ name: "Acme" }) };
    await expect(
      runSatelliteReconcileTick({ config: cfg, orgs, adminClient: { provisionOrganization } as any, logger } as any),
    ).resolves.toBeUndefined();
    expect(logger.error).toHaveBeenCalled();
  });
});
```

- [ ] **Step 2: Run to verify failure**

Run: `pnpm --filter @cardinalhq/maestro test:unit -- satellite-reconcile`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement `satellite-reconcile.ts`**

```ts
import { type SatelliteConfig, buildOrgBuckets } from "./satellite-config.js";
import { type LakerunnerAdminClient } from "../lib/lakerunner-admin-client.js";

export interface OrgNameResolver {
  findById(orgId: string): Promise<{ name: string } | null>;
}

export interface SatelliteReconcileDeps {
  config: SatelliteConfig;
  orgs: OrgNameResolver;
  adminClient: LakerunnerAdminClient;
  logger: { info: (m: string, x?: unknown) => void; warn: (m: string, x?: unknown) => void; error: (m: string, x?: unknown) => void };
}

export async function runSatelliteReconcileTick(deps: SatelliteReconcileDeps): Promise<void> {
  for (const [orgId, org] of Object.entries(deps.config.organizations)) {
    try {
      const record = await deps.orgs.findById(orgId);
      if (record == null) {
        deps.logger.warn("satellite reconcile: org not found in Maestro; skipping", { orgId });
        continue;
      }
      const buckets = buildOrgBuckets(org);
      await deps.adminClient.provisionOrganization(orgId, record.name, buckets, []);
      deps.logger.info("satellite reconcile: provisioned org", { orgId, buckets: buckets.length });
    } catch (err) {
      deps.logger.error("satellite reconcile: org failed", { orgId, error: String(err) });
    }
  }
}
```

- [ ] **Step 4: Run to verify pass**

Run: `pnpm --filter @cardinalhq/maestro test:unit -- satellite-reconcile`
Expected: PASS.

- [ ] **Step 5: Wire the tick into the worker**

In `lakerunner-provisioning-worker.ts`, mirror the alerting-reconcile cron (the `lastAlertingReconcileAt` / interval pattern, ~lines 129-157). Add to the worker deps an optional `satelliteConfig?: SatelliteConfig` and the `orgs` resolver + `adminClient` (the worker already constructs/holds an admin client for provisioning — reuse it). Add a `lastSatelliteReconcileAt` and an interval from `LAKERUNNER_SATELLITE_RECONCILE_INTERVAL_MS` (default `300_000` = 5 min). On each poll tick, when `satelliteConfig` is set and the interval has elapsed (and on the first tick), call `runSatelliteReconcileTick({...})`; on error, log and do not advance the timestamp (retry next tick). Skip entirely when `satelliteConfig` is undefined.

- [ ] **Step 6: Wire startup in `index.ts`**

Near the `DEFAULT_BUCKET_*` parsing (~lines 327-340), add:

```ts
import { parseSatelliteConfig } from "./services/satellite-config.js";

let satelliteConfig;
try {
  satelliteConfig = parseSatelliteConfig(process.env.MAESTRO_SATELLITE_CONFIG);
} catch (err) {
  logger.error("Invalid MAESTRO_SATELLITE_CONFIG; satellite reconcile disabled", { error: String(err) });
  satelliteConfig = undefined; // fail safe: bad config disables the feature, does not crash Maestro
}
```

Pass `satelliteConfig` (and the `orgs` repo, if not already in worker deps) into `startLakerunnerProvisioningWorker({ ... })` (~lines 2044-2058).

- [ ] **Step 7: Run the package's unit suite + typecheck**

Run: `pnpm --filter @cardinalhq/maestro test:unit` and `pnpm --filter @cardinalhq/maestro typecheck` (or the repo's `tsc -b` / lint script).
Expected: PASS, no type errors.

- [ ] **Step 8: Commit**

```bash
git add packages/maestro/src/services/satellite-reconcile.ts packages/maestro/src/services/lakerunner-provisioning-worker.ts packages/maestro/src/index.ts packages/maestro/src/services/__tests__/satellite-reconcile.test.ts
git commit -m "maestro: periodic satellite reconcile tick + startup wiring"
```

---

## Self-Review

**Spec coverage:**
- "Extend DesiredBucket + admin payload with mode/sqs_queue_url/managed_by" → Task 1.
- "Read the satellite JSON and build per-org DesiredBucket[]" → Task 2.
- "Declarative reconcile, managed_by=external_json_config, empty api_keys" → Task 3 (relies on the foundation's managed_by-scoped replace + empty-api_keys no-op).
- "Deploy-supplied (env), Maestro syncs" → Task 3 startup wiring (`MAESTRO_SATELLITE_CONFIG`).

**Decisions baked in (note for reviewers):**
- Stateless env-driven (no new Maestro DB table) — matches the existing `MAESTRO_BOOTSTRAP_BUCKET_*` / `DEFAULT_BUCKET_*` pattern and the spec's "configdb is the source of truth, Maestro is the sync engine."
- zod for JSON validation (already a dependency; nested shape benefits from it) rather than the hand-rolled `bootstrap.ts` style.
- `cloud_provider` is an optional per-collector field defaulting to `"aws"` (the satellite JSON spec is AWS-centric; lakerunner's `DesiredBucket` requires it).
- Bad `MAESTRO_SATELLITE_CONFIG` disables the feature with a logged error rather than crashing Maestro (fail-safe).

**Cross-repo dependency:** requires the lakerunner foundation PR (managed_by-scoped replace + empty-api_keys no-op) to be merged/available in the lakerunner image Maestro provisions against. Until then the reconcile would clobber other-origin rows / keys.

## Handoff to the CFN plan

CFN must deliver the satellite JSON to the Maestro container as the env var `MAESTRO_SATELLITE_CONFIG` (a JSON string, the document in Global Constraints), sourced from SSM `/cardinal/satellites`. The reconcile interval override is `LAKERUNNER_SATELLITE_RECONCILE_INTERVAL_MS` (default 5 min).
