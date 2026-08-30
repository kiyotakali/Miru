#!/bin/bash
# scan_secrets.sh — Hardcoded-secret scanner for ContextLife / Miru.
#
# Scans either the source tree or a built artifact (DMG / APK contents)
# for forbidden strings: VPS-specific values that must never leak into
# distributed binaries (IP, owner identifiers, third-party API endpoints,
# credentials).
#
# Exit code:
#   0  — clean
#   1  — at least one violation
#
# Usage:
#   bash scripts/scan_secrets.sh                       # scan source tree
#   bash scripts/scan_secrets.sh dist/Miru.app         # scan DMG bundle
#   bash scripts/scan_secrets.sh path/to/app-debug.apk # scan APK (unzip first)
#
# Implementation notes:
#   - Single combined grep over all patterns is much faster than N greps.
#   - Allowlist filters paths/files that are *expected* to mention these
#     strings (deploy scripts, dev docs, internal config). A match outside
#     the allowlist is a real violation.
#   - Binary files and bulk dirs (.git, .venv, dist, build) are excluded
#     to keep the scan fast and free of false positives from compiled
#     fonts / certs / etc. that contain incidental high-entropy bytes.

set -uo pipefail

TARGET="${1:-.}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

case "$TARGET" in
  /*) ;;
  *) TARGET="$(cd "$REPO_ROOT" && cd "$TARGET" 2>/dev/null && pwd || echo "$REPO_ROOT/$TARGET")" ;;
esac

if [ ! -e "$TARGET" ]; then
  echo "❌ Target not found: $TARGET"
  exit 1
fi

echo "🔍 Scanning: $TARGET"
echo

# --- forbidden patterns + their human-readable reasons --------------------
# Use parallel arrays: PATTERN[i] ↔ REASON[i].
PATTERNS=(
    '110\.40\.153\.44'
    '101\.43\.110\.213'
    '/Users/kiyotakali'
    'qingyuntop'
    'sk-(or-v1-)?[A-Za-z0-9_-]{32,}'
    'ghp_[A-Za-z0-9]{30,}'
    'cfut_[A-Za-z0-9]{30,}'
    '-----BEGIN (OPENSSH|RSA|EC|PRIVATE) PRIVATE KEY-----'
)
REASONS=(
    'VPS public IP — must not appear in distributed binaries'
    'Historical test VPS public IP — must not appear in public source or binaries'
    'Maintainer-local absolute path — must not appear in public source or binaries'
    'Old LLM proxy hostname — should be backend-internal only'
    'OpenAI-style API key'
    'GitHub personal access token'
    'Cloudflare API token'
    'Private key material'
)

# --- allowlist: paths permitted to contain the patterns above -------------
# These are expected (deploy scripts, dev docs, the scanner itself).
# A match against any of these substrings in the path is filtered out.
# AWS key pattern intentionally NOT included — too noisy on binary fonts;
# rely on ghp_/cfut_/sk- which are far more specific.
ALLOW_PATHS_REGEX='scripts/scan_secrets\.sh|tests/test_no_secret_leakage|tests/test_client_provisioning|\.env\.example|^[^:]*data/|/data/users/|^[^:]*dist/'

# --- excluded directories (skipped entirely by grep) ----------------------
# Saves time + avoids binary-content false positives.
EXCLUDE_DIRS=(
    '.git' '.claude' '.venv' '__pycache__' 'node_modules'
    '.gradle' '.pytest_cache' 'intermediates' 'build' 'dist'
    'target'           # Rust / Tauri compiled output (multi-GB!)
    'cargo'            # Rust dependency cache
    '.cargo'
    'cache'
    '.cxx'             # Android NDK compile cache (full local paths inside)
    '.idea'            # IDE config (user paths)
    'captures'         # Android Studio captures (binaries)
)
EXCLUDE_DIR_ARGS=()
for d in "${EXCLUDE_DIRS[@]}"; do
    EXCLUDE_DIR_ARGS+=(--exclude-dir="$d")
done

# --- excluded file extensions (binary/non-source) -------------------------
EXCLUDE_FILES=(
    '*.pyc' '*.so' '*.dylib' '*.dll' '*.png' '*.jpg' '*.jpeg'
    '*.ico' '*.icns' '*.ttf' '*.woff' '*.woff2'
    '*.zip' '*.dmg' '*.apk' '*.aab'
    '*.cdi3.json' '*.moc3' '*.pose3.json' '*.physics3.json' '*.userdata3.json'
    'package-lock.json' 'yarn.lock'
)
EXCLUDE_FILE_ARGS=()
for f in "${EXCLUDE_FILES[@]}"; do
    EXCLUDE_FILE_ARGS+=(--exclude="$f")
done

# --- combined regex: matches if ANY single forbidden pattern hits ---------
COMBINED=$(IFS='|'; echo "${PATTERNS[*]}")

# Single grep pass: outputs "path:line:content" for each match.
# `grep -I` skips binary files. `-Ea` extended regex, treat as text.
ALL_MATCHES=$(grep -rIaE "$COMBINED" "$TARGET" \
    "${EXCLUDE_DIR_ARGS[@]}" \
    "${EXCLUDE_FILE_ARGS[@]}" \
    2>/dev/null \
    | grep -vE "$ALLOW_PATHS_REGEX" \
    || true)

# --- categorize each match by which pattern it hit ------------------------
VIOLATIONS=0

i=0
for pattern in "${PATTERNS[@]}"; do
    reason="${REASONS[$i]}"
    i=$((i + 1))
    matches=$(printf '%s' "$ALL_MATCHES" | grep -E -- "$pattern" || true)
    matches=$(printf '%s' "$matches" | sed '/^$/d')
    if [ -n "$matches" ]; then
        echo "🚨 Forbidden pattern matched: $pattern"
        echo "   ($reason)"
        echo "$matches" | head -n 8 | sed 's/^/   /'
        match_count=$(printf '%s\n' "$matches" | wc -l | tr -d ' ')
        if [ "$match_count" -gt 8 ]; then
            echo "   ... and $((match_count - 8)) more"
        fi
        echo
        VIOLATIONS=$((VIOLATIONS + 1))
    fi
done

if [ "$VIOLATIONS" -eq 0 ]; then
    echo "✅ Clean — no hardcoded secrets / forbidden strings found"
    exit 0
fi

echo "❌ Found $VIOLATIONS violation pattern(s) — block release"
exit 1
