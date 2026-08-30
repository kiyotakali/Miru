#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="${1:-}"

if [[ -z "$VERSION" ]]; then
  echo "Usage: bash deploy/docker_build.sh <version>" >&2
  echo "Example: bash deploy/docker_build.sh 0.2.0" >&2
  exit 2
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "[docker-build] docker command not found. Install/start Docker Desktop first." >&2
  exit 127
fi

IMAGE="miru/server:${VERSION}"
PLATFORM="${MIRU_DOCKER_PLATFORM:-}"
if [[ -n "$PLATFORM" ]]; then
  echo "[docker-build] Building ${IMAGE} for ${PLATFORM}"
  docker buildx build \
    --platform "${PLATFORM}" \
    --load \
    --file "${ROOT}/deploy/Dockerfile" \
    --tag "${IMAGE}" \
    "${ROOT}"
else
  echo "[docker-build] Building ${IMAGE}"
  docker build \
    --file "${ROOT}/deploy/Dockerfile" \
    --tag "${IMAGE}" \
    "${ROOT}"
fi

echo "[docker-build] Built ${IMAGE}"
