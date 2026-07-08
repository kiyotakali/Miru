#!/usr/bin/env bash
# Deploy the public Miru project page to the Miru VPS external homepage slot.
#
# Required:
#   MIRU_PAGE_SSH_HOST      SSH login, e.g. root@203.0.113.10
#   MIRU_PAGE_SSH_KEY_PATH  Private key file written by GitHub Actions
#
# Optional:
#   MIRU_PAGE_SSH_PORT      Default: 22
#   MIRU_PAGE_REMOTE_DIR    Default: /opt/miru/project_page
#   MIRU_PAGE_PUBLIC_URL    Default: https://mirulife.top/

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

SSH_HOST="${MIRU_PAGE_SSH_HOST:-}"
SSH_PORT="${MIRU_PAGE_SSH_PORT:-22}"
SSH_KEY_PATH="${MIRU_PAGE_SSH_KEY_PATH:-}"
REMOTE_BASE="${MIRU_PAGE_REMOTE_DIR:-/opt/miru/project_page}"
PUBLIC_URL="${MIRU_PAGE_PUBLIC_URL:-https://mirulife.top/}"

[[ -n "$SSH_HOST" ]] || { echo "::error::MIRU_PAGE_SSH_HOST is required"; exit 2; }
[[ -n "$SSH_KEY_PATH" ]] || { echo "::error::MIRU_PAGE_SSH_KEY_PATH is required"; exit 2; }
[[ -f "$SSH_KEY_PATH" ]] || { echo "::error::SSH key not found: $SSH_KEY_PATH"; exit 2; }
[[ -f index.html ]] || { echo "::error::index.html is missing"; exit 2; }
[[ -d _page ]] || { echo "::error::_page directory is missing"; exit 2; }
[[ -f _page/css/main.css ]] || { echo "::error::_page/css/main.css is missing"; exit 2; }
[[ -f _page/js/main.js ]] || { echo "::error::_page/js/main.js is missing"; exit 2; }

if ! grep -q '_page/' index.html; then
  echo "::error::index.html must reference _page/* assets to avoid /assets conflicts"
  exit 2
fi

SHA="${GITHUB_SHA:-}"
if [[ -z "$SHA" ]] && command -v git >/dev/null 2>&1; then
  SHA="$(git rev-parse --short=12 HEAD 2>/dev/null || true)"
fi
SHA="${SHA:-manual-$(date +%Y%m%d%H%M%S)}"

RELEASE_DIR="${REMOTE_BASE}/releases/${SHA}"
CURRENT_LINK="${REMOTE_BASE}/current"

SSH_OPTS=(
  -i "$SSH_KEY_PATH"
  -p "$SSH_PORT"
  -o IdentitiesOnly=yes
  -o StrictHostKeyChecking=no
  -o PasswordAuthentication=no
  -o ConnectTimeout=20
  -o ServerAliveInterval=15
)

quote() { printf "%q" "$1"; }

REMOTE_BASE_Q="$(quote "$REMOTE_BASE")"
RELEASE_DIR_Q="$(quote "$RELEASE_DIR")"
CURRENT_LINK_Q="$(quote "$CURRENT_LINK")"

echo "[miru-page] Deploying ${SHA} to ${SSH_HOST}:${RELEASE_DIR}"

ssh "${SSH_OPTS[@]}" "$SSH_HOST" "mkdir -p ${RELEASE_DIR_Q}"

export COPYFILE_DISABLE=1
tar -czf - index.html _page .nojekyll README.md | \
  ssh "${SSH_OPTS[@]}" "$SSH_HOST" "tar -xzf - -C ${RELEASE_DIR_Q}"

ssh "${SSH_OPTS[@]}" "$SSH_HOST" "
  set -euo pipefail
  mkdir -p ${REMOTE_BASE_Q}
  ln -sfn ${RELEASE_DIR_Q} ${CURRENT_LINK_Q}
  if [ -d ${REMOTE_BASE_Q}/releases ]; then
    find ${REMOTE_BASE_Q}/releases -mindepth 1 -maxdepth 1 -type d | sort | head -n -8 | xargs -r rm -rf
  fi
"

echo "[miru-page] Switched current -> ${SHA}"

if command -v curl >/dev/null 2>&1; then
  echo "[miru-page] Verifying ${PUBLIC_URL}"
  for attempt in 1 2 3; do
    html="$(curl -fsSL --max-time 20 "${PUBLIC_URL}" || true)"
    css_status="$(curl -fsSIL --max-time 20 "${PUBLIC_URL%/}/_page/css/main.css" | head -1 || true)"
    if [[ "$html" == *"Miru"* && "$css_status" == *"200"* ]]; then
      echo "[miru-page] Public page verification passed."
      exit 0
    fi
    echo "[miru-page] Verification attempt ${attempt}/3 failed; retrying..."
    sleep 3
  done
  echo "::error::Public page verification failed after deployment"
  exit 1
fi

echo "[miru-page] curl unavailable; skipped public verification."
