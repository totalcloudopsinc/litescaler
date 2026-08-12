#!/usr/bin/env bash
set -euo pipefail

# Build and push the lite-scaler image.
#
# Usage: ./build.sh [tag]
#   env overrides: IMAGE_NAME, IMAGE_TAG, REGISTRY, PLATFORM
#
# The platform is pinned to linux/amd64 on purpose. The cluster runs amd64 nodes
# (standard-v3 / highfreq-v3), but this is usually built on an arm64 Mac, where a
# native build pushes without complaint and then crash-loops at runtime with
# "exec format error". Pinning it costs a little build time via emulation and
# removes a failure that only shows up in the cluster.

IMAGE_NAME="${IMAGE_NAME:-lite-scaler}"
IMAGE_TAG="${1:-${IMAGE_TAG:-latest}}"
REGISTRY="${REGISTRY:-cr.yandex/crpcvsde88m85as3hdu0}"
PLATFORM="${PLATFORM:-linux/amd64}"

FULL_IMAGE="${REGISTRY}/${IMAGE_NAME}:${IMAGE_TAG}"

# Also tag with the commit so a running pod can be traced back to a revision;
# the overlays reference :latest, so that tag has to move too.
NAMES=("${FULL_IMAGE}")
if GIT_SHA="$(git rev-parse --short HEAD 2>/dev/null)"; then
  [[ -n "$(git status --porcelain 2>/dev/null)" ]] && GIT_SHA="${GIT_SHA}-dirty"
  NAMES+=("${REGISTRY}/${IMAGE_NAME}:${GIT_SHA}")
fi

TAGS=()
for n in "${NAMES[@]}"; do TAGS+=(-t "$n"); done

docker info >/dev/null 2>&1 || {
  echo "ERROR: Docker daemon is not reachable — start Docker Desktop first." >&2
  exit 1
}

# Pushing to cr.yandex needs the credential helper wired into ~/.docker/config.json.
grep -q "cr.yandex" ~/.docker/config.json 2>/dev/null || {
  echo "ERROR: no cr.yandex credentials in ~/.docker/config.json." >&2
  echo "       Run: yc container registry configure-docker" >&2
  exit 1
}

echo "Building for ${PLATFORM}"
printf '  tag: %s\n' "${NAMES[@]}"

# buildx (not plain `docker build`) so --platform cross-builds and pushes a
# manifest the amd64 nodes can actually pull.
docker buildx build \
  --platform "${PLATFORM}" \
  "${TAGS[@]}" \
  --push \
  .

echo "Pushed: ${FULL_IMAGE}"
