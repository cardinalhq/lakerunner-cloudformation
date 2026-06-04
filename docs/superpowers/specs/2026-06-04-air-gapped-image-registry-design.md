# Air-gapped image registry override + image manifest (Phase 1: satellite)

Date: 2026-06-04
Status: approved (Phase 1 scoped; Phase 2 deferred to a later PR)

## Problem

Air-gapped customers cannot pull our container images from the public
registries (`public.ecr.aws`, `ghcr.io`). They need two things:

1. A way to point the stacks at their own private mirror instead of the
   public registries.
2. A concrete, machine-readable list of every image a stack runs, so their
   security team can mirror and scan each image before allowing it into the
   private registry.

We already let customers override templates from a private mirror via the
single `TemplateBaseUrl` parameter (one prefix, child filenames appended).
There is no equivalent single knob for container images: today each image is
its own full-URI parameter (`OtelImage`, `LakerunnerImage`, ...), so an
operator must override each one with a complete URI. There is also no image
manifest artifact.

## Scope

This work is split into two phases. **This spec covers Phase 1 only.**

- **Phase 1 (this PR): satellite stacks/scripts.** The satellite is the
  smallest, self-contained surface: `satellite_services.py` runs exactly one
  container image (the otel collector); `satellite_infra_base.py` runs no
  containers (IAM/S3/SQS only); the satellite deploy scripts reference no
  images. Phase 1 establishes the `ImageRegistry` pattern and the manifest
  generator against this small surface.
- **Phase 2 (later PR, out of scope here): the main lakerunner stack.** Apply
  the same `ImageRegistry` pattern to `lakerunner_services.py`
  (lakerunner/maestro/dex), decide the handling of the two utility images
  (`busybox`/`dex_init` and `initcontainer-grafana`/`db_init`), and extend the
  manifest + doc to all six images. Phase 2 design notes are recorded at the
  end of this document so the decisions are not lost, but nothing in Phase 2
  is built now.

## Decisions

- **First-party Cardinal images flatten under a single `ImageRegistry`
  prefix.** In Phase 1 the only such image is the otel collector. When
  `ImageRegistry` is set, the image resolves to
  `${ImageRegistry}/<basename>:<tag>` (the registry/namespace prefix is
  swapped; the customer mirrors the image into one flat namespace).
- **The existing per-image full-URI parameter remains as an escape hatch**
  that wins over the prefix.
- **The manifest is the upstream source list.** It always lists the public
  upstream image references (what to pull/scan/mirror *from*), regardless of
  `ImageRegistry`.
- **Phase 1 manifest lists satellite images only** (the otel collector). It
  honestly reflects what a satellite-only customer runs.

## Design

### 1. Resolution model (three-way precedence)

A new root parameter `ImageRegistry` (Type String, Default `""`) is added to
`satellite_services.py`. The otel image is resolved in the template body via
CloudFormation `Conditions` + `Fn::If`, in this precedence order:

1. If the per-image override `OtelImage` is non-empty -> use it verbatim
   (full-URI escape hatch wins).
2. Else if `ImageRegistry` is non-empty -> `Fn::Sub("${ImageRegistry}/<suffix>")`
   where `<suffix>` is the flattened `basename:tag` (or `basename@digest`).
3. Else -> the public default
   (`public.ecr.aws/cardinalhq.io/cardinalhq-otel-collector:v1.8.0`).

To make the precedence expressible in CloudFormation, **`OtelImage`'s default
flips from the full public URI to `""`** (empty means "resolve me"). The
public default and the flattened suffix are both computed in Python from the
`images.otel` value in `cardinal-defaults.yaml`; they are baked into the
template as literals, so the generated template is self-contained.

Concretely, the rendered template gains:

- Parameter `ImageRegistry`, `Default: ""`,
  `Description`: registry/namespace prefix for first-party Cardinal images;
  leave empty to pull from the public default. When set, images resolve to
  `<ImageRegistry>/<image>:<tag>`.
- Parameter `OtelImage`, `Default: ""` (was the full URI),
  `Description`: full image URI override for the otel collector; wins over
  `ImageRegistry`; leave empty to use `ImageRegistry` or the public default.
- Condition `OtelImageProvided = Not(Equals(Ref(OtelImage), ""))`.
- Condition `ImageRegistryProvided = Not(Equals(Ref(ImageRegistry), ""))`.
- The container `Image` becomes:
  `If(OtelImageProvided, Ref(OtelImage), If(ImageRegistryProvided, Sub("${ImageRegistry}/cardinalhq-otel-collector:v1.8.0"), "public.ecr.aws/cardinalhq.io/cardinalhq-otel-collector:v1.8.0"))`.

### 2. Shared helper in `images.py`

Add a registry-aware image helper alongside the existing `add_image_override`
(the old helper stays for any caller not yet migrated). The new helper:

- Ensures the `ImageRegistry` parameter exists on the template (adds it once;
  safe to call for multiple images in Phase 2).
- Adds the per-image parameter with `Default: ""`.
- Adds the `*Provided` conditions (idempotent on the shared `ImageRegistry`
  condition).
- Returns the resolved `Fn::If` expression to assign to the container `Image`.

The flattened suffix is derived from the default URI as "everything after the
last `/`" -- this yields `cardinalhq-otel-collector:v1.8.0` for the otel
image, and is correct for `:tag`, `@sha256:` digest, and busybox/ghcr forms
(needed in Phase 2). This derivation lives in a small pure function with its
own unit test.

`satellite_services.py` switches its `image_ref = add_image_override(...)`
call (line ~242) to the new helper, and adds `ImageRegistry` to the console
parameter-group metadata (next to `OtelImage` in the "Image overrides"
group).

### 3. Image manifest generator

A new module `src/cardinal_cfn/image_manifest.py` reads the `images:` block
from `cardinal-defaults.yaml` and emits a newline-delimited, sorted, deduped
list of full upstream image references for a given stack. It carries a
stack -> image-keys mapping; Phase 1 registers `satellite -> ["otel"]`
(Phase 2 adds the lakerunner stack's keys).

`build.sh` is extended to emit the satellite manifest after generating the
satellite-services template:

```
python3 -m cardinal_cfn.image_manifest satellite > generated-templates/satellite-images.txt
```

For Phase 1 `satellite-images.txt` contains exactly:

```
public.ecr.aws/cardinalhq.io/cardinalhq-otel-collector:v1.8.0
```

The manifest reflects the upstream public references, never the customer's
`ImageRegistry` value -- it is the list to mirror *from*.

### 4. Documentation

New `docs/air-gapped-images.md`, Phase-1 scope:

- States the satellite collector runs a single image (the otel collector) and
  points at `generated-templates/satellite-images.txt` as the canonical list.
- Explains the two override paths: set `ImageRegistry=<mirror-prefix>` to
  flatten the image to `<mirror-prefix>/cardinalhq-otel-collector:v1.8.0`, or
  set `OtelImage=<full-uri>` to override outright (wins over `ImageRegistry`).
- Includes a short illustrative mirror snippet (e.g. a skopeo/docker
  pull-retag-push loop reading `satellite-images.txt`).
- Notes that the central lakerunner stack's images are covered in a later
  release (Phase 2).

### 5. Changelog

Add a `CHANGELOG.md` entry (operator-facing):

- New `ImageRegistry` parameter on `cardinal-satellite-services`.
- `OtelImage` default is now empty (resolves to the public default when both
  `OtelImage` and `ImageRegistry` are empty -- no behavior change for existing
  installs that set neither).
- No data-bearing resource is replaced.
- Upgrade action: none, unless mirroring images for an air-gapped install.

## Testing

- **Unit -- suffix derivation:** `cardinalhq-otel-collector:v1.8.0` from the
  otel default; plus `:tag`, `@sha256:` digest, and a ghcr/busybox-style input
  (so the function is ready for Phase 2).
- **Unit -- manifest:** `image_manifest satellite` output equals
  `[cardinal-defaults.yaml images.otel]`.
- **Template (cloud-radar) on `cardinal-satellite-services`:**
  - Defaults (both params empty) -> otel container `Image` is the public
    default.
  - `ImageRegistry=mirror.example/cardinal` -> `Image ==
    mirror.example/cardinal/cardinalhq-otel-collector:v1.8.0`.
  - `OtelImage=registry.internal/otel:custom` (with and without
    `ImageRegistry` set) -> `Image == registry.internal/otel:custom`.
- **Lint:** `cardinal-satellite-services.yaml` passes cfn-lint with no new
  errors (conditions + `Fn::If` + `Fn::Sub` render cleanly).

## Explicit scope choices / risks

- `OtelImage`'s default flips from a full URI to `""`. This is a visible
  parameter-surface change (the console no longer shows the default URI) but
  is behavior-preserving for installs that set neither knob. Documented in the
  changelog.
- The satellite stack runs only one image, so the single-prefix value is
  modest in isolation; its purpose is to establish the pattern Phase 2 reuses
  across the main stack and to give a registry-level knob now.

## Phase 2 notes (not built in this PR)

Recorded so the prior decisions are not lost:

- Apply the same `ImageRegistry` resolution to `lakerunner_services.py` for
  the first-party images `lakerunner`, `maestro`, `dex`. Resolve in the root
  and pass the final strings down to the existing child `*Image` parameters,
  leaving children otherwise unchanged (preserves the B -> C service-tier
  split rule). Standalone child deploys that bypass the root will not get the
  prefix -- an accepted limitation.
- **Utility images keep their own per-image knobs** (not folded into
  `ImageRegistry`): `dex_init` (busybox `public.ecr.aws/docker/library/busybox:1.37`,
  used purely as a shell to render the dex config) and `db_init`
  (`ghcr.io/cardinalhq/initcontainer-grafana:latest`, repurposed as a psql
  client for two `CREATE DATABASE` calls + a keepalive sleeper). The lakerunner
  `migrate` command connects to an already-existing database and does not
  create it, so the psql step cannot be dropped without a change in the
  `lakerunner`/`maestro` binaries.
- `db_init` is intentionally left as-is for now (the `:latest` mutable tag and
  the "grafana" misnomer/registry mismatch with the charts are known and will
  be revisited).
- Extend the manifest generator (`lakerunner -> [...]`) and
  `docs/air-gapped-images.md` to cover all six images.
