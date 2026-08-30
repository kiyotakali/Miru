#!/bin/bash
# Build Miru.app + DMG for macOS
#
# Prerequisites:
#   - Python 3.12 venv with all deps installed
#   - Tauri pet binary built: cd src-tauri && cargo build --release
#
# Usage:
#   bash build_mac.sh

set -e

VERSION="0.2.0"
APP_NAME="Miru"
VENV=".venv/bin"

echo "=== Building ${APP_NAME} v${VERSION} ==="

# Step 0: Pre-build security scan — fail fast if hardcoded secrets / VPS
# identifiers slipped into the source. The same script gets run again on
# the built bundle (Step 6) so anything that was clean in source but got
# bundled by mistake (rare) also gets caught.
echo "[0/6] Pre-build secret scan (source)..."
if ! bash scripts/scan_secrets.sh; then
    echo "❌ Secret scan failed on source — fix violations before release"
    exit 1
fi

# Step 1: Ensure PyInstaller + rumps installed
echo "[1/6] Checking dependencies..."
${VENV}/python -m pip install -q pyinstaller rumps

# Step 2: PyInstaller build
echo "[2/6] PyInstaller..."
${VENV}/pyinstaller miru.spec --clean --noconfirm 2>&1 | grep -E "completed|ERROR|WARNING" | head -20

# Step 3: Inject Tauri pet binary
echo "[3/6] Injecting Tauri pet..."
TAURI_BIN="src-tauri/target/release/app"
if [ -f "$TAURI_BIN" ]; then
    cp "$TAURI_BIN" "dist/${APP_NAME}.app/Contents/MacOS/miru-pet"
    chmod +x "dist/${APP_NAME}.app/Contents/MacOS/miru-pet"
    echo "  OK: miru-pet injected"
else
    echo "  WARN: Tauri binary not found at $TAURI_BIN — pet will not work"
fi

# Step 4: Post-build secret scan on the actual .app bundle.
# Catches anything that the Python source happened to import or
# concatenate at build-time into the bundle (rare but possible).
echo "[4/6] Post-build secret scan (Miru.app)..."
if ! bash scripts/scan_secrets.sh "dist/${APP_NAME}.app"; then
    echo "❌ Secret scan failed on built bundle — do not release this DMG"
    exit 1
fi

# Step 5: Ad-hoc code sign
echo "[5/6] Signing..."
codesign --force --deep -s - "dist/${APP_NAME}.app"

# Step 6: Create DMG (with Applications drag UI)
echo "[6/6] Creating DMG..."
rm -f "dist/${APP_NAME}-${VERSION}.dmg" 2>/dev/null
STAGE_DIR="dist/dmg_stage"
rm -rf "$STAGE_DIR" && mkdir -p "$STAGE_DIR"
cp -R "dist/${APP_NAME}.app" "$STAGE_DIR/"
ln -s /Applications "$STAGE_DIR/Applications"
hdiutil create -volname "${APP_NAME}" \
    -srcfolder "$STAGE_DIR" \
    -ov -format UDZO \
    "dist/${APP_NAME}-${VERSION}.dmg"
rm -rf "$STAGE_DIR"

echo ""
echo "=== Build complete ==="
du -sh "dist/${APP_NAME}.app"
ls -lh "dist/${APP_NAME}-${VERSION}.dmg"
