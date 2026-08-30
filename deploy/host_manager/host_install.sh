#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=deploy/host_manager/common.sh
source "$SCRIPT_DIR/common.sh"

SERVER_IP="${SERVER_IP:-}"
PORT_START="$MIRU_HOST_PORT_START"
PORT_END="$MIRU_HOST_PORT_END"
DEFAULT_IMAGE="$MIRU_HOST_DEFAULT_IMAGE"
SKIP_DOCKER_INSTALL=0
FORCE=0

usage() {
  cat <<'EOF'
Usage: host_install.sh [options]

Initializes a Miru host that can run multiple isolated Miru instances.

Options:
  --home DIR             Host manager directory. Default: /opt/miru-host.
  --server-ip IP         Public IPv4 encoded into every generated invite.
  --port-start PORT      First port available for instances. Default: 5001.
  --port-end PORT        Last port available for instances. Default: 5010.
  --default-image IMAGE  Default Miru server image tag. Default: miru/server:<version>.
  --skip-docker-install  Do not install Docker/Compose.
  --force                Replace host.json when values changed.
  -h, --help             Show this help.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --home) MIRU_HOST_HOME="$2"; shift 2 ;;
    --server-ip) SERVER_IP="$2"; shift 2 ;;
    --port-start) PORT_START="$2"; shift 2 ;;
    --port-end) PORT_END="$2"; shift 2 ;;
    --default-image) DEFAULT_IMAGE="$2"; shift 2 ;;
    --skip-docker-install) SKIP_DOCKER_INSTALL=1; shift ;;
    --force) FORCE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

need_cmd python3
if [[ -z "$SERVER_IP" ]]; then
  info "Detecting public IPv4"
  SERVER_IP="$(detect_public_ipv4 || true)"
fi
[[ -n "$SERVER_IP" ]] || die "SERVER_IP is required; pass --server-ip"
is_ipv4 "$SERVER_IP" || die "SERVER_IP must be an IPv4 address, got $SERVER_IP"
is_port "$PORT_START" || die "--port-start must be 1..65535"
is_port "$PORT_END" || die "--port-end must be 1..65535"

if [[ "$SKIP_DOCKER_INSTALL" != "1" ]]; then
  install_docker_if_needed
fi

args=(
  init-host
  --home "$MIRU_HOST_HOME"
  --server-ip "$SERVER_IP"
  --port-start "$PORT_START"
  --port-end "$PORT_END"
  --default-image "$DEFAULT_IMAGE"
)
if [[ "$FORCE" == "1" ]]; then
  args+=(--force)
fi

python3 "$HOST_MANAGER_PY" "${args[@]}"
sync_host_tools_best_effort
