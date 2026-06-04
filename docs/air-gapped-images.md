# Air-gapped image mirroring

Air-gapped installs cannot pull container images from the public registries.
This page lists the images each Cardinal stack runs and shows how to point the
deploy drivers at a private mirror.

## How image selection works

The deploy drivers, not the operator, own image identity. For our first-party
images on **public ECR** (`public.ecr.aws/cardinalhq.io/*`), the repository path
and pinned tag/digest are **baked into the published driver** (single-sourced
from `cardinal-defaults.yaml`); the operator supplies only `IMAGE_REGISTRY` —
the registry/prefix to pull from. The driver composes
`${IMAGE_REGISTRY}/<locked-path>:<tag>@<digest>` and passes it as a literal
stack parameter. This keeps the driver + stack the supported, version-locked
deploy path (no console deploys, no hand-edited image URLs).

`IMAGE_REGISTRY` defaults to `public.ecr.aws`. Point it at an ECR pull-through
cache root (or any mirror that preserves the upstream path) and every
first-party image resolves under it with one knob.

### External / utility images are special-cased

Two images are not on our public ECR and are **not** governed by
`IMAGE_REGISTRY`. They keep their own full-URI override env vars (default: the
template default), so you mirror them however your registry organizes
third-party content:

- `busybox` (`public.ecr.aws/docker/library/busybox`, the dex-init shell) —
  override with `DEX_INIT_IMAGE`.
- `initcontainer-grafana` (`ghcr.io/cardinalhq/...`, the db-init psql client) —
  override with `DB_INIT_IMAGE`.

## Image lists (mirror + scan)

Generated at build time, one ref per line — the upstream public references to
pull, scan, and mirror FROM (independent of `IMAGE_REGISTRY`):

- `generated-templates/satellite-images.txt` — the satellite stack.
- `generated-templates/lakerunner-images.txt` — the lakerunner application stack.

First-party images are digest-pinned (multi-arch index digests; tasks run
ARM64). Current refs:

| Stack | Image | IMAGE_REGISTRY? |
|---|---|---|
| satellite | `public.ecr.aws/cardinalhq.io/cardinalhq-otel-collector:v1.8.0@sha256:9906…` | yes |
| lakerunner | `public.ecr.aws/cardinalhq.io/lakerunner:v1.40.4@sha256:532a…` | yes |
| lakerunner | `public.ecr.aws/cardinalhq.io/maestro:v1.50.0@sha256:642e…` | yes |
| lakerunner | `public.ecr.aws/cardinalhq.io/dex-customization:v0.1.0@sha256:a1d0…` | yes |
| lakerunner | `public.ecr.aws/docker/library/busybox:1.37` | no — `DEX_INIT_IMAGE` |
| lakerunner | `ghcr.io/cardinalhq/initcontainer-grafana:latest` | no — `DB_INIT_IMAGE` |

## Mirroring with an ECR pull-through cache (recommended)

Create a pull-through cache rule for ECR Public, then point `IMAGE_REGISTRY` at
the cache root. ECR preserves the full upstream path and digest, so the locked
suffixes resolve unchanged:

```sh
aws ecr create-pull-through-cache-rule \
  --ecr-repository-prefix aws-public \
  --upstream-registry-url public.ecr.aws

IMAGE_REGISTRY=<acct>.dkr.ecr.<region>.amazonaws.com/aws-public
```

A single public-ECR pull-through cache covers every first-party image (and the
busybox utility image, if you also point `DEX_INIT_IMAGE` at it). The
`ghcr.io` db-init image needs its own mirror/rule (set `DB_INIT_IMAGE`).

First-pull IAM note: on the first pull of a not-yet-cached image, the ECS task
**execution role** needs `ecr:BatchImportUpstreamImage` plus repository
auto-creation (`ecr:CreateRepository` on the principal, or a registry
repository-creation template) in addition to the usual pull permissions. Once
cached, standard pull permissions suffice.

## Deploying from the mirror

Satellite:

```sh
IMAGE_REGISTRY=<acct>.dkr.ecr.<region>.amazonaws.com/aws-public \
  STACK_NAME=... REGION=... \
  ./scripts/deploy-satellite-services.sh
```

Lakerunner application stack:

```sh
IMAGE_REGISTRY=<acct>.dkr.ecr.<region>.amazonaws.com/aws-public \
  DEX_INIT_IMAGE=<acct>.dkr.ecr.<region>.amazonaws.com/aws-public/docker/library/busybox:1.37 \
  DB_INIT_IMAGE=<your-mirror>/initcontainer-grafana:<tag> \
  STACK_NAME=... REGION=... INFRA_BASE_STACK=... INFRA_RDS_STACK=... \
  SATELLITE_INFRA_BASE_STACK=... CLUSTER_ARN=... CLUSTER_NAME=... \
  VPC_ID=... PRIVATE_SUBNETS=... ORGANIZATION_ID=... DEX_ADMIN_PASSWORD_HASH=... \
  ./scripts/deploy-lakerunner-services.sh
```

Leave `IMAGE_REGISTRY` unset to pull first-party images from the public default.

## Stack version

Every deploy driver bakes its published version at publish time, so a published
driver deploys its own matching templates. `STACK_VERSION` is optional and
defaults to that baked version; set it to target a different published version.
`VERSION` is accepted as a legacy alias.
