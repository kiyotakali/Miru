"""ScreenSensor — device-side screenshot capture and change detection.

Captures screenshots locally, detects pixel-level changes, compresses
to JPEG, and POSTs to the central backend's /api/device/screenshot endpoint.

For the Mac (local backend), this POSTs to localhost. For remote devices,
it POSTs to the backend's LAN/public IP.

This module also contains OS-level idle detection (get_system_idle_seconds),
used by AttentionEngine / activity inference for local-device presence detection.
"""
from __future__ import annotations

import io
import os
import sys
import threading
import time
from datetime import datetime

from PIL import Image, ImageChops

# Config
CHECK_INTERVAL = 30       # default seconds between change-detection checks; lower bound is 5
MIN_INTERVAL = 5          # hard floor — sensor.py, screen_analyzer.py and Android must all agree
CHANGE_THRESHOLD = 0.30   # 30% pixel diff — keeps Miru's eyes from triggering on micro-changes (cursor, scroll, notification ticker). Match android/ScreenCaptureService.java.
THUMBNAIL_SIZE = (200, 150)
CAPTURE_MAX_SIZE = (1920, 1080)
JPEG_QUALITY = 60


# ---------------------------------------------------------------------------
# OS-level idle detection (keyboard/mouse inactivity)
# ---------------------------------------------------------------------------

def get_system_idle_seconds() -> float | None:
    """Seconds since last keyboard/mouse input. Returns None if unavailable."""
    try:
        if sys.platform == "darwin":
            return _get_idle_macos()
        elif sys.platform == "win32":
            return _get_idle_windows()
    except Exception:
        pass
    return None


def _get_idle_macos() -> float | None:
    import re
    import subprocess
    result = subprocess.run(
        ["ioreg", "-c", "IOHIDSystem", "-d", "4"],
        capture_output=True, text=True, timeout=5,
    )
    for line in result.stdout.split("\n"):
        if "HIDIdleTime" in line:
            match = re.search(r"= (\d+)", line)
            if match:
                return int(match.group(1)) / 1_000_000_000
    return None


def _get_idle_windows() -> float | None:
    import ctypes
    import ctypes.wintypes

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.wintypes.UINT),
            ("dwTime", ctypes.wintypes.DWORD),
        ]

    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
        return None
    millis = ctypes.windll.kernel32.GetTickCount() - lii.dwTime
    return millis / 1000.0


# ---------------------------------------------------------------------------
# ScreenSensor
# ---------------------------------------------------------------------------

class ScreenSensor:
    """Captures screenshots and POSTs changed frames to the central backend."""

    def __init__(self, backend_url: str = "http://localhost:5001",
                 device_id: str = "local", auth_token: str | None = None):
        self._backend_url = backend_url.rstrip("/")
        self._device_id = device_id
        self._auth_token = auth_token
        self._last_thumbnail: Image.Image | None = None
        self._running = False
        self._stop_event = threading.Event()
        # Separate wake event: lets set_interval() break the current sleep
        # without stopping the loop (which is what _stop_event.set() does).
        self._wake_event = threading.Event()
        # Track last screen active time (for local activity fallback)
        self._last_screen_active_time: datetime | None = None
        self._lock = threading.Lock()
        # Backoff: stop retrying after consecutive capture failures (e.g. no permission)
        self._capture_fail_count = 0
        self._capture_disabled = False
        self._capture_disabled_until = None  # auto-retry after this timestamp
        self._last_capture_display_index: int | None = None
        self._last_capture_display_name: str = ""
        self._last_capture_size: tuple[int, int] | None = None
        self._last_capture_error: str = ""
        self._last_capture_at: datetime | None = None
        # User-controlled on/off (default off — user must opt in)
        self._user_enabled = False
        # Capture interval (seconds). Overridden by user_settings on first sync
        # and by set_interval() when the user changes it at runtime.
        self._interval = CHECK_INTERVAL

    def start(self, interval: int | None = None):
        """Start the sensor loop in a background thread."""
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        if interval is not None:
            self._interval = max(int(interval), MIN_INTERVAL)
        t = threading.Thread(target=self._loop, daemon=True)
        t.start()
        print(f"[ScreenSensor] Started (every {self._interval}s, backend={self._backend_url})")

    def set_interval(self, seconds: int):
        """Update capture interval at runtime. Takes effect on the next tick."""
        try:
            seconds = max(int(seconds), MIN_INTERVAL)
        except (TypeError, ValueError):
            return
        if seconds != self._interval:
            print(f"[ScreenSensor] Interval updated: {self._interval}s → {seconds}s")
            self._interval = seconds
            # Wake the loop so the new interval takes effect on the very next
            # tick instead of waiting out the remainder of the old one.
            self._wake_event.set()

    def stop(self):
        """Stop the sensor loop. After this returns, no new screenshot
        will be taken even if a tick is already scheduled. Safe to call
        multiple times and from any thread.
        """
        self._user_enabled = False   # belt-and-braces: tick() early-returns
        self._stop_event.set()        # marks "done" for _loop's exit check
        self._wake_event.set()        # breaks the current _loop wait immediately
        self._running = False

    def get_last_screen_active_time(self) -> datetime | None:
        with self._lock:
            return self._last_screen_active_time

    def set_enabled(self, enabled: bool):
        was_enabled = self._user_enabled
        self._user_enabled = bool(enabled)
        if enabled:
            # Reset backoff so capture resumes immediately
            self._capture_disabled = False
            self._capture_disabled_until = None
            self._capture_fail_count = 0
            if not was_enabled:
                # An explicit opt-in should upload the current context even if
                # it resembles the last frame captured before a pause/account
                # switch. Wake the loop instead of waiting a full interval.
                with self._lock:
                    self._last_thumbnail = None
                self._wake_event.set()
        print(f"[ScreenSensor] {'Enabled' if enabled else 'Disabled'} by user")

    def is_enabled(self) -> bool:
        return self._user_enabled

    def update_config(self, backend_url: str | None = None,
                     device_id: str | None = None,
                     auth_token: str | None = None):
        """Update runtime config without recreating the sensor singleton.

        Why this exists: client mode flow re-calls `get_sensor()` after the
        user logs in or switches accounts (new token). Before this method,
        the second call returned the cached instance unchanged, so the
        sensor kept POSTing with the *previous* session's token → 401
        forever. Now `get_sensor()` forwards new params here so the
        already-running loop picks them up on the next tick.
        """
        identity_changed = False
        if backend_url:
            new_url = backend_url.rstrip("/")
            if new_url != self._backend_url:
                print(f"[ScreenSensor] backend_url updated: {self._backend_url} → {new_url}")
                self._backend_url = new_url
                identity_changed = True
        if device_id:
            if device_id != self._device_id:
                identity_changed = True
            self._device_id = device_id
        if auth_token is not None and auth_token != self._auth_token:
            tok_preview = (auth_token[:8] + "...") if auth_token else "(empty)"
            print(f"[ScreenSensor] auth_token updated (now: {tok_preview})")
            self._auth_token = auth_token
            # Reset capture backoff so the new token gets an immediate retry
            self._capture_disabled = False
            self._capture_fail_count = 0
            identity_changed = True
        if identity_changed:
            # Change detection is scoped to the selected backend/account.
            # Carrying a thumbnail across sessions can suppress the first
            # upload to the new account when both screens look similar.
            with self._lock:
                self._last_thumbnail = None
                self._last_screen_active_time = None
            # A running DMG process can switch from one remote account to
            # another without recreating the singleton. Do not carry the
            # previous account's in-memory screenshot toggle into the new
            # account; reload the current local per-device setting instead.
            self._sync_enabled_from_backend(force=True)

    def _sync_enabled_from_backend(self, force: bool = False):
        """Read screenshot_enabled setting from the LOCAL Flask, not the VPS.

        Why local: the sensor is a local process controlled by the user's
        local UI toggles and by the first-launch permission guide. The VPS
        copy of user_settings belongs to *phone*-initiated changes, and
        reading it here would silently overwrite a freshly opted-in local
        value (classic race: guide POSTs screenshot_enabled=true → set_enabled
        flips flag to true → _loop then reads VPS false → flag clobbered).
        """
        import requests as _req
        try:
            # LOCAL Flask — auth middleware lets client-mode requests through
            # without a token (auth.py trusts 127.0.0.1 in client mode).
            resp = _req.get("http://127.0.0.1:5001/api/user-settings", timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                settings = data.get("settings", data)
                # If the local flag is true we honor it. If false, we also
                # honor it — but we only overwrite the flag if our own stored
                # value isn't already true (protects against the race where
                # set_enabled(true) was called while this sync was pending).
                local_enabled = bool(settings.get("screenshot_enabled", False))
                if force or local_enabled or not self._user_enabled:
                    self._user_enabled = local_enabled
                # Also pick up the user's configured interval on first sync
                try:
                    new_int = int(settings.get("auto_screenshot_interval", self._interval))
                    self._interval = max(new_int, MIN_INTERVAL)
                except (TypeError, ValueError):
                    pass
                print(f"[ScreenSensor] Initial screenshot_enabled = {self._user_enabled}, "
                      f"interval = {self._interval}s (local)")
        except Exception as e:
            print(f"[ScreenSensor] Could not read initial setting: {e}")

    def _wait_for_backend(self):
        """Block until the Flask backend port is accepting connections."""
        import socket
        from urllib.parse import urlparse
        parsed = urlparse(self._backend_url)
        host = parsed.hostname or "localhost"
        port = parsed.port or 5001
        for attempt in range(60):
            if self._stop_event.is_set():
                return
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(1)
                s.connect((host, port))
                s.close()
                print(f"[ScreenSensor] Backend ready (took ~{attempt}s)")
                return
            except Exception:
                self._stop_event.wait(1)
        print(f"[ScreenSensor] Backend not ready after 60s, starting anyway")

    def _loop(self):
        self._wait_for_backend()
        self._sync_enabled_from_backend()
        while not self._stop_event.is_set():
            try:
                self.tick()
            except Exception as e:
                print(f"[ScreenSensor] Error: {e}")
            # Wait up to self._interval seconds. stop() and set_interval()
            # both set wake_event to break the wait immediately; the loop
            # condition above then either exits (stop) or continues with the
            # new interval (set_interval).
            self._wake_event.clear()
            self._wake_event.wait(self._interval)

    def tick(self):
        """One cycle: capture → thumbnail → pixel diff → POST if changed."""
        if not self._user_enabled:
            return
        # Soft-backoff: after 3 consecutive failures we pause capture for
        # ~5 min (then auto-retry) instead of permanently disabling. This
        # recovers automatically once macOS Screen Recording permission is
        # granted — no Miru restart required.
        if self._capture_disabled:
            from datetime import datetime as _dt
            if self._capture_disabled_until and _dt.now() < self._capture_disabled_until:
                return
            # Cooldown expired — re-try
            self._capture_disabled = False
            self._capture_fail_count = 0
            print("[ScreenSensor] Retrying screen capture after backoff cooldown")
        screenshot = self._capture_screenshot()
        if screenshot is None:
            self._capture_fail_count += 1
            detail = self._last_capture_error or "capture returned no image"
            print(
                f"[ScreenSensor] Capture attempt failed "
                f"({self._capture_fail_count}/3): {detail}"
            )
            if self._capture_fail_count >= 3:
                from datetime import datetime as _dt, timedelta as _td
                self._capture_disabled = True
                self._capture_disabled_until = _dt.now() + _td(minutes=5)
                print("[ScreenSensor] Screen capture was unavailable 3 times "
                      "consecutively. Backing off for 5 min before retry.")
            return
        self._capture_fail_count = 0  # reset on success

        thumbnail = screenshot.copy()
        thumbnail.thumbnail(THUMBNAIL_SIZE)

        if not self._screen_changed(thumbnail):
            return

        # Screen changed
        with self._lock:
            self._last_screen_active_time = datetime.now()

        # Compress to JPEG and POST
        try:
            screenshot.thumbnail(CAPTURE_MAX_SIZE)
            buf = io.BytesIO()
            screenshot.convert("RGB").save(buf, format="JPEG", quality=JPEG_QUALITY)
            jpeg_bytes = buf.getvalue()
            self._post_screenshot(jpeg_bytes)
        except Exception as e:
            print(f"[ScreenSensor] Compress/POST failed: {e}")

    def _screen_changed(self, thumbnail: Image.Image) -> bool:
        """Compare current thumbnail to previous. Returns True if >threshold change."""
        if self._last_thumbnail is None:
            self._last_thumbnail = thumbnail
            return True

        try:
            diff = ImageChops.difference(
                self._last_thumbnail.convert("RGB"),
                thumbnail.convert("RGB"),
            )
            pixels = list(diff.getdata())
            total = len(pixels)
            changed = sum(1 for r, g, b in pixels if max(r, g, b) > 15)
            ratio = changed / total if total > 0 else 0
        except Exception:
            ratio = 1.0

        self._last_thumbnail = thumbnail
        return ratio >= CHANGE_THRESHOLD

    def _capture_screenshot(self) -> Image.Image | None:
        """Capture the screen. Returns PIL Image or None."""
        try:
            if sys.platform == "darwin":
                return self._capture_macos()
            elif sys.platform == "win32":
                return self._capture_windows()
            else:
                from PIL import ImageGrab
                img = ImageGrab.grab()
                if img is None or img.size[0] == 0:
                    return None
                return img
        except Exception as e:
            print(f"[ScreenSensor] Screenshot failed: {e}")
            return None

    def _record_capture_debug(self, display_idx: int | None,
                              size: tuple[int, int] | None = None,
                              error: str = "", display_name: str = ""):
        with self._lock:
            self._last_capture_display_index = display_idx
            self._last_capture_display_name = display_name
            self._last_capture_size = size
            self._last_capture_error = error[:300] if error else ""
            self._last_capture_at = datetime.now()

    def _capture_windows(self) -> Image.Image | None:
        """Capture only the Windows monitor that currently owns the cursor."""
        from PIL import ImageGrab
        from windows.platform import get_cursor_monitor

        monitor = get_cursor_monitor()
        if monitor is None:
            self._record_capture_debug(None, error="no Windows monitor found")
            return None
        try:
            image = ImageGrab.grab(bbox=monitor.bbox, all_screens=True)
            if image is None or image.size[0] <= 0 or image.size[1] <= 0:
                self._record_capture_debug(
                    monitor.index,
                    error="empty Windows capture",
                    display_name=monitor.device_name,
                )
                return None
            self._record_capture_debug(
                monitor.index,
                size=image.size,
                display_name=monitor.device_name,
            )
            return image
        except Exception as exc:
            self._record_capture_debug(
                monitor.index,
                error=str(exc),
                display_name=monitor.device_name,
            )
            raise

    def probe_capture(self) -> dict:
        """Perform a user-requested capture probe without uploading pixels."""
        image = self._capture_screenshot()
        if image is None:
            return {
                "ok": False,
                "error": self._last_capture_error or "screen capture failed",
                "display_index": self._last_capture_display_index,
                "display_name": self._last_capture_display_name,
            }
        return {
            "ok": True,
            "width": image.size[0],
            "height": image.size[1],
            "display_index": self._last_capture_display_index,
            "display_name": self._last_capture_display_name,
        }

    def _capture_macos(self) -> Image.Image | None:
        """Capture the screen the mouse is on (multi-display aware).

        2026-05-17: macOS 14+ 实测 `screencapture` 不带 -D 时只截主屏
        (跟手册描述的"拼接所有屏"行为不一致). 用户接外接显示器在副屏工作
        时, 没有 -D 会丢掉副屏上下文 — Miru 看不到代码 / 文档. 改成根据
        鼠标所在屏来截, 直觉上跟随注意力焦点.

        `screencapture -D <n>` 是 1-indexed 屏幕索引 (1=主屏, 2=副屏...),
        和 NSScreen.screens() 顺序一致.
        """
        import subprocess
        import tempfile

        display_idx = self._mouse_screen_index()  # 1-based for -D flag

        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        tmp.close()
        try:
            cmd = ["screencapture", "-x", "-C"]
            if display_idx:
                cmd.append(f"-D{display_idx}")
            cmd.append(tmp.name)
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            if result.returncode != 0:
                err = (result.stderr or result.stdout or "screencapture failed").strip()
                self._record_capture_debug(display_idx, error=err)
                print(f"[ScreenSensor] screencapture failed "
                      f"(display={display_idx or 'default'}): {err[:160]}")
                return None
            if not os.path.exists(tmp.name) or os.path.getsize(tmp.name) == 0:
                self._record_capture_debug(display_idx, error="empty capture file")
                return None
            img = Image.open(tmp.name)
            img.load()
            self._record_capture_debug(display_idx, size=img.size)
            return img
        finally:
            try:
                os.unlink(tmp.name)
            except Exception:
                pass

    @staticmethod
    def _mouse_screen_index() -> int | None:
        """Find which NSScreen contains the mouse pointer.

        Returns 1-based index for `screencapture -D <n>`, or None if
        detection fails (caller falls back to default screencapture
        behavior, which captures the main display on macOS 14+).
        """
        try:
            from AppKit import NSScreen, NSEvent
            loc = NSEvent.mouseLocation()
            for i, s in enumerate(NSScreen.screens()):
                f = s.frame()
                if (f.origin.x <= loc.x <= f.origin.x + f.size.width
                        and f.origin.y <= loc.y <= f.origin.y + f.size.height):
                    return i + 1
        except Exception as e:
            print(f"[ScreenSensor] mouse screen lookup failed: {e}")
        return None

    def _post_screenshot(self, jpeg_bytes: bytes):
        """POST screenshot to backend /api/device/screenshot."""
        import requests

        url = f"{self._backend_url}/api/device/screenshot"
        headers = {}
        if self._auth_token:
            headers["Authorization"] = f"Bearer {self._auth_token}"

        try:
            resp = requests.post(
                url,
                files={"image": ("screenshot.jpg", jpeg_bytes, "image/jpeg")},
                data={"device_id": self._device_id},
                headers=headers,
                timeout=30,
            )
            if resp.status_code != 200:
                print(f"[ScreenSensor] POST failed: {resp.status_code}")
        except Exception as e:
            print(f"[ScreenSensor] POST error: {e}")

    def capture_now(self) -> bytes | None:
        """Capture and return JPEG bytes immediately (for look_at_screen tool)."""
        screenshot = self._capture_screenshot()
        if screenshot is None:
            return None
        screenshot.thumbnail(CAPTURE_MAX_SIZE)
        buf = io.BytesIO()
        screenshot.convert("RGB").save(buf, format="JPEG", quality=JPEG_QUALITY)
        return buf.getvalue()


# Singleton for local sensor
_instance: ScreenSensor | None = None


def get_sensor(backend_url: str | None = None,
               device_id: str | None = None,
               auth_token: str | None = None) -> ScreenSensor:
    """Get or create the local ScreenSensor singleton.

    Pass config on first call to customize. Subsequent calls forward any
    new backend_url / device_id / auth_token into the existing instance
    via update_config(), so the running loop picks up the latest values
    on the next tick. This is what makes account switching work — the
    old fire-and-forget singleton kept stale tokens forever.
    """
    global _instance
    if _instance is None:
        _instance = ScreenSensor(
            backend_url=backend_url or "http://localhost:5001",
            device_id=device_id or "local",
            auth_token=auth_token,
        )
    else:
        _instance.update_config(backend_url=backend_url,
                                 device_id=device_id,
                                 auth_token=auth_token)
    return _instance
