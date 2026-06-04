"""Image manifest generator.

Emits the upstream public container images a given stack runs, one per line,
sorted and deduped. This is the source list air-gapped customers mirror and
scan FROM; it always reflects the public defaults in cardinal-defaults.yaml,
never a customer's overridden image.
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
