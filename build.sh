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

# ...but having the entry is not enough. `docker-credential-yc` resolves cr.yandex
# against the profile pinned in credhelper-config.yaml, NOT the active yc profile.
# On a laptop with several clouds those differ, and the push then dies with an
# opaque "denied: Permission denied" long after the build has finished. The
# mapping is one profile per registry HOST, so two clouds both on cr.yandex cannot
# be served by the helper at the same time.
CREDHELPER_CFG="${HOME}/.config/yandex-cloud/credhelper-config.yaml"
if [[ -f "${CREDHELPER_CFG}" ]]; then
  BOUND="$(sed -nE 's/^cr\.yandex:[[:space:]]*(.+)$/\1/p' "${CREDHELPER_CFG}" | tr -d '[:space:]')"
  ACTIVE="$(yc config profile list 2>/dev/null | sed -nE 's/^([^[:space:]]+)[[:space:]]+ACTIVE.*$/\1/p')"
  if [[ -n "${BOUND}" && -n "${ACTIVE}" && "${BOUND}" != "${ACTIVE}" ]]; then
    echo "WARN: the docker credential helper resolves cr.yandex via yc profile" >&2
    echo "      '${BOUND}', but the active profile is '${ACTIVE}'." >&2
    echo "      If the push below fails with 'denied: Permission denied', log in" >&2
    echo "      explicitly instead — note that the helper takes precedence, so the" >&2
    echo "      cr.yandex entry has to leave credHelpers in ~/.docker/config.json" >&2
    echo "      first (put it back afterwards):" >&2
    echo "        docker login cr.yandex -u iam -p \"\$(yc iam create-token)\"" >&2
  fi
fi

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
