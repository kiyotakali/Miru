#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
SELF_HOST_DIR="$SCRIPT_DIR/../self_host"
HOST_MANAGER_PY="$REPO_ROOT/scripts/host_manager.py"

MIRU_HOST_HOME="${MIRU_HOST_HOME:-/opt/miru-host}"
MIRU_IMAGE_VERSION="${MIRU_IMAGE_VERSION:-0.2.0}"
MIRU_DEFAULT_IMAGE="${MIRU_DEFAULT_IMAGE:-miru/server:${MIRU_IMAGE_VERSION}}"
MIRU_HOST_DEFAULT_IMAGE="${MIRU_HOST_DEFAULT_IMAGE:-$MIRU_DEFAULT_IMAGE}"
MIRU_HOST_PORT_START="${MIRU_HOST_PORT_START:-5001}"
MIRU_HOST_PORT_END="${MIRU_HOST_PORT_END:-5010}"

info() { printf '[miru-host] %s\n' "$*"; }
warn() { printf '[miru-host] WARNING: %s\n' "$*" >&2; }
die() { printf '[miru-host] ERROR: %s\n' "$*" >&2; exit 1; }

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "$1 command not found"
}

run_as_root() {
  if [[ "$(id -u)" == "0" ]]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    die "root privileges required and sudo not found"
  fi
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

detect_os() {
  [[ -f /etc/os-release ]] || die "/etc/os-release not found"
  # shellcheck disable=SC1091
  . /etc/os-release
  case "${ID:-}" in
    ubuntu|debian) printf '%s %s\n' "$ID" "${VERSION_CODENAME:-}" ;;
    *) die "Miru can automatically install Docker on Ubuntu/Debian only; got ID=${ID:-unknown}. Please install Docker and Docker Compose first, then retry." ;;
  esac
}

detect_public_ipv4() {
  local candidate
  for url in \
    "https://api.ipify.org" \
    "https://ifconfig.me/ip"; do
    candidate="$(curl --noproxy '*' -fsS --max-time 4 "$url" 2>/dev/null || true)"
    candidate="$(printf '%s' "$candidate" | tr -d '[:space:]')"
    if is_ipv4 "$candidate"; then
      printf '%s' "$candidate"
      return 0
    fi
  done
  return 1
}

install_docker_if_needed() {
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    info "Docker and Compose already installed"
    return
  fi
  if command -v docker >/dev/null 2>&1 && command -v docker-compose >/dev/null 2>&1; then
    info "Docker and docker-compose already installed"
    return
  fi

  local os_id codename
  read -r os_id codename < <(detect_os)
  [[ -n "$codename" ]] || die "VERSION_CODENAME missing in /etc/os-release"

  info "Installing Docker Engine for ${os_id} ${codename}"
  run_as_root rm -f /etc/apt/sources.list.d/docker.list
  run_as_root apt-get update
  run_as_root apt-get install -y ca-certificates curl gnupg python3
  run_as_root install -m 0755 -d /etc/apt/keyrings

  local official_ready=0
  if [[ -s /etc/apt/keyrings/docker.asc ]]; then
    official_ready=1
  elif curl --retry 4 --retry-delay 2 --retry-all-errors -fsSL "https://download.docker.com/linux/${os_id}/gpg" \
      | run_as_root tee /etc/apt/keyrings/docker.asc >/dev/null; then
    official_ready=1
  else
    warn "Docker official GPG key download failed; falling back to distro packages"
  fi

  if [[ "$official_ready" == "1" ]]; then
    run_as_root chmod a+r /etc/apt/keyrings/docker.asc
    local arch
    arch="$(dpkg --print-architecture)"
    printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/%s %s stable\n' \
      "$arch" "$os_id" "$codename" | run_as_root tee /etc/apt/sources.list.d/docker.list >/dev/null
    if run_as_root apt-get update && run_as_root apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin; then
      official_ready=2
    fi
  fi

  if [[ "$official_ready" != "2" ]]; then
    warn "Docker official apt repo/install failed; falling back to distro docker.io + docker-compose"
    run_as_root rm -f /etc/apt/sources.list.d/docker.list
    run_as_root apt-get update
    run_as_root apt-get install -y docker.io docker-compose
  fi
  if command -v systemctl >/dev/null 2>&1; then
    run_as_root systemctl enable --now docker
  fi
  command -v docker >/dev/null 2>&1 || die "docker command is still missing after installation"
  if ! docker compose version >/dev/null 2>&1 && ! command -v docker-compose >/dev/null 2>&1; then
    die "Docker Compose is still missing after installation"
  fi
}

json_field() {
  local key="$1"
  local payload
  payload="$(cat)"
  python3 - "$key" "$payload" <<'PY'
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
if value is None:
    value = ""
print(value)
PY
}

instance_json() {
  local instance_id="$1"
  python3 "$HOST_MANAGER_PY" get-instance --home "$MIRU_HOST_HOME" --instance-id "$instance_id"
}

compose_down_instance() {
  local home="$1"
  local compose_project="$2"
  [[ -f "$home/compose.yaml" ]] || return 0
  MIRU_HOME="$home" MIRU_COMPOSE_PROJECT="$compose_project" bash -c \
    'source "$0"; compose_in_home down' "$SELF_HOST_DIR/common.sh" || true
}

remove_instance_dir() {
  local home="$1"
  local resolved_home resolved_host
  resolved_home="$(python3 -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).expanduser().resolve())' "$home")"
  resolved_host="$(python3 -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).expanduser().resolve())' "$MIRU_HOST_HOME")"
  case "$resolved_home" in
    "$resolved_host"/instances/*) rm -rf "$resolved_home" ;;
    *) die "refusing to remove unexpected path: $home" ;;
  esac
}

sync_host_tools_best_effort() {
  local sync_script="$SCRIPT_DIR/sync_tools.sh"
  [[ -f "$sync_script" ]] || return 0
  MIRU_HOST_HOME="$MIRU_HOST_HOME" bash "$sync_script" \
    --home "$MIRU_HOST_HOME" \
    --source-root "$REPO_ROOT" \
    --quiet >/dev/null 2>&1 || warn "failed to sync host manager tools into $MIRU_HOST_HOME"
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
