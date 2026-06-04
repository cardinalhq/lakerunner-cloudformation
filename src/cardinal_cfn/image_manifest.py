"""Image manifest + image-reference helpers.

`manifest <stack>` emits the upstream public container images a stack runs, one
per line, sorted and deduped -- the source list air-gapped customers mirror and
scan FROM. `suffix <key>` emits an image's registry-relative path (everything
after the registry host), which the deploy drivers bake in so only the registry
prefix is operator-supplied. Both read the pinned defaults in
cardinal-defaults.yaml, so there is a single source of truth for image refs.
"""

import sys

from cardinal_cfn.defaults import load_defaults

# Which cardinal-defaults.yaml images.* keys each stack runs.
# Phase 1: satellite only. Phase 2 adds the lakerunner stack's keys.
STACK_IMAGE_KEYS = {
    "satellite": ["otel"],
}


def image_ref(key: str) -> str:
    """Return the full pinned image reference for an images.* key."""
    images = load_defaults()["images"]
    if key not in images:
        known = ", ".join(sorted(images))
        raise ValueError(f"unknown image key: {key!r} (known: {known})")
    return images[key]


def registry_relative(ref: str) -> str:
    """Return the registry-relative path of an image reference.

    Everything after the registry host (the first '/'). The drivers bake this
    so the operator supplies only the registry/prefix:

        public.ecr.aws/cardinalhq.io/cardinalhq-otel-collector:v1.8.0@sha256:...
            -> cardinalhq.io/cardinalhq-otel-collector:v1.8.0@sha256:...
    """
    if "/" not in ref:
        raise ValueError(f"image reference has no registry component: {ref!r}")
    return ref.split("/", 1)[1]


def manifest_lines(stack: str) -> list:
    """Return the sorted, deduped upstream image refs for a stack."""
    if stack not in STACK_IMAGE_KEYS:
        known = ", ".join(sorted(STACK_IMAGE_KEYS))
        raise ValueError(f"unknown stack: {stack!r} (known: {known})")
    refs = {image_ref(key) for key in STACK_IMAGE_KEYS[stack]}
    return sorted(refs)


def main(argv: list) -> int:
    if len(argv) == 2 and argv[0] == "manifest":
        for line in manifest_lines(argv[1]):
            print(line)
        return 0
    if len(argv) == 2 and argv[0] == "suffix":
        print(registry_relative(image_ref(argv[1])))
        return 0
    print(
        "usage: python3 -m cardinal_cfn.image_manifest manifest <stack>\n"
        "       python3 -m cardinal_cfn.image_manifest suffix <image-key>",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
