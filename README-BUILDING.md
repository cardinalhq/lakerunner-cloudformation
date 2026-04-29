# Cardinal CloudFormation — Building & Testing

## Prerequisites

- Python 3.11+
- `make install` to create `.venv` and install `requirements.txt`

## Build

```bash
make build
```

Generates `generated-templates/cardinal-vpc.yaml`, `generated-templates/cardinal-lakerunner.yaml`, and the eleven nested children under `generated-templates/cardinal-lakerunner/`. Runs `cfn-lint` on the lot.

## Test

```bash
make test            # run all tests
make test-unit       # unit tests only
make test-templates  # per-template tests only
make check           # alias for `make test` (pre-push gate)
```

The suite uses pytest + cloud-radar; offline, no AWS credentials needed.

## Project layout

- `src/cardinal_cfn/` — generator package
  - `root.py` — root template (`cardinal-lakerunner.yaml`)
  - `cardinal_vpc.py` — standalone VPC template (`cardinal-vpc.yaml`)
  - `children/` — eleven nested stack generators
  - `defaults.py`, `naming.py`, `parameters.py`, `images.py`, `policies.py`, `install_id.py`, `listener_priorities.py` — shared helpers
- `cardinal-defaults.yaml` — service definitions, image references, API keys seed, storage profile defaults
- `tests/unit/` — helper-level tests
- `tests/templates/` — template-shape tests using `cloud-radar`
- `build.sh` — generator driver (called by `make build`)

## Adding a service

1. Add the service block to `cardinal-defaults.yaml` under `services.<key>`.
1. Decide which tier owns it (query / process / control) and add it in the corresponding `src/cardinal_cfn/children/services_<tier>.py`.
1. If it attaches to the ALB, add a priority to `src/cardinal_cfn/listener_priorities.py`.
1. Add tests under `tests/templates/test_services_<tier>.py`.
1. Run `make build && make test`.

## Releases

Tag a commit `v*` on `main`. The release workflow generates templates with `CARDINAL_VERSION=<tag>` baked into `TemplateBaseUrl`, syncs to `s3://cardinal-cfn/lakerunner/<tag>/` and `s3://cardinal-cfn/lakerunner/latest/`, and creates a GitHub release with the deployable URLs.
