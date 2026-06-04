# Air-gapped image mirroring

Air-gapped installs cannot pull container images from the public registries.
This page lists the images each Cardinal stack runs and shows how to point the
deploy driver at a private mirror.

> Scope: this covers the **satellite** stack (`cardinal-satellite-services`),
> deployed via `deploy-satellite-services.sh`. The central Lakerunner stack's
> images are covered in a later release.

## How image selection works

The deploy driver, not the operator, owns the image identity. The collector's
repository path and pinned tag/digest are **baked into the published driver**
(single-sourced from `cardinal-defaults.yaml`); the operator supplies only the
registry/prefix to pull from. This keeps the driver + stack the supported,
version-locked deploy path — there is no per-image URL to hand-edit and no
console deploy.

The image the driver deploys is:

```
${IMAGE_REGISTRY}/cardinalhq.io/cardinalhq-otel-collector:v1.8.0@sha256:9906…
```

- `IMAGE_REGISTRY` — operator-supplied registry/prefix. Default: `public.ecr.aws`.
- the rest (repo path + pinned tag + digest) — locked in the driver.

## Images the satellite stack runs

The satellite collector runs a single container image. The canonical,
digest-pinned, machine-readable list is generated at build time:

`generated-templates/satellite-images.txt`

For the current release:

- `public.ecr.aws/cardinalhq.io/cardinalhq-otel-collector:v1.8.0@sha256:9906eea2b38f1614047ada60ce7887704652484bb5b01a7f8a1d932277e1f151`
  — the otel collector that receives telemetry and writes to the satellite raw
  bucket. (Multi-arch index digest; the collector task runs ARM64.)

This file always lists the upstream public reference — the image to pull, scan,
and mirror *from* — regardless of `IMAGE_REGISTRY`.

## Mirroring

### Option A — ECR pull-through cache (recommended)

Create a pull-through cache rule for ECR Public, then point `IMAGE_REGISTRY` at
the cache root. ECR preserves the full upstream path and digest, so the locked
suffix resolves unchanged:

```sh
aws ecr create-pull-through-cache-rule \
  --ecr-repository-prefix aws-public \
  --upstream-registry-url public.ecr.aws

IMAGE_REGISTRY=<acct>.dkr.ecr.<region>.amazonaws.com/aws-public
# -> <acct>.dkr.ecr.<region>.amazonaws.com/aws-public/cardinalhq.io/cardinalhq-otel-collector:v1.8.0@sha256:9906…
```

Note: on the *first* pull of a not-yet-cached image, the ECS task **execution
role** needs `ecr:BatchImportUpstreamImage` (and repository auto-creation:
`ecr:CreateRepository` on the principal or a registry repository-creation
template) in addition to the usual pull permissions. Once cached, standard pull
permissions suffice.

### Option B — manual mirror

Pull each image in `satellite-images.txt`, scan it, and push it into your
registry preserving the repo path and digest (e.g. with skopeo). Then set
`IMAGE_REGISTRY` to your registry root.

## Deploying from the mirror

```sh
IMAGE_REGISTRY=<acct>.dkr.ecr.<region>.amazonaws.com/aws-public \
  STACK_NAME=... REGION=... \
  ./scripts/deploy-satellite-services.sh
```

Leave `IMAGE_REGISTRY` unset to pull from the public default.

`STACK_VERSION` is optional and defaults to the version baked into the driver at
publish time, so a published driver deploys its own matching templates. Set
`STACK_VERSION` to deploy a different published version. (`VERSION` is accepted
as a legacy alias.)
