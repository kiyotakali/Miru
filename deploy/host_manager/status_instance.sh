#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=deploy/host_manager/common.sh
source "$SCRIPT_DIR/common.sh"

INSTANCE_ID=""

usage() {
  cat <<'EOF'
Usage: status_instance.sh --instance-id ID [options]

Options:
  --home DIR          Host manager directory. Default: /opt/miru-host.
  --instance-id ID    Instance id.
  --json              Print JSON. This is the default; accepted for consistency.
  -h, --help          Show this help.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --home) MIRU_HOST_HOME="$2"; shift 2 ;;
    --instance-id) INSTANCE_ID="$2"; shift 2 ;;
    --json) shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done
[[ -n "$INSTANCE_ID" ]] || die "--instance-id is required"

meta="$(instance_json "$INSTANCE_ID")"
instance_home="$(printf '%s' "$meta" | json_field instance.instance_home)"
compose_project="$(printf '%s' "$meta" | json_field instance.compose_project)"

status_json="$(
  MIRU_COMPOSE_PROJECT="$compose_project" \
  bash "$SELF_HOST_DIR/status.sh" --home "$instance_home" --json 2>/dev/null || true
)"

python3 - "$meta" "$status_json" <<'PY'
import json
import sys

meta = json.loads(sys.argv[1])
raw = sys.argv[2].strip()
status = {}
if raw:
    try:
        status = json.loads(raw)
    except Exception as exc:
        status = {"ok": False, "error": f"invalid status JSON: {exc}", "raw": raw[-500:]}
else:
    status = {"ok": False, "error": "status.sh returned no JSON"}
print(json.dumps({"ok": bool(status.get("ok")), "instance": meta.get("instance"), "runtime": status}, ensure_ascii=False, sort_keys=True))
PY
