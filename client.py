"""Miru Client Mode — thin client for Mac/PC when backend runs on VPS.

Responsibilities:
  1. Screenshot capture → POST to remote VPS (sensor.py)
  2. Desktop pet window (Tauri) pointing to VPS
  3. Open browser to VPS web UI

All backend logic (AttentionEngine, memory, emotion, LLM calls) runs on the VPS.
This script only provides local screen observation + UI shell.

Usage:
  python3 client.py                          # uses saved config
  python3 client.py --server http://IP:5001  # first-time setup
"""
from __future__ import annotations

import json
import os
import platform
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "data" / "client_config.json"


def _load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_config(cfg: dict):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def _fetch_token(server_url: str) -> str | None:
    """Try to fetch token from server (only works from localhost on server side).
    Falls back to reading local data/auth.json.
    """
    # Try local auth.json first (synced from server)
    local_auth = ROOT / "data" / "auth.json"
    if local_auth.exists():
        try:
            with open(local_auth, "r") as f:
                return json.load(f).get("token", "")
        except Exception:
            pass
    return None


def setup(server_url: str = "", auth_token: str = "") -> dict:
    """Interactive setup for client mode."""
    cfg = _load_config()

    if server_url:
        cfg["server_url"] = server_url.rstrip("/")
    elif not cfg.get("server_url"):
        # Resolve default via discovery endpoint (operator-controllable);
        # falls back to bundled default if the discovery host is unreachable.
        from discovery import resolve_server_url as _disc
        default_url = _disc(cached=None)
        url = input(f"服务器地址 [回车使用默认 {default_url}]: ").strip().rstrip("/") or default_url
        cfg["server_url"] = url

    if auth_token:
        cfg["auth_token"] = auth_token
    elif not cfg.get("auth_token"):
        token = _fetch_token(cfg["server_url"])
        if token:
            cfg["auth_token"] = token
            print(f"[Client] Token loaded from local auth.json")
        else:
            token = input("Auth token (从 VPS data/auth.json 中获取): ").strip()
            if not token:
                print("Error: auth token is required")
                sys.exit(1)
            cfg["auth_token"] = token

    # Device name
    if not cfg.get("device_name"):
        cfg["device_name"] = platform.node() or "MacBook"

    _save_config(cfg)
    print(f"[Client] Config saved to {CONFIG_PATH}")
    return cfg


def start_sensor(cfg: dict) -> threading.Thread | None:
    """Start ScreenSensor in background thread, posting to VPS."""
    try:
        from sensor import get_sensor
        import device_manager

        device_id = device_manager.get_local_device_id()
        device_name = cfg.get("device_name", platform.node() or "MacBook")

        # Register this device with VPS
        try:
            import requests
            requests.post(
                f"{cfg['server_url']}/api/device/register",
                json={
                    "name": device_name,
                    "type": "desktop",
                    "platform": sys.platform,
                    "device_id": device_id,
                },
                headers={"Authorization": f"Bearer {cfg['auth_token']}"},
                timeout=5,
            )
            print(f"[Client] Device registered: {device_name} ({device_id})")
        except Exception as e:
            print(f"[Client] Device registration failed (non-fatal): {e}")

        sensor = get_sensor(
            backend_url=cfg["server_url"],
            device_id=device_id,
            auth_token=cfg["auth_token"],
        )
        sensor.start()
        print(f"[Client] ScreenSensor started → {cfg['server_url']}")

        # Start heartbeat in background
        def _heartbeat_loop():
            while True:
                try:
                    import requests as _req
                    _req.post(
                        f"{cfg['server_url']}/api/device/heartbeat",
                        json={"device_id": device_id},
                        headers={"Authorization": f"Bearer {cfg['auth_token']}"},
                        timeout=5,
                    )
                except Exception:
                    pass
                time.sleep(60)

        ht = threading.Thread(target=_heartbeat_loop, daemon=True)
        ht.start()

        return sensor
    except Exception as e:
        print(f"[Client] ScreenSensor failed to start: {e}")
        return None


def start_pet(cfg: dict):
    """Launch Tauri desktop pet pointing to VPS."""
    try:
        import user_settings

        pet_url = f"{cfg['server_url']}/pet?token={cfg['auth_token']}"
        pet_hotkey = user_settings.get().get("pet_hotkey",
            "cmd+option+m" if platform.system() == "Darwin" else "ctrl+alt+m")

        # Look for Tauri binary
        tauri_bin = None
        for candidate in [
            ROOT / "src-tauri" / "target" / "release" / "miru-pet",
            ROOT / "src-tauri" / "target" / "release" / "Miru Pet",
            ROOT / "src-tauri" / "target" / "debug" / "miru-pet",
        ]:
            if candidate.exists():
                tauri_bin = candidate
                break

        if tauri_bin is None:
            print("[Client] Tauri pet binary not found — skipping desktop pet")
            return None

        env = os.environ.copy()
        env["MIRU_PET_URL"] = pet_url
        env["MIRU_PET_HOTKEY"] = pet_hotkey

        proc = subprocess.Popen(
            [str(tauri_bin), "--url", pet_url, "--hotkey", pet_hotkey],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print(f"[Client] Desktop pet started (PID {proc.pid})")
        return proc
    except Exception as e:
        print(f"[Client] Desktop pet failed: {e}")
        return None


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Miru Client Mode")
    parser.add_argument("--server", help="VPS server URL (omit to auto-discover)")
    parser.add_argument("--token", help="Auth token")
    parser.add_argument("--no-browser", action="store_true", help="Don't open browser")
    parser.add_argument("--no-pet", action="store_true", help="Don't launch desktop pet")
    parser.add_argument("--no-sensor", action="store_true", help="Don't start screen sensor")
    args = parser.parse_args()

    print("=" * 50)
    print("  Miru Client Mode")
    print("  Screen sensor + UI shell (backend on VPS)")
    print("=" * 50)

    # Setup / load config
    cfg = setup(server_url=args.server or "", auth_token=args.token or "")
    server_url = cfg["server_url"]
    token = cfg["auth_token"]

    # Test connection
    try:
        import requests
        resp = requests.get(
            f"{server_url}/api/user-settings",
            headers={"Authorization": f"Bearer {token}"},
            timeout=5,
        )
        if resp.status_code == 200:
            print(f"[Client] Connected to VPS: {server_url}")
        else:
            print(f"[Client] Warning: VPS returned status {resp.status_code}")
    except Exception as e:
        print(f"[Client] Warning: Cannot reach VPS ({e})")
        print(f"[Client] Will keep trying in background...")

    # Start screen sensor
    if not args.no_sensor:
        start_sensor(cfg)

    # Open browser
    if not args.no_browser:
        url = f"{server_url}/?token={token}"
        print(f"[Client] Opening browser: {server_url}")
        webbrowser.open(url)

    # Start desktop pet
    pet_proc = None
    if not args.no_pet:
        pet_proc = start_pet(cfg)

    print()
    print(f"[Client] Running. Ctrl+C to stop.")
    print(f"[Client] VPS: {server_url}")
    print(f"[Client] Sensor: {'active' if not args.no_sensor else 'disabled'}")
    print(f"[Client] Pet: {'active' if pet_proc else 'disabled'}")
    print()

    # Keep main thread alive
    def _signal_handler(sig, frame):
        print("\n[Client] Shutting down...")
        if pet_proc and pet_proc.poll() is None:
            pet_proc.terminate()
        sys.exit(0)

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        _signal_handler(None, None)


if __name__ == "__main__":
    main()
