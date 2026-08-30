#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="${1:-}"

usage() {
  cat <<'EOF'
Usage: bash deploy/docker_push_tcr.sh <version>

Build/tag/push the Miru server image to Tencent Cloud TCR.

Environment:
  TCR_REGISTRY       Default: ccr.ccs.tencentyun.com
  TCR_NAMESPACE      Default: contextlife
  TCR_REPOSITORY     Default: miru-server
  TCR_USERNAME       Optional. If set with TCR_PASSWORD, docker login runs.
  TCR_PASSWORD       Optional. Passed to docker login via stdin.
  MIRU_DOCKER_PLATFORM  Default for release: linux/amd64
  PUSH_STABLE        Set to 1 to also push :stable.

Example:
  TCR_NAMESPACE=contextlife TCR_USERNAME=... TCR_PASSWORD=... \
    bash deploy/docker_push_tcr.sh 0.1.0
EOF
}

if [[ -z "$VERSION" || "$VERSION" == "-h" || "$VERSION" == "--help" ]]; then
  usage
  [[ -n "$VERSION" ]] && exit 0 || exit 2
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "[tcr-push] docker command not found. Install/start Docker Desktop first." >&2
  exit 127
fi

TCR_REGISTRY="${TCR_REGISTRY:-ccr.ccs.tencentyun.com}"
TCR_NAMESPACE="${TCR_NAMESPACE:-contextlife}"
TCR_REPOSITORY="${TCR_REPOSITORY:-miru-server}"
LOCAL_IMAGE="miru/server:${VERSION}"
REMOTE_IMAGE="${TCR_REGISTRY}/${TCR_NAMESPACE}/${TCR_REPOSITORY}:${VERSION}"
REMOTE_STABLE="${TCR_REGISTRY}/${TCR_NAMESPACE}/${TCR_REPOSITORY}:stable"

if [[ -n "${TCR_USERNAME:-}" || -n "${TCR_PASSWORD:-}" ]]; then
  if [[ -z "${TCR_USERNAME:-}" || -z "${TCR_PASSWORD:-}" ]]; then
    echo "[tcr-push] TCR_USERNAME and TCR_PASSWORD must be set together." >&2
    exit 2
  fi
  echo "[tcr-push] Logging in to ${TCR_REGISTRY} as ${TCR_USERNAME}"
  printf '%s' "$TCR_PASSWORD" | docker login "$TCR_REGISTRY" \
    --username "$TCR_USERNAME" \
    --password-stdin
fi

if ! docker image inspect "$LOCAL_IMAGE" >/dev/null 2>&1; then
  echo "[tcr-push] Local image ${LOCAL_IMAGE} not found; building linux/amd64."
  MIRU_DOCKER_PLATFORM="${MIRU_DOCKER_PLATFORM:-linux/amd64}" \
    bash "$ROOT/deploy/docker_build.sh" "$VERSION"
fi

echo "[tcr-push] Tagging ${LOCAL_IMAGE} -> ${REMOTE_IMAGE}"
docker tag "$LOCAL_IMAGE" "$REMOTE_IMAGE"
echo "[tcr-push] Pushing ${REMOTE_IMAGE}"
docker push "$REMOTE_IMAGE"

if [[ "${PUSH_STABLE:-0}" == "1" ]]; then
  echo "[tcr-push] Tagging ${LOCAL_IMAGE} -> ${REMOTE_STABLE}"
  docker tag "$LOCAL_IMAGE" "$REMOTE_STABLE"
  echo "[tcr-push] Pushing ${REMOTE_STABLE}"
  docker push "$REMOTE_STABLE"
fi

cat <<EOF
[tcr-push] Done.
MIRU_IMAGE=${REMOTE_IMAGE}
EOF
