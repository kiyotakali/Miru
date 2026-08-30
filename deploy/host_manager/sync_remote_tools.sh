#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

SSH_TARGET=""
SSH_KEY=""
MIRU_HOST_HOME="${MIRU_HOST_HOME:-/opt/miru-host}"
SOURCE_ROOT="$REPO_ROOT"
REMOTE_ROOT=""

usage() {
  cat <<'EOF'
Usage: sync_remote_tools.sh --ssh TARGET [options]

Uploads the current Host Instance Manager bundle to a Linux host and runs
sync_tools.sh there. Use this to repair older hosts whose host home has only
ledgers and no maintenance scripts.

Options:
  --ssh TARGET       SSH target, for example root@203.0.113.10.
  --ssh-key FILE     SSH private key.
  --home DIR         Host manager directory on the remote host. Default: /opt/miru-host.
  --source-root DIR  Local repository/tool bundle root. Default: current repo.
  --remote-root DIR  Temporary remote directory. Default: /tmp/miru-host-tools-sync-<ts>.
  -h, --help         Show this help.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ssh) SSH_TARGET="$2"; shift 2 ;;
    --ssh-key) SSH_KEY="$2"; shift 2 ;;
    --home) MIRU_HOST_HOME="$2"; shift 2 ;;
    --source-root) SOURCE_ROOT="$2"; shift 2 ;;
    --remote-root) REMOTE_ROOT="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) printf '[miru-host] ERROR: unknown option: %s\n' "$1" >&2; exit 1 ;;
  esac
done

[[ -n "$SSH_TARGET" ]] || { usage >&2; exit 1; }
[[ -d "$SOURCE_ROOT/deploy/host_manager" ]] || { printf '[miru-host] ERROR: missing deploy/host_manager under %s\n' "$SOURCE_ROOT" >&2; exit 1; }
[[ -d "$SOURCE_ROOT/deploy/self_host" ]] || { printf '[miru-host] ERROR: missing deploy/self_host under %s\n' "$SOURCE_ROOT" >&2; exit 1; }
[[ -f "$SOURCE_ROOT/scripts/host_manager.py" ]] || { printf '[miru-host] ERROR: missing scripts/host_manager.py under %s\n' "$SOURCE_ROOT" >&2; exit 1; }

REMOTE_ROOT="${REMOTE_ROOT:-/tmp/miru-host-tools-sync-$(date +%Y%m%d%H%M%S)}"
q_remote_root="$(printf '%q' "$REMOTE_ROOT")"
q_home="$(printf '%q' "$MIRU_HOST_HOME")"

ssh_cmd=(ssh)
if [[ -n "$SSH_KEY" ]]; then
  ssh_cmd+=(-i "$SSH_KEY")
fi
ssh_cmd+=("$SSH_TARGET")

remote_cmd="set -euo pipefail; rm -rf $q_remote_root; mkdir -p $q_remote_root/repo; tar -xzf - -C $q_remote_root/repo; bash $q_remote_root/repo/deploy/host_manager/sync_tools.sh --home $q_home --source-root $q_remote_root/repo --json; rm -rf $q_remote_root"

tar_extra=()
if tar --no-xattrs -cf /dev/null -C "$SOURCE_ROOT" scripts/host_manager.py >/dev/null 2>&1; then
  tar_extra+=(--no-xattrs)
elif tar --disable-copyfile -cf /dev/null -C "$SOURCE_ROOT" scripts/host_manager.py >/dev/null 2>&1; then
  tar_extra+=(--disable-copyfile)
fi
tar_args=("${tar_extra[@]}" -czf - -C "$SOURCE_ROOT" deploy/host_manager deploy/self_host scripts/host_manager.py)

COPYFILE_DISABLE=1 tar "${tar_args[@]}" | "${ssh_cmd[@]}" "$remote_cmd"
