#!/bin/bash
# Start Miru with Cloudflare Tunnel for external access
# Usage: ./start_with_tunnel.sh
#
# This starts:
#   1. cloudflared tunnel on port 5001 (provides HTTPS URL)
#   2. app.py with TUNNEL_URL set (QR codes use tunnel URL)
#
# Your phone can scan the QR code from anywhere (not just same WiFi).

set -e
cd "$(dirname "$0")"

PORT=${PORT:-5001}
TUNNEL_LOG="/tmp/miru_tunnel.log"

cleanup() {
    echo ""
    echo "[Miru] Shutting down..."
    # Kill tunnel if running
    if [ -n "$TUNNEL_PID" ]; then
        kill "$TUNNEL_PID" 2>/dev/null || true
    fi
    # Kill app if running
    if [ -n "$APP_PID" ]; then
        kill "$APP_PID" 2>/dev/null || true
    fi
    exit 0
}
trap cleanup INT TERM

echo "[Miru] Starting Cloudflare tunnel on port $PORT..."
cloudflared tunnel --url "http://localhost:$PORT" > "$TUNNEL_LOG" 2>&1 &
TUNNEL_PID=$!

# Wait for tunnel URL to appear in logs (usually 2-4 seconds)
TUNNEL_URL=""
for i in $(seq 1 15); do
    TUNNEL_URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$TUNNEL_LOG" 2>/dev/null | head -1)
    if [ -n "$TUNNEL_URL" ]; then
        break
    fi
    sleep 1
done

if [ -z "$TUNNEL_URL" ]; then
    echo "[Miru] Warning: Could not detect tunnel URL. Falling back to LAN mode."
    echo "[Miru] Check $TUNNEL_LOG for details."
    export TUNNEL_URL=""
else
    echo "[Miru] Tunnel ready: $TUNNEL_URL"
    export TUNNEL_URL="$TUNNEL_URL"
fi

echo "[Miru] Starting app.py..."
echo ""
python3 app.py &
APP_PID=$!

echo ""
echo "============================================"
echo "  Miru is running!"
if [ -n "$TUNNEL_URL" ]; then
    echo "  Local:  http://localhost:$PORT"
    echo "  Tunnel: $TUNNEL_URL"
    echo ""
    echo "  Open Settings > Connect Phone to see QR code"
    echo "  Phone can connect from anywhere via tunnel URL"
else
    echo "  Local: http://localhost:$PORT"
    echo "  (Tunnel failed — phone must be on same WiFi)"
fi
echo "============================================"
echo ""
echo "Press Ctrl+C to stop."

# Wait for app to exit
wait "$APP_PID"
