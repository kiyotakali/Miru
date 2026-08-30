#!/usr/bin/env bash
# Export a public-source snapshot without private operations material or
# third-party files which are not licensed under Miru's Apache-2.0 license.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SOURCE_REF="${1:-HEAD}"
DEST="${2:-$ROOT/public-source-export}"
RESOLVED_REF="$(git -C "$ROOT" rev-parse "${SOURCE_REF}^{commit}")"

if [ -e "$DEST" ] && [ "$(find "$DEST" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]; then
    echo "Destination must be empty: $DEST" >&2
    exit 1
fi

mkdir -p "$DEST"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

git -C "$ROOT" archive --format=tar "$RESOLVED_REF" -o "$TMP_DIR/source.tar"
tar -xf "$TMP_DIR/source.tar" -C "$DEST"

# Maintainer-only context, production deployment helpers, and live smoke tests.
rm -rf \
    "$DEST/.github" \
    "$DEST/AGENTS.md" \
    "$DEST/CLAUDE.md" \
    "$DEST/docs/AGENT_MEMORY_INDEX.md" \
    "$DEST/docs/AI_MAINTAINER_CONTEXT.md" \
    "$DEST/docs/PRIVATE_SERVER_RELEASE_TODO.md" \
    "$DEST/docs/PUBLIC_RELEASE_REPO_PLAN.md" \
    "$DEST/docs/PROJECT_PAGE_CODEX_COLLABORATOR.md" \
    "$DEST/docs/agent_memory" \
    "$DEST/docs/agent_review" \
    "$DEST/docs/releases" \
    "$DEST/deploy/update_project_page.sh" \
    "$DEST/deploy/update_vps.sh" \
    "$DEST/deploy/upload_release.sh" \
    "$DEST/deploy/release_real_api_smoke.sh" \
    "$DEST/scripts/claude_review_milestone.sh" \
    "$DEST/scripts/claude_stream_monitor.py" \
    "$DEST/scripts/release_real_api_smoke.py" \
    "$DEST/scripts/vps_daily_slot_writes_e2e.py" \
    "$DEST/scripts/vps_screen_semantic_gate_e2e.py" \
    "$DEST/tests/test_agent_docs_sync.py" \
    "$DEST/tests/test_agent_review_workflow.py" \
    "$DEST/tests/test_release_real_api_smoke.py"

# Live2D files use separate Live2D licenses. They are intentionally not
# relicensed or redistributed as part of Miru's Apache-2.0 source snapshot.
rm -rf \
    "$DEST/assets/js/live2d" \
    "$DEST/assets/live2d/Hiyori" \
    "$DEST/miru-mobile/android/app/src/main/cpp/CubismCore" \
    "$DEST/miru-mobile/android/app/src/main/cpp/Framework"

printf '%s\n' "$RESOLVED_REF" > "$DEST/.miru-runtime-source-ref"

blocked=$(find "$DEST" -type f \( \
    -path '*/docs/agent_memory/*' -o \
    -path '*/docs/agent_review/*' -o \
    -path '*/miru-mobile/android/app/src/main/cpp/CubismCore/*' -o \
    -path '*/miru-mobile/android/app/src/main/cpp/Framework/*' -o \
    -path '*/assets/live2d/Hiyori/*' -o \
    -path '*/assets/js/live2d/*' \
\) -print -quit)
if [ -n "$blocked" ]; then
    echo "Blocked path remained in export: $blocked" >&2
    exit 1
fi

echo "Public source exported from $RESOLVED_REF to $DEST"
