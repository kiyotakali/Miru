#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=deploy/self_host/common.sh
source "$SCRIPT_DIR/common.sh"

NON_INTERACTIVE=0
FORCE=0
SKIP_DOCKER_INSTALL=0
SKIP_PULL="${MIRU_SKIP_PULL:-0}"
ALLOW_NO_API="${MIRU_ALLOW_NO_API:-0}"
IMAGE_TAR="${MIRU_IMAGE_TAR:-}"

usage() {
  cat <<'EOF'
Usage: install.sh [options]

Provisioning-ready Miru private server installer.

Options:
  --non-interactive       Fail instead of prompting for missing values.
  --server-ip IP          Public IPv4 encoded into the invitation.
  --server-port PORT      Public port clients should dial. Default: 5001.
  --image IMAGE           Docker image tag. Default: miru/server:<version>.
  --image-tar FILE        Load this docker save tar before starting Miru.
  --home DIR              Install directory. Default: /opt/miru.
  --allow-no-api          Allow boot/login-only install without model keys.
  --skip-docker-install   Require Docker to already be installed.
  --skip-pull             Use a preloaded local image instead of docker pull.
  --force                 Overwrite existing compose/.env.
  -h, --help              Show this help.

Official provisioning should call this non-interactively with SERVER_IP,
SERVER_PORT, MIRU_IMAGE, and AI_* env vars already set.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --non-interactive) NON_INTERACTIVE=1; shift ;;
    --server-ip) SERVER_IP="$2"; shift 2 ;;
    --server-port) SERVER_PORT="$2"; shift 2 ;;
    --image) MIRU_IMAGE="$2"; shift 2 ;;
    --image-tar) IMAGE_TAR="$2"; shift 2 ;;
    --home) MIRU_HOME="$2"; shift 2 ;;
    --allow-no-api) ALLOW_NO_API=1; shift ;;
    --skip-docker-install) SKIP_DOCKER_INSTALL=1; shift ;;
    --skip-pull) SKIP_PULL=1; shift ;;
    --force) FORCE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

detect_os() {
  [[ -f /etc/os-release ]] || die "/etc/os-release not found"
  # shellcheck disable=SC1091
  . /etc/os-release
  case "${ID:-}" in
    ubuntu|debian) printf '%s %s\n' "$ID" "${VERSION_CODENAME:-}" ;;
    *) die "Miru can automatically install Docker on Ubuntu/Debian only; got ID=${ID:-unknown}. Please install Docker and Docker Compose first, then retry." ;;
  esac
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

install_docker_if_needed() {
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    info "Docker and Compose already installed"
    return
  fi
  if command -v docker >/dev/null 2>&1 && command -v docker-compose >/dev/null 2>&1; then
    info "Docker and docker-compose already installed"
    return
  fi
  [[ "$SKIP_DOCKER_INSTALL" == "0" ]] || die "Docker/Compose missing and --skip-docker-install was set"

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
  if ! command -v docker >/dev/null 2>&1; then
    die "docker command is still missing after installation"
  fi
  if ! docker compose version >/dev/null 2>&1 && ! command -v docker-compose >/dev/null 2>&1; then
    die "Docker Compose is still missing after installation"
  fi
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

prompt_value() {
  local var="$1"
  local label="$2"
  local secret="${3:-0}"
  local current="${!var:-}"
  if [[ -n "$current" ]]; then
    return
  fi
  if [[ "$NON_INTERACTIVE" != "0" ]]; then
    return 0
  fi
  if [[ "$secret" == "1" ]]; then
    printf '%s: ' "$label" >&2
    read -rsp "" current
    printf '\n' >&2
  else
    printf '%s: ' "$label" >&2
    read -r current
  fi
  export "$var=$current"
}

validate_api_config() {
  AI_VISION_HOST="${AI_VISION_HOST:-openrouter.ai/api}"
  AI_VISION_MODEL="${AI_VISION_MODEL:-qwen/qwen3.5-9b}"
  AI_CHAT_HOST="${AI_CHAT_HOST:-api.deepseek.com}"
  AI_CHAT_MODEL="${AI_CHAT_MODEL:-deepseek-v4-pro}"
  AI_MEMORY_HOST="${AI_MEMORY_HOST:-api.deepseek.com}"
  AI_MEMORY_MODEL="${AI_MEMORY_MODEL:-deepseek-v4-flash}"

  prompt_value AI_VISION_KEY "AI vision API key" 1
  prompt_value AI_CHAT_KEY "AI chat API key" 1
  prompt_value AI_MEMORY_KEY "AI memory API key" 1

  local missing=()
  for key in AI_VISION_HOST AI_VISION_MODEL AI_CHAT_HOST AI_CHAT_MODEL AI_MEMORY_HOST AI_MEMORY_MODEL; do
    [[ -n "${!key:-}" ]] || missing+=("$key")
  done
  for key in AI_VISION_KEY AI_CHAT_KEY AI_MEMORY_KEY; do
    if [[ -z "${!key:-}" || "${!key:-}" == "sk-or-your-openrouter-key" || "${!key:-}" == "your-api-key" ]]; then
      missing+=("$key")
    fi
  done
  if (( ${#missing[@]} > 0 )) && [[ "$ALLOW_NO_API" != "1" ]]; then
    die "missing required API config: ${missing[*]}"
  fi
}

ensure_runtime_dir() {
  if mkdir -p "$@" 2>/dev/null; then
    return 0
  fi
  run_as_root mkdir -p "$@"
}

install_runtime_file() {
  local mode="$1"
  local src="$2"
  local dst="$3"
  if install -m "$mode" "$src" "$dst" 2>/dev/null; then
    return 0
  fi
  run_as_root install -m "$mode" "$src" "$dst"
}

write_runtime_files() {
  ensure_runtime_dir "$MIRU_HOME/data/_admin" "$MIRU_HOME/logs" "$MIRU_HOME/backups"
  if [[ -f "$MIRU_HOME/.env" && "$FORCE" != "1" ]]; then
    die "$MIRU_HOME/.env already exists; use --force to overwrite"
  fi
  if [[ -f "$MIRU_HOME/compose.yaml" && "$FORCE" != "1" ]]; then
    die "$MIRU_HOME/compose.yaml already exists; use --force to overwrite"
  fi

  local tmp_env tmp_compose tmp_ai_config runtime_tz runtime_timezone
  tmp_env="$(mktemp)"
  tmp_compose="$(mktemp)"
  tmp_ai_config="$(mktemp)"
  runtime_tz="${TZ:-Asia/Shanghai}"
  runtime_timezone="${TIMEZONE:-$runtime_tz}"
  cat >"$tmp_env" <<EOF
MIRU_IMAGE=${MIRU_IMAGE}
MIRU_CONTAINER_NAME=${MIRU_CONTAINER_NAME}

SERVER_IP=${SERVER_IP}
SERVER_PORT=${SERVER_PORT}

TZ=${runtime_tz}
TIMEZONE=${runtime_timezone}

AI_VISION_HOST=${AI_VISION_HOST:-}
AI_VISION_KEY=${AI_VISION_KEY:-}
AI_VISION_MODEL=${AI_VISION_MODEL:-}

AI_CHAT_HOST=${AI_CHAT_HOST:-}
AI_CHAT_KEY=${AI_CHAT_KEY:-}
AI_CHAT_MODEL=${AI_CHAT_MODEL:-}

AI_MEMORY_HOST=${AI_MEMORY_HOST:-}
AI_MEMORY_KEY=${AI_MEMORY_KEY:-}
AI_MEMORY_MODEL=${AI_MEMORY_MODEL:-}
EOF
  AI_VISION_HOST="$AI_VISION_HOST" \
  AI_VISION_KEY="${AI_VISION_KEY:-}" \
  AI_VISION_MODEL="$AI_VISION_MODEL" \
  AI_CHAT_HOST="$AI_CHAT_HOST" \
  AI_CHAT_KEY="${AI_CHAT_KEY:-}" \
  AI_CHAT_MODEL="$AI_CHAT_MODEL" \
  AI_MEMORY_HOST="$AI_MEMORY_HOST" \
  AI_MEMORY_KEY="${AI_MEMORY_KEY:-}" \
  AI_MEMORY_MODEL="$AI_MEMORY_MODEL" \
  python3 - "$tmp_ai_config" <<'PY'
import json
import os
import sys

out = sys.argv[1]
payload = {
    "version": 2,
    "tiers": {
        "vision": {
            "host": os.environ.get("AI_VISION_HOST", ""),
            "api_key": os.environ.get("AI_VISION_KEY", ""),
            "model": os.environ.get("AI_VISION_MODEL", ""),
            "supports_images": True,
            "max_tokens": 50000,
        },
        "chat": {
            "host": os.environ.get("AI_CHAT_HOST", ""),
            "api_key": os.environ.get("AI_CHAT_KEY", ""),
            "model": os.environ.get("AI_CHAT_MODEL", ""),
            "supports_images": False,
            "max_tokens": 50000,
        },
        "memory": {
            "host": os.environ.get("AI_MEMORY_HOST", ""),
            "api_key": os.environ.get("AI_MEMORY_KEY", ""),
            "model": os.environ.get("AI_MEMORY_MODEL", ""),
            "supports_images": False,
            "max_tokens": 65536,
        },
    },
}
with open(out, "w", encoding="utf-8") as f:
    json.dump(payload, f, ensure_ascii=False, indent=2)
    f.write("\n")
PY
  cp "$SCRIPT_DIR/compose.yaml.template" "$tmp_compose"
  install_runtime_file 600 "$tmp_env" "$MIRU_HOME/.env"
  install_runtime_file 600 "$tmp_ai_config" "$MIRU_HOME/data/_admin/ai_config.json"
  install_runtime_file 644 "$tmp_compose" "$MIRU_HOME/compose.yaml"
  rm -f "$tmp_env" "$tmp_compose" "$tmp_ai_config"
}

main() {
  if [[ -z "${SERVER_IP:-}" ]]; then
    info "Detecting public IPv4"
    SERVER_IP="$(detect_public_ipv4 || true)"
  fi
  [[ -n "${SERVER_IP:-}" ]] || die "SERVER_IP is required; pass --server-ip or set SERVER_IP"
  is_ipv4 "$SERVER_IP" || die "SERVER_IP must be an IPv4 address, got ${SERVER_IP}"
  is_port "$SERVER_PORT" || die "SERVER_PORT must be 1..65535, got ${SERVER_PORT}"

  validate_api_config
  install_docker_if_needed
  if [[ -n "$IMAGE_TAR" ]]; then
    [[ -f "$IMAGE_TAR" ]] || die "image tar not found: $IMAGE_TAR"
    info "Loading docker image from $IMAGE_TAR"
    load_output="$(run_as_root docker load -i "$IMAGE_TAR")"
    if ! run_as_root docker image inspect "$MIRU_IMAGE" >/dev/null 2>&1; then
      loaded_ref="$(printf '%s\n' "$load_output" | awk -F': ' '/Loaded image:/ {print $2; exit}')"
      if [[ -z "$loaded_ref" ]]; then
        loaded_ref="$(printf '%s\n' "$load_output" | awk -F': ' '/Loaded image ID:/ {print $2; exit}')"
      fi
      [[ -n "$loaded_ref" ]] || die "docker load finished but did not report a loadable image reference"
      info "Tagging loaded image ${loaded_ref} as ${MIRU_IMAGE}"
      run_as_root docker tag "$loaded_ref" "$MIRU_IMAGE"
    fi
    SKIP_PULL=1
  fi
  write_runtime_files

  if [[ "$SKIP_PULL" == "1" ]]; then
    info "Skipping docker pull for preloaded image ${MIRU_IMAGE}"
  else
    info "Pulling ${MIRU_IMAGE}"
    compose_in_home pull
  fi
  info "Starting Miru"
  compose_in_home up -d

  wait_health 120 || die "Miru health check did not become ready"
  local code
  code="$(wait_invitation 90)" || die "invitation code did not appear"

  info "Miru private server is ready"
  printf 'SERVER_URL=http://%s:%s\n' "$SERVER_IP" "$SERVER_PORT"
  printf 'MIRU_INVITATION_CODE=%s\n' "$code"
  printf 'MIRU_HOME=%s\n' "$MIRU_HOME"
}

main "$@"
