#!/bin/bash
# Miru Mac Client Installer
# Downloads dependencies, builds .app, sets up auto-start
set -e

MIRU_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PLIST_SRC="$MIRU_DIR/deploy/com.contextlife.miru.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.contextlife.miru.plist"

echo "=========================================="
echo "  Miru Mac Client Installer"
echo "=========================================="
echo ""
echo "Miru directory: $MIRU_DIR"
echo ""

# Step 1: Check Python 3
if ! command -v python3 &>/dev/null; then
    echo "Error: python3 not found. Please install Python 3.9+ first."
    exit 1
fi

PYTHON_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "[1/4] Python $PYTHON_VERSION found"

# Step 2: Install client dependencies
echo "[2/4] Installing dependencies..."
pip3 install --quiet rumps Pillow requests 2>/dev/null || \
    pip3 install --user --quiet rumps Pillow requests

echo "  Dependencies installed"

# Step 3: Create data directory
mkdir -p "$MIRU_DIR/data"

# Step 4: Set up LaunchAgent for auto-start
echo "[3/4] Setting up auto-start..."
mkdir -p "$HOME/Library/LaunchAgents"

# Generate plist with correct paths
cat > "$PLIST_DST" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.contextlife.miru</string>
    <key>ProgramArguments</key>
    <array>
        <string>$(which python3)</string>
        <string>$MIRU_DIR/client_app.py</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
    <key>WorkingDirectory</key>
    <string>$MIRU_DIR</string>
    <key>StandardOutPath</key>
    <string>/tmp/miru-client.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/miru-client.log</string>
</dict>
</plist>
EOF

echo "  LaunchAgent installed: $PLIST_DST"

# Step 5: Load LaunchAgent
echo "[4/4] Starting Miru..."
launchctl unload "$PLIST_DST" 2>/dev/null || true
launchctl load "$PLIST_DST"

echo ""
echo "=========================================="
echo "  Miru installed successfully!"
echo "=========================================="
echo ""
echo "  Look for the Miru icon in your menu bar."
echo "  First launch will ask for VPS server URL and token."
echo ""
echo "  Useful commands:"
echo "    Start:   launchctl load ~/Library/LaunchAgents/com.contextlife.miru.plist"
echo "    Stop:    launchctl unload ~/Library/LaunchAgents/com.contextlife.miru.plist"
echo "    Logs:    tail -f /tmp/miru-client.log"
echo "    Run:     python3 $MIRU_DIR/client_app.py"
echo ""
