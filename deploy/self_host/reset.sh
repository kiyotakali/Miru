#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=deploy/self_host/common.sh
source "$SCRIPT_DIR/common.sh"

MODE="user-data"
ASSUME_YES=0

usage() {
  cat <<'EOF'
Usage: reset.sh [options]

Default reset mode is --user-data: clear users/memory/chat/invites and create
a fresh invitation while preserving .env and AI provider configuration.

Options:
  --home DIR     Miru install directory. Default: /opt/miru.
  --user-data    Clear user data only. Default.
  --factory      Clear all data; .env and compose.yaml remain.
  --yes          Do not prompt before destructive action.
  -h, --help     Show this help.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --home) MIRU_HOME="$2"; shift 2 ;;
    --user-data) MODE="user-data"; shift ;;
    --factory) MODE="factory"; shift ;;
    --yes) ASSUME_YES=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

[[ -d "$MIRU_HOME" ]] || die "$MIRU_HOME does not exist"
[[ -f "$MIRU_HOME/compose.yaml" ]] || die "$MIRU_HOME/compose.yaml does not exist"
[[ -f "$MIRU_HOME/.env" ]] || die "$MIRU_HOME/.env does not exist"

if [[ "$MODE" == "factory" ]]; then
  confirm_or_die "Factory reset deletes all Miru data. Type FACTORY to continue:" "FACTORY" "$ASSUME_YES"
else
  confirm_or_die "User-data reset deletes users, memory, chats, screenshots, and old invites. Type RESET to continue:" "RESET" "$ASSUME_YES"
fi

"$SCRIPT_DIR/backup.sh" --home "$MIRU_HOME" --output "$MIRU_HOME/backups/pre-reset-$(timestamp).tar.gz" >/dev/null

compose_in_home down || true

if [[ "$MODE" == "factory" ]]; then
  rm -rf "$MIRU_HOME/data"
  mkdir -p "$MIRU_HOME/data" "$MIRU_HOME/logs"
else
  tmp="$(mktemp -d)"
  cleanup() { rm -rf "$tmp"; }
  trap cleanup EXIT

  if [[ -f "$MIRU_HOME/data/auth.json" ]]; then
    cp -a "$MIRU_HOME/data/auth.json" "$tmp/auth.json"
  fi
  if [[ -f "$MIRU_HOME/data/_admin/ai_config.json" ]]; then
    cp -a "$MIRU_HOME/data/_admin/ai_config.json" "$tmp/ai_config.json"
  fi

  rm -rf "$MIRU_HOME/data"
  mkdir -p "$MIRU_HOME/data/_admin" "$MIRU_HOME/logs"

  if [[ -f "$tmp/auth.json" ]]; then
    cp -a "$tmp/auth.json" "$MIRU_HOME/data/auth.json"
  fi
  if [[ -f "$tmp/ai_config.json" ]]; then
    cp -a "$tmp/ai_config.json" "$MIRU_HOME/data/_admin/ai_config.json"
  fi
fi

compose_in_home up -d
wait_health 120 || die "Miru health check did not recover after reset"
code="$(wait_invitation 90)" || die "new invitation code did not appear"

info "Reset complete"
printf 'MIRU_INVITATION_CODE=%s\n' "$code"
