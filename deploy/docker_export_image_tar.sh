#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="${1:-}"
OUT_DIR="${2:-$ROOT/dist}"

usage() {
  cat <<'EOF'
Usage: bash deploy/docker_export_image_tar.sh <version> [output-dir]

Export miru/server:<version> as a gzip-compressed docker save archive for
GitHub Releases or other static downloads.

Recommended release flow:
  MIRU_DOCKER_PLATFORM=linux/amd64 bash deploy/docker_build.sh 0.1.0
  bash deploy/docker_export_image_tar.sh 0.1.0

Output:
  dist/miru-server-<version>-linux-amd64.tar.gz
  dist/miru-server-<version>-linux-amd64.tar.gz.sha256
EOF
}

if [[ -z "$VERSION" || "$VERSION" == "-h" || "$VERSION" == "--help" ]]; then
  usage
  [[ -n "$VERSION" ]] && exit 0 || exit 2
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "[docker-export] docker command not found. Install/start Docker Desktop first." >&2
  exit 127
fi

IMAGE="miru/server:${VERSION}"
ARCHIVE="miru-server-${VERSION}-linux-amd64.tar.gz"
OUT_PATH="${OUT_DIR}/${ARCHIVE}"

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "[docker-export] Local image ${IMAGE} not found. Build it first:" >&2
  echo "  MIRU_DOCKER_PLATFORM=linux/amd64 bash deploy/docker_build.sh ${VERSION}" >&2
  exit 2
fi

mkdir -p "$OUT_DIR"
echo "[docker-export] Exporting ${IMAGE} -> ${OUT_PATH}"
docker save "$IMAGE" | gzip -c >"$OUT_PATH"

if command -v shasum >/dev/null 2>&1; then
  (cd "$OUT_DIR" && shasum -a 256 "$ARCHIVE" >"${ARCHIVE}.sha256")
elif command -v sha256sum >/dev/null 2>&1; then
  (cd "$OUT_DIR" && sha256sum "$ARCHIVE" >"${ARCHIVE}.sha256")
else
  echo "[docker-export] WARNING: shasum/sha256sum not found; checksum skipped" >&2
fi

cat <<EOF
[docker-export] Done.
ARCHIVE=${OUT_PATH}
EOF
