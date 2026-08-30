#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=deploy/host_manager/common.sh
source "$SCRIPT_DIR/common.sh"

INSTANCE_ID=""
BACKUP=""
ASSUME_YES=0

usage() {
  cat <<'EOF'
Usage: restore_instance.sh BACKUP.tar.gz --instance-id ID [options]

Options:
  --home DIR          Host manager directory. Default: /opt/miru-host.
  --instance-id ID    Instance id.
  --yes               Do not prompt before overwriting.
  -h, --help          Show this help.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --home) MIRU_HOST_HOME="$2"; shift 2 ;;
    --instance-id) INSTANCE_ID="$2"; shift 2 ;;
    --yes) ASSUME_YES=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *)
      if [[ -z "$BACKUP" ]]; then
        BACKUP="$1"; shift
      else
        die "unknown option: $1"
      fi
      ;;
  esac
done
[[ -n "$INSTANCE_ID" ]] || die "--instance-id is required"
[[ -n "$BACKUP" ]] || die "backup file is required"

meta="$(instance_json "$INSTANCE_ID")"
instance_home="$(printf '%s' "$meta" | json_field instance.instance_home)"
server_ip="$(printf '%s' "$meta" | json_field instance.server_ip)"
server_port="$(printf '%s' "$meta" | json_field instance.server_port)"
compose_project="$(printf '%s' "$meta" | json_field instance.compose_project)"

args=("$BACKUP" --home "$instance_home" --server-ip "$server_ip" --server-port "$server_port")
[[ "$ASSUME_YES" == "1" ]] && args+=(--yes)
MIRU_COMPOSE_PROJECT="$compose_project" bash "$SELF_HOST_DIR/restore.sh" "${args[@]}"
invite="$(MIRU_COMPOSE_PROJECT="$compose_project" bash "$SELF_HOST_DIR/status.sh" --home "$instance_home" --json | python3 -c 'import json,sys; print(json.load(sys.stdin).get("invitation_code",""))')"
if [[ "$invite" == MIRU-* ]]; then
  python3 "$HOST_MANAGER_PY" mark-instance --home "$MIRU_HOST_HOME" --instance-id "$INSTANCE_ID" --status ready --invitation-code "$invite" >/dev/null
fi
