# Single-file deploy drivers (install/update path)

## Problem

The five chained deploy scripts in `scripts/` (`deploy-lakerunner-infra-base`,
`deploy-lakerunner-infra-rds`, `deploy-lakerunner-services`,
`deploy-satellite-infra-base`, `deploy-satellite-services`) are thin wrappers
that `exec` a shared engine, `scripts/deploy-stack.sh`. Two committed files per
deploy is a gitops hazard for customers who copy scripts into their own
environment: copy one, miss the other, and the script breaks.

## Goal

Each per-stack deploy driver in `scripts/` becomes a single, self-contained,
runnable file. Customers see only the fat per-stack drivers. Scope is the
install/update path only; teardown/cleanup and the `deploy-lakerunner.sh`
monolith are out of scope and left untouched.

## Design

Source of truth moves to a new top-level `scripts-src/`:

```
scripts-src/
  build.sh                              # the builder; run via `make scripts`
  parts/
    base.sh                             # the engine (today's deploy-stack.sh)
    deploy-lakerunner-infra-base.sh     # the five front-halves (fragments)
    deploy-lakerunner-infra-rds.sh
    deploy-lakerunner-services.sh
    deploy-satellite-infra-base.sh
    deploy-satellite-services.sh
```

`scripts/` holds the generated, committed, customer-facing drivers (same five
filenames). `scripts/deploy-stack.sh` is removed — the engine now lives only as
`parts/base.sh` and is embedded into each driver.

### Build mechanism

For every `parts/*.sh` except `base.sh`, the builder writes `scripts/<name>.sh`:

1. A fresh `#!/bin/sh`.
2. One comment line marking the file generated (no multi-line header — customers
   will edit these in their environment; the line just states edits are
   overwritten on regenerate).
3. The fragment body, shebang stripped.
4. The engine body (`parts/base.sh`), shebang stripped.

The fragment runs first: it validates its stack-specific env and sets
`TEMPLATE_URL` / `PARAMS` / `FILE_PARAMS` / `FROM_STACKS` / `MAPS`. The embedded
engine then runs in the same shell and consumes them.

### Concatenation-ready fragments

The `parts/` fragments are authored for embedding, not standalone `exec`:

- No `SCRIPT_DIR=` line, no trailing `exec "$SCRIPT_DIR/deploy-stack.sh"`.
- The `services` fragment additionally drops its own `cleanup_cert()` +
  `trap cleanup_cert` (the engine owns the single trap now) and the child-run
  line.

Each fragment keeps its stack-specific `usage()`, argument rejection, and
required-env validation. The engine's generic `usage()`/`--help` short-circuit
are left in the embedded copy as harmless dead code (the fragment validates and
exits before the engine's missing-required path is reachable, so the
stack-specific `usage()` is the one a customer ever sees).

### Single trap, cert temp dir

The combined script has exactly one trap — the engine's. The engine's
`cleanup()` is extended to also `rm -rf` an optional `${cert_dir:-}` so the
services driver's create-time self-signed cert temp dir is still cleaned. In a
standalone engine run (`parts/base.sh`, used by tests) `cert_dir` is unset and
the branch is a no-op.

## Build wiring

- New `make scripts` target runs `scripts-src/build.sh`.
- `build.sh` (the template generator) also invokes the script builder so
  `make build` keeps both templates and drivers current.
- A drift test regenerates the drivers into a temp dir and asserts the committed
  `scripts/*.sh` are byte-identical, the same guard pattern used for templates.

## Tests and docs

- `tests/unit/test_deploy_stack_lint.py`: engine shellcheck + internal-hook
  invocations point at `scripts-src/parts/base.sh`; shellcheck of the shipped
  drivers and the services-token assertions point at the generated
  `scripts/deploy-*.sh`. Add the drift test.
- `docs/operations/jenkins-chained-deploy.md`: note the drivers are now
  self-contained and `deploy-stack.sh` is an internal build input, not a file in
  `scripts/`. The env-var interface is unchanged.

## Non-goals

Teardown/cleanup consolidation, the `deploy-lakerunner.sh` monolith, and any
change to the per-stack env-var interface or chaining order.
