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
scan, and mirror *from* — regardless of which image you actually deploy.

## Mirroring

Pull the image listed in `satellite-images.txt`, scan it, and push it into
your private registry. For example, with
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

Image selection lives in the deploy script, not the CloudFormation template:
the template takes a literal `OtelImage` parameter, and
`deploy-satellite-services.sh` passes whatever you choose.

Set `OTEL_IMAGE` to the full URI of your mirrored image when running the
deploy script:

```sh
OTEL_IMAGE=mirror.corp/cardinal/cardinalhq-otel-collector:v1.8.0 \
  STACK_NAME=... REGION=... VERSION=... \
  ./scripts/deploy-satellite-services.sh
```

Leave `OTEL_IMAGE` unset to pull the template's public default.
