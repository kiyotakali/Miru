"""Miru Mac Menu Bar App — lightweight client for VPS backend.

Sits in the macOS menu bar, runs ScreenSensor in the background,
and provides quick access to the Miru web UI.

Usage:
  python3 client_app.py                          # uses saved config
  python3 client_app.py --server http://IP:5001  # first-time setup
"""
from __future__ import annotations

import json
import os
import platform
import sys
import threading
import time
import webbrowser
from pathlib import Path

import rumps

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "data" / "client_config.json"


# ---------------------------------------------------------------------------
# Config helpers (shared with client.py)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Miru Menu Bar App
# ---------------------------------------------------------------------------

class MiruApp(rumps.App):
    def __init__(self, server_url: str = "", auth_token: str = ""):
        super().__init__("Miru", quit_button=None)

        # Load or initialize config
        self.cfg = _load_config()
        if server_url:
            self.cfg["server_url"] = server_url.rstrip("/")
        if auth_token:
            self.cfg["auth_token"] = auth_token
        if not self.cfg.get("device_name"):
            self.cfg["device_name"] = platform.node() or "MacBook"

        # State
        self._sensor = None
        self._sensor_running = False
        self._connected = False

        # Menu items
        self.status_item = rumps.MenuItem("Status: Starting...", callback=None)
        self.status_item.set_callback(None)  # non-clickable

        self.open_item = rumps.MenuItem("Open Miru", callback=self.open_browser)
        self.sensor_item = rumps.MenuItem("Pause Sensor", callback=self.toggle_sensor)
        self.settings_item = rumps.MenuItem("Settings...", callback=self.show_settings)
        self.quit_item = rumps.MenuItem("Quit Miru", callback=self.quit_app)

        self.menu = [
            self.status_item,
            None,  # separator
            self.open_item,
            self.sensor_item,
            None,
            self.settings_item,
            self.quit_item,
        ]

        # Check if we have config; if not, prompt on launch
        if not self.cfg.get("server_url") or not self.cfg.get("auth_token"):
            # Defer to after app starts
            rumps.Timer(self._first_time_setup, 1).start()
        else:
            _save_config(self.cfg)
            rumps.Timer(self._boot, 0.5).start()

    # -- Boot sequence -------------------------------------------------------

    def _boot(self, _=None):
        """Connect to VPS, register device, start sensor."""
        threading.Thread(target=self._boot_thread, daemon=True).start()

    def _boot_thread(self):
        server = self.cfg.get("server_url", "")
        token = self.cfg.get("auth_token", "")

        # Test connection
        try:
            import requests
            resp = requests.get(
                f"{server}/api/user-settings",
                headers={"Authorization": f"Bearer {token}"},
                timeout=5,
            )
            if resp.status_code == 200:
                self._connected = True
                self._update_status("Connected")
            else:
                self._update_status(f"Error ({resp.status_code})")
                return
        except Exception as e:
            self._update_status(f"Offline")
            # Retry in background
            threading.Thread(target=self._retry_connect, daemon=True).start()
            return

        # Register device
        self._register_device()

        # Start sensor
        self._start_sensor()

        # Start heartbeat
        threading.Thread(target=self._heartbeat_loop, daemon=True).start()

        # Auto-open browser on first connect
        webbrowser.open(f"{server}/?token={token}")

    def _retry_connect(self):
        """Retry connection every 30s until success."""
        for _ in range(120):  # up to 1 hour
            time.sleep(30)
            try:
                import requests
                resp = requests.get(
                    f"{self.cfg['server_url']}/api/user-settings",
                    headers={"Authorization": f"Bearer {self.cfg['auth_token']}"},
                    timeout=5,
                )
                if resp.status_code == 200:
                    self._connected = True
                    self._update_status("Connected")
                    self._register_device()
                    self._start_sensor()
                    threading.Thread(target=self._heartbeat_loop, daemon=True).start()
                    return
            except Exception:
                pass
        self._update_status("Failed to connect")

    def _register_device(self):
        try:
            import requests
            from device_manager import get_local_device_id
            device_id = get_local_device_id()
            requests.post(
                f"{self.cfg['server_url']}/api/device/register",
                json={
                    "name": self.cfg.get("device_name", platform.node()),
                    "type": "desktop",
                    "platform": sys.platform,
                    "device_id": device_id,
                    "token": self.cfg["auth_token"],
                },
                timeout=5,
            )
        except Exception as e:
            print(f"[MiruApp] Device registration failed: {e}")

    def _start_sensor(self):
        if self._sensor_running:
            return
        try:
            from sensor import get_sensor
            from device_manager import get_local_device_id

            device_id = get_local_device_id()
            self._sensor = get_sensor(
                backend_url=self.cfg["server_url"],
                device_id=device_id,
                auth_token=self.cfg["auth_token"],
            )
            self._sensor.start()
            self._sensor_running = True
            self.sensor_item.title = "Pause Sensor"
            self._update_status("Connected (Sensor On)")
        except Exception as e:
            print(f"[MiruApp] Sensor failed: {e}")
            self._update_status("Connected (Sensor Error)")

    def _heartbeat_loop(self):
        while self._connected:
            try:
                import requests
                from device_manager import get_local_device_id
                requests.post(
                    f"{self.cfg['server_url']}/api/device/heartbeat",
                    json={"device_id": get_local_device_id()},
                    headers={"Authorization": f"Bearer {self.cfg['auth_token']}"},
                    timeout=5,
                )
            except Exception:
                pass
            time.sleep(60)

    # -- Menu callbacks ------------------------------------------------------

    def open_browser(self, _):
        server = self.cfg.get("server_url", "")
        token = self.cfg.get("auth_token", "")
        if server and token:
            webbrowser.open(f"{server}/?token={token}")
        elif server:
            webbrowser.open(server)
        else:
            rumps.alert("No server configured", "Please configure the server in Settings.")

    def toggle_sensor(self, _):
        if self._sensor_running and self._sensor:
            self._sensor.stop()
            self._sensor_running = False
            self.sensor_item.title = "Resume Sensor"
            if self._connected:
                self._update_status("Connected (Sensor Paused)")
        else:
            self._start_sensor()

    def show_settings(self, _):
        # Server URL
        resp = rumps.Window(
            title="Miru Settings",
            message="VPS server URL:",
            default_text=self.cfg.get("server_url", ""),
            ok="Next",
            cancel="Cancel",
            dimensions=(320, 24),
        ).run()
        if not resp.clicked:
            return
        new_url = resp.text.strip().rstrip("/")
        if new_url:
            self.cfg["server_url"] = new_url

        # Auth token
        resp = rumps.Window(
            title="Miru Settings",
            message="Auth token:",
            default_text=self.cfg.get("auth_token", ""),
            ok="Save",
            cancel="Cancel",
            dimensions=(320, 24),
        ).run()
        if not resp.clicked:
            return
        new_token = resp.text.strip()
        if new_token:
            self.cfg["auth_token"] = new_token

        _save_config(self.cfg)
        rumps.notification("Miru", "Settings saved", "Reconnecting...")

        # Reconnect
        self._connected = False
        if self._sensor:
            self._sensor.stop()
            self._sensor_running = False
        self._boot()

    def quit_app(self, _):
        if self._sensor:
            self._sensor.stop()
        rumps.quit_application()

    # -- First-time setup ----------------------------------------------------

    def _first_time_setup(self, timer):
        timer.stop()

        from discovery import resolve_server_url as _disc
        _default_url = _disc(cached=None)
        resp = rumps.Window(
            title="Welcome to Miru",
            message=f"Server URL (leave empty for default {_default_url}):",
            ok="Next",
            cancel="Quit",
            default_text=_default_url,
            dimensions=(320, 24),
        ).run()
        if not resp.clicked:
            rumps.quit_application()
            return
        url = resp.text.strip().rstrip("/") or _default_url
        self.cfg["server_url"] = url

        resp = rumps.Window(
            title="Welcome to Miru",
            message="Enter your auth token\n(from VPS data/auth.json):",
            ok="Connect",
            cancel="Quit",
            dimensions=(320, 24),
        ).run()
        if not resp.clicked:
            rumps.quit_application()
            return
        token = resp.text.strip()
        if not token:
            rumps.alert("Error", "Auth token is required.")
            rumps.quit_application()
            return
        self.cfg["auth_token"] = token

        _save_config(self.cfg)
        self._boot()

    # -- Helpers -------------------------------------------------------------

    def _update_status(self, text: str):
        self.status_item.title = f"Status: {text}"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Miru Mac Menu Bar App")
    parser.add_argument("--server", help="VPS server URL")
    parser.add_argument("--token", help="Auth token")
    args = parser.parse_args()

    app = MiruApp(
        server_url=args.server or "",
        auth_token=args.token or "",
    )
    app.run()


if __name__ == "__main__":
    main()
