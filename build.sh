#!/usr/bin/env bash
set -euo pipefail

# Build the lite-scaller Docker image.
# Usage: ./build.sh [image-name] [tag]
#   or set IMAGE_NAME / IMAGE_TAG env vars.

IMAGE_NAME="${1:-${IMAGE_NAME:-lite-scaller}}"
IMAGE_TAG="${2:-${IMAGE_TAG:-latest}}"
FULL_IMAGE="cr.yandex/crph9fp6537285vojlnc/${IMAGE_NAME}:${IMAGE_TAG}"

echo "Building ${FULL_IMAGE}..."
docker build -t "${FULL_IMAGE}" .
echo "Done: ${FULL_IMAGE}"

docker push "${FULL_IMAGE}"
