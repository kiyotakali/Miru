#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=deploy/host_manager/common.sh
source "$SCRIPT_DIR/common.sh"

INSTANCE_ID=""
IMAGE=""
IMAGE_TAR=""
SKIP_BACKUP=0
SKIP_PULL=0

usage() {
  cat <<'EOF'
Usage: update_instance.sh --instance-id ID [options]

Options:
  --home DIR          Host manager directory. Default: /opt/miru-host.
  --instance-id ID    Instance id.
  --image IMAGE       New Miru server image.
  --image-tar FILE    Load this docker save tar before updating.
  --skip-backup       Do not create a pre-update backup.
  --skip-pull         Use a preloaded local image instead of docker pull.
  -h, --help          Show this help.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --home) MIRU_HOST_HOME="$2"; shift 2 ;;
    --instance-id) INSTANCE_ID="$2"; shift 2 ;;
    --image) IMAGE="$2"; shift 2 ;;
    --image-tar) IMAGE_TAR="$2"; shift 2 ;;
    --skip-backup) SKIP_BACKUP=1; shift ;;
    --skip-pull) SKIP_PULL=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done
[[ -n "$INSTANCE_ID" ]] || die "--instance-id is required"

meta="$(instance_json "$INSTANCE_ID")"
instance_home="$(printf '%s' "$meta" | json_field instance.instance_home)"
compose_project="$(printf '%s' "$meta" | json_field instance.compose_project)"
args=(--home "$instance_home")
[[ -n "$IMAGE" ]] && args+=(--image "$IMAGE")
[[ -n "$IMAGE_TAR" ]] && args+=(--image-tar "$IMAGE_TAR")
[[ "$SKIP_BACKUP" == "1" ]] && args+=(--skip-backup)
[[ "$SKIP_PULL" == "1" ]] && args+=(--skip-pull)
MIRU_COMPOSE_PROJECT="$compose_project" bash "$SELF_HOST_DIR/update.sh" "${args[@]}"
if [[ -n "$IMAGE" ]]; then
  python3 "$HOST_MANAGER_PY" set-image --home "$MIRU_HOST_HOME" --instance-id "$INSTANCE_ID" --image "$IMAGE" >/dev/null
fi
