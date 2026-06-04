# Air-gapped image override + image manifest (Phase 1: satellite)

Date: 2026-06-04
Status: implemented (Phase 1; Phase 2 deferred to a later PR)

Note: this spec was simplified during implementation. The original draft put a
three-way `ImageRegistry`/`Fn::If` resolver inside the CloudFormation template;
the final design moves image selection into the deploy script and keeps the
template a plain literal parameter. See "Design" below for what was built.

## Problem

Air-gapped customers cannot pull our container images from the public
registries (`public.ecr.aws`, `ghcr.io`). They need two things:

1. A way to point the stack at their own private mirror instead of the public
   registries.
2. A concrete, machine-readable list of every image a stack runs, so their
   security team can mirror and scan each image before allowing it into the
   private registry.

## Scope

This work is split into two phases. **This spec covers Phase 1 only.**

- **Phase 1 (this PR): satellite stack/script.** The satellite is the smallest,
  self-contained surface: `satellite_services.py` runs exactly one container
  image (the otel collector); `satellite_infra_base.py` runs no containers
  (IAM/S3/SQS only). Phase 1 establishes the script-driven override pattern and
  the manifest generator against this small surface.
- **Phase 2 (later PR, out of scope here): the main lakerunner stack.** Extend
  the same script-driven override + manifest to `lakerunner_services.py`
  (lakerunner/maestro/dex) and decide the handling of the two utility images
  (`busybox`/`dex_init` and `initcontainer-grafana`/`db_init`). Phase 2 notes
  are recorded at the end so the decisions are not lost; nothing in Phase 2 is
  built now.

## Decisions

- **The deploy script selects the image; the template takes it as a literal.**
  `deploy-satellite-services.sh` accepts an optional `OTEL_IMAGE` full-URI
  environment variable and passes it through as the literal `OtelImage`
  CloudFormation parameter. The template is unchanged — `OtelImage` is the
  plain pass-through parameter it already was, defaulting to the public image.
  No registry-prefix math, no `Fn::If`, no conditions in the template.
- **Why a full-URI passthrough rather than a registry prefix:** the satellite
  runs exactly one image, so a single `OTEL_IMAGE` value is exactly as
  ergonomic as a prefix, with far less machinery. A registry-prefix knob only
  earns its keep across many images (Phase 2), where the script can derive
  per-image suffixes from the generated manifest.
- **The manifest is the upstream source list.** It always lists the public
  upstream image reference (what to pull/scan/mirror *from*), regardless of
  what the operator deploys. Phase 1 lists satellite images only (the otel
  collector).

## Design

### 1. Image selection in the deploy script

`scripts-src/parts/deploy-satellite-services.sh` (the front-half source for the
generated single-file driver `scripts/deploy-satellite-services.sh`) gains an
optional `OTEL_IMAGE` input:

- Documented in the script's `usage()` under Optional inputs.
- Added to the input-echo diagnostic loop.
- When set, appends `OtelImage=$OTEL_IMAGE` to the `PARAMS` list the engine
  passes to `create-change-set`. When unset, no `OtelImage` override is sent and
  the engine falls back to the template's `Default` (the public image).

The driver is regenerated with `make scripts`; `tests/unit/test_deploy_stack_lint.py`
asserts the generated copy matches a fresh build (drift gate), and the script
passes shellcheck.

No change to `satellite_services.py` or `images.py`: the template already
declares `OtelImage` as a literal pass-through parameter (`add_image_override`).

### 2. Image manifest generator

`src/cardinal_cfn/image_manifest.py` reads the `images:` block from
`cardinal-defaults.yaml` and emits a newline-delimited, sorted, deduped list of
full upstream image references for a given stack. It carries a stack ->
image-keys mapping; Phase 1 registers `satellite -> ["otel"]` (Phase 2 adds the
lakerunner stack's keys).

`build.sh` emits the satellite manifest after generating the satellite-services
template:

```
python3 -m cardinal_cfn.image_manifest satellite > generated-templates/satellite-images.txt
```

For Phase 1 `satellite-images.txt` contains exactly:

```
public.ecr.aws/cardinalhq.io/cardinalhq-otel-collector:v1.8.0
```

The manifest reflects the upstream public reference, never the operator's
override — it is the list to mirror *from*. (`generated-templates/` is
gitignored; the manifest is a build artifact, published alongside templates.)

### 3. Documentation

`docs/air-gapped-images.md` (Phase-1 scope): states the satellite collector
runs a single image and points at `satellite-images.txt`; shows a skopeo mirror
loop; and documents setting `OTEL_IMAGE` on the deploy script to deploy the
mirrored image (unset = public default). Notes the central lakerunner stack's
images are covered in a later release.

### 4. Changelog

`CHANGELOG.md` entry (`v0.0.125`): the generated `satellite-images.txt`; the
`OTEL_IMAGE` deploy-script input that maps to the literal `OtelImage` param;
unset preserves the public default; no resource replacement; upgrade action =
none unless mirroring.

## Testing

- **Unit -- manifest:** `image_manifest.manifest_lines("satellite")` equals
  `[cardinal-defaults.yaml images.otel]`; an unknown stack raises `ValueError`.
- **Drift + lint:** the regenerated `deploy-satellite-services.sh` matches a
  fresh `make scripts` build and passes shellcheck (existing
  `test_deploy_stack_lint.py`).
- **Build:** `make build` emits `generated-templates/satellite-images.txt` and
  cfn-lint stays clean.
- The front-half's `OTEL_IMAGE -> OtelImage=` mapping is verified behaviorally
  (run the front-half with the engine stubbed); the combined single-file driver
  rejects arguments, so the engine's internal test hooks cannot be reached
  through it — matching the repo's existing approach of linting the front-half
  rather than unit-testing its env→PARAMS assembly.

## Phase 2 notes (not built in this PR)

- Extend `OTEL_IMAGE`-style passthrough (or a registry prefix that flattens via
  the manifest) to the main lakerunner stack's first-party images
  (`lakerunner`, `maestro`, `dex`), and extend the manifest generator
  (`lakerunner -> [...]`) and `docs/air-gapped-images.md` to all six images.
- **Utility images** in the main stack: `dex_init` (busybox
  `public.ecr.aws/docker/library/busybox:1.37`, used purely as a shell to
  render the dex config) and `db_init`
  (`ghcr.io/cardinalhq/initcontainer-grafana:latest`, repurposed as a psql
  client for two `CREATE DATABASE` calls + a keepalive sleeper). The lakerunner
  `migrate` command connects to an already-existing database and does not
  create it, so the psql step cannot be dropped without a change in the
  `lakerunner`/`maestro` binaries. `db_init` is intentionally left as-is for now
  (the `:latest` mutable tag and the "grafana" misnomer/registry mismatch with
  the charts are known and will be revisited).
