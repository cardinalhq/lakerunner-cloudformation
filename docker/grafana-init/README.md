# Grafana Init Container

This directory contains the Dockerfile and scripts for building the Grafana initialization container used in the Lakerunner CloudFormation deployment.

## Purpose

The init container handles:
- Setting up Grafana datasource provisioning directories
- Writing datasource configuration for the Cardinal Lakerunner plugin
- Implementing data reset functionality via reset tokens
- Ensuring proper filesystem permissions and directory structure

## Building the Image

To build and push the multi-architecture init container image:

```bash
# Login to ECR (if not already authenticated)
aws ecr-public get-login-password --region us-east-1 | docker login --username AWS --password-stdin public.ecr.aws

# Build and push multi-architecture image (AMD64 + ARM64)
./build.sh
```

For local development and testing:

```bash
# Build for current architecture only (for local testing)
docker buildx build --platform linux/amd64 -t public.ecr.aws/cardinalhq.io/lakerunner-grafana-initcontainer:latest --load .
```

## Usage

This image is used automatically by the Grafana CloudFormation stack as an init container. It runs before the main Grafana container starts and sets up the necessary configuration.

## Environment Variables

The container expects these environment variables:

- `PROVISIONING_DIR`: Grafana provisioning directory (default: `/etc/grafana/provisioning`)
- `RESET_TOKEN`: Optional token to trigger Grafana data reset
- `GRAFANA_DATASOURCE_CONFIG`: YAML configuration for the Cardinal datasource

## Volumes

The container needs access to these mounted volumes:

- `/var/lib/grafana`: Grafana data directory (for reset token file)
- `/etc/grafana/provisioning`: Grafana provisioning directory (to write datasource config)

## Air-gapped Deployment

This approach solves air-gapped deployment issues by:

1. Pre-building the init logic into a container image
2. Using a distroless base image with shell support for minimal attack surface
3. Eliminating the need to download Alpine packages at runtime
4. Providing a self-contained initialization solution