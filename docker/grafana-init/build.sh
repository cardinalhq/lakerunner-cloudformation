#!/bin/bash
# Build script for Grafana Init Container (multi-architecture)

set -e

IMAGE_NAME="public.ecr.aws/cardinalhq.io/lakerunner/initcontainer-grafana:latest"

echo "Building Grafana Init Container for multiple architectures..."

# Create buildx builder if it doesn't exist
if ! docker buildx inspect multiarch >/dev/null 2>&1; then
    echo "Creating buildx builder..."
    docker buildx create --name multiarch --use
fi

# Use the multiarch builder
docker buildx use multiarch

# Build for both AMD64 and ARM64
echo "Building for linux/amd64 and linux/arm64..."
docker buildx build \
    --platform linux/amd64,linux/arm64 \
    --pull \
    -t "$IMAGE_NAME" \
    --push \
    .

echo "Build and push complete!"
echo "Multi-architecture image available at: $IMAGE_NAME"

echo ""
echo "To use locally for testing (single architecture):"
echo "  docker buildx build --platform linux/amd64 --pull -t $IMAGE_NAME --load ."