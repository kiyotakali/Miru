#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=deploy/host_manager/common.sh
source "$SCRIPT_DIR/common.sh"

INSTANCE_ID=""
OUTPUT=""
INCLUDE_LOGS=0
NO_ENV=0

usage() {
  cat <<'EOF'
Usage: backup_instance.sh --instance-id ID [options]

Options:
  --home DIR          Host manager directory. Default: /opt/miru-host.
  --instance-id ID    Instance id.
  --output FILE       Backup file path.
  --include-logs      Include instance logs.
  --no-env            Exclude .env from backup.
  -h, --help          Show this help.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --home) MIRU_HOST_HOME="$2"; shift 2 ;;
    --instance-id) INSTANCE_ID="$2"; shift 2 ;;
    --output) OUTPUT="$2"; shift 2 ;;
    --include-logs) INCLUDE_LOGS=1; shift ;;
    --no-env) NO_ENV=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done
[[ -n "$INSTANCE_ID" ]] || die "--instance-id is required"

meta="$(instance_json "$INSTANCE_ID")"
instance_home="$(printf '%s' "$meta" | json_field instance.instance_home)"
args=(--home "$instance_home")
[[ -n "$OUTPUT" ]] && args+=(--output "$OUTPUT")
[[ "$INCLUDE_LOGS" == "1" ]] && args+=(--include-logs)
[[ "$NO_ENV" == "1" ]] && args+=(--no-env)
bash "$SELF_HOST_DIR/backup.sh" "${args[@]}"
