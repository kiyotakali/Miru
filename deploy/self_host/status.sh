#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=deploy/self_host/common.sh
source "$SCRIPT_DIR/common.sh"

JSON=0

usage() {
  cat <<'EOF'
Usage: status.sh [options]

Options:
  --home DIR   Miru install directory. Default: /opt/miru.
  --json       Print machine-readable JSON for provisioning.
  -h, --help   Show this help.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --home) MIRU_HOME="$2"; shift 2 ;;
    --json) JSON=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

local_health="failed"
public_health="unknown"
url="$(server_url)"
host_port="$(server_port_from_env)"
host_port="${host_port:-$SERVER_PORT}"
if curl --noproxy '*' -fsS --max-time 3 "http://127.0.0.1:${host_port}/api/health" >/dev/null 2>&1; then
  local_health="ok"
fi
if [[ -n "$url" ]]; then
  if curl --noproxy '*' -fsS --max-time 3 "${url}/api/health" >/dev/null 2>&1; then
    public_health="ok"
  else
    public_health="failed"
  fi
fi

code="$(invitation_code)"
container_status="unknown"
if command -v docker >/dev/null 2>&1 && [[ -f "$MIRU_HOME/compose.yaml" ]]; then
  if docker compose version >/dev/null 2>&1; then
    container_status="$(cd "$MIRU_HOME" && docker compose -f compose.yaml -p "$MIRU_COMPOSE_PROJECT" ps --format json 2>/dev/null | head -c 2000 || true)"
  elif command -v docker-compose >/dev/null 2>&1; then
    container_status="$(cd "$MIRU_HOME" && docker-compose -f compose.yaml -p "$MIRU_COMPOSE_PROJECT" ps 2>/dev/null | head -c 2000 || true)"
  fi
fi

if [[ "$JSON" == "1" ]]; then
  need_cmd python3
  python3 - "$local_health" "$public_health" "$url" "$code" "$MIRU_HOME" "$container_status" <<'PY'
import json, sys
local_health, public_health, url, code, home, container = sys.argv[1:]
print(json.dumps({
    "ok": local_health == "ok",
    "health": local_health,
    "local_health": local_health,
    "public_health": public_health,
    "server_url": url,
    "invitation_code": code,
    "miru_home": home,
    "container_status": container,
}, ensure_ascii=False))
PY
else
  printf 'Miru home: %s\n' "$MIRU_HOME"
  printf 'Server URL: %s\n' "${url:-unknown}"
  printf 'Health: %s\n' "$local_health"
  printf 'Public health: %s\n' "$public_health"
  printf 'Invitation: %s\n' "${code:-not ready}"
  if command -v docker >/dev/null 2>&1 && [[ -f "$MIRU_HOME/compose.yaml" ]]; then
    (cd "$MIRU_HOME" && compose_cmd ps || true)
  fi
fi
