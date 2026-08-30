#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=deploy/host_manager/common.sh
source "$SCRIPT_DIR/common.sh"

SOURCE_ROOT="$REPO_ROOT"
QUIET=0
JSON_OUTPUT=0

usage() {
  cat <<'EOF'
Usage: sync_tools.sh [options]

Copies the current Host Instance Manager tool bundle into the host home.
This is safe to run repeatedly and is part of the real provisioning path.

Options:
  --home DIR         Host manager directory. Default: /opt/miru-host.
  --source-root DIR  Repository/tool bundle root. Default: current script root.
  --quiet            Do not print human-readable output.
  --json             Print machine-readable JSON.
  -h, --help         Show this help.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --home) MIRU_HOST_HOME="$2"; shift 2 ;;
    --source-root) SOURCE_ROOT="$2"; shift 2 ;;
    --quiet) QUIET=1; shift ;;
    --json) JSON_OUTPUT=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

need_cmd python3

SOURCE_ROOT="$(python3 -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).expanduser().resolve())' "$SOURCE_ROOT")"
MIRU_HOST_HOME="$(python3 -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).expanduser().resolve())' "$MIRU_HOST_HOME")"

for required in \
  "$SOURCE_ROOT/deploy/host_manager/common.sh" \
  "$SOURCE_ROOT/deploy/self_host/common.sh" \
  "$SOURCE_ROOT/scripts/host_manager.py"; do
  [[ -e "$required" ]] || die "tool bundle is missing $required"
done

mkdir -p "$MIRU_HOST_HOME/tools"
tmp="$(mktemp -d)"
new_dir="$MIRU_HOST_HOME/tools/current.new.$$"
old_dir="$MIRU_HOST_HOME/tools/current.old.$$"
cleanup() {
  rm -rf "$tmp" "$new_dir" "$old_dir"
}
trap cleanup EXIT

mkdir -p "$tmp/deploy" "$tmp/scripts"
cp -R "$SOURCE_ROOT/deploy/host_manager" "$tmp/deploy/host_manager"
cp -R "$SOURCE_ROOT/deploy/self_host" "$tmp/deploy/self_host"
cp "$SOURCE_ROOT/scripts/host_manager.py" "$tmp/scripts/host_manager.py"
find "$tmp" \( -name '__pycache__' -o -name '*.pyc' \) -prune -exec rm -rf {} +
find "$tmp/deploy" -type f -name '*.sh' -exec chmod 755 {} +
chmod 755 "$tmp/scripts/host_manager.py"

python3 - "$tmp" "$SOURCE_ROOT" <<'PY'
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

root = Path(sys.argv[1])
source_root = sys.argv[2]
files = []
for path in sorted(p for p in root.rglob("*") if p.is_file()):
    files.append(path.relative_to(root).as_posix())
manifest = {
    "schema_version": 1,
    "synced_at": datetime.now(timezone.utc).isoformat(),
    "source_root": source_root,
    "files": files,
}
(root / "MANIFEST.json").write_text(
    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
PY

mv "$tmp" "$new_dir"
if [[ -e "$MIRU_HOST_HOME/tools/current" ]]; then
  mv "$MIRU_HOST_HOME/tools/current" "$old_dir"
fi
mv "$new_dir" "$MIRU_HOST_HOME/tools/current"
rm -rf "$old_dir"

link_alias() {
  local name="$1"
  local target="$MIRU_HOST_HOME/$name"
  if [[ -L "$target" || ! -e "$target" ]]; then
    ln -sfn "tools/current/$name" "$target"
  else
    warn "$target exists and is not a symlink; leaving it unchanged"
  fi
}

link_alias deploy
link_alias scripts

if [[ "$JSON_OUTPUT" == "1" ]]; then
  python3 - "$MIRU_HOST_HOME" <<'PY'
import json
import sys
from pathlib import Path
home = Path(sys.argv[1])
print(json.dumps({
    "ok": True,
    "home": str(home),
    "tools_dir": str(home / "tools" / "current"),
    "host_manager_dir": str(home / "deploy" / "host_manager"),
}, ensure_ascii=False, sort_keys=True))
PY
elif [[ "$QUIET" != "1" ]]; then
  info "Host manager tools synced to $MIRU_HOST_HOME/tools/current"
  printf 'MIRU_HOST_TOOLS_DIR=%s\n' "$MIRU_HOST_HOME/tools/current"
fi
