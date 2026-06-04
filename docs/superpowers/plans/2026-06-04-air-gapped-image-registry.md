# Air-gapped Image Override (Phase 1: satellite) Implementation Plan

> **Note:** This plan was simplified during execution. The original draft built
> a three-way `ImageRegistry`/`Fn::If` resolver inside the template plus
> troposphere helpers (`flatten_suffix`, `add_registry_image`). That was
> discarded in favor of script-driven selection: the deploy script passes a
> literal `OtelImage`, the template is unchanged. The tasks below reflect what
> was actually built. See the spec for rationale.

**Goal:** Let air-gapped operators deploy the satellite collector from a private
mirror by setting `OTEL_IMAGE` on the deploy script, and publish a generated
manifest of the upstream image(s) to mirror and scan.

**Architecture:** `deploy-satellite-services.sh` accepts an optional
`OTEL_IMAGE` full-URI env var and forwards it as the literal `OtelImage`
CloudFormation parameter; unset preserves the template's public default. A new
`image_manifest` generator emits `generated-templates/satellite-images.txt`,
wired into `build.sh`. The CloudFormation template and `images.py` are
unchanged.

**Tech Stack:** POSIX sh (deploy driver), Python 3 (manifest generator),
pytest, shellcheck, cfn-lint.

**Spec:** `docs/superpowers/specs/2026-06-04-air-gapped-image-registry-design.md`

---

### Task 1: `OTEL_IMAGE` passthrough in the deploy script — DONE

**Files:**
- `scripts-src/parts/deploy-satellite-services.sh` (front-half source)
- `scripts/deploy-satellite-services.sh` (regenerated via `make scripts`)

- [x] Document `OTEL_IMAGE` in `usage()` (Optional inputs), pointing at
  `satellite-images.txt` / `docs/air-gapped-images.md`.
- [x] Add `OTEL_IMAGE` to the input-echo diagnostic loop.
- [x] Append `OtelImage=$OTEL_IMAGE` to `params` when `OTEL_IMAGE` is set
  (mirrors the existing `ALB_SCHEME` / `INGEST_SOURCE_CIDR` conditional lines);
  omit when unset so the engine falls back to the template `Default`.
- [x] Regenerate with `make scripts`; `test_deploy_stack_lint.py` (drift +
  shellcheck) stays green.
- [x] Verify behaviorally: front-half with engine stubbed emits
  `OtelImage=<uri>` when `OTEL_IMAGE` is set and omits it otherwise.

### Task 2: Image manifest generator — DONE

**Files:**
- Create: `src/cardinal_cfn/image_manifest.py`
- Test: `tests/unit/test_image_manifest.py`

- [x] `manifest_lines(stack)` reads `cardinal-defaults.yaml` `images:` via
  `load_defaults()`, returns sorted/deduped full refs for the stack's keys;
  `STACK_IMAGE_KEYS = {"satellite": ["otel"]}`; unknown stack raises
  `ValueError`. CLI `python3 -m cardinal_cfn.image_manifest <stack>`.
- [x] Tests: satellite manifest equals `[images.otel]`; unknown stack raises.

### Task 3: Wire the manifest into `build.sh` — DONE

- [x] After generating `cardinal-satellite-services.yaml`, emit
  `generated-templates/satellite-images.txt` via the manifest CLI.
- [x] `make build` emits the file; cfn-lint clean.

### Task 4: Docs + changelog — DONE

- [x] `docs/air-gapped-images.md`: satellite image list, skopeo mirror loop,
  `OTEL_IMAGE` usage.
- [x] `CHANGELOG.md` `v0.0.125` entry: manifest + `OTEL_IMAGE` input; unset =
  public default; no resource replacement.

### Task 5: Full verification — DONE

- [x] `make build` (generate + lint) and `make test` (482 passed, 1 skipped).

---

## v0.0.126 rework (locked image + STACK_VERSION) — DONE

Superseded the v0.0.125 `OTEL_IMAGE` full-URI passthrough with a driver-locked
image and an optional baked `STACK_VERSION`.

- [x] **Pin the otel digest** in `cardinal-defaults.yaml`
  (`...:v1.8.0@sha256:9906…`, multi-arch index digest). Flows into the template
  default + manifest (single source).
- [x] **`image_manifest.py`**: subcommands `manifest <stack>` / `suffix <key>`;
  `image_ref` + `registry_relative` helpers; tests updated. `build.sh` uses
  `manifest satellite`.
- [x] **`scripts-src/build.sh`**: bake `@@STACK_VERSION@@` (`${CARDINAL_VERSION:-dev}`)
  and `@@OTEL_IMAGE_SUFFIX@@` (`image_manifest suffix otel`) into generated
  drivers via `sed`.
- [x] **`deploy-satellite-services.sh` front-half**: replace `OTEL_IMAGE` with
  `IMAGE_REGISTRY` (default `public.ecr.aws`) composing the baked suffix into a
  literal `OtelImage`; `VERSION` -> optional `STACK_VERSION` (baked default,
  `VERSION` legacy alias). Regenerated; drift + shellcheck green.
- [x] **Docs/changelog**: `docs/air-gapped-images.md` (IMAGE_REGISTRY + ECR
  pull-through + first-pull IAM note), `CHANGELOG.md` `v0.0.126`.
- [x] **Verify**: `make build` (digest-pinned manifest, cfn-lint clean),
  `make test` (487 passed, 1 skipped), behavioral front-half check.

## Phase 2 (later PR, not built here)

Extend the locked-suffix + `IMAGE_REGISTRY` baking + manifest to the main
lakerunner stack (`lakerunner`, `maestro`, `dex`), handle the two utility images
(`busybox`/`dex_init`, `initcontainer-grafana`/`db_init` — the psql step can't
be dropped without a binary change), and extend the manifest/doc to all six
images.
