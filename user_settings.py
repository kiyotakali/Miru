"""Per-user settings (timezone, screenshot interval, pet UI preferences).

Storage: {g.user_data_dir}/user_settings.json
Auth: reads Flask `g.user_data_dir` — must be called inside a user context.

Timezone fallback chain:
    user's `timezone` -> server_config's `default_timezone` -> system local
"""

from __future__ import annotations

import json
import os
import platform
import tempfile
import threading
from datetime import datetime


USER_FIELDS = {
    "timezone",
    "screenshot_enabled",
    "screenshot_perm_guided",
    "auto_screenshot_interval",
    "pet_hotkey",
    "pet_collapse_delay",
}

DEFAULT_SCREENSHOT_INTERVAL = 30
MIN_SCREENSHOT_INTERVAL = 5
MAX_SCREENSHOT_INTERVAL = 3600


_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[str, threading.RLock] = {}


def _path_lock(path: str) -> threading.RLock:
    normalized = os.path.abspath(path)
    with _LOCKS_GUARD:
        lock = _PATH_LOCKS.get(normalized)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[normalized] = lock
        return lock


def _data_dir() -> str | None:
    try:
        from flask import g
    except ImportError:
        return None
    try:
        data_dir = getattr(g, "user_data_dir", None)
    except RuntimeError:
        return None
    if data_dir:
        import storage
        return storage.get_data_dir()
    return None


def _config_path() -> str | None:
    d = _data_dir()
    if not d:
        return None
    return os.path.join(d, "user_settings.json")


def _load() -> dict:
    path = _config_path()
    if not path or not os.path.exists(path):
        return {}
    with _path_lock(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}


def _save(data: dict) -> bool:
    """Persist data. Returns True on success, False if no user context."""
    path = _config_path()
    if not path:
        return False
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    with _path_lock(path):
        fd, tmp = tempfile.mkstemp(
            dir=d,
            prefix=os.path.basename(path) + ".",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
    return True


def _default_pet_hotkey() -> str:
    # Cmd+Option+M on macOS: M = Miru; Option (alt) combos are rare in
    # mainstream apps. Avoids collisions with WeChat / 飞书 / QQ / 钉钉
    # (all use Cmd+Shift+A for screenshots) AND leaves the system's
    # Cmd+M (minimize window) intact.
    return "cmd+option+m" if platform.system() == "Darwin" else "ctrl+alt+m"


def get() -> dict:
    """Return user settings with defaults applied + clamping."""
    saved = _load()

    hotkey = saved.get("pet_hotkey") or _default_pet_hotkey()

    try:
        collapse = int(saved.get("pet_collapse_delay", 4000))
    except Exception:
        collapse = 4000
    collapse = min(max(collapse, 1000), 60000)

    try:
        interval = int(saved.get("auto_screenshot_interval", DEFAULT_SCREENSHOT_INTERVAL))
    except Exception:
        interval = DEFAULT_SCREENSHOT_INTERVAL
    interval = min(max(interval, MIN_SCREENSHOT_INTERVAL), MAX_SCREENSHOT_INTERVAL)

    return {
        "timezone": saved.get("timezone", ""),
        "screenshot_enabled": bool(saved.get("screenshot_enabled", False)),
        # Sticky one-way flag: once user has seen the first-launch screen
        # recording permission guide, never show it again. Decoupled from
        # screenshot_enabled (user may disable capture later but still has
        # seen the guide).
        "screenshot_perm_guided": bool(saved.get("screenshot_perm_guided", False)),
        "auto_screenshot_interval": interval,
        "pet_hotkey": hotkey,
        "pet_collapse_delay": collapse,
    }


def update(payload: dict) -> dict:
    """Filter payload to user fields and persist."""
    if not isinstance(payload, dict):
        return {"ok": False, "error": "invalid payload"}
    path = _config_path()
    if path is None:
        return {"ok": False, "error": "no user context"}

    with _path_lock(path):
        saved = _load()

        if "screenshot_enabled" in payload:
            saved["screenshot_enabled"] = bool(payload["screenshot_enabled"])

        if "screenshot_perm_guided" in payload:
            # One-way sticky flag — only allow setting to true (latching).
            # Prevents a buggy/malicious client from re-arming the modal.
            if bool(payload["screenshot_perm_guided"]):
                saved["screenshot_perm_guided"] = True

        if "pet_hotkey" in payload and payload["pet_hotkey"] is not None:
            hotkey = str(payload["pet_hotkey"] or "").strip().lower()
            if hotkey:
                saved["pet_hotkey"] = hotkey

        if "pet_collapse_delay" in payload and payload["pet_collapse_delay"] is not None:
            try:
                delay = int(payload["pet_collapse_delay"])
                saved["pet_collapse_delay"] = min(max(delay, 1000), 60000)
            except (ValueError, TypeError):
                pass

        if "auto_screenshot_interval" in payload and payload["auto_screenshot_interval"] is not None:
            try:
                interval = int(payload["auto_screenshot_interval"])
                saved["auto_screenshot_interval"] = min(
                    max(interval, MIN_SCREENSHOT_INTERVAL),
                    MAX_SCREENSHOT_INTERVAL,
                )
            except (ValueError, TypeError):
                pass

        if "timezone" in payload:
            tz = str(payload.get("timezone") or "").strip()
            if tz:
                try:
                    from zoneinfo import ZoneInfo
                    ZoneInfo(tz)
                    saved["timezone"] = tz
                except Exception:
                    return {"ok": False, "error": f"Invalid timezone: {tz}"}
            else:
                saved["timezone"] = ""

        _save(saved)
        return {"ok": True, "settings": get()}


def screenshot_interval() -> int:
    """Convenience helper for background threads that only need this value."""
    return get()["auto_screenshot_interval"]


def user_now() -> datetime:
    """Current time in the user's timezone, with fallbacks.

    Resolution: user.timezone -> server.default_timezone -> system local.
    """
    tz_name = get().get("timezone", "") or ""
    if not tz_name:
        try:
            import server_config
            tz_name = server_config.get().get("default_timezone", "") or ""
        except Exception:
            tz_name = ""
    if tz_name:
        try:
            from zoneinfo import ZoneInfo
            return datetime.now(ZoneInfo(tz_name))
        except Exception:
            pass
    return datetime.now()
