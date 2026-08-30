#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="${1:-local}"
IMAGE="miru/server:${VERSION}"
NAME="miru-smoke-$$"
HOST_PORT="${MIRU_DOCKER_HOST_PORT:-5081}"
CONTAINER_PORT="${MIRU_DOCKER_CONTAINER_PORT:-5001}"
SERVER_IP="${SERVER_IP:-127.0.0.1}"
SERVER_PORT="${SERVER_PORT:-$HOST_PORT}"
ENV_FILE="${MIRU_DOCKER_ENV_FILE:-${ROOT}/.env}"
KEEP="${MIRU_DOCKER_KEEP:-0}"
ALLOW_NO_API="${MIRU_DOCKER_ALLOW_NO_API:-0}"
API_CONFIG_KEYS=(
  AI_CHAT_HOST AI_CHAT_KEY AI_CHAT_MODEL
  AI_MEMORY_HOST AI_MEMORY_KEY AI_MEMORY_MODEL
  AI_VISION_HOST AI_VISION_KEY AI_VISION_MODEL
)
TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/miru-docker-smoke.XXXXXX")"
DATA_DIR="${TMP_ROOT}/data"
LOG_DIR="${TMP_ROOT}/logs"
SSE_LOG="${TMP_ROOT}/sse.log"

cleanup() {
  if command -v docker >/dev/null 2>&1; then
    docker rm -f "$NAME" >/dev/null 2>&1 || true
  fi
  if [[ "$KEEP" != "1" ]]; then
    rm -rf "$TMP_ROOT"
  else
    echo "[docker-smoke] Kept temp dir: $TMP_ROOT"
  fi
}
trap cleanup EXIT

die() {
  echo "[docker-smoke] ERROR: $*" >&2
  if command -v docker >/dev/null 2>&1; then
    docker logs --tail 120 "$NAME" >&2 || true
  fi
  exit 1
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "$1 command not found"
}

env_value() {
  local key="$1"
  local value="${!key:-}"
  if [[ -n "$value" ]]; then
    printf '%s' "$value"
    return
  fi
  if [[ -f "$ENV_FILE" ]]; then
    awk -F= -v k="$key" '
      $0 !~ /^[[:space:]]*#/ && $1 == k {
        sub(/^[^=]*=/, "", $0);
        gsub(/^[[:space:]]+|[[:space:]]+$/, "", $0);
        gsub(/^"|"$/, "", $0);
        gsub(/^'\''|'\''$/, "", $0);
        print $0;
        exit
      }
    ' "$ENV_FILE"
  fi
}

valid_secret() {
  local value="$1"
  [[ -n "$value" && "$value" != "sk-or-your-openrouter-key" && "$value" != "your-api-key" ]]
}

check_api_config() {
  local missing=()
  for key in "${API_CONFIG_KEYS[@]}"; do
    local value
    value="$(env_value "$key")"
    if [[ "$key" == *_KEY ]]; then
      valid_secret "$value" || missing+=("$key")
    elif [[ -z "$value" ]]; then
      missing+=("$key")
    fi
  done
  if (( ${#missing[@]} > 0 )); then
    if [[ "$ALLOW_NO_API" == "1" ]]; then
      echo "[docker-smoke] WARNING: missing API config (${missing[*]}), running boot/login-only smoke"
      return 1
    fi
    die "missing API config for full smoke: ${missing[*]}. Set MIRU_DOCKER_ENV_FILE or export env vars."
  fi
  return 0
}

wait_http() {
  local url="$1"
  local seconds="${2:-60}"
  for _ in $(seq 1 "$seconds"); do
    if curl --noproxy '*' -fsS --max-time 2 "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

json_get() {
  local key="$1"
  python3 -c 'import json,sys; print((json.load(sys.stdin).get(sys.argv[1]) or ""))' "$key"
}

wait_chat_reply() {
  local base="$1"
  local token="$2"
  local timeout="${3:-180}"
  for _ in $(seq 1 "$timeout"); do
    if curl --noproxy '*' -fsS --max-time 8 -H "Authorization: Bearer ${token}" \
        "${base}/api/chat/history?limit=30" |
      python3 -c 'import json,sys; data=json.load(sys.stdin); raise SystemExit(0 if any(m.get("role")=="assistant" for m in data) else 1)' \
        >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

wait_usage_label() {
  local regex="$1"
  local timeout="${2:-180}"
  local path="${DATA_DIR}/_admin/llm_usage.jsonl"
  for _ in $(seq 1 "$timeout"); do
    if [[ -f "$path" ]] && python3 - "$path" "$regex" <<'PY' >/dev/null 2>&1
import json, re, sys
path, pattern = sys.argv[1], re.compile(sys.argv[2])
try:
    for line in open(path, encoding="utf-8"):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        if pattern.search(str(row.get("call_label", ""))):
            raise SystemExit(0)
except FileNotFoundError:
    pass
raise SystemExit(1)
PY
    then
      return 0
    fi
    sleep 1
  done
  return 1
}

wait_file_json_condition() {
  local path="$1"
  local expr="$2"
  local timeout="${3:-90}"
  for _ in $(seq 1 "$timeout"); do
    if [[ -f "$path" ]] && python3 - "$path" "$expr" <<'PY' >/dev/null 2>&1
import json, sys
path, expr = sys.argv[1], sys.argv[2]
data = json.load(open(path, encoding="utf-8"))
raise SystemExit(0 if eval(expr, {"data": data}) else 1)
PY
    then
      return 0
    fi
    sleep 1
  done
  return 1
}

make_test_screenshot() {
  local out="$1"
  local py="${PYTHON_BIN:-}"
  if [[ -z "$py" ]]; then
    if [[ -x "${ROOT}/.venv/bin/python" ]]; then
      py="${ROOT}/.venv/bin/python"
    else
      py="python3"
    fi
  fi
  "$py" - "$out" <<'PY'
from PIL import Image, ImageDraw
import sys
out = sys.argv[1]
img = Image.new("RGB", (1280, 720), "#f7f7f7")
d = ImageDraw.Draw(img)
lines = [
    "Miru Docker Smoke Test",
    "VS Code: deploy/Dockerfile, scripts/docker_init.py",
    "Task: implement Miru Server Docker image",
    "Status: testing screenshot upload, AttentionEngine, Memory pipeline",
    "This screen is intentionally semantic and project-related.",
]
y = 90
for i, line in enumerate(lines):
    d.text((90, y), line, fill="#111111")
    y += 82
d.rectangle((70, 60, 1210, 650), outline="#3483fa", width=5)
img.save(out, "JPEG", quality=90)
PY
}

need_cmd docker
need_cmd curl
need_cmd python3

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  die "image ${IMAGE} not found. Run: bash deploy/docker_build.sh ${VERSION}"
fi

full_api=1
check_api_config || full_api=0

mkdir -p "$DATA_DIR" "$LOG_DIR"
env_args=()
if [[ -f "$ENV_FILE" ]]; then
  env_args+=(--env-file "$ENV_FILE")
fi
for key in "${API_CONFIG_KEYS[@]}"; do
  if [[ -n "${!key:-}" ]]; then
    # Passing only the variable name keeps secret values out of docker's argv.
    env_args+=(-e "$key")
  fi
done

echo "[docker-smoke] Starting ${IMAGE} on http://${SERVER_IP}:${SERVER_PORT}"
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
  "$IMAGE" >/dev/null

BASE="http://${SERVER_IP}:${SERVER_PORT}"
wait_http "${BASE}/api/health" 90 || die "health check did not become ready"

BOOTSTRAP="${DATA_DIR}/_admin/docker_bootstrap.json"
[[ -f "$BOOTSTRAP" ]] || die "docker bootstrap file missing"
INVITE="$(python3 - "$BOOTSTRAP" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8")).get("current_full_code", ""))
PY
)"
[[ "$INVITE" == MIRU-* ]] || die "invalid invitation code in bootstrap"
echo "[docker-smoke] Invitation generated: ${INVITE}"

LOGIN_JSON="$(curl --noproxy '*' -fsS --max-time 12 -H 'Content-Type: application/json' \
  -d "{\"code\":\"${INVITE}\"}" "${BASE}/api/auth/login")"
TOKEN="$(printf '%s' "$LOGIN_JSON" | json_get token)"
USER_ID="$(printf '%s' "$LOGIN_JSON" | json_get user_id)"
[[ -n "$TOKEN" && -n "$USER_ID" ]] || die "login did not return token/user_id"
echo "[docker-smoke] Login ok for ${USER_ID}"

curl --noproxy '*' -Ns --max-time 8 "${BASE}/api/events?token=${TOKEN}&device_id=docker_smoke_sse" >"$SSE_LOG" 2>/dev/null &
SSE_PID=$!
sleep 2

if [[ "$full_api" == "1" ]]; then
  echo "[docker-smoke] Sending real chat message"
  curl --noproxy '*' -fsS --max-time 12 \
    -H "Authorization: Bearer ${TOKEN}" \
    -H 'Content-Type: application/json' \
    -d '{"text":"Docker smoke test：请用一句话回复我，确认这个 Miru 私有服务器镜像的主 agent 正常。"}' \
    "${BASE}/api/chat" >/dev/null
  wait_chat_reply "$BASE" "$TOKEN" 180 || die "assistant reply not observed in chat history"
  wait_usage_label 'agent_chat_iter' 180 || die "agent_chat_iter usage label not observed"
  echo "[docker-smoke] Chat agent ok"

  SCREENSHOT="${TMP_ROOT}/docker-smoke-screen.jpg"
  make_test_screenshot "$SCREENSHOT"
  echo "[docker-smoke] Uploading test screenshot"
  curl --noproxy '*' -fsS --max-time 20 \
    -H "Authorization: Bearer ${TOKEN}" \
    -F "device_id=docker_smoke_android" \
    -F "captured_at=$(date -u '+%Y-%m-%dT%H:%M:%S')" \
    -F "image=@${SCREENSHOT};type=image/jpeg" \
    "${BASE}/api/device/screenshot" >/dev/null

  USER_DIR="${DATA_DIR}/users/${USER_ID}"
  wait_file_json_condition "${USER_DIR}/screenshot_log.json" 'isinstance(data, dict) and any(isinstance(v, list) and len(v) >= 1 for v in data.values())' 60 \
    || die "screenshot_log.json did not record upload"
  wait_usage_label 'ScreenObservationVLM' 150 || die "ScreenObservationVLM usage label not observed"
  wait_usage_label 'AttentionEngineEvaluate' 150 || die "AttentionEngineEvaluate usage label not observed"
  wait_usage_label 'ScreenSemanticGate|ScreenSlotWriterV3|Pass4Append:screenshot' 180 \
    || die "screen memory gate/writer usage label not observed"
  wait_file_json_condition "${USER_DIR}/screen_semantic_gate_log.json" 'isinstance(data, dict) and any(any(isinstance(e, dict) and e.get("passed_gate") for e in entries) for entries in data.values() if isinstance(entries, list))' 90 \
    || die "screen_semantic_gate_log.json did not record a passed gate event"
  echo "[docker-smoke] Screenshot, AttentionEngine, and screen-memory path ok"
fi

if ps -p "$SSE_PID" >/dev/null 2>&1; then
  wait "$SSE_PID" || true
fi
if ! grep -q "connected" "$SSE_LOG" 2>/dev/null; then
  die "SSE connected event not observed"
fi
echo "[docker-smoke] SSE ok"

echo "[docker-smoke] Restarting container to verify persistence"
docker restart "$NAME" >/dev/null
wait_http "${BASE}/api/health" 90 || die "health check did not recover after restart"

LOGIN_JSON_2="$(curl --noproxy '*' -fsS --max-time 12 -H 'Content-Type: application/json' \
  -d "{\"code\":\"${INVITE}\"}" "${BASE}/api/auth/login")"
USER_ID_2="$(printf '%s' "$LOGIN_JSON_2" | json_get user_id)"
[[ "$USER_ID_2" == "$USER_ID" ]] || die "restart login returned different user (${USER_ID_2} != ${USER_ID})"

BOOTSTRAP_INVITE_2="$(python3 - "$BOOTSTRAP" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8")).get("current_full_code", ""))
PY
)"
[[ "$BOOTSTRAP_INVITE_2" == "$INVITE" ]] || die "invitation changed after restart"
echo "[docker-smoke] Persistence ok"

echo "[docker-smoke] PASS"
