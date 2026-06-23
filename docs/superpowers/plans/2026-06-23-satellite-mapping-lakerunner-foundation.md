# Satellite Mapping — Lakerunner Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the lakerunner binary the contract foundation for Maestro-synced satellite mappings: extend the `/api/v1/provision` contract + configdb schema with collector `mode`, an SQS queue URL, and `managed_by` provenance; make `pubsub-sqs` source its poll list from configdb; and remove the autoregister feature.

**Architecture:** configdb already models cooked routing (`organization_buckets.writes_to_instance_num`) and source deletion (`bucket_configurations.delete_sources`). This plan adds `bucket_configurations.sqs_queue_url` and `organization_buckets.managed_by`, then teaches the provision endpoint to translate a per-collector `mode` (`normal` / `read-only` / `satellite`) into those columns, scope its declarative replace by `managed_by`, and store the queue URL. `pubsub-sqs` stops reading numbered `SQS_*` env groups and instead queries configdb. Autoregister (which guessed mappings at runtime) is deleted.

**Tech Stack:** Go, pgx/v5 + sqlc, golang-migrate (embedded configdb migrations), testcontainers-based integration tests (`//go:build integration` + `testhelpers.StartPostgres()`).

**Repo:** `/Users/mgraff/git/github/cardinalhq/lakerunner` — work in a dedicated git worktree (per project rule for the lakerunner repo). All paths below are relative to that repo root.

## Global Constraints

- configdb migrations: create with `make new-configdb-migration name=<desc>`; files land at `configdb/migrations/<unix_ts>_<desc>.{up,down}.sql`; they are embedded (`configdb/migrations/embed.go`) and validated at startup. Always author the matching `.down.sql`.
- After any schema/query change run `make generate` (`go generate ./...`) to regenerate `configdb/models.go` and `configdb/*.sql.go` from `configdb/queries/*.sql` via sqlc (`configdb/sqlc.yaml`).
- Unit tests: `*_test.go` co-located; run `make test-only`. Integration tests: `//go:build integration` tag + `testhelpers.StartPostgres()`; run `make test-integration` (Docker required). Full gate: `make test`.
- `mode` enum values are exactly `normal`, `read-only`, `satellite`. Empty/missing means `normal`.
- `managed_by` default for legacy/omitted is `unmanaged`; the JSON-sync path uses `external_json_config`. The declarative replace deletes stale rows only for the `managed_by` values present in the request payload.
- Mode → column mapping (server-derived, never sent by client): `normal` = `delete_sources=true`, `writes_to_instance_num=NULL`; `read-only` = `delete_sources=false`, `writes_to_instance_num=<normal collector's instance_num>`; `satellite` = `delete_sources=true`, `writes_to_instance_num=<normal collector's instance_num>`.
- Exactly one `normal` collector per org per provision request; reject otherwise.
- Do not add advertisements/emoji; short commit messages.

---

### Task 1: configdb migration — add `sqs_queue_url` and `managed_by`

**Files:**
- Create: `configdb/migrations/<ts>_add_satellite_mapping_columns.up.sql`
- Create: `configdb/migrations/<ts>_add_satellite_mapping_columns.down.sql`
- Regenerate: `configdb/models.go` (via sqlc; do not hand-edit)
- Test: `configdb/migration_satellite_test.go` (new, integration)

**Interfaces:**
- Produces: column `bucket_configurations.sqs_queue_url TEXT` (nullable); column `organization_buckets.managed_by TEXT NOT NULL DEFAULT 'unmanaged'`. sqlc regenerates `BucketConfiguration.SqsQueueUrl pgtype.Text` and `OrganizationBucket.ManagedBy string` on the generated models.

- [ ] **Step 1: Create the migration files**

Run:

```bash
make new-configdb-migration name=add_satellite_mapping_columns
```

- [ ] **Step 2: Write the up migration**

Edit the generated `.up.sql`:

```sql
ALTER TABLE bucket_configurations
    ADD COLUMN sqs_queue_url TEXT;

ALTER TABLE organization_buckets
    ADD COLUMN managed_by TEXT NOT NULL DEFAULT 'unmanaged';

CREATE INDEX IF NOT EXISTS organization_buckets_managed_by_idx
    ON organization_buckets (organization_id, managed_by);
```

- [ ] **Step 3: Write the down migration**

Edit the generated `.down.sql`:

```sql
DROP INDEX IF EXISTS organization_buckets_managed_by_idx;
ALTER TABLE organization_buckets DROP COLUMN managed_by;
ALTER TABLE bucket_configurations DROP COLUMN sqs_queue_url;
```

- [ ] **Step 4: Write the failing integration test**

Create `configdb/migration_satellite_test.go`:

```go
//go:build integration

package configdb

import (
	"context"
	"testing"
)

func TestSatelliteColumnsExist(t *testing.T) {
	ctx := context.Background()
	pool := pgc.Pool(t) // testhelpers pattern used by other configdb integration tests

	var n int
	err := pool.QueryRow(ctx, `
		SELECT count(*) FROM information_schema.columns
		WHERE (table_name = 'bucket_configurations' AND column_name = 'sqs_queue_url')
		   OR (table_name = 'organization_buckets'  AND column_name = 'managed_by')`).Scan(&n)
	if err != nil {
		t.Fatalf("query: %v", err)
	}
	if n != 2 {
		t.Fatalf("expected 2 new columns present, got %d", n)
	}
}
```

(Match the exact pool/helper accessor used in `configdb/store_integration_test.go`; adapt `pgc.Pool(t)` to the established helper if it differs.)

- [ ] **Step 5: Run it to verify it fails**

Run: `make test-integration` (or `go test -tags integration ./configdb/ -run TestSatelliteColumnsExist -v`)
Expected: FAIL — columns do not exist yet because models/migrations not regenerated/applied.

- [ ] **Step 6: Regenerate and apply**

Run:

```bash
make generate
```

Expected: `configdb/models.go` now shows `SqsQueueUrl` on `BucketConfiguration` and `ManagedBy` on `OrganizationBucket`.

- [ ] **Step 7: Run the test to verify it passes**

Run: `go test -tags integration ./configdb/ -run TestSatelliteColumnsExist -v`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add configdb/migrations configdb/models.go configdb/migration_satellite_test.go
git commit -m "configdb: add sqs_queue_url and managed_by columns"
```

---

### Task 2: configdb store — mode-derived routing, managed_by-scoped replace, sqs_queue_url

**Files:**
- Modify: `configdb/queries/bucket_management.sql` (upsert + delete-stale + list-queues queries)
- Modify: `configdb/store_provision.go` (`ProvisionBucketParams` ~lines 30-41; `ProvisionOrganization`)
- Regenerate: `configdb/bucket_management.sql.go` (sqlc)
- Test: `configdb/store_provision_satellite_test.go` (new, integration)

**Interfaces:**
- Consumes: the columns from Task 1.
- Produces:
  - `ProvisionBucketParams` gains fields: `SQSQueueURL *string`, `ManagedBy string`, `DeleteSources bool`, `IsNormal bool`. (`InstanceNum` stays server-assigned.)
  - `ProvisionOrganization(ctx, ProvisionOrgParams)` semantics: within a transaction, upsert each incoming bucket_configuration (including `sqs_queue_url`, `delete_sources`); assign instance_nums per collector as today; set `writes_to_instance_num` of every non-`IsNormal` bucket to the `IsNormal` bucket's instance_num; set `organization_buckets.managed_by`; then delete `organization_buckets` rows for this org whose `managed_by` ∈ {distinct ManagedBy of incoming buckets} and whose `collector_name` ∉ incoming collectors.
  - New query method `ListSatelliteQueues(ctx)` → `[]SatelliteQueueRow{ OrganizationID uuid.UUID; CollectorName string; QueueURL string; Region string; Role pgtype.Text }` (used by Task 4/5).

- [ ] **Step 1: Write the failing integration test**

Create `configdb/store_provision_satellite_test.go`:

```go
//go:build integration

package configdb

import (
	"context"
	"testing"

	"github.com/google/uuid"
)

func TestProvisionModeRoutingAndManagedBy(t *testing.T) {
	ctx := context.Background()
	store := newTestStore(t) // mirror the constructor used in store_integration_test.go
	org := uuid.New()

	url1, url2 := "https://sqs/central", "https://sqs/eu"
	params := ProvisionOrgParams{
		OrganizationID: org,
		Name:           "Acme",
		Enabled:        true,
		Buckets: []ProvisionBucketParams{
			{BucketName: "central", CollectorName: "central", CloudProvider: "aws", Region: "us-east-1",
				SQSQueueURL: &url1, ManagedBy: "external_json_config", DeleteSources: true, IsNormal: true},
			{BucketName: "eu-raw", CollectorName: "eu", CloudProvider: "aws", Region: "eu-west-1",
				SQSQueueURL: &url2, ManagedBy: "external_json_config", DeleteSources: false, IsNormal: false},
		},
	}
	if _, err := store.ProvisionOrganization(ctx, params); err != nil {
		t.Fatalf("provision: %v", err)
	}

	// The normal collector writes to self (NULL); the read-only one writes to the normal instance_num.
	rows := store.queryOrgBuckets(t, ctx, org) // small test helper: collector_name -> {writesTo *int16, managedBy string}
	central, eu := rows["central"], rows["eu"]
	if central.WritesTo != nil {
		t.Fatalf("normal collector should write to self (NULL), got %v", *central.WritesTo)
	}
	if eu.WritesTo == nil || *eu.WritesTo != central.InstanceNum {
		t.Fatalf("read-only collector should write to normal instance_num %d, got %v", central.InstanceNum, eu.WritesTo)
	}
	if eu.ManagedBy != "external_json_config" {
		t.Fatalf("managed_by not stored: %q", eu.ManagedBy)
	}
}

func TestProvisionReplaceScopedByManagedBy(t *testing.T) {
	ctx := context.Background()
	store := newTestStore(t)
	org := uuid.New()

	// Seed a UI-owned row directly (managed_by='ui').
	store.seedOrgBucket(t, ctx, org, "manual", "manual-bucket", "ui")

	// Provision a JSON-owned set that does NOT mention "manual".
	url := "https://sqs/central"
	_, err := store.ProvisionOrganization(ctx, ProvisionOrgParams{
		OrganizationID: org, Name: "Acme", Enabled: true,
		Buckets: []ProvisionBucketParams{
			{BucketName: "central", CollectorName: "central", CloudProvider: "aws", Region: "us-east-1",
				SQSQueueURL: &url, ManagedBy: "external_json_config", DeleteSources: true, IsNormal: true},
		},
	})
	if err != nil {
		t.Fatalf("provision: %v", err)
	}

	rows := store.queryOrgBuckets(t, ctx, org)
	if _, ok := rows["manual"]; !ok {
		t.Fatalf("ui-owned row must survive a JSON-scoped replace")
	}
	if _, ok := rows["central"]; !ok {
		t.Fatalf("json-owned row must be present")
	}
}
```

(Implement the small test helpers `newTestStore`, `queryOrgBuckets`, `seedOrgBucket` next to the test, reusing the pool from `testmain_test.go`.)

- [ ] **Step 2: Run to verify it fails**

Run: `go test -tags integration ./configdb/ -run 'TestProvisionMode|TestProvisionReplace' -v`
Expected: FAIL — `ProvisionBucketParams` lacks the new fields / routing+scoping not implemented.

- [ ] **Step 3: Extend `ProvisionBucketParams`**

In `configdb/store_provision.go`, add to the struct (keep existing fields):

```go
type ProvisionBucketParams struct {
	BucketName    string
	CollectorName string
	CloudProvider string
	Region        string
	Endpoint      *string
	Role          *string
	UsePathStyle  bool
	InsecureTLS   bool
	// satellite-mapping additions
	SQSQueueURL   *string
	ManagedBy     string
	DeleteSources bool
	IsNormal      bool
}
```

- [ ] **Step 4: Add/extend sqlc queries**

In `configdb/queries/bucket_management.sql` add (and adjust the existing upserts to carry the new columns):

```sql
-- name: UpsertBucketConfigurationWithQueue :one
INSERT INTO bucket_configurations
    (id, bucket_name, cloud_provider, region, endpoint, role, use_path_style, insecure_tls, delete_sources, sqs_queue_url)
VALUES (gen_random_uuid(), @bucket_name, @cloud_provider, @region, @endpoint, @role, @use_path_style, @insecure_tls, @delete_sources, @sqs_queue_url)
ON CONFLICT (bucket_name) DO UPDATE SET
    cloud_provider = EXCLUDED.cloud_provider,
    region         = EXCLUDED.region,
    endpoint       = EXCLUDED.endpoint,
    role           = EXCLUDED.role,
    use_path_style = EXCLUDED.use_path_style,
    insecure_tls   = EXCLUDED.insecure_tls,
    delete_sources = EXCLUDED.delete_sources,
    sqs_queue_url  = EXCLUDED.sqs_queue_url
RETURNING id;

-- name: SetOrganizationBucketWritesTo :exec
UPDATE organization_buckets
SET writes_to_instance_num = @writes_to_instance_num
WHERE organization_id = @organization_id AND collector_name = @collector_name;

-- name: DeleteStaleManagedOrgBuckets :exec
DELETE FROM organization_buckets
WHERE organization_id = @organization_id
  AND managed_by = ANY(@managed_by_scopes::text[])
  AND collector_name <> ALL(@keep_collectors::text[]);

-- name: ListSatelliteQueues :many
SELECT ob.organization_id, ob.collector_name, bc.sqs_queue_url, bc.region, bc.role
FROM organization_buckets ob
JOIN bucket_configurations bc ON bc.id = ob.bucket_id
WHERE bc.sqs_queue_url IS NOT NULL AND bc.sqs_queue_url <> '';
```

Ensure the existing `organization_buckets` upsert query accepts `managed_by`.

- [ ] **Step 5: Implement routing + scoped delete in `ProvisionOrganization`**

In the transaction body: after upserting bucket_configurations and assigning instance_nums and inserting organization_buckets (with `managed_by`), add:

```go
// Identify the single normal collector's instance_num.
var normalInstance int16
normalFound := false
for _, b := range params.Buckets {
	if b.IsNormal {
		normalInstance = instanceNumByCollector[b.CollectorName]
		normalFound = true
		break
	}
}
if !normalFound {
	return result, fmt.Errorf("provision: org %s has no normal collector", params.OrganizationID)
}
// Route non-normal collectors to the normal instance; normal writes to self (leave NULL).
for _, b := range params.Buckets {
	if b.IsNormal {
		continue
	}
	if err := q.SetOrganizationBucketWritesTo(ctx, SetOrganizationBucketWritesToParams{
		WritesToInstanceNum: pgtype.Int2{Int16: normalInstance, Valid: true},
		OrganizationID:      params.OrganizationID,
		CollectorName:       b.CollectorName,
	}); err != nil {
		return result, err
	}
}
// Scoped declarative delete: remove stale rows only for managed_by values in this payload.
scopes := distinctManagedBy(params.Buckets)
keep := collectorNames(params.Buckets)
if err := q.DeleteStaleManagedOrgBuckets(ctx, DeleteStaleManagedOrgBucketsParams{
	OrganizationID: params.OrganizationID,
	ManagedByScopes: scopes,
	KeepCollectors:  keep,
}); err != nil {
	return result, err
}
```

Add helpers `distinctManagedBy` and `collectorNames` in the same file. When a bucket's `ManagedBy` is empty, treat it as `"unmanaged"` (preserves legacy full-replace behavior for single-bucket callers).

- [ ] **Step 6: Regenerate**

Run: `make generate`
Expected: `configdb/bucket_management.sql.go` gains `UpsertBucketConfigurationWithQueue`, `SetOrganizationBucketWritesTo`, `DeleteStaleManagedOrgBuckets`, `ListSatelliteQueues`.

- [ ] **Step 7: Run the tests to verify they pass**

Run: `go test -tags integration ./configdb/ -run 'TestProvisionMode|TestProvisionReplace' -v`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add configdb/
git commit -m "configdb: mode-derived routing, managed_by-scoped replace, queue url"
```

---

### Task 3: provision API contract — `mode`, `sqs_queue_url`, `managed_by`

**Files:**
- Modify: `adminapi/types.go` (`DesiredBucket` ~lines 225-235; `ProvisionOrganizationRequest` ~253-259)
- Modify: `admin/provision.go` (`validateAndBuildProvisionParams` ~63-190)
- Test: `admin/provision_test.go` (extend; mock store, no Docker)

**Interfaces:**
- Consumes: `ProvisionBucketParams` fields from Task 2.
- Produces: wire contract `DesiredBucket` gains `Mode string json:"mode,omitempty"`, `SQSQueueURL string json:"sqs_queue_url,omitempty"`, `ManagedBy string json:"managed_by,omitempty"`. `validateAndBuildProvisionParams` rejects unknown `mode` and any org without exactly one `normal` collector, and translates `mode`→(`DeleteSources`, `IsNormal`).

- [ ] **Step 1: Write the failing unit tests**

In `admin/provision_test.go` add:

```go
func TestProvisionRejectsZeroNormal(t *testing.T) {
	req := validProvisionRequest()
	req.Buckets = []adminapi.DesiredBucket{
		{BucketName: "a", CollectorName: "a", CloudProvider: "aws", Region: "r", Mode: "read-only"},
	}
	_, err := validateAndBuildProvisionParams(req)
	if err == nil {
		t.Fatal("expected error: no normal collector")
	}
}

func TestProvisionRejectsTwoNormal(t *testing.T) {
	req := validProvisionRequest()
	req.Buckets = []adminapi.DesiredBucket{
		{BucketName: "a", CollectorName: "a", CloudProvider: "aws", Region: "r", Mode: "normal"},
		{BucketName: "b", CollectorName: "b", CloudProvider: "aws", Region: "r", Mode: "normal"},
	}
	_, err := validateAndBuildProvisionParams(req)
	if err == nil {
		t.Fatal("expected error: more than one normal collector")
	}
}

func TestProvisionModeTranslation(t *testing.T) {
	req := validProvisionRequest()
	req.Buckets = []adminapi.DesiredBucket{
		{BucketName: "a", CollectorName: "a", CloudProvider: "aws", Region: "r", Mode: ""},          // defaults normal
		{BucketName: "b", CollectorName: "b", CloudProvider: "aws", Region: "r", Mode: "satellite"}, // delete + route
	}
	params, err := validateAndBuildProvisionParams(req)
	if err != nil {
		t.Fatalf("unexpected: %v", err)
	}
	byName := map[string]ProvisionBucketParams{}
	for _, b := range params.Buckets {
		byName[b.CollectorName] = b
	}
	if !byName["a"].IsNormal || !byName["a"].DeleteSources {
		t.Fatal("normal must be IsNormal + DeleteSources")
	}
	if byName["b"].IsNormal || !byName["b"].DeleteSources {
		t.Fatal("satellite must be non-normal + DeleteSources")
	}
}
```

- [ ] **Step 2: Run to verify failure**

Run: `go test ./admin/ -run TestProvision -v`
Expected: FAIL — `Mode` field undefined / validation absent.

- [ ] **Step 3: Extend the wire types**

In `adminapi/types.go`, add to `DesiredBucket`:

```go
	Mode        string `json:"mode,omitempty"`          // normal|read-only|satellite; empty=normal
	SQSQueueURL string `json:"sqs_queue_url,omitempty"`
	ManagedBy   string `json:"managed_by,omitempty"`
```

- [ ] **Step 4: Implement validation + translation**

In `admin/provision.go` `validateAndBuildProvisionParams`, in the bucket loop:

```go
mode := b.Mode
if mode == "" {
	mode = "normal"
}
var deleteSources, isNormal bool
switch mode {
case "normal":
	deleteSources, isNormal = true, true
case "read-only":
	deleteSources, isNormal = false, false
case "satellite":
	deleteSources, isNormal = true, false
default:
	return ProvisionOrgParams{}, fmt.Errorf("bucket %q: unknown mode %q", b.CollectorName, b.Mode)
}
if isNormal {
	normalCount++
}
var qURL *string
if b.SQSQueueURL != "" {
	qURL = &b.SQSQueueURL
}
managedBy := b.ManagedBy
if managedBy == "" {
	managedBy = "unmanaged"
}
out = append(out, ProvisionBucketParams{
	BucketName: b.BucketName, CollectorName: b.CollectorName,
	CloudProvider: b.CloudProvider, Region: b.Region,
	Endpoint: strPtrOrNil(b.Endpoint), Role: strPtrOrNil(b.Role),
	UsePathStyle: b.UsePathStyle, InsecureTLS: b.InsecureTLS,
	SQSQueueURL: qURL, ManagedBy: managedBy,
	DeleteSources: deleteSources, IsNormal: isNormal,
})
```

After the loop:

```go
if normalCount != 1 {
	return ProvisionOrgParams{}, fmt.Errorf("org %s must have exactly one normal collector, got %d", req.OrganizationID, normalCount)
}
```

(Reuse the existing endpoint/role nil-helper if present instead of `strPtrOrNil`.)

- [ ] **Step 5: Run tests to verify pass**

Run: `go test ./admin/ -run TestProvision -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add adminapi/types.go admin/provision.go admin/provision_test.go
git commit -m "admin: provision contract gains mode, sqs_queue_url, managed_by"
```

---

### Task 4: configdb-sourced queue list accessor

**Files:**
- Modify: `internal/pubsub/sqs.go` (add a configdb-backed loader)
- Test: `internal/pubsub/sqs_configdb_test.go` (new, integration)

**Interfaces:**
- Consumes: `ListSatelliteQueues` from Task 2.
- Produces: `func LoadQueuesFromConfigDB(ctx context.Context, q QueueLister) ([]config.SQSQueueConfig, error)` where `QueueLister` is a one-method interface (`ListSatelliteQueues(ctx) ([]configdb.ListSatelliteQueuesRow, error)`). Maps each row to `config.SQSQueueConfig{QueueURL, Region, RoleARN}`, falling back region→`AWS_REGION`→default as `DiscoverSQSQueues` does today.

- [ ] **Step 1: Write the failing integration test**

Create `internal/pubsub/sqs_configdb_test.go`:

```go
//go:build integration

package pubsub

import (
	"context"
	"testing"
)

func TestLoadQueuesFromConfigDB(t *testing.T) {
	ctx := context.Background()
	store := newConfigdbForQueues(t) // seeds two org_buckets, one with sqs_queue_url set, one NULL
	queues, err := LoadQueuesFromConfigDB(ctx, store)
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	if len(queues) != 1 {
		t.Fatalf("expected 1 queue (NULL url excluded), got %d", len(queues))
	}
	if queues[0].QueueURL == "" || queues[0].Region == "" {
		t.Fatalf("queue not mapped: %+v", queues[0])
	}
}
```

- [ ] **Step 2: Run to verify failure**

Run: `go test -tags integration ./internal/pubsub/ -run TestLoadQueuesFromConfigDB -v`
Expected: FAIL — `LoadQueuesFromConfigDB` undefined.

- [ ] **Step 3: Implement the loader**

In `internal/pubsub/sqs.go`:

```go
type QueueLister interface {
	ListSatelliteQueues(ctx context.Context) ([]configdb.ListSatelliteQueuesRow, error)
}

func LoadQueuesFromConfigDB(ctx context.Context, q QueueLister) ([]config.SQSQueueConfig, error) {
	rows, err := q.ListSatelliteQueues(ctx)
	if err != nil {
		return nil, err
	}
	out := make([]config.SQSQueueConfig, 0, len(rows))
	for _, r := range rows {
		region := r.Region
		if region == "" {
			region = config.DefaultRegion() // same fallback DiscoverSQSQueues uses
		}
		role := ""
		if r.Role.Valid {
			role = r.Role.String
		}
		out = append(out, config.SQSQueueConfig{
			QueueURL: r.SqsQueueUrl.String,
			Region:   region,
			RoleARN:  role,
		})
	}
	return out, nil
}
```

(If `config` has no exported `DefaultRegion`, replicate the literal fallback chain used in `config/sqs.go`.)

- [ ] **Step 4: Run to verify pass**

Run: `go test -tags integration ./internal/pubsub/ -run TestLoadQueuesFromConfigDB -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add internal/pubsub/sqs.go internal/pubsub/sqs_configdb_test.go
git commit -m "pubsub: configdb-sourced queue list loader"
```

---

### Task 5: pubsub-sqs polls configdb-sourced queues (with periodic refresh)

**Files:**
- Modify: `internal/pubsub/sqs.go` (`NewSQSService` ~35-65, `Run` ~91)
- Modify: `cmd/pubsub.go` (wire the configdb store into the service; drop `DiscoverSQSQueues`)
- Test: `internal/pubsub/sqs_refresh_test.go` (new)

**Interfaces:**
- Consumes: `LoadQueuesFromConfigDB` (Task 4).
- Produces: `NewSQSService` takes a `QueueLister` (configdb store) instead of reading env; on startup it loads queues from configdb, and `Run` re-loads on a ticker (default 60s, override `PUBSUB_QUEUE_REFRESH_SECONDS`) so reconciled satellites are picked up without restart. Pollers are started/stopped as the set changes.

- [ ] **Step 1: Write the failing test**

Create `internal/pubsub/sqs_refresh_test.go` (unit, fake lister):

```go
package pubsub

import (
	"context"
	"testing"
)

type fakeLister struct{ calls int }

func (f *fakeLister) ListSatelliteQueues(ctx context.Context) ([]configdb.ListSatelliteQueuesRow, error) {
	f.calls++
	return nil, nil
}

func TestNewSQSServiceLoadsFromConfigDB(t *testing.T) {
	f := &fakeLister{}
	svc, err := NewSQSService(context.Background(), f /*, other existing deps */)
	if err != nil {
		t.Fatalf("new: %v", err)
	}
	if f.calls == 0 {
		t.Fatal("expected NewSQSService to load queues from configdb at construction")
	}
	_ = svc
}
```

- [ ] **Step 2: Run to verify failure**

Run: `go test ./internal/pubsub/ -run TestNewSQSServiceLoadsFromConfigDB -v`
Expected: FAIL — `NewSQSService` still calls `DiscoverSQSQueues` / wrong signature.

- [ ] **Step 3: Rework `NewSQSService` and `Run`**

Replace the `config.DiscoverSQSQueues()` call in `NewSQSService` with a `QueueLister` dependency and `LoadQueuesFromConfigDB`. In `Run`, add a refresh ticker:

```go
refresh := time.Duration(envIntDefault("PUBSUB_QUEUE_REFRESH_SECONDS", 60)) * time.Second
ticker := time.NewTicker(refresh)
defer ticker.Stop()
for {
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-ticker.C:
		queues, err := LoadQueuesFromConfigDB(ctx, s.lister)
		if err != nil {
			s.log.Warn("queue refresh failed", "err", err)
			continue
		}
		s.reconcilePollers(queues) // start pollers for new URLs, stop for removed
	}
}
```

Add `reconcilePollers` to diff the running poller set against `queues` by `QueueURL`.

- [ ] **Step 4: Update the caller**

In `cmd/pubsub.go`, construct the configdb store and pass it to `NewSQSService`; remove the `DiscoverSQSQueues` usage.

- [ ] **Step 5: Run tests + build**

Run: `go test ./internal/pubsub/... -v && go build ./...`
Expected: PASS + clean build.

- [ ] **Step 6: Commit**

```bash
git add internal/pubsub/ cmd/pubsub.go
git commit -m "pubsub: poll configdb-sourced queues with periodic refresh"
```

---

### Task 6: remove autoregister

**Files:**
- Delete: `internal/storageprofile/autoregister.go` (and its test, if any)
- Modify: `config/config.go` (remove `Autoregister` / `AutoregisterWritesToInstance` ~206-222)
- Modify: `cmd/pubsub.go` (remove `sqsAutoRegisterConfig` ~55-74, its call ~161-162, provider setup ~39-42)
- Modify: `internal/pubsub/worklane_handler.go` (remove the autoregister call on unseen (org,bucket))
- Test: adjust/remove autoregister tests; add a regression test that an unseen object no longer auto-creates a mapping

**Interfaces:**
- Consumes: nothing new.
- Produces: an unseen (org, bucket) on the otel-raw path is a no-op/ignored (logged), never a configdb write. `config.Config` no longer exposes the autoregister fields.

- [ ] **Step 1: Write the failing regression test**

In `internal/pubsub/worklane_handler_test.go` add a test asserting that handling an object for an (org, bucket) not present in configdb does **not** create an `organization_buckets` row (it should be skipped/logged). Expected initially: FAIL if autoregister still runs (row created), or compile error referencing removed symbols once you start deleting — write the assertion first against current behavior so it fails.

- [ ] **Step 2: Run to verify failure**

Run: `go test -tags integration ./internal/pubsub/ -run Autoregister -v` (or the worklane handler test)
Expected: FAIL — autoregister currently creates the row.

- [ ] **Step 3: Delete autoregister**

```bash
git rm internal/storageprofile/autoregister.go
```

Remove the config fields in `config/config.go`, the `sqsAutoRegisterConfig` func + call + provider setup in `cmd/pubsub.go`, and the call site in `internal/pubsub/worklane_handler.go` (replace with a skip+log).

- [ ] **Step 4: Run tests + build**

Run: `make test-only && go build ./...`
Expected: PASS + clean build (fix any references to removed symbols).

- [ ] **Step 5: Full integration gate**

Run: `make test-integration`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "pubsub: remove autoregister (mappings now come from configdb)"
```

---

## Self-Review

**Spec coverage:**
- "configdb schema: managed_by + sqs_queue_url; routing reuses writes_to/delete_sources" → Task 1, 2.
- "DesiredBucket gains mode, sqs_queue_url, managed_by; server derives delete_sources/writes_to from mode + single normal collector" → Task 3 (+ Task 2 routing).
- "fully-declarative reconcile scoped by managed_by" → Task 2 (`DeleteStaleManagedOrgBuckets`).
- "pubsub-sqs reads its poll list from configdb" → Task 4, 5.
- "autoregister removed" → Task 6.
- "exactly one normal collector per org" → Task 3 validation.

**Not in this plan (correctly deferred to other repos' plans):**
- Maestro reading the JSON and sending the per-org `DesiredBucket[]` with `managed_by=external_json_config` (conductor plan).
- CFN `SATELLITE_CONFIG` → SSM → Maestro env, and deletion of the numbered-queue hack + `PUBSUB_AUTOREGISTER*` params (CFN plan).

**Open items handed to execution:**
- Exact `testhelpers` accessor names (`pgc.Pool` vs a store constructor) — match the existing `configdb/store_integration_test.go` and `internal/pubsub` integration setups.
- Whether `config.DefaultRegion()` is exported — replicate the literal fallback if not.

## Contract handoff (consumed by the Maestro + CFN plans)

The provision wire contract after this plan:

```jsonc
// POST /api/v1/provision  — buckets[] is a full declarative set per org, scoped by managed_by
{
  "organization_id": "…", "name": "…", "enabled": true,
  "buckets": [
    { "bucket_name": "central", "collector_name": "central", "cloud_provider": "aws",
      "region": "us-east-1", "sqs_queue_url": "https://…/central",
      "mode": "normal", "managed_by": "external_json_config" },
    { "bucket_name": "eu-raw", "collector_name": "eu", "cloud_provider": "aws",
      "region": "eu-west-1", "role": "arn:aws:iam::…:role/…",
      "sqs_queue_url": "https://…/eu", "mode": "read-only", "managed_by": "external_json_config" }
  ]
}
```
