# Air-gapped Image Registry Override (Phase 1: satellite) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give air-gapped customers a single `ImageRegistry` prefix knob on the `cardinal-satellite-services` stack (governing the otel collector image), plus a generated manifest listing the upstream image(s) to mirror and scan.

**Architecture:** Add two reusable helpers to `src/cardinal_cfn/images.py` — a pure `flatten_suffix()` and a registry-aware `add_registry_image()` that declares an `ImageRegistry` prefix parameter, a per-image full-URI override parameter (default empty), the matching CloudFormation `Conditions`, and returns the resolved `Fn::If` expression with precedence override > prefix > public default. Migrate `satellite_services.py`'s single otel image to it. Add an `image_manifest` generator wired into `build.sh` emitting `generated-templates/satellite-images.txt`. Document and changelog the new parameter.

**Tech Stack:** Python 3, troposphere, pytest, cloud-radar (existing test deps), cfn-lint. Tests run via `make test` / `pytest`. Generators emit YAML through `build.sh` (`make build`).

**Spec:** `docs/superpowers/specs/2026-06-04-air-gapped-image-registry-design.md`

---

## File Structure

- `src/cardinal_cfn/images.py` (modify) — add `flatten_suffix()` and `add_registry_image()` next to the existing `add_image_override()` (which stays for non-migrated callers).
- `src/cardinal_cfn/image_manifest.py` (create) — stack -> upstream-image-list generator; CLI entrypoint.
- `src/cardinal_cfn/satellite_services.py` (modify) — use `add_registry_image()` for `OtelImage`; add `ImageRegistry` to the console parameter group.
- `tests/unit/test_images.py` (modify) — unit tests for `flatten_suffix()` and `add_registry_image()`.
- `tests/unit/test_image_manifest.py` (create) — unit tests for the manifest generator.
- `tests/templates/test_satellite_services.py` (modify) — template-level precedence assertions.
- `build.sh` (modify) — emit `satellite-images.txt`.
- `docs/air-gapped-images.md` (create) — operator doc (Phase 1 scope).
- `CHANGELOG.md` (modify) — new version entry.

---

### Task 1: `flatten_suffix()` pure helper

**Files:**
- Modify: `src/cardinal_cfn/images.py`
- Test: `tests/unit/test_images.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_images.py`:

```python
from cardinal_cfn.images import flatten_suffix


def test_flatten_suffix_tag():
    assert (
        flatten_suffix("public.ecr.aws/cardinalhq.io/cardinalhq-otel-collector:v1.8.0")
        == "cardinalhq-otel-collector:v1.8.0"
    )


def test_flatten_suffix_ghcr_and_busybox():
    assert flatten_suffix("ghcr.io/cardinalhq/initcontainer-grafana:latest") == "initcontainer-grafana:latest"
    assert flatten_suffix("public.ecr.aws/docker/library/busybox:1.37") == "busybox:1.37"


def test_flatten_suffix_digest():
    assert (
        flatten_suffix("public.ecr.aws/cardinalhq.io/lakerunner@sha256:abc123")
        == "lakerunner@sha256:abc123"
    )


def test_flatten_suffix_no_registry():
    assert flatten_suffix("busybox:1.37") == "busybox:1.37"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `make install >/dev/null 2>&1; source .venv/bin/activate && pytest tests/unit/test_images.py -k flatten_suffix -v`
Expected: FAIL with `ImportError: cannot import name 'flatten_suffix'`.

- [ ] **Step 3: Write minimal implementation**

In `src/cardinal_cfn/images.py`, after the existing `add_image_override` function, add:

```python
def flatten_suffix(image_ref: str) -> str:
    """Return the repo basename + tag/digest from a full image reference.

    Everything after the final '/'. Correct for ':tag', '@sha256:' digest,
    and refs with no registry component.

        public.ecr.aws/cardinalhq.io/cardinalhq-otel-collector:v1.8.0
            -> cardinalhq-otel-collector:v1.8.0
    """
    return image_ref.rsplit("/", 1)[-1]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `source .venv/bin/activate && pytest tests/unit/test_images.py -k flatten_suffix -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/cardinal_cfn/images.py tests/unit/test_images.py
git commit -m "feat(images): add flatten_suffix helper for registry-prefix derivation"
```

---

### Task 2: `add_registry_image()` helper

**Files:**
- Modify: `src/cardinal_cfn/images.py`
- Test: `tests/unit/test_images.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_images.py`:

```python
from cardinal_cfn.images import add_registry_image


def test_add_registry_image_declares_params_and_conditions():
    t = Template()
    add_registry_image(
        t,
        name="OtelImage",
        default="public.ecr.aws/cardinalhq.io/cardinalhq-otel-collector:v1.8.0",
        description="otel override",
    )
    rendered = _to_dict(t)
    # Per-image override param defaults to empty (empty == "resolve me").
    assert rendered["Parameters"]["OtelImage"]["Default"] == ""
    # Shared registry prefix param is declared once, default empty.
    assert rendered["Parameters"]["ImageRegistry"]["Default"] == ""
    # Both gating conditions exist.
    assert "OtelImageProvided" in rendered["Conditions"]
    assert "ImageRegistryProvided" in rendered["Conditions"]


def test_add_registry_image_returns_three_way_if():
    t = Template()
    ref = add_registry_image(
        t,
        name="OtelImage",
        default="public.ecr.aws/cardinalhq.io/cardinalhq-otel-collector:v1.8.0",
        description="otel override",
    )
    assert ref.to_dict() == {
        "Fn::If": [
            "OtelImageProvided",
            {"Ref": "OtelImage"},
            {
                "Fn::If": [
                    "ImageRegistryProvided",
                    {"Fn::Sub": "${ImageRegistry}/cardinalhq-otel-collector:v1.8.0"},
                    "public.ecr.aws/cardinalhq.io/cardinalhq-otel-collector:v1.8.0",
                ]
            },
        ]
    }


def test_add_registry_image_shares_one_registry_param():
    """Calling twice on the same template declares ImageRegistry only once."""
    t = Template()
    add_registry_image(t, name="OtelImage", default="r/a:1", description="a")
    add_registry_image(t, name="MaestroImage", default="r/b:2", description="b")
    rendered = _to_dict(t)
    assert "ImageRegistry" in rendered["Parameters"]
    assert "OtelImageProvided" in rendered["Conditions"]
    assert "MaestroImageProvided" in rendered["Conditions"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `source .venv/bin/activate && pytest tests/unit/test_images.py -k add_registry_image -v`
Expected: FAIL with `ImportError: cannot import name 'add_registry_image'`.

- [ ] **Step 3: Write minimal implementation**

In `src/cardinal_cfn/images.py`, change the import line at the top from:

```python
from troposphere import Parameter, Ref, Template
```

to:

```python
from troposphere import Equals, If, Not, Parameter, Ref, Sub, Template
```

Then, after `flatten_suffix`, add:

```python
def add_registry_image(
    t: Template,
    *,
    name: str,
    default: str,
    description: str,
    registry_param: str = "ImageRegistry",
):
    """Declare a registry-aware image parameter; return the resolved image.

    Precedence, highest first:
      1. ``<name>`` (full-URI override) when non-empty.
      2. ``${<registry_param>}/<basename>:<tag>`` when ``<registry_param>`` is
         non-empty (air-gapped mirror; the registry/namespace is flattened to a
         single prefix).
      3. ``default`` (the public upstream reference).

    The per-image parameter defaults to "" so the precedence is expressible in
    CloudFormation: an empty value means "resolve me from the prefix or the
    default". The shared ``registry_param`` and its condition are declared once
    per template (safe to call for multiple images).
    """
    if registry_param not in t.parameters:
        t.add_parameter(
            Parameter(
                registry_param,
                Type="String",
                Default="",
                Description=(
                    "Registry/namespace prefix for first-party Cardinal images "
                    "(air-gapped mirror). Leave empty to pull from the public "
                    "default. When set, images resolve to <prefix>/<image>:<tag>."
                ),
            )
        )
    registry_cond = registry_param + "Provided"
    if registry_cond not in t.conditions:
        t.add_condition(registry_cond, Not(Equals(Ref(registry_param), "")))

    t.add_parameter(
        Parameter(name, Type="String", Default="", Description=description)
    )
    override_cond = name + "Provided"
    t.add_condition(override_cond, Not(Equals(Ref(name), "")))

    suffix = flatten_suffix(default)
    return If(
        override_cond,
        Ref(name),
        If(
            registry_cond,
            Sub("${" + registry_param + "}/" + suffix),
            default,
        ),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `source .venv/bin/activate && pytest tests/unit/test_images.py -v`
Expected: PASS (all `flatten_suffix`, `add_registry_image`, and pre-existing `add_image_override` tests).

- [ ] **Step 5: Commit**

```bash
git add src/cardinal_cfn/images.py tests/unit/test_images.py
git commit -m "feat(images): add registry-aware add_registry_image helper"
```

---

### Task 3: Migrate satellite otel image to `add_registry_image()`

**Files:**
- Modify: `src/cardinal_cfn/satellite_services.py` (import ~line 74; image_ref ~line 242; param group ~line 291)
- Test: `tests/templates/test_satellite_services.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/templates/test_satellite_services.py` (the file already imports `json` and defines the `td` fixture):

```python
from cardinal_cfn.defaults import load_defaults


def _otel_image(td):
    return td["Resources"]["OtelGrpcTaskDef"]["Properties"]["ContainerDefinitions"][0]["Image"]


def test_image_registry_parameters_default_empty(td):
    assert td["Parameters"]["ImageRegistry"]["Default"] == ""
    assert td["Parameters"]["OtelImage"]["Default"] == ""


def test_image_resolution_conditions_present(td):
    assert "ImageRegistryProvided" in td["Conditions"]
    assert "OtelImageProvided" in td["Conditions"]


def test_otel_image_three_way_precedence(td):
    otel = load_defaults()["images"]["otel"]
    suffix = otel.rsplit("/", 1)[-1]
    assert _otel_image(td) == {
        "Fn::If": [
            "OtelImageProvided",
            {"Ref": "OtelImage"},
            {
                "Fn::If": [
                    "ImageRegistryProvided",
                    {"Fn::Sub": "${ImageRegistry}/" + suffix},
                    otel,
                ]
            },
        ]
    }


def test_image_registry_in_parameter_group(td):
    groups = td["Metadata"]["AWS::CloudFormation::Interface"]["ParameterGroups"]
    image_group = next(g for g in groups if g["Label"]["default"] == "Image overrides")
    assert "ImageRegistry" in image_group["Parameters"]
    assert "OtelImage" in image_group["Parameters"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `source .venv/bin/activate && pytest tests/templates/test_satellite_services.py -k "image_registry or three_way or resolution_conditions" -v`
Expected: FAIL — `ImageRegistry` not in Parameters / Image is `{"Ref": "OtelImage"}` not the `Fn::If`.

- [ ] **Step 3: Write minimal implementation**

In `src/cardinal_cfn/satellite_services.py`:

Change the import (around line 74) from:

```python
from cardinal_cfn.images import add_image_override
```

to:

```python
from cardinal_cfn.images import add_registry_image
```

Replace the `image_ref = add_image_override(...)` block (around lines 241-247) with:

```python
    # Image override (registry-aware: ImageRegistry prefix > OtelImage > default)
    image_ref = add_registry_image(
        t,
        name="OtelImage",
        default=defaults["images"]["otel"],
        description=(
            "Full image URI override for the otel collector. Wins over "
            "ImageRegistry. Leave empty to use ImageRegistry or the public "
            "default."
        ),
    )
```

In the parameter-group metadata (around line 290-292), change:

```python
            {
                "label": "Image overrides",
                "parameters": ["OtelImage"],
            },
```

to:

```python
            {
                "label": "Image overrides",
                "parameters": ["ImageRegistry", "OtelImage"],
            },
```

- [ ] **Step 4: Run test to verify it passes**

Run: `source .venv/bin/activate && pytest tests/templates/test_satellite_services.py -v`
Expected: PASS (new tests + all pre-existing satellite tests).

- [ ] **Step 5: Commit**

```bash
git add src/cardinal_cfn/satellite_services.py tests/templates/test_satellite_services.py
git commit -m "feat(satellite): add ImageRegistry prefix override for the otel image"
```

---

### Task 4: Image manifest generator

**Files:**
- Create: `src/cardinal_cfn/image_manifest.py`
- Test: `tests/unit/test_image_manifest.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_image_manifest.py`:

```python
"""Tests for the image manifest generator."""

import pytest

from cardinal_cfn import image_manifest
from cardinal_cfn.defaults import load_defaults


def test_satellite_manifest_is_otel_image():
    assert image_manifest.manifest_lines("satellite") == [
        load_defaults()["images"]["otel"]
    ]


def test_unknown_stack_raises():
    with pytest.raises(ValueError):
        image_manifest.manifest_lines("nope")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `source .venv/bin/activate && pytest tests/unit/test_image_manifest.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'cardinal_cfn.image_manifest'`.

- [ ] **Step 3: Write minimal implementation**

Create `src/cardinal_cfn/image_manifest.py`:

```python
"""Image manifest generator.

Emits the upstream public container images a given stack runs, one per line,
sorted and deduped. This is the source list air-gapped customers mirror and
scan FROM; it always reflects the public defaults in cardinal-defaults.yaml,
never a customer's ImageRegistry override.
"""

import sys

from cardinal_cfn.defaults import load_defaults

# Which cardinal-defaults.yaml images.* keys each stack runs.
# Phase 1: satellite only. Phase 2 adds the lakerunner stack's keys.
STACK_IMAGE_KEYS = {
    "satellite": ["otel"],
}


def manifest_lines(stack: str) -> list:
    """Return the sorted, deduped upstream image refs for a stack."""
    if stack not in STACK_IMAGE_KEYS:
        known = ", ".join(sorted(STACK_IMAGE_KEYS))
        raise ValueError(f"unknown stack: {stack!r} (known: {known})")
    images = load_defaults()["images"]
    refs = {images[key] for key in STACK_IMAGE_KEYS[stack]}
    return sorted(refs)


def main(argv: list) -> int:
    if len(argv) != 1:
        print(
            "usage: python3 -m cardinal_cfn.image_manifest <stack>",
            file=sys.stderr,
        )
        return 2
    for line in manifest_lines(argv[0]):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `source .venv/bin/activate && pytest tests/unit/test_image_manifest.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Verify the CLI emits the satellite image**

Run: `PYTHONPATH=src python3 -m cardinal_cfn.image_manifest satellite`
Expected output (single line, matches the otel default in cardinal-defaults.yaml):
`public.ecr.aws/cardinalhq.io/cardinalhq-otel-collector:v1.8.0`

- [ ] **Step 6: Commit**

```bash
git add src/cardinal_cfn/image_manifest.py tests/unit/test_image_manifest.py
git commit -m "feat: add image manifest generator (satellite scope)"
```

---

### Task 5: Wire the manifest into `build.sh`

**Files:**
- Modify: `build.sh` (after the cardinal-satellite-services generation, ~line 34)

- [ ] **Step 1: Add the manifest generation line**

In `build.sh`, immediately after the two lines that generate `cardinal-satellite-services.yaml`:

```sh
echo "Generating cardinal-satellite-services.yaml..."
python3 -m cardinal_cfn.satellite_services > generated-templates/cardinal-satellite-services.yaml
```

insert:

```sh
echo "Generating satellite-images.txt..."
python3 -m cardinal_cfn.image_manifest satellite > generated-templates/satellite-images.txt
```

- [ ] **Step 2: Run the full build and verify the manifest is emitted**

Run: `make build`
Expected: build completes; `generated-templates/satellite-images.txt` exists.

Run: `cat generated-templates/satellite-images.txt`
Expected: `public.ecr.aws/cardinalhq.io/cardinalhq-otel-collector:v1.8.0`

- [ ] **Step 3: Verify cfn-lint is still clean on the satellite template**

Run: `source .venv/bin/activate && cfn-lint generated-templates/cardinal-satellite-services.yaml`
Expected: no errors (exit 0). Warnings only if pre-existing.

- [ ] **Step 4: Commit**

```bash
git add build.sh
git commit -m "build: emit generated-templates/satellite-images.txt"
```

---

### Task 6: Operator documentation

**Files:**
- Create: `docs/air-gapped-images.md`

- [ ] **Step 1: Write the doc**

Create `docs/air-gapped-images.md`:

```markdown
# Air-gapped image mirroring

Air-gapped installs cannot pull container images from the public registries.
This page lists the images each Cardinal stack runs and shows how to point the
stack at a private mirror.

> Scope: this covers the **satellite** stack (`cardinal-satellite-services`).
> The central Lakerunner stack's images are covered in a later release.

## Images the satellite stack runs

The satellite collector runs a single container image. The canonical,
machine-readable list is generated at build time:

`generated-templates/satellite-images.txt`

For the current release that is:

- `public.ecr.aws/cardinalhq.io/cardinalhq-otel-collector:v1.8.0` — the otel
  collector that receives telemetry and writes to the satellite raw bucket.

This file always lists the upstream public references — the images to pull,
scan, and mirror *from* — regardless of any `ImageRegistry` override you set at
deploy time.

## Mirroring

Pull each image listed in `satellite-images.txt`, scan it, and push it into
your private registry under a single namespace prefix. For example, with
[skopeo](https://github.com/containers/skopeo) and a mirror prefix of
`mirror.corp/cardinal`:

```sh
PREFIX=mirror.corp/cardinal
while read -r img; do
  name=${img##*/}                     # cardinalhq-otel-collector:v1.8.0
  skopeo copy "docker://${img}" "docker://${PREFIX}/${name}"
done < generated-templates/satellite-images.txt
```

## Pointing the stack at your mirror

Two override paths on `cardinal-satellite-services`, highest precedence first:

1. `OtelImage` — a full image URI that overrides the otel image outright. Wins
   over `ImageRegistry`. Example: `registry.internal/team/otel:v1.8.0`.
2. `ImageRegistry` — a registry/namespace prefix applied to first-party
   Cardinal images. When set, the otel image resolves to
   `${ImageRegistry}/cardinalhq-otel-collector:v1.8.0`. This matches the
   flattened layout produced by the mirror loop above (set
   `ImageRegistry=mirror.corp/cardinal`).

Leave both empty to pull from the public default.
```

- [ ] **Step 2: Commit**

```bash
git add docs/air-gapped-images.md
git commit -m "docs: air-gapped image mirroring guide (satellite)"
```

---

### Task 7: Changelog entry

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Determine the next version**

Run: `grep -m1 '^## v0.0.' CHANGELOG.md`
Note the highest version shown (e.g. `## v0.0.124`). The new entry uses the
next patch number (e.g. `v0.0.125`). Call it `vNEW` below.

- [ ] **Step 2: Insert the new entry**

Insert a new section directly above the current top `## v0.0.NNN` heading (i.e.
after the preamble paragraph that ends with the "Earliest recorded version"
line), using the next version number determined in Step 1:

```markdown
## vNEW

- **New `ImageRegistry` parameter on `cardinal-satellite-services`** for
  air-gapped installs. When set, first-party Cardinal images (currently the
  otel collector) resolve to `<ImageRegistry>/<image>:<tag>` — e.g.
  `mirror.corp/cardinal/cardinalhq-otel-collector:v1.8.0`. Leave empty to pull
  from the public default. The existing `OtelImage` full-URI override still
  wins over `ImageRegistry`.
- **`OtelImage` default is now empty.** Empty means "resolve from
  `ImageRegistry` or the public default" — no behavior change for installs that
  set neither parameter; the collector still runs the public default image.
- A generated `satellite-images.txt` (in `generated-templates/`) lists the
  upstream image(s) to mirror and scan. See `docs/air-gapped-images.md`.
- No data-bearing resource is replaced. Upgrade action: none, unless you are
  mirroring images for an air-gapped install.
```

(Replace `vNEW` with the actual `## v0.0.NNN` heading from Step 1.)

- [ ] **Step 3: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs: changelog entry for satellite ImageRegistry parameter"
```

---

### Task 8: Final full verification

- [ ] **Step 1: Run the full build (generate + lint)**

Run: `make build`
Expected: all templates generated; `generated-templates/satellite-images.txt` present; cfn-lint reports no errors.

- [ ] **Step 2: Run the full test suite**

Run: `make test`
Expected: all tests pass (helper unit + per-template).

- [ ] **Step 3: Sanity-check the generated satellite template**

Run: `grep -n "ImageRegistry\|OtelImage\|cardinalhq-otel-collector" generated-templates/cardinal-satellite-services.yaml | head`
Expected: shows the `ImageRegistry` and `OtelImage` parameters and the `Fn::If` / `Fn::Sub` image resolution.

- [ ] **Step 4: Commit any regenerated artifacts (if the repo tracks them)**

Run: `git status --short`
If `generated-templates/` is tracked and changed, commit it:

```bash
git add generated-templates/
git commit -m "build: regenerate satellite template + image manifest"
```

If `generated-templates/` is gitignored, skip this step.

---

## Self-Review Notes

- **Spec coverage:** resolution model (Task 2, 3), shared helper (Task 1, 2), manifest generator (Task 4), build wiring (Task 5), doc (Task 6), changelog (Task 7), testing — suffix derivation + manifest + satellite precedence + lint (Tasks 1-5, 8). The `OtelImage` default flip and parameter-group update are in Task 3.
- **Type consistency:** `flatten_suffix(image_ref)` and `add_registry_image(t, *, name, default, description, registry_param="ImageRegistry")` and `manifest_lines(stack)` / `STACK_IMAGE_KEYS` are used identically wherever referenced.
- **Out of scope (Phase 2):** main `lakerunner_services.py` images, busybox/`db_init` utility-image handling, extending the manifest/doc to all six images.
