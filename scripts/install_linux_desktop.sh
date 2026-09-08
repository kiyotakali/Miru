#!/usr/bin/env bash
# Register an already prepared source checkout; no root access required.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
case "$root" in
  *%*) echo "Desktop menu integration requires a checkout path without %." >&2; exit 1 ;;
esac
python="$root/.venv/bin/python"
test -x "$python"
QT_API=pyside6 "$python" -s -c 'import gi; gi.require_version("WebKit2", "4.1"); from gi.repository import WebKit2; import webview, qtpy.QtWebEngineWidgets, Xlib'
"$python" -s - "$root" <<'PY'
import os
from pathlib import Path
import sys

root = Path(sys.argv[1])
data_home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
desktop = data_home / "applications/miru.desktop"
desktop.parent.mkdir(parents=True, exist_ok=True)

def value(text):
    return str(text).replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r")

def argument(path):
    # Exec quoting and desktop-entry string escaping are separate layers.
    text = str(path)
    for char in ('\\', '"', '`', '$'):
        text = text.replace(char, "\\" + char)
    return value('"' + text + '"')

desktop.write_text(
    "[Desktop Entry]\nType=Application\nName=Miru (Ubuntu preview)\n"
    "Comment=AI companion\n"
    f"Exec={argument(root / '.venv/bin/python')} -s {argument(root / 'linux_launcher.py')}\n"
    f"Path={value(root)}\nIcon={value(root / 'src-tauri/icons/128x128.png')}\n"
    "Terminal=false\nCategories=Utility;\nStartupNotify=true\n", encoding="utf-8")
desktop.chmod(0o644)
print(f"Installed {desktop}")
PY
