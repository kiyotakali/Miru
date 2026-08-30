#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=deploy/host_manager/common.sh
source "$SCRIPT_DIR/common.sh"

SERVER_IP="${SERVER_IP:-}"
PORT_START=5101
PORT_END=5108
IMAGE="${MIRU_HOST_DEFAULT_IMAGE}"
IMAGE_TAR=""
ENV_FILE=""
PREFIX="e2e"
ALLOW_NO_API=0
SKIP_PULL="${MIRU_SKIP_PULL:-0}"
KEEP=0
CLEAN_EXISTING=0
FULL_API=1
WORK_DIR=""

usage() {
  cat <<'EOF'
Usage: e2e_four_instances.sh [options]

Runs Milestone E: create four isolated Miru instances on one host and verify
health, login, cross-instance isolation, SSE, optional LLM smoke, and
per-instance lifecycle operations.

Options:
  --home DIR             Host manager directory. Default: /opt/miru-host.
  --server-ip IP         Public IPv4 encoded into generated invites.
  --port-start PORT      First host port. Default: 5101.
  --port-end PORT        Last host port. Default: 5108.
  --image IMAGE          Miru server image tag. Default: miru/server:<version>.
  --image-tar FILE       Load this docker save tar when creating instances.
  --env-file FILE        Private env file with AI_VISION/CHAT/MEMORY config.
  --prefix NAME          Instance prefix. Default: e2e.
  --allow-no-api         Boot/login-only mode when API keys are absent.
  --skip-pull            Use a preloaded image.
  --no-api-smoke         Skip chat/screenshot/LLM checks.
  --clean-existing       Delete instances matching this prefix before running.
  --keep                 Keep created instances after the test.
  --work-dir DIR         Directory for temporary evidence files.
  -h, --help             Show this help.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --home) MIRU_HOST_HOME="$2"; shift 2 ;;
    --server-ip) SERVER_IP="$2"; shift 2 ;;
    --port-start) PORT_START="$2"; shift 2 ;;
    --port-end) PORT_END="$2"; shift 2 ;;
    --image) IMAGE="$2"; shift 2 ;;
    --image-tar) IMAGE_TAR="$2"; shift 2 ;;
    --env-file) ENV_FILE="$2"; shift 2 ;;
    --prefix) PREFIX="$2"; shift 2 ;;
    --allow-no-api) ALLOW_NO_API=1; shift ;;
    --skip-pull) SKIP_PULL=1; shift ;;
    --no-api-smoke) FULL_API=0; shift ;;
    --clean-existing) CLEAN_EXISTING=1; shift ;;
    --keep) KEEP=1; shift ;;
    --work-dir) WORK_DIR="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

need_cmd python3
need_cmd curl
need_cmd docker

if [[ -z "$SERVER_IP" ]]; then
  info "Detecting public IPv4"
  SERVER_IP="$(detect_public_ipv4 || true)"
fi
[[ -n "$SERVER_IP" ]] || die "SERVER_IP is required; pass --server-ip"
is_ipv4 "$SERVER_IP" || die "SERVER_IP must be IPv4, got $SERVER_IP"
is_port "$PORT_START" || die "--port-start must be 1..65535"
is_port "$PORT_END" || die "--port-end must be 1..65535"
[[ "$PREFIX" =~ ^[a-z0-9][a-z0-9-]{1,24}$ ]] || die "--prefix must be lowercase letters, digits, hyphen"
if [[ -n "$ENV_FILE" ]]; then
  [[ -f "$ENV_FILE" ]] || die "env file not found: $ENV_FILE"
elif [[ "$FULL_API" == "1" && "$ALLOW_NO_API" != "1" ]]; then
  warn "No --env-file provided; create_instance.sh will rely on exported AI_* env vars"
fi

if [[ -z "$WORK_DIR" ]]; then
  WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/miru-host-e2e.XXXXXX")"
else
  mkdir -p "$WORK_DIR"
fi
SUMMARY="$WORK_DIR/summary.json"
SENSITIVE="$WORK_DIR/sensitive"
mkdir -p "$SENSITIVE"
chmod 700 "$SENSITIVE"

IDS=("${PREFIX}-a" "${PREFIX}-b" "${PREFIX}-c" "${PREFIX}-d")
REPLACEMENT="${PREFIX}-e"
TOKENS=()
USERS=()
URLS=()
PORTS=()
HOMES=()
INVITES=()
CREATED=()

mask_secret() {
  local value="$1"
  local n=${#value}
  if (( n <= 8 )); then
    printf '***'
  else
    printf '%s...%s' "${value:0:5}" "${value: -4}"
  fi
}

json_get() {
  local expr="$1"
  local payload
  payload="$(cat)"
  python3 - "$expr" "$payload" <<'PY'
import json
import sys
data = json.loads(sys.argv[2])
value = data
for part in sys.argv[1].split("."):
    if isinstance(value, dict):
        value = value.get(part, "")
    else:
        value = ""
        break
print("" if value is None else value)
PY
}

http_code() {
  local method="$1"
  local url="$2"
  shift 2
  curl --noproxy '*' -sS -o /dev/null -w '%{http_code}' -X "$method" "$@" "$url" || true
}

wait_login_rejected() {
  local base="$1"
  local invite="$2"
  local timeout="${3:-45}"
  local code
  for _ in $(seq 1 "$timeout"); do
    code="$(http_code POST "${base}/api/auth/login" -H 'Content-Type: application/json' -d "{\"code\":\"${invite}\"}")"
    if [[ "$code" == "200" ]]; then
      return 1
    fi
    if [[ "$code" == "401" || "$code" == "403" ]]; then
      return 0
    fi
    sleep 1
  done
  return 2
}

wait_http() {
  local url="$1"
  local seconds="${2:-90}"
  for _ in $(seq 1 "$seconds"); do
    if curl --noproxy '*' -fsS --max-time 3 "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

wait_chat_reply() {
  local base="$1"
  local token="$2"
  local prompt="$3"
  local timeout="${4:-180}"
  local history
  for _ in $(seq 1 "$timeout"); do
    history="$(curl --noproxy '*' -fsS --max-time 8 -H "Authorization: Bearer ${token}" \
        "${base}/api/chat/history?limit=30" 2>/dev/null || true)"
    if python3 - "$prompt" "$history" <<'PY' >/dev/null 2>&1; then
import json
import sys

prompt = sys.argv[1]
data = json.loads(sys.argv[2] or "[]")
seen_prompt = False
proactive_types = {"proactive", "proactive_care", "care"}
for msg in data:
    text = msg.get("text") or msg.get("content") or msg.get("message") or ""
    if msg.get("role") == "user" and prompt in text:
        seen_prompt = True
        continue
    if (
        seen_prompt
        and msg.get("role") == "assistant"
        and msg.get("type") not in proactive_types
    ):
        raise SystemExit(0)
raise SystemExit(1)
PY
      return 0
    fi
    sleep 1
  done
  return 1
}

wait_usage_label() {
  local path="$1"
  local regex="$2"
  local timeout="${3:-180}"
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

make_test_png() {
  local out="$1"
  python3 - "$out" <<'PY'
import struct
import sys
import zlib

out = sys.argv[1]
w, h = 900, 520
rows = []
for y in range(h):
    row = bytearray()
    for x in range(w):
        bg = 248 if (x // 40 + y // 40) % 2 == 0 else 235
        r, g, b = bg, bg, bg
        if 40 < x < 860 and 40 < y < 480:
            r, g, b = 252, 252, 248
        # Draw blocky "MIRU E2E" style bars, enough to be nonblank.
        if 110 < y < 160 and any(a < x < b for a, b in [(100,130),(145,175),(210,240),(275,305),(340,430),(470,560),(600,690)]):
            r, g, b = 20, 40, 70
        if 230 < y < 310 and 100 < x < 800 and (x + y) % 17 < 8:
            r, g, b = 35, 110, 220
        if 360 < y < 420 and 100 < x < 800:
            r, g, b = 245, 210, 90
        row.extend([r, g, b])
    rows.append(b"\x00" + bytes(row))

def chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)

raw = b"".join(rows)
png = b"\x89PNG\r\n\x1a\n"
png += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
png += chunk(b"IDAT", zlib.compress(raw, 6))
png += chunk(b"IEND", b"")
open(out, "wb").write(png)
PY
}

cleanup_instances() {
  if [[ "$KEEP" == "1" ]]; then
    info "Keeping e2e instances under $MIRU_HOST_HOME; token evidence remains in $SENSITIVE"
    return
  fi
  if (( ${#CREATED[@]} == 0 )); then
    return
  fi
  for id in "${CREATED[@]}"; do
    bash "$SCRIPT_DIR/delete_instance.sh" --home "$MIRU_HOST_HOME" --instance-id "$id" --yes >/dev/null 2>&1 || true
  done
}
trap cleanup_instances EXIT

delete_if_exists() {
  local id="$1"
  if python3 "$HOST_MANAGER_PY" get-instance --home "$MIRU_HOST_HOME" --instance-id "$id" >/dev/null 2>&1; then
    bash "$SCRIPT_DIR/delete_instance.sh" --home "$MIRU_HOST_HOME" --instance-id "$id" --yes >/dev/null
  fi
}

if [[ "$CLEAN_EXISTING" == "1" ]]; then
  for id in "${IDS[@]}" "$REPLACEMENT"; do
    delete_if_exists "$id"
  done
fi

info "Initializing host manager at $MIRU_HOST_HOME"
bash "$SCRIPT_DIR/host_install.sh" \
  --home "$MIRU_HOST_HOME" \
  --server-ip "$SERVER_IP" \
  --port-start "$PORT_START" \
  --port-end "$PORT_END" \
  --default-image "$IMAGE" \
  --skip-docker-install >/dev/null

create_args_base=(--home "$MIRU_HOST_HOME" --image "$IMAGE" --json)
[[ "$ALLOW_NO_API" == "1" ]] && create_args_base+=(--allow-no-api)
[[ "$SKIP_PULL" == "1" ]] && create_args_base+=(--skip-pull)
[[ -n "$IMAGE_TAR" ]] && create_args_base+=(--image-tar "$IMAGE_TAR")
[[ -n "$ENV_FILE" ]] && create_args_base+=(--env-file "$ENV_FILE")

for id in "${IDS[@]}"; do
  info "Creating $id"
  out="$WORK_DIR/create-${id}.json"
  bash "$SCRIPT_DIR/create_instance.sh" "${create_args_base[@]}" --instance-id "$id" >"$out"
  CREATED+=("$id")
  inst="$(json_get instance <"$out")"
  url="$(json_get instance.server_url <"$out")"
  port="$(json_get instance.server_port <"$out")"
  home="$(json_get instance.instance_home <"$out")"
  invite="$(json_get instance.invitation_code <"$out")"
  [[ -n "$url" && -n "$port" && "$invite" == MIRU-* ]] || die "$id create output incomplete"
  URLS+=("$url")
  PORTS+=("$port")
  HOMES+=("$home")
  INVITES+=("$invite")
done

python3 "$HOST_MANAGER_PY" audit --home "$MIRU_HOST_HOME" >"$WORK_DIR/audit-after-create.json"
python3 - "$WORK_DIR/audit-after-create.json" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
if not data.get("ok"):
    raise SystemExit(data)
PY

python3 - "$WORK_DIR" "${PORTS[@]}" <<'PY'
import sys
ports = sys.argv[2:]
if len(set(ports)) != len(ports):
    raise SystemExit(f"duplicate ports: {ports}")
PY

for i in "${!IDS[@]}"; do
  id="${IDS[$i]}"
  url="${URLS[$i]}"
  invite="${INVITES[$i]}"
  info "Checking health/login/SSE for $id at $url"
  wait_http "$url/api/health" 120 || die "$id health failed"
  login="$WORK_DIR/login-${id}.json"
  curl --noproxy '*' -fsS --max-time 15 -H 'Content-Type: application/json' \
    -d "{\"code\":\"${invite}\"}" "$url/api/auth/login" >"$login"
  token="$(json_get token <"$login")"
  user_id="$(json_get user_id <"$login")"
  [[ -n "$token" && -n "$user_id" ]] || die "$id login missing token/user_id"
  printf '%s' "$token" >"$SENSITIVE/token-${id}"
  chmod 600 "$SENSITIVE/token-${id}"
  TOKENS+=("$token")
  USERS+=("$user_id")

  me="$(curl --noproxy '*' -fsS --max-time 8 -H "Authorization: Bearer ${token}" "$url/api/auth/me")"
  [[ "$(printf '%s' "$me" | json_get user_id)" == "$user_id" ]] || die "$id /api/auth/me user mismatch"

  sse_log="$WORK_DIR/sse-${id}.log"
  curl --noproxy '*' -Ns --max-time 8 "${url}/api/events?token=${token}&device_id=milestone_e_${id}" >"$sse_log" 2>/dev/null || true
  grep -q "connected" "$sse_log" || die "$id SSE connected event not observed"
done

info "Checking cross-instance auth rejection"
code="$(http_code GET "${URLS[1]}/api/auth/me" -H "Authorization: Bearer ${TOKENS[0]}")"
[[ "$code" == "401" ]] || die "token from ${IDS[0]} unexpectedly worked on ${IDS[1]} (HTTP $code)"
code="$(http_code POST "${URLS[1]}/api/auth/login" -H 'Content-Type: application/json' -d "{\"code\":\"${INVITES[0]}\"}")"
[[ "$code" == "401" ]] || die "invite from ${IDS[0]} unexpectedly worked on ${IDS[1]} (HTTP $code)"

if [[ "$FULL_API" == "1" ]]; then
  for i in "${!IDS[@]}"; do
    id="${IDS[$i]}"
    url="${URLS[$i]}"
    token="${TOKENS[$i]}"
    home="${HOMES[$i]}"
    user_id="${USERS[$i]}"
    info "Running chat/screenshot smoke for $id"
    prompt_text="Milestone E ${id} smoke：请用一句话确认这个独立 Miru instance 在线。"
    curl --noproxy '*' -fsS --max-time 15 \
      -H "Authorization: Bearer ${token}" \
      -H 'Content-Type: application/json' \
      -d "{\"text\":\"${prompt_text}\"}" \
      "$url/api/chat" >/dev/null
    wait_chat_reply "$url" "$token" "$prompt_text" 180 || die "$id assistant reply not observed"
    wait_usage_label "$home/data/_admin/llm_usage.jsonl" 'agent_chat_iter' 180 || die "$id agent_chat_iter usage not observed"

    shot="$WORK_DIR/screenshot-${id}.png"
    make_test_png "$shot"
    curl --noproxy '*' -fsS --max-time 30 \
      -H "Authorization: Bearer ${token}" \
      -F "device_id=milestone_e_${id}" \
      -F "captured_at=$(date -u '+%Y-%m-%dT%H:%M:%S')" \
      -F "image=@${shot};type=image/png" \
      "$url/api/device/screenshot" >/dev/null
    wait_file_json_condition "$home/data/users/${user_id}/screenshot_log.json" \
      'isinstance(data, dict) and any(isinstance(v, list) and len(v) >= 1 for v in data.values())' 90 \
      || die "$id screenshot_log did not record upload"
    wait_usage_label "$home/data/_admin/llm_usage.jsonl" 'ScreenObservationVLM' 180 || die "$id ScreenObservationVLM usage not observed"
    wait_usage_label "$home/data/_admin/llm_usage.jsonl" 'AttentionEngineEvaluate' 180 || die "$id AttentionEngineEvaluate usage not observed"
  done
fi

info "Checking reset isolation on ${IDS[1]}"
old_invite_b="${INVITES[1]}"
bash "$SCRIPT_DIR/reset_instance.sh" --home "$MIRU_HOST_HOME" --instance-id "${IDS[1]}" --user-data --yes >/dev/null
wait_http "${URLS[1]}/api/health" 90 || die "${IDS[1]} health did not recover after reset"
status_b="$WORK_DIR/status-${IDS[1]}-after-reset.json"
bash "$SCRIPT_DIR/status_instance.sh" --home "$MIRU_HOST_HOME" --instance-id "${IDS[1]}" >"$status_b"
new_invite_b="$(json_get runtime.invitation_code <"$status_b")"
[[ "$new_invite_b" == MIRU-* && "$new_invite_b" != "$old_invite_b" ]] || die "reset did not produce a new invite"
if ! wait_login_rejected "${URLS[1]}" "$old_invite_b" 45; then
  die "old invite still works or reset endpoint stayed unreachable after reset"
fi
curl --noproxy '*' -fsS --max-time 12 -H 'Content-Type: application/json' \
  -d "{\"code\":\"${new_invite_b}\"}" "${URLS[1]}/api/auth/login" >/dev/null
for idx in 0 2 3; do
  wait_http "${URLS[$idx]}/api/health" 60 || die "${IDS[$idx]} unhealthy after reset of ${IDS[1]}"
done

info "Checking backup/restore isolation on ${IDS[2]}"
marker="${HOMES[2]}/data/milestone_e_marker.txt"
printf 'marker-%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%S')" >"$marker"
backup_out="$WORK_DIR/backup-${IDS[2]}.txt"
bash "$SCRIPT_DIR/backup_instance.sh" --home "$MIRU_HOST_HOME" --instance-id "${IDS[2]}" >"$backup_out"
backup_file="$(awk -F= '$1=="MIRU_BACKUP_FILE"{print $2}' "$backup_out" | tail -1)"
[[ -f "$backup_file" ]] || die "backup file missing for ${IDS[2]}"
rm -f "$marker"
bash "$SCRIPT_DIR/restore_instance.sh" "$backup_file" --home "$MIRU_HOST_HOME" --instance-id "${IDS[2]}" --yes >/dev/null
[[ -f "$marker" ]] || die "restore did not bring marker back for ${IDS[2]}"
for idx in 0 1 3; do
  wait_http "${URLS[$idx]}/api/health" 60 || die "${IDS[$idx]} unhealthy after restore of ${IDS[2]}"
done

info "Checking update isolation on ${IDS[3]}"
bash "$SCRIPT_DIR/update_instance.sh" --home "$MIRU_HOST_HOME" --instance-id "${IDS[3]}" --image "$IMAGE" --skip-pull --skip-backup >/dev/null
wait_http "${URLS[3]}/api/health" 90 || die "${IDS[3]} unhealthy after update"
for idx in 0 1 2; do
  wait_http "${URLS[$idx]}/api/health" 60 || die "${IDS[$idx]} unhealthy after update of ${IDS[3]}"
done

info "Checking delete/recreate port reuse for ${IDS[0]}"
freed_port="${PORTS[0]}"
old_home_a="${HOMES[0]}"
old_user_a="${USERS[0]}"
bash "$SCRIPT_DIR/delete_instance.sh" --home "$MIRU_HOST_HOME" --instance-id "${IDS[0]}" --yes >/dev/null
CREATED=("${IDS[1]}" "${IDS[2]}" "${IDS[3]}")
[[ ! -d "$old_home_a" ]] || die "deleted instance home still exists: $old_home_a"
replacement_json="$WORK_DIR/create-${REPLACEMENT}.json"
bash "$SCRIPT_DIR/create_instance.sh" "${create_args_base[@]}" --instance-id "$REPLACEMENT" >"$replacement_json"
CREATED+=("$REPLACEMENT")
replacement_port="$(json_get instance.server_port <"$replacement_json")"
replacement_home="$(json_get instance.instance_home <"$replacement_json")"
replacement_url="$(json_get instance.server_url <"$replacement_json")"
[[ "$replacement_port" == "$freed_port" ]] || die "replacement did not reuse freed port ($replacement_port != $freed_port)"
[[ ! -d "$replacement_home/data/users/$old_user_a" ]] || die "replacement reused deleted user's data"
wait_http "$replacement_url/api/health" 120 || die "replacement health failed"

python3 "$HOST_MANAGER_PY" audit --home "$MIRU_HOST_HOME" >"$WORK_DIR/audit-final.json"
python3 - "$WORK_DIR/audit-final.json" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
if not data.get("ok"):
    raise SystemExit(data)
PY

stats_file="$WORK_DIR/docker-stats.txt"
docker stats --no-stream --format 'table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}' \
  "miru-${IDS[1]}" "miru-${IDS[2]}" "miru-${IDS[3]}" "miru-${REPLACEMENT}" >"$stats_file" 2>/dev/null || true

python3 - "$SUMMARY" "$MIRU_HOST_HOME" "$SERVER_IP" "$IMAGE" "$WORK_DIR" "$stats_file" "$KEEP" <<'PY'
import json
import pathlib
import sys

summary, home, ip, image, work, stats_file, keep = sys.argv[1:]
records = []
for path in sorted(pathlib.Path(work).glob("create-*.json")):
    data = json.load(open(path, encoding="utf-8"))
    inst = data.get("instance", {})
    if not inst:
        continue
    invite = inst.get("invitation_code", "")
    inst = {k: v for k, v in inst.items() if k != "invitation_code"}
    inst["invitation_code_masked"] = invite[:5] + "..." + invite[-4:] if invite else ""
    records.append(inst)
payload = {
    "ok": True,
    "milestone": "E",
    "host_home": home,
    "server_ip": ip,
    "image": image,
    "work_dir": work,
    "kept_instances": keep == "1",
    "instances_seen": records,
    "docker_stats": pathlib.Path(stats_file).read_text(encoding="utf-8") if pathlib.Path(stats_file).exists() else "",
}
json.dump(payload, open(summary, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
PY

info "Milestone E PASS. Evidence: $WORK_DIR"
