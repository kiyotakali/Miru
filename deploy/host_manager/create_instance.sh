#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=deploy/host_manager/common.sh
source "$SCRIPT_DIR/common.sh"

INSTANCE_ID=""
SERVER_IP=""
REQUESTED_PORT=""
IMAGE=""
IMAGE_TAR=""
ENV_FILE=""
ALLOW_NO_API=0
SKIP_PULL="${MIRU_SKIP_PULL:-0}"
KEEP_FAILED=0
JSON_OUTPUT=0

usage() {
  cat <<'EOF'
Usage: create_instance.sh [options]

Creates one isolated Miru Docker instance on this host and returns its invite.

Options:
  --home DIR            Host manager directory. Default: /opt/miru-host.
  --instance-id ID      Optional stable instance id.
  --server-ip IP        Override host public IPv4 for this instance.
  --port PORT           Allocate a specific public port.
  --image IMAGE         Miru server image. Default comes from host.json.
  --image-tar FILE      Load this docker save tar before starting Miru.
  --env-file FILE       Source AI_* environment values from a private env file.
  --allow-no-api        Allow boot/login-only install without model keys.
  --skip-pull           Use a preloaded local image instead of docker pull.
  --keep-failed         Keep failed instance directory and ledger for debugging.
  --json                Print machine-readable JSON only.
  -h, --help            Show this help.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --home) MIRU_HOST_HOME="$2"; shift 2 ;;
    --instance-id) INSTANCE_ID="$2"; shift 2 ;;
    --server-ip) SERVER_IP="$2"; shift 2 ;;
    --port) REQUESTED_PORT="$2"; shift 2 ;;
    --image) IMAGE="$2"; shift 2 ;;
    --image-tar) IMAGE_TAR="$2"; shift 2 ;;
    --env-file) ENV_FILE="$2"; shift 2 ;;
    --allow-no-api) ALLOW_NO_API=1; shift ;;
    --skip-pull) SKIP_PULL=1; shift ;;
    --keep-failed) KEEP_FAILED=1; shift ;;
    --json) JSON_OUTPUT=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

need_cmd python3
sync_host_tools_best_effort

load_ai_env_file() {
  local file="$1"
  local line key value
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    [[ -z "$line" || "$line" == \#* || "$line" != *=* ]] && continue
    key="${line%%=*}"
    value="${line#*=}"
    key="${key#"${key%%[![:space:]]*}"}"
    key="${key%"${key##*[![:space:]]}"}"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    value="${value%\"}"
    value="${value#\"}"
    value="${value%\'}"
    value="${value#\'}"
    case "$key" in
      AI_VISION_HOST|AI_VISION_KEY|AI_VISION_MODEL|\
      AI_CHAT_HOST|AI_CHAT_KEY|AI_CHAT_MODEL|\
      AI_MEMORY_HOST|AI_MEMORY_KEY|AI_MEMORY_MODEL)
        export "$key=$value"
        ;;
    esac
  done <"$file"
}

if [[ -n "$ENV_FILE" ]]; then
  [[ -f "$ENV_FILE" ]] || die "env file not found: $ENV_FILE"
  load_ai_env_file "$ENV_FILE"
fi

alloc_args=(allocate-instance --home "$MIRU_HOST_HOME")
[[ -n "$INSTANCE_ID" ]] && alloc_args+=(--instance-id "$INSTANCE_ID")
[[ -n "$SERVER_IP" ]] && alloc_args+=(--server-ip "$SERVER_IP")
[[ -n "$REQUESTED_PORT" ]] && alloc_args+=(--port "$REQUESTED_PORT")
[[ -n "$IMAGE" ]] && alloc_args+=(--image "$IMAGE")

allocation="$(python3 "$HOST_MANAGER_PY" "${alloc_args[@]}")"
instance_id="$(printf '%s' "$allocation" | json_field instance.instance_id)"
instance_home="$(printf '%s' "$allocation" | json_field instance.instance_home)"
server_ip="$(printf '%s' "$allocation" | json_field instance.server_ip)"
server_port="$(printf '%s' "$allocation" | json_field instance.server_port)"
image="$(printf '%s' "$allocation" | json_field instance.image)"
container_name="$(printf '%s' "$allocation" | json_field instance.container_name)"
compose_project="$(printf '%s' "$allocation" | json_field instance.compose_project)"

cleanup_failed() {
  local code=$?
  if [[ "$code" == "0" ]]; then
    return
  fi
  if [[ "$KEEP_FAILED" == "1" ]]; then
    python3 "$HOST_MANAGER_PY" mark-instance --home "$MIRU_HOST_HOME" \
      --instance-id "$instance_id" --status failed --error "create_instance.sh failed with exit $code" >/dev/null || true
    return
  fi
  compose_down_instance "$instance_home" "$compose_project" || true
  if [[ -n "$instance_home" && -d "$instance_home" ]]; then
    remove_instance_dir "$instance_home" || true
  fi
  python3 "$HOST_MANAGER_PY" remove-instance --home "$MIRU_HOST_HOME" --instance-id "$instance_id" >/dev/null || true
}
trap cleanup_failed EXIT

install_args=(
  --non-interactive
  --home "$instance_home"
  --server-ip "$server_ip"
  --server-port "$server_port"
  --image "$image"
  --skip-docker-install
  --force
)
[[ "$ALLOW_NO_API" == "1" ]] && install_args+=(--allow-no-api)
[[ "$SKIP_PULL" == "1" ]] && install_args+=(--skip-pull)
[[ -n "$IMAGE_TAR" ]] && install_args+=(--image-tar "$IMAGE_TAR")

if [[ "$JSON_OUTPUT" != "1" ]]; then
  info "Creating instance $instance_id on ${server_ip}:${server_port}"
fi

install_log="$(mktemp)"
if ! MIRU_CONTAINER_NAME="$container_name" \
  MIRU_COMPOSE_PROJECT="$compose_project" \
  bash "$SELF_HOST_DIR/install.sh" "${install_args[@]}" >"$install_log" 2>&1; then
  cat "$install_log" >&2 || true
  rm -f "$install_log"
  exit 1
fi
install_output="$(cat "$install_log")"
rm -f "$install_log"
invite="$(printf '%s\n' "$install_output" | awk -F= '$1 == "MIRU_INVITATION_CODE" {print $2; exit}')"
if [[ -z "$invite" ]]; then
  invite="$(MIRU_COMPOSE_PROJECT="$compose_project" bash "$SELF_HOST_DIR/status.sh" --home "$instance_home" --json | python3 -c 'import json,sys; print(json.load(sys.stdin).get("invitation_code",""))')"
fi
[[ "$invite" == MIRU-* ]] || die "instance started but invitation code was not found"

ready="$(python3 "$HOST_MANAGER_PY" mark-instance --home "$MIRU_HOST_HOME" \
  --instance-id "$instance_id" --status ready --invitation-code "$invite")"
trap - EXIT

if [[ "$JSON_OUTPUT" == "1" ]]; then
  printf '%s\n' "$ready"
else
  printf '%s\n' "$install_output"
  printf 'MIRU_INSTANCE_ID=%s\n' "$instance_id"
  printf '%s\n' "$ready"
fi
