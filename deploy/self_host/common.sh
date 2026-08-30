#!/usr/bin/env bash
set -euo pipefail

MIRU_HOME="${MIRU_HOME:-/opt/miru}"
MIRU_IMAGE_VERSION="${MIRU_IMAGE_VERSION:-0.2.0}"
MIRU_DEFAULT_IMAGE="${MIRU_DEFAULT_IMAGE:-miru/server:${MIRU_IMAGE_VERSION}}"
MIRU_IMAGE="${MIRU_IMAGE:-$MIRU_DEFAULT_IMAGE}"
MIRU_CONTAINER_NAME="${MIRU_CONTAINER_NAME:-miru-server}"
MIRU_COMPOSE_PROJECT="${MIRU_COMPOSE_PROJECT:-miru}"
SERVER_PORT="${SERVER_PORT:-5001}"
CONTAINER_PORT="${CONTAINER_PORT:-5001}"

info() { printf '[miru] %s\n' "$*"; }
warn() { printf '[miru] WARNING: %s\n' "$*" >&2; }
die() { printf '[miru] ERROR: %s\n' "$*" >&2; exit 1; }

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "$1 command not found"
}

is_ipv4() {
  local ip="$1"
  [[ "$ip" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || return 1
  local IFS=.
  local a b c d
  read -r a b c d <<<"$ip"
  for octet in "$a" "$b" "$c" "$d"; do
    [[ "$octet" =~ ^[0-9]+$ ]] || return 1
    (( octet >= 0 && octet <= 255 )) || return 1
  done
}

is_port() {
  local port="$1"
  [[ "$port" =~ ^[0-9]+$ ]] || return 1
  (( port >= 1 && port <= 65535 ))
}

compose_cmd() {
  if docker compose version >/dev/null 2>&1; then
    docker compose -f compose.yaml -p "$MIRU_COMPOSE_PROJECT" "$@"
  elif command -v docker-compose >/dev/null 2>&1; then
    docker-compose -f compose.yaml -p "$MIRU_COMPOSE_PROJECT" "$@"
  else
    die "Docker Compose not found"
  fi
}

compose_in_home() {
  (cd "$MIRU_HOME" && compose_cmd "$@")
}

env_file() {
  printf '%s/.env' "$MIRU_HOME"
}

read_env_var() {
  local key="$1"
  local file="${2:-$(env_file)}"
  [[ -f "$file" ]] || return 0
  awk -F= -v k="$key" '
    $0 !~ /^[[:space:]]*#/ && $1 == k {
      sub(/^[^=]*=/, "", $0);
      print $0;
      exit
    }
  ' "$file"
}

set_env_var() {
  local key="$1"
  local value="$2"
  local file="${3:-$(env_file)}"
  mkdir -p "$(dirname "$file")"
  local tmp
  tmp="$(mktemp)"
  if [[ -f "$file" ]]; then
    awk -F= -v k="$key" -v v="$value" '
      BEGIN { done=0 }
      $0 !~ /^[[:space:]]*#/ && $1 == k {
        print k "=" v;
        done=1;
        next
      }
      { print }
      END {
        if (!done) print k "=" v
      }
    ' "$file" >"$tmp"
  else
    printf '%s=%s\n' "$key" "$value" >"$tmp"
  fi
  install -m 600 "$tmp" "$file"
  rm -f "$tmp"
}

server_ip_from_env() {
  read_env_var SERVER_IP
}

server_port_from_env() {
  local port
  port="$(read_env_var SERVER_PORT)"
  printf '%s' "${port:-5001}"
}

server_url() {
  local ip port
  ip="$(server_ip_from_env)"
  port="$(server_port_from_env)"
  [[ -n "$ip" && -n "$port" ]] || return 0
  printf 'http://%s:%s' "$ip" "$port"
}

bootstrap_path() {
  printf '%s/data/_admin/docker_bootstrap.json' "$MIRU_HOME"
}

invitation_code() {
  local path
  path="$(bootstrap_path)"
  [[ -f "$path" ]] || return 0
  python3 - "$path" <<'PY'
import json, sys
try:
    print(json.load(open(sys.argv[1], encoding="utf-8")).get("current_full_code", ""))
except Exception:
    print("")
PY
}

wait_health() {
  local seconds="${1:-90}"
  local local_url external_url host_port
  host_port="$(server_port_from_env)"
  host_port="${host_port:-$SERVER_PORT}"
  local_url="http://127.0.0.1:${host_port}/api/health"
  external_url="$(server_url)/api/health"
  for _ in $(seq 1 "$seconds"); do
    if curl --noproxy '*' -fsS --max-time 2 "$local_url" >/dev/null 2>&1; then
      return 0
    fi
    if [[ "$external_url" == http://* ]] \
        && curl --noproxy '*' -fsS --max-time 2 "$external_url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

wait_invitation() {
  local seconds="${1:-90}"
  local code
  for _ in $(seq 1 "$seconds"); do
    code="$(invitation_code)"
    if [[ "$code" == MIRU-* ]]; then
      printf '%s' "$code"
      return 0
    fi
    sleep 1
  done
  return 1
}

timestamp() {
  date '+%Y%m%d_%H%M%S'
}

confirm_or_die() {
  local prompt="$1"
  local expected="$2"
  local assume_yes="${3:-0}"
  if [[ "$assume_yes" == "1" ]]; then
    return 0
  fi
  if [[ ! -t 0 ]]; then
    die "refusing destructive action without --yes in non-interactive mode"
  fi
  printf '%s ' "$prompt"
  local answer
  read -r answer
  [[ "$answer" == "$expected" ]] || die "confirmation mismatch"
}
