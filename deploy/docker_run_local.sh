#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="${1:-local}"
IMAGE="miru/server:${VERSION}"
NAME="${MIRU_DOCKER_NAME:-miru-server-local}"
HOST_PORT="${MIRU_DOCKER_HOST_PORT:-5001}"
CONTAINER_PORT="${MIRU_DOCKER_CONTAINER_PORT:-5001}"
SERVER_IP="${SERVER_IP:-127.0.0.1}"
SERVER_PORT="${SERVER_PORT:-$HOST_PORT}"
DATA_DIR="${MIRU_DOCKER_DATA_DIR:-${ROOT}/.docker/miru-data}"
LOG_DIR="${MIRU_DOCKER_LOG_DIR:-${ROOT}/.docker/miru-logs}"
ENV_FILE="${MIRU_DOCKER_ENV_FILE:-${ROOT}/.env}"

if ! command -v docker >/dev/null 2>&1; then
  echo "[docker-run] docker command not found. Install/start Docker Desktop first." >&2
  exit 127
fi

mkdir -p "$DATA_DIR" "$LOG_DIR"

env_args=()
if [[ -f "$ENV_FILE" ]]; then
  env_args+=(--env-file "$ENV_FILE")
fi

echo "[docker-run] Starting ${NAME} from ${IMAGE}"
echo "[docker-run] Host URL: http://${SERVER_IP}:${SERVER_PORT}"
echo "[docker-run] Data: ${DATA_DIR}"
echo "[docker-run] Logs: ${LOG_DIR}"

docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d \
  --name "$NAME" \
  ${env_args[@]+"${env_args[@]}"} \
  -e DATA_DIR=/opt/miru/data \
  -e LOG_DIR=/opt/miru/logs \
  -e MIRU_HEADLESS=1 \
  -e FLASK_DEBUG=false \
  -e PORT="$CONTAINER_PORT" \
  -e SERVER_IP="$SERVER_IP" \
  -e SERVER_PORT="$SERVER_PORT" \
  -p "${HOST_PORT}:${CONTAINER_PORT}" \
  -v "${DATA_DIR}:/opt/miru/data" \
  -v "${LOG_DIR}:/opt/miru/logs" \
  "$IMAGE"

echo "[docker-run] Waiting for invitation code..."
for _ in $(seq 1 40); do
  if [[ -f "${DATA_DIR}/_admin/docker_bootstrap.json" ]]; then
    code="$(python3 - "$DATA_DIR/_admin/docker_bootstrap.json" "$SERVER_IP" "$SERVER_PORT" <<'PY'
import json, sys
payload = json.load(open(sys.argv[1], encoding="utf-8"))
expected_ip = sys.argv[2]
expected_port = int(sys.argv[3])
if payload.get("server_ip") == expected_ip and int(payload.get("server_port") or 0) == expected_port:
    print(payload.get("current_full_code", ""))
PY
)"
    if [[ -n "$code" ]]; then
      echo "[docker-run] Invitation code: ${code}"
      echo "[docker-run] Open: http://${SERVER_IP}:${SERVER_PORT}/login"
      exit 0
    fi
  fi
  sleep 1
done

echo "[docker-run] Container started, but no invitation code appeared yet. Recent logs:" >&2
docker logs --tail 80 "$NAME" >&2 || true
exit 1
