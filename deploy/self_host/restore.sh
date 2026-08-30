#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=deploy/self_host/common.sh
source "$SCRIPT_DIR/common.sh"

BACKUP=""
ASSUME_YES=0
OVERRIDE_SERVER_IP=""
OVERRIDE_SERVER_PORT=""

usage() {
  cat <<'EOF'
Usage: restore.sh BACKUP.tar.gz [options]

Restores data/.env/compose.yaml from a backup. A pre-restore backup is created
automatically when current data exists.

Options:
  --home DIR             Miru install directory. Default: /opt/miru.
  --server-ip IP         Rewrite SERVER_IP after restore, useful on a new VPS.
  --server-port PORT     Rewrite SERVER_PORT after restore.
  --yes                  Do not prompt before overwriting current state.
  -h, --help             Show this help.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --home) MIRU_HOME="$2"; shift 2 ;;
    --server-ip) OVERRIDE_SERVER_IP="$2"; shift 2 ;;
    --server-port) OVERRIDE_SERVER_PORT="$2"; shift 2 ;;
    --yes) ASSUME_YES=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *)
      if [[ -z "$BACKUP" ]]; then
        BACKUP="$1"; shift
      else
        die "unknown option: $1"
      fi
      ;;
  esac
done

[[ -n "$BACKUP" ]] || die "backup file is required"
[[ -f "$BACKUP" ]] || die "backup file not found: $BACKUP"
if [[ -n "$OVERRIDE_SERVER_IP" ]]; then
  is_ipv4 "$OVERRIDE_SERVER_IP" || die "--server-ip must be IPv4"
fi
if [[ -n "$OVERRIDE_SERVER_PORT" ]]; then
  is_port "$OVERRIDE_SERVER_PORT" || die "--server-port must be 1..65535"
fi

confirm_or_die "Restore will overwrite $MIRU_HOME. Type RESTORE to continue:" "RESTORE" "$ASSUME_YES"

mkdir -p "$MIRU_HOME/backups"
if [[ -d "$MIRU_HOME/data" ]]; then
  "$SCRIPT_DIR/backup.sh" --home "$MIRU_HOME" --output "$MIRU_HOME/backups/pre-restore-$(timestamp).tar.gz" >/dev/null
fi

if [[ -f "$MIRU_HOME/compose.yaml" ]] && command -v docker >/dev/null 2>&1; then
  compose_in_home down || true
fi

tmp="$(mktemp -d)"
cleanup() { rm -rf "$tmp"; }
trap cleanup EXIT

tar -xzf "$BACKUP" -C "$tmp"
[[ -d "$tmp/data" ]] || die "backup missing data/"

rm -rf "$MIRU_HOME/data"
mkdir -p "$MIRU_HOME"
cp -a "$tmp/data" "$MIRU_HOME/data"
if [[ -f "$tmp/compose.yaml" ]]; then
  cp -a "$tmp/compose.yaml" "$MIRU_HOME/compose.yaml"
fi
if [[ -f "$tmp/.env" ]]; then
  cp -a "$tmp/.env" "$MIRU_HOME/.env"
  chmod 600 "$MIRU_HOME/.env"
elif [[ ! -f "$MIRU_HOME/.env" ]]; then
  die "backup did not include .env and no existing $MIRU_HOME/.env exists"
fi

if [[ -n "$OVERRIDE_SERVER_IP" ]]; then
  set_env_var SERVER_IP "$OVERRIDE_SERVER_IP"
fi
if [[ -n "$OVERRIDE_SERVER_PORT" ]]; then
  set_env_var SERVER_PORT "$OVERRIDE_SERVER_PORT"
fi

compose_in_home up -d
wait_health 120 || die "Miru health check did not recover after restore"

info "Restore complete"
"$SCRIPT_DIR/status.sh" --home "$MIRU_HOME"
