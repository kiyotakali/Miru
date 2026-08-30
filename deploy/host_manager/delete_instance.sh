#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=deploy/host_manager/common.sh
source "$SCRIPT_DIR/common.sh"

INSTANCE_ID=""
ASSUME_YES=0
KEEP_DATA=0

usage() {
  cat <<'EOF'
Usage: delete_instance.sh --instance-id ID [options]

Options:
  --home DIR          Host manager directory. Default: /opt/miru-host.
  --instance-id ID    Instance id.
  --keep-data         Stop container and free port but keep instance directory.
  --yes               Do not prompt before destructive action.
  -h, --help          Show this help.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --home) MIRU_HOST_HOME="$2"; shift 2 ;;
    --instance-id) INSTANCE_ID="$2"; shift 2 ;;
    --keep-data) KEEP_DATA=1; shift ;;
    --yes) ASSUME_YES=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done
[[ -n "$INSTANCE_ID" ]] || die "--instance-id is required"

meta="$(instance_json "$INSTANCE_ID")"
instance_home="$(printf '%s' "$meta" | json_field instance.instance_home)"
compose_project="$(printf '%s' "$meta" | json_field instance.compose_project)"

if [[ "$KEEP_DATA" == "1" ]]; then
  confirm_or_die "Delete ledger and stop instance $INSTANCE_ID, keeping files. Type DELETE to continue:" "DELETE" "$ASSUME_YES"
else
  confirm_or_die "Delete instance $INSTANCE_ID and all its data. Type DELETE to continue:" "DELETE" "$ASSUME_YES"
fi

compose_down_instance "$instance_home" "$compose_project"
if [[ "$KEEP_DATA" != "1" && -d "$instance_home" ]]; then
  remove_instance_dir "$instance_home"
fi
python3 "$HOST_MANAGER_PY" remove-instance --home "$MIRU_HOST_HOME" --instance-id "$INSTANCE_ID"
