# Air-gapped image override + image manifest (Phase 1: satellite)

Date: 2026-06-04
Status: implemented (Phase 1; Phase 2 deferred to a later PR)

This design evolved across two releases:

- **v0.0.125** introduced a generated image manifest and a full-URI `OTEL_IMAGE`
  passthrough on the deploy driver (template stays a literal `OtelImage` param).
- **v0.0.126** locked the image identity into the satellite driver: the
  collector repo path + pinned tag/digest are baked at publish time, and the
  operator supplies only `IMAGE_REGISTRY` (a registry/pull-through prefix). The
  driver also made the stack version optional (`STACK_VERSION`, baked default).
- **v0.0.127** (current) rolled the same model across all stacks: the lakerunner
  application driver bakes lakerunner/maestro/dex (digest-pinned) behind
  `IMAGE_REGISTRY`; the external busybox/db-init images keep full-URI overrides;
  and `STACK_VERSION` is now optional on every deploy driver.

The sections below describe the v0.0.127 end state.

## Problem

Air-gapped customers cannot pull container images from the public registries
(`public.ecr.aws`, `ghcr.io`). They need (1) to point the deploy at a private
mirror, and (2) a concrete, machine-readable list of every image a stack runs
so their security team can mirror and scan it. The supported deploy path is the
driver + stack (no CloudFormation console), so the driver is where image and
version selection live.

## Scope

- **Phase 1 (done, v0.0.125–126): satellite.** `satellite_services.py` runs one
  image (the otel collector); `satellite_infra_base.py` runs none. The driver
  `deploy-satellite-services.sh` owns selection.
- **Phase 2 (done, v0.0.127): all remaining stacks.** The lakerunner application
  driver bakes lakerunner/maestro/dex behind `IMAGE_REGISTRY`; the
  busybox/grafana utility images stay as full-URI overrides; and `STACK_VERSION`
  is optional on every deploy driver (the infra drivers run no images, so they
  get only the version change).

## Decisions

- **The driver locks the image identity; the operator supplies only a registry
  prefix.** The collector's registry-relative path + pinned tag/digest
  (`cardinalhq.io/cardinalhq-otel-collector:v1.8.0@sha256:…`) is baked into the
  published driver from `cardinal-defaults.yaml`. The operator sets
  `IMAGE_REGISTRY` (default `public.ecr.aws`); the driver composes
  `${IMAGE_REGISTRY}/<locked-suffix>` and passes it as the literal `OtelImage`
  parameter. This suits ECR pull-through caches, which preserve the full
  upstream path and digest under a prefix.
- **The image is digest-pinned.** `cardinal-defaults.yaml` pins the multi-arch
  index digest; it flows into the template default and the manifest, so the
  template, manifest, and driver are single-sourced.
- **`STACK_VERSION` is optional, baked.** The published driver defaults to the
  version baked at publish time (deploying its own matching templates).
  `VERSION` remains a legacy alias.
- **Manifest is the upstream source list** — the public, pinned reference to
  mirror/scan FROM, independent of `IMAGE_REGISTRY`. Phase 1 lists satellite
  images only.

## Design

### 1. Build-time baking (one mechanism)

`scripts-src/build.sh` substitutes two placeholders into each generated driver:

- `@@STACK_VERSION@@` -> `${CARDINAL_VERSION:-dev}` (the release tag; `dev`
  locally — so committed drivers carry `dev`, published drivers carry the tag).
- `@@OTEL_IMAGE_SUFFIX@@` -> `python3 -m cardinal_cfn.image_manifest suffix otel`
  (the otel image's registry-relative path from `cardinal-defaults.yaml`).

The release already runs `./build.sh` with `CARDINAL_VERSION` set and publishes
`scripts/` to S3, so published drivers are version- and image-locked.
`tests/unit/test_deploy_stack_lint.py` asserts the committed drivers match a
fresh build (drift gate) and pass shellcheck.

### 2. Driver front-half (`deploy-satellite-services.sh`)

- `STACK_VERSION` (optional) -> `${STACK_VERSION:-${VERSION:-<baked>}}`; used in
  `TEMPLATE_URL`. `VERSION` no longer required.
- `IMAGE_REGISTRY` (optional, default `public.ecr.aws`) + baked suffix ->
  `OtelImage=${IMAGE_REGISTRY}/<suffix>`, always passed as a `PARAMS` override.
- `usage()`, the input-echo loop, and resolved-value diagnostics updated.

No change to `satellite_services.py` (it already declares `OtelImage` as a
literal param; its default now carries the pinned digest via defaults).

### 3. Manifest generator (`image_manifest.py`)

- `manifest <stack>` — sorted/deduped upstream refs for a stack
  (`satellite -> ["otel"]`). `build.sh` writes `satellite-images.txt`.
- `suffix <image-key>` — the image's registry-relative path (everything after
  the registry host), used by `scripts-src/build.sh` to bake the driver.
- `image_ref(key)` / `registry_relative(ref)` helpers; all read the pinned
  `cardinal-defaults.yaml`.

### 4. Docs + changelog

`docs/air-gapped-images.md`: how selection works, the pinned image list, an ECR
pull-through example (with the first-pull IAM note), and `IMAGE_REGISTRY` /
`STACK_VERSION` usage. `CHANGELOG.md` `v0.0.126`.

## Testing

- **Unit (`image_manifest`):** manifest equals the pinned otel default; unknown
  stack/key raise; `registry_relative` strips the registry host and requires
  one; otel suffix equals the default minus its registry.
- **Drift + lint:** committed `deploy-satellite-services.sh` matches a fresh
  `make scripts` build (which bakes `dev` + the pinned suffix) and passes
  shellcheck.
- **Build:** `make build` emits a digest-pinned `satellite-images.txt`; the
  template `OtelImage` default carries the digest; cfn-lint clean.
- **Behavioral:** front-half (engine stubbed) composes
  `OtelImage=${IMAGE_REGISTRY}/<pinned-suffix>` and resolves `STACK_VERSION`
  (default baked, `VERSION` alias honored). The combined driver rejects
  arguments, so the engine's internal hooks can't be reached through it —
  matching the repo's lint-the-front-half approach.

## Phase 2 implementation (v0.0.127)

- `cardinal-defaults.yaml` pins lakerunner/maestro/dex to their multi-arch index
  digests (alongside otel). `scripts-src/build.sh` bakes the registry-relative
  suffix for each first-party image (`image_manifest suffix <key>`) and
  `@@STACK_VERSION@@` into every driver.
- `deploy-lakerunner-services.sh` composes `${IMAGE_REGISTRY}/<suffix>` for
  lakerunner/maestro/dex (always passed as literal params) and drops the old
  per-image full-URI overrides + the dead `OTEL_IMAGE` passthrough (the
  lakerunner root has no `OtelImage` param).
- The external/utility images stay as full-URI overrides: `DEX_INIT_IMAGE`
  (busybox, a shell to render the dex config) and `DB_INIT_IMAGE`
  (`initcontainer-grafana`, a psql client for two `CREATE DATABASE` calls + a
  keepalive sleeper). They are not on our public ECR and are not driven by
  `IMAGE_REGISTRY`. The lakerunner `migrate` command connects to an existing
  database and does not create it, so the psql step can't be dropped without a
  binary change; `db_init` stays `:latest` (the grafana misnomer/registry
  mismatch are known and out of scope).
- The infra drivers (`infra-base`, `infra-rds`, `satellite-infra-base`) run no
  images, so they receive only the `STACK_VERSION` change.
- `image_manifest` gains a `lakerunner` stack key (all five images) and
  `build.sh` emits `lakerunner-images.txt`.
