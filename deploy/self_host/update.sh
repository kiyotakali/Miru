#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=deploy/self_host/common.sh
source "$SCRIPT_DIR/common.sh"

NEW_IMAGE=""
SKIP_BACKUP=0
SKIP_PULL="${MIRU_SKIP_PULL:-0}"
IMAGE_TAR="${MIRU_IMAGE_TAR:-}"

usage() {
  cat <<'EOF'
Usage: update.sh [options]

Updates the Miru server image while keeping /opt/miru/data and /opt/miru/logs.

Options:
  --home DIR       Miru install directory. Default: /opt/miru.
  --image IMAGE    New image. Default: MIRU_IMAGE from env/.env.
  --image-tar FILE Load this docker save tar before updating Miru.
  --skip-backup    Do not create a pre-update backup.
  --skip-pull      Use a preloaded local image instead of docker pull.
  -h, --help       Show this help.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --home) MIRU_HOME="$2"; shift 2 ;;
    --image) NEW_IMAGE="$2"; shift 2 ;;
    --image-tar) IMAGE_TAR="$2"; shift 2 ;;
    --skip-backup) SKIP_BACKUP=1; shift ;;
    --skip-pull) SKIP_PULL=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

[[ -f "$MIRU_HOME/.env" ]] || die "$MIRU_HOME/.env does not exist"
[[ -f "$MIRU_HOME/compose.yaml" ]] || die "$MIRU_HOME/compose.yaml does not exist"

if [[ -z "$NEW_IMAGE" ]]; then
  NEW_IMAGE="$(read_env_var MIRU_IMAGE)"
fi
[[ -n "$NEW_IMAGE" ]] || die "no image provided and MIRU_IMAGE missing from .env"

if [[ "$SKIP_BACKUP" != "1" ]]; then
  "$SCRIPT_DIR/backup.sh" --home "$MIRU_HOME" --output "$MIRU_HOME/backups/pre-update-$(timestamp).tar.gz" >/dev/null
fi

if [[ -n "$IMAGE_TAR" ]]; then
  [[ -f "$IMAGE_TAR" ]] || die "image tar not found: $IMAGE_TAR"
  info "Loading docker image from $IMAGE_TAR"
  docker load -i "$IMAGE_TAR" >/dev/null
  SKIP_PULL=1
fi

set_env_var MIRU_IMAGE "$NEW_IMAGE"
if [[ "$SKIP_PULL" == "1" ]]; then
  info "Skipping docker pull for preloaded image $NEW_IMAGE"
else
  info "Pulling $NEW_IMAGE"
  compose_in_home pull
fi
compose_in_home up -d
wait_health 120 || die "Miru health check did not recover after update"

info "Update complete"
"$SCRIPT_DIR/status.sh" --home "$MIRU_HOME"
