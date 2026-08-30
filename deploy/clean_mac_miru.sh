#!/bin/bash
# Completely wipe all Miru local state on Mac.
#
# Use before "fresh install" testing. This removes login state, WebKit
# localStorage (miru_auth_token / miru_desktop_perm_guided flags), cookies,
# HTTP cache, desktop pet data, saved window state, and anything else that
# would let a newly installed Miru skip the invitation-code screen.
#
# DOES remove:
#   - ~/Library/Application Support/Miru/            (config.json, data/, logs)
#   - ~/Library/WebKit/com.contextlife.miru/         (localStorage, IndexedDB, cookies)
#   - ~/Library/WebKit/miru-pet/                     (Tauri pet WebView)
#   - ~/Library/HTTPStorages/com.contextlife.*       (HTTP cookies)
#   - ~/Library/Caches/com.contextlife.*             (HTTP cache)
#   - ~/Library/Preferences/com.contextlife.miru.plist  (NSUserDefaults)
#   - ~/Library/Saved Application State/com.contextlife.miru.savedState/
#   - ~/Library/Containers/com.contextlife.miru/     (if sandboxed)
#   - Keychain entries under service "Miru" / server "mirulife.top"
#   - /tmp/miru_* + /tmp/.pet.pid + /tmp/.child_pids
#
# DOES NOT remove:
#   - /Applications/Miru.app  (bundle itself — drag new DMG over it yourself)
#   - macOS TCC Screen Recording permission (system-protected)
#   - Any VPS-side user data
#
# Usage:
#   bash deploy/clean_mac_miru.sh           # interactive confirm
#   bash deploy/clean_mac_miru.sh --yes     # skip confirm

set -uo pipefail

# Only run on macOS
if [[ "$(uname)" != "Darwin" ]]; then
    echo "error: this script is macOS-only" >&2
    exit 1
fi

YES=""
if [[ "${1:-}" == "--yes" || "${1:-}" == "-y" ]]; then
    YES="1"
fi

if [[ -z "$YES" ]]; then
    echo "This will WIPE all local Miru state on this Mac (login, localStorage,"
    echo "cookies, caches, pet data). The Miru.app bundle itself is NOT touched."
    echo
    read -r -p "Continue? [y/N] " reply
    if [[ "$reply" != "y" && "$reply" != "Y" ]]; then
        echo "aborted"; exit 0
    fi
fi

# ------------------------------------------------------------------
# 1. Stop any running Miru / pet processes first, so they can't
#    rewrite the files we're about to delete.
# ------------------------------------------------------------------
echo "[1/9] Stopping Miru processes…"
killall Miru miru-pet 2>/dev/null || true
# Best-effort wait — give NSApp's graceful shutdown up to 3s
for i in 1 2 3; do
    pgrep -f "/Applications/Miru.app/Contents/MacOS/" >/dev/null || break
    sleep 1
done
# If still alive, SIGKILL all known pet binary locations. The dev-build
# binary is literally named `app` (Cargo default), so `pkill miru-pet`
# won't match it — include target/(release|debug)/app explicitly.
pgrep -f "/Applications/Miru.app/Contents/MacOS/" | xargs -r kill -9 2>/dev/null || true
pkill -9 -f "src-tauri/target/(release|debug)/app" 2>/dev/null || true
pkill -9 -f "Miru\.app/Contents/MacOS/miru-pet" 2>/dev/null || true

# ------------------------------------------------------------------
# 2. Persistent app data (config.json lives here — this is the source
#    of "auto-login on relaunch")
# ------------------------------------------------------------------
echo "[2/9] Removing Application Support data…"
rm -rf "$HOME/Library/Application Support/Miru"
rm -rf "$HOME/Library/Application Support/com.contextlife.miru"
rm -rf "$HOME/Library/Application Support/miru-pet"
rm -rf "$HOME/Library/Application Support/com.contextlife.shell"

# ------------------------------------------------------------------
# 3. WebView data — localStorage holds miru_auth_token + the
#    "first-launch guide dismissed" flag. This is the SECOND source of
#    auto-login: the web page recovers the token from localStorage if
#    the native config.json is missing.
# ------------------------------------------------------------------
echo "[3/9] Removing WebKit / WebView data…"
rm -rf "$HOME/Library/WebKit/com.contextlife.miru"
rm -rf "$HOME/Library/WebKit/miru-pet"
rm -rf "$HOME/Library/WebKit/com.contextlife.shell"

# ------------------------------------------------------------------
# 4. HTTP Storage (macOS 14+ splits cookies out of WebKit into here)
# ------------------------------------------------------------------
echo "[4/9] Removing HTTPStorages…"
rm -rf "$HOME/Library/HTTPStorages/com.contextlife.miru"
rm -rf "$HOME/Library/HTTPStorages/com.contextlife.shell"
rm -rf "$HOME/Library/HTTPStorages/miru-pet"
rm -f "$HOME/Library/HTTPStorages/com.contextlife.miru.binarycookies"
rm -f "$HOME/Library/HTTPStorages/com.contextlife.shell.binarycookies"
rm -f "$HOME/Library/HTTPStorages/miru-pet.binarycookies"

# ------------------------------------------------------------------
# 5. Old-style cookie jar (pre-macOS 14)
# ------------------------------------------------------------------
echo "[5/9] Removing legacy cookie jars…"
rm -f "$HOME/Library/Cookies/com.contextlife.miru.binarycookies"
rm -f "$HOME/Library/Cookies/com.contextlife.shell.binarycookies"
rm -f "$HOME/Library/Cookies/miru-pet.binarycookies"

# ------------------------------------------------------------------
# 6. Caches
# ------------------------------------------------------------------
echo "[6/9] Removing Caches…"
rm -rf "$HOME/Library/Caches/com.contextlife.miru"
rm -rf "$HOME/Library/Caches/com.contextlife.shell"
rm -rf "$HOME/Library/Caches/miru-pet"

# ------------------------------------------------------------------
# 7. NSUserDefaults (Preferences) — also clear the in-memory copy held
#    by cfprefsd, otherwise a relaunch will rewrite the plist from cache
# ------------------------------------------------------------------
echo "[7/9] Removing Preferences + flushing cfprefsd…"
rm -f "$HOME/Library/Preferences/com.contextlife.miru.plist"
rm -f "$HOME/Library/Preferences/com.contextlife.shell.plist"
rm -f "$HOME/Library/Preferences/miru-pet.plist"
defaults delete com.contextlife.miru 2>/dev/null || true
defaults delete com.contextlife.shell 2>/dev/null || true
defaults delete miru-pet 2>/dev/null || true

# ------------------------------------------------------------------
# 8. Saved Application State (window positions) + sandbox containers
# ------------------------------------------------------------------
echo "[8/9] Removing Saved State / Containers…"
rm -rf "$HOME/Library/Saved Application State/com.contextlife.miru.savedState"
rm -rf "$HOME/Library/Saved Application State/miru-pet.savedState"
rm -rf "$HOME/Library/Containers/com.contextlife.miru"
rm -rf "$HOME/Library/Containers/miru-pet"
rm -rf "$HOME/Library/Group Containers/com.contextlife.miru"

# ------------------------------------------------------------------
# 9. Keychain + /tmp temp files + leftover tunnel processes
# ------------------------------------------------------------------
echo "[9/9] Removing Keychain + /tmp + stray processes…"
security delete-generic-password -s Miru 2>/dev/null || true
security delete-generic-password -s miru 2>/dev/null || true
security delete-generic-password -s "com.contextlife.miru" 2>/dev/null || true
security delete-internet-password -s mirulife.top 2>/dev/null || true

rm -f /tmp/.pet.pid /tmp/.child_pids /tmp/miru_tunnel.log
rm -rf /tmp/miru-chunks /tmp/miru_test_* /tmp/miru_screen_test.png

# Kill any stray cloudflared tunnel that was pointed at our Flask port
pkill -f 'cloudflared tunnel --url http://localhost:5001' 2>/dev/null || true

# ------------------------------------------------------------------
# Verification — any Miru data left?
# ------------------------------------------------------------------
echo
echo "== Verification =="
left=$(find "$HOME/Library" \
    -iname "*contextlife*" -o -iname "*miru*" 2>/dev/null \
    | grep -v -E "/Chrome/|/Edge/|DiagnosticReports|mcp-feedback|com.apple.python" \
    | grep -v "ContextLife-airi" \
    | head)
if [[ -z "$left" ]]; then
    echo "✓ No Miru-related files left under ~/Library"
else
    echo "⚠ Some files survived (may be newly recreated by launchd/cfprefsd):"
    echo "$left"
fi

# Token-presence check — limit to Miru-relevant dirs only. Scanning
# the full ~/Library (Chrome/CloudKit/Caches) takes minutes and used
# to hang the whole script. 5s timeout is also a safety net.
tok_left=$(
  {
    grep -rl --include='*.json' --include='*.sqlite3*' --include='*.plist' \
      -- "QWWtp7wPBmAVoWqRF1TbgslP5KBiggjMluO" \
      "$HOME/Library/Application Support/Miru" \
      "$HOME/Library/WebKit/com.contextlife.miru" \
      "$HOME/Library/WebKit/miru-pet" \
      "$HOME/Library/HTTPStorages" \
      "$HOME/Library/Preferences" \
      "$HOME/Library/Caches/com.contextlife.miru" \
      2>/dev/null &
    _grep_pid=$!
    ( sleep 5 && kill -TERM "$_grep_pid" 2>/dev/null ) &
    wait "$_grep_pid" 2>/dev/null
  } | head -3
)
if [[ -n "$tok_left" ]]; then
    echo
    echo "⚠ auth_token still found in:"
    echo "$tok_left"
fi

# Clear pet singleton lock so next launch doesn't see a stale PID
rm -f "$HOME/Library/Application Support/Miru/data/.pet.lock" 2>/dev/null

echo
echo "Done. Next Miru launch should land on /login (invitation code)."
