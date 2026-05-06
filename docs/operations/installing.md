# Installing Cardinal lakerunner -- Phase 1 (preview)

This is the in-progress runbook for the new layered install model defined
in `docs/superpowers/specs/2026-05-06-cardinal-cfn-prereqs-split-design.md`.

**Status:** Phase 1 ships the two privileged-identity setup scripts that
the new model depends on. The CFN-stack refactor (a single `cardinal-
infra-app.yaml` + `cardinal-lakerunner.yaml` to replace the current root
+ twelve nested children) is Phase 2 and is not yet available.

While Phase 2 is in flight, install lakerunner via the existing
`scripts/deploy-lakerunner.sh` against the current
`cardinal-lakerunner.yaml` root. The new scripts described here are for
review and dry-running only.

## New artifacts in Phase 1

`make build` now also emits two POSIX shell scripts alongside the
existing CFN templates:

| Artifact | Purpose |
|---|---|
| `generated-templates/cardinal-prereqs.sh` | Privileged-identity script that creates the four IAM roles and three security groups Cardinal needs. Idempotent ensure-* helpers; no update path. |
| `generated-templates/cardinal-data-setup.sh` | Privileged-identity script that creates the data-bearing resources (RDS, S3 ingest bucket + lifecycle, SQS queue + policy, S3->SQS notification, license/internal-keys/admin-key/maestro-db secrets, two SSM params). Same idempotency model. |

Both scripts print a `Key=Value` block to stdout and (with
`--output-file PATH`) write a JSON document indexed by CFN parameter
name. That file is the manual handoff to the Phase 2 CFN stacks once
they exist.

## Phase 1 dry-run

To inspect what the scripts would do without executing them, generate
them and read the source:

```sh
make build
less generated-templates/cardinal-prereqs.sh
less generated-templates/cardinal-data-setup.sh
```

Both are POSIX-shell, depend only on `aws` v2 and `jq` (data-setup
additionally needs `openssl`), and contain no Python or other build-
time dependencies.

## Why this is split into two phases

Refactoring the existing twelve nested-children CFN templates into the
new two-flat-stacks shape is a large-surface change that benefits from
landing on its own. Phase 1 lays the foundation -- shared IAM-policy
builders, the new naming/tag conventions, both shell-script generators
-- so Phase 2's PR is a focused refactor rather than a refactor + new-
feature combination.

See `docs/superpowers/plans/2026-05-06-cardinal-cfn-prereqs-split.md`
for the full implementation plan including the deferred Phase 2 tasks.
