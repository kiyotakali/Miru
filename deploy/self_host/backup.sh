#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=deploy/self_host/common.sh
source "$SCRIPT_DIR/common.sh"

OUTPUT=""
KEEP_DAYS="${MIRU_BACKUP_KEEP_DAYS:-30}"
INCLUDE_ENV=1
INCLUDE_LOGS=0

usage() {
  cat <<'EOF'
Usage: backup.sh [options]

Creates a sensitive Miru server backup. By default it includes .env because
official restore needs model/API configuration to be complete.

Options:
  --home DIR        Miru install directory. Default: /opt/miru.
  --output FILE     Backup file path. Default: /opt/miru/backups/miru-backup-<ts>.tar.gz
  --keep-days N     Delete old backups after N days. Default: 30.
  --no-env          Exclude .env. Restore will then need an existing .env.
  --include-logs    Include logs/ as well as data/.
  -h, --help        Show this help.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --home) MIRU_HOME="$2"; shift 2 ;;
    --output) OUTPUT="$2"; shift 2 ;;
    --keep-days) KEEP_DAYS="$2"; shift 2 ;;
    --no-env) INCLUDE_ENV=0; shift ;;
    --include-logs) INCLUDE_LOGS=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

[[ -d "$MIRU_HOME" ]] || die "$MIRU_HOME does not exist"
[[ -d "$MIRU_HOME/data" ]] || die "$MIRU_HOME/data does not exist"

mkdir -p "$MIRU_HOME/backups"
if [[ -z "$OUTPUT" ]]; then
  OUTPUT="$MIRU_HOME/backups/miru-backup-$(timestamp).tar.gz"
fi
mkdir -p "$(dirname "$OUTPUT")"

tmp="$(mktemp -d)"
cleanup() { rm -rf "$tmp"; }
trap cleanup EXIT

cat >"$tmp/backup_manifest.json" <<EOF
{
  "version": 1,
  "created_at": "$(date -Iseconds)",
  "miru_home": "$MIRU_HOME",
  "includes_env": $([[ "$INCLUDE_ENV" == "1" ]] && echo true || echo false),
  "includes_logs": $([[ "$INCLUDE_LOGS" == "1" ]] && echo true || echo false)
}
EOF

items=(data compose.yaml)
if [[ "$INCLUDE_ENV" == "1" ]]; then
  [[ -f "$MIRU_HOME/.env" ]] || die "$MIRU_HOME/.env does not exist"
  items+=(.env)
fi
if [[ "$INCLUDE_LOGS" == "1" ]]; then
  items+=(logs)
fi

info "Creating backup: $OUTPUT"
tar -czf "$OUTPUT" -C "$MIRU_HOME" "${items[@]}" -C "$tmp" backup_manifest.json
chmod 600 "$OUTPUT"

size="$(du -h "$OUTPUT" | awk '{print $1}')"
info "Backup created ($size)"
if [[ "$INCLUDE_ENV" == "1" ]]; then
  warn "Backup includes .env and API keys. Keep this file private."
fi

if [[ "$KEEP_DAYS" =~ ^[0-9]+$ ]]; then
  find "$MIRU_HOME/backups" -name 'miru-backup-*.tar.gz' -mtime +"$KEEP_DAYS" -delete 2>/dev/null || true
fi

printf 'MIRU_BACKUP_FILE=%s\n' "$OUTPUT"
