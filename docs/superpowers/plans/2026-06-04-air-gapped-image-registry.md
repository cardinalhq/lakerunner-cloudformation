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

## Phase 2 (later PR, not built here)

Extend the script-driven override + manifest to the main lakerunner stack
(`lakerunner`, `maestro`, `dex`), handle the two utility images
(`busybox`/`dex_init`, `initcontainer-grafana`/`db_init` — the psql step can't
be dropped without a binary change), and extend the manifest/doc to all six
images.
