#!/bin/bash
# Clear Miru.app's WebView caches WITHOUT logging the user out.
#
# What it removes:
#   - ~/Library/Caches/com.contextlife.miru/WebKit/NetworkCache/   (HTTP cache: JS/CSS/images)
#   - ~/Library/WebKit/com.contextlife.miru/WebsiteData/Default/*/IndexedDB/  (cachedFetch app cache)
#
# What it KEEPS:
#   - LocalStorage (miru_auth_token, miru_desktop_perm_guided, ...)
#   - Application Support config.json (Python launcher's auth_token)
#   - Keychain entries
#
# Use this when index.html / main.css ships an update and the WebView is
# serving stale JS. After running, just re-open Miru.app — you'll stay
# logged in and the next page load will fetch fresh JS.
#
# Usage: bash deploy/clean_mac_cache_keep_login.sh

set -uo pipefail

if [[ "$(uname)" != "Darwin" ]]; then
    echo "error: macOS only" >&2; exit 1
fi

echo "[1/4] Stopping Miru…"
killall Miru miru-pet 2>/dev/null || true
for i in 1 2 3; do
    if ! pgrep -q "Miru" && ! pgrep -q "miru-pet"; then break; fi
    sleep 1
done
# Force kill if still alive
killall -9 Miru miru-pet 2>/dev/null || true

echo "[2/4] Clearing NetworkCache (HTTP cache for JS/CSS)…"
NET_CACHE="$HOME/Library/Caches/com.contextlife.miru/WebKit/NetworkCache"
if [[ -d "$NET_CACHE" ]]; then
    rm -rf "$NET_CACHE"
    echo "  ✓ removed $NET_CACHE"
else
    echo "  (not present, skipping)"
fi

echo "[3/4] Clearing IndexedDB (cachedFetch app cache)…"
WEBKIT_DATA="$HOME/Library/WebKit/com.contextlife.miru/WebsiteData/Default"
if [[ -d "$WEBKIT_DATA" ]]; then
    found=0
    for hash_dir in "$WEBKIT_DATA"/*/; do
        for inner in "$hash_dir"*/; do
            if [[ -d "${inner}IndexedDB" ]]; then
                rm -rf "${inner}IndexedDB"
                echo "  ✓ removed ${inner}IndexedDB"
                found=1
            fi
        done
    done
    [[ $found -eq 0 ]] && echo "  (no IndexedDB dirs found, skipping)"
else
    echo "  (not present, skipping)"
fi

echo "[4/4] Verifying LocalStorage is preserved…"
LS_FOUND=""
if [[ -d "$WEBKIT_DATA" ]]; then
    for hash_dir in "$WEBKIT_DATA"/*/; do
        for inner in "$hash_dir"*/; do
            if [[ -d "${inner}LocalStorage" ]]; then
                size=$(du -sh "${inner}LocalStorage" 2>/dev/null | awk '{print $1}')
                echo "  ✓ kept ${inner}LocalStorage ($size)"
                LS_FOUND="1"
            fi
        done
    done
fi
[[ -z "$LS_FOUND" ]] && echo "  (no LocalStorage found — you may need to log in if this is a fresh install)"

echo
echo "Done. Open Miru.app — you'll stay logged in, JS reloads fresh."
