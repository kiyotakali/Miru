#!/usr/bin/env bash
set -euo pipefail

APP_PATH="${1:-/Applications/Miru.app}"

die() {
  printf '[miru-acceptance] ERROR: %s\n' "$*" >&2
  exit 1
}

[[ -d "$APP_PATH" ]] || die "$APP_PATH does not exist. Install the DMG into /Applications first."

osascript -e 'tell application "Miru" to quit' >/dev/null 2>&1 || true
sleep 1

kill_matching() {
  local pattern="$1"
  local pids
  pids="$(pgrep -f "$pattern" 2>/dev/null || true)"
  [[ -n "$pids" ]] || return 0
  printf '%s\n' "$pids" | while IFS= read -r pid; do
    [[ -n "$pid" ]] || continue
    kill "$pid" 2>/dev/null || true
  done
}

kill_matching "$APP_PATH/Contents/MacOS/"
kill_matching "miru-pet"
sleep 1

kill_matching "$APP_PATH/Contents/MacOS/"
kill_matching "miru-pet"

open "$APP_PATH"
