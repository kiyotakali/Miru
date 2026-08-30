"""Multi-device registry and presence tracking.

Tracks connected devices (desktops, phones), their heartbeats, and
provides aggregated presence detection for AttentionEngine / activity inference.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import socket
import sys
import threading
import time
from datetime import datetime, timedelta

def _data_dir() -> str:
    try:
        import storage
        return storage.get_data_dir()
    except ImportError:
        return os.environ.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))


def _devices_path() -> str:
    return os.path.join(_data_dir(), "devices.json")
_lock = threading.Lock()


def _load_devices() -> list[dict]:
    path = _devices_path()
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return []
    return []


def _save_devices(devices: list[dict]):
    os.makedirs(_data_dir(), exist_ok=True)
    with open(_devices_path(), "w") as f:
        json.dump(devices, f, indent=2, ensure_ascii=False, default=str)


def register_device(name: str, device_type: str, device_platform: str,
                    device_id: str | None = None) -> dict:
    """Register a device. If device_id exists, update it; otherwise create new.

    Args:
        name: Human-readable name (e.g. "MacBook Pro", "Pixel 7")
        device_type: "desktop" | "phone" | "tablet"
        device_platform: "macos" | "windows" | "linux" | "android" | "ios" | "web"
        device_id: Optional explicit ID. Auto-generated if None.

    Returns:
        The device dict.
    """
    now = datetime.now().isoformat()
    with _lock:
        devices = _load_devices()
        if not device_id:
            device_id = f"{device_type}_{os.urandom(6).hex()}"

        # Update existing or create new
        existing = next((d for d in devices if d["device_id"] == device_id), None)
        if existing:
            existing["name"] = name
            existing["type"] = device_type
            existing["platform"] = device_platform
            existing["last_seen"] = now
        else:
            device = {
                "device_id": device_id,
                "name": name,
                "type": device_type,
                "platform": device_platform,
                "registered_at": now,
                "last_seen": now,
            }
            devices.append(device)
            existing = device

        _save_devices(devices)
        print(f"[DeviceManager] Registered: {existing['name']} ({device_id})")
        result = dict(existing)

    # Broadcast device list change (outside lock to avoid deadlock)
    _broadcast_device_list()
    return result


def heartbeat(device_id: str) -> bool:
    """Update last_seen for a device. Returns True if device exists.

    Also auto-beautifies ugly device names on heartbeat (one-time migration).
    """
    now = datetime.now().isoformat()
    with _lock:
        devices = _load_devices()
        for d in devices:
            if d["device_id"] == device_id:
                d["last_seen"] = now
                # Auto-beautify ugly hostnames (one-time migration)
                name = d.get("name", "")
                if ".local" in name or name.startswith("DESKTOP-"):
                    d["name"] = beautify_device_name(name, d.get("platform", ""))
                _save_devices(devices)
                return True
    return False


# Throttle device_list broadcasts — at most once per 5 seconds, per user
_last_device_broadcast = {}  # {user_id: epoch}

def _broadcast_device_list():
    """Broadcast device list to all SSE clients (throttled)."""
    try:
        from flask import g
        uid = getattr(g, "user_id", "_admin")
    except (RuntimeError, ImportError):
        uid = "_admin"
    now = time.time()
    if now - _last_device_broadcast.get(uid, 0.0) < 5:
        return
    _last_device_broadcast[uid] = now
    try:
        import sse
        devices = get_devices()
        online = get_online_devices()
        online_ids = {d["device_id"] for d in online}
        for d in devices:
            d["is_online"] = d["device_id"] in online_ids
        sse.broadcast("device_list_changed", {"devices": devices}, user_id=uid)
    except Exception:
        pass


def _parse_last_seen(d: dict) -> datetime | None:
    """Parse the last_seen timestamp from a device record. Returns None if missing/malformed."""
    try:
        return datetime.fromisoformat(d.get("last_seen", ""))
    except (KeyError, ValueError, TypeError):
        return None


# Cleanup tunables — exposed as module attrs so tests can monkeypatch
STALE_DAYS = 30  # devices unseen this long are dropped permanently


def _dedupe_and_cleanup(devices: list[dict]) -> tuple[list[dict], bool, dict[str, str]]:
    """Apply two cleanup rules and return (cleaned, did_change, merge_map).

    A. Drop devices whose last_seen is older than STALE_DAYS days.
       These are persistent device records that haven't reconnected
       in a month — almost certainly retired.

    B. Within each (name, platform) group, keep ONLY the entry with
       the most recent last_seen. Older duplicates are device_id rotations
       caused by LocalStorage clears (mobile pm clear / Vivo system cleanup
       / browser data wipe). The user perceives them as one physical
       device, so we hide all but the latest.

    `merge_map`: {old_device_id: kept_device_id} for the rule-B mergers.
    Callers (e.g. get_devices) use this to rewrite stale references in
    other JSON files like screenshot_log so historical data shows up
    under the correct (current) device record.

    Trade-off: if the user genuinely owns two same-model phones on the same
    account, they'll see only one. Acceptable — the alternative (multiple
    duplicates from LocalStorage rotation) is a far more common annoyance.
    """
    if not devices:
        return [], False, {}

    now = datetime.now()
    cutoff_30d = now - timedelta(days=STALE_DAYS)

    fresh = []
    for d in devices:
        ts = _parse_last_seen(d)
        # Missing/malformed last_seen → keep (don't lose data on bad records)
        if ts is None or ts > cutoff_30d:
            fresh.append(d)

    # Within (name, platform), keep latest by last_seen.
    # Track the kept device_id per group so we can build a merge_map of
    # losers → winner for downstream data rewrites.
    groups: dict[tuple, dict] = {}
    losers: list[dict] = []
    for d in fresh:
        key = (d.get("name", ""), d.get("platform", ""))
        existing = groups.get(key)
        if existing is None:
            groups[key] = d
            continue
        d_ts = _parse_last_seen(d) or datetime.min
        existing_ts = _parse_last_seen(existing) or datetime.min
        if d_ts > existing_ts:
            losers.append(existing)
            groups[key] = d
        else:
            losers.append(d)

    deduped = list(groups.values())

    merge_map: dict[str, str] = {}
    for loser in losers:
        loser_id = loser.get("device_id")
        winner = groups.get((loser.get("name", ""), loser.get("platform", "")))
        winner_id = winner.get("device_id") if winner else None
        if loser_id and winner_id and loser_id != winner_id:
            merge_map[loser_id] = winner_id

    did_change = (len(deduped) != len(devices))
    return deduped, did_change, merge_map


# Files that may contain device_id references as foreign keys.
# When a device is merged in dedupe, every entry in these files using a
# merged-away device_id becomes a dangling reference — UI will show the
# raw ID instead of a name, stats will split history into ghost rows,
# etc. The cascade rewrite below walks all of them.
#
# Adding a new data file with a device_id field? Add it here and write
# a regression test in test_device_dedupe.py to prove it cascades.
_DEVICE_ID_CASCADE_FILES = [
    # (storage path getter, list of dict-keys whose value is a device_id)
    # The "d" key is the compact form used by screenshot_log/emotion_log.
    ("screenshot_log_path", ("d", "device_id")),
    ("chat_history_path",   ("device_id",)),
    ("emotion_log_path",    ("d", "device_id")),
]


def _rewrite_device_ids_in_file(path: str, keys: tuple[str, ...],
                                merge_map: dict[str, str]) -> int:
    """Walk a JSON file's nested structure and rewrite any value found
    under one of `keys` if it's a known orphan in merge_map.

    Robust against shape variation: chat_history is a list of message
    dicts; screenshot_log/emotion_log are {date: [entries]} dicts.
    Both are handled by the same recursive walker.
    """
    if not os.path.exists(path):
        return 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, IOError):
        return 0

    counter = [0]

    def _walk(obj):
        if isinstance(obj, dict):
            for k, v in list(obj.items()):
                if k in keys and isinstance(v, str) and v in merge_map:
                    obj[k] = merge_map[v]
                    counter[0] += 1
                else:
                    _walk(v)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item)

    _walk(data)

    if counter[0]:
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, default=str)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except IOError:
            return 0
    return counter[0]


def _cascade_device_id_merge(merge_map: dict[str, str]) -> dict[str, int]:
    """For every cascade file declared in _DEVICE_ID_CASCADE_FILES,
    rewrite orphan device_id references onto their merge winners.

    Returns {filename: rewrite_count} for logging / metrics. A new data
    file using device_id as a foreign key MUST be added to
    _DEVICE_ID_CASCADE_FILES + covered by a test, otherwise old entries
    will surface as orphans the next time the user clears LocalStorage.
    """
    if not merge_map:
        return {}
    try:
        import storage
    except ImportError:
        return {}

    summary: dict[str, int] = {}
    for path_attr, keys in _DEVICE_ID_CASCADE_FILES:
        getter = getattr(storage, path_attr, None)
        if not callable(getter):
            continue
        try:
            path = getter()
        except Exception:
            continue
        n = _rewrite_device_ids_in_file(path, keys, merge_map)
        if n:
            summary[os.path.basename(path)] = n
    if summary:
        print(f"[DeviceManager] Cascade rewrite: {summary}")
    return summary


# Backwards-compat alias (existing tests reference this name).
def _rewrite_screenshot_log_device_ids(merge_map: dict[str, str]) -> int:
    """Single-file shim retained for the existing dedupe tests."""
    if not merge_map:
        return 0
    try:
        import storage
        path = storage.screenshot_log_path()
    except Exception:
        return 0
    return _rewrite_device_ids_in_file(path, ("d",), merge_map)


def get_devices() -> list[dict]:
    """Return registered devices after applying cleanup.

    Cleanup rules (transparent on read; persists if anything changed):
      - Drop devices with last_seen >30 days ago
      - Within same (name, platform), keep only the most-recent device_id
        (LocalStorage rotation defense)
      - Rewrite screenshot_log entries pointing at merged-away device_ids
        so historical screenshots show up under the surviving device.
    """
    with _lock:
        devices = _load_devices()
        cleaned, changed, merge_map = _dedupe_and_cleanup(devices)
        if changed:
            _save_devices(cleaned)
        if merge_map:
            # Cascade the merge across all data files holding device_id
            # foreign keys. Never let an IO blip in one file fail the
            # device list lookup itself.
            try:
                _cascade_device_id_merge(merge_map)
            except Exception as e:
                print(f"[DeviceManager] cascade rewrite failed: {e}")
        return cleaned


def get_online_devices(timeout_seconds: int = 120) -> list[dict]:
    """Return devices with a heartbeat within timeout_seconds.

    Operates on the post-cleanup device list — duplicates and stale
    records never appear here either.
    """
    devices = get_devices()  # already cleaned + deduped
    cutoff = time.time() - timeout_seconds
    result = []
    for d in devices:
        ts = _parse_last_seen(d)
        if ts is not None and ts.timestamp() > cutoff:
            result.append(d)
    return result


def is_any_device_active(timeout_seconds: int = 1800) -> bool:
    """True if any device had activity within timeout (default 30 min).

    Used by AttentionEngine / activity inference to determine if user is
    present across all devices.
    """
    return len(get_online_devices(timeout_seconds)) > 0


def record_device_activity(device_id: str):
    """Record activity (screenshot upload, chat) for a device.

    Same as heartbeat but semantically different — called on meaningful events.
    """
    heartbeat(device_id)


def beautify_device_name(raw_name: str, device_platform: str = "") -> str:
    """Turn an ugly hostname into a human-readable device name.

    Examples:
        "alex-de-MacBook-Air.local"    → "MacBook Air"
        "DESKTOP-A1B2C3"               → "Windows PC"
        "ubuntu-server"                → "Linux Server"
    """
    import re
    name = raw_name.strip()

    # Strip common suffixes
    name = re.sub(r'\.local$', '', name, flags=re.IGNORECASE)

    # macOS: extract Mac model from hostname like "usernameMacBook-Air" or "My-MacBook-Pro"
    mac_match = re.search(r'((?:Mac ?Book[- ]?(?:Air|Pro)?|iMac|Mac[- ]?Mini|Mac[- ]?Studio|Mac[- ]?Pro))', name, re.IGNORECASE)
    if mac_match:
        model = mac_match.group(1)
        # Normalize: "MacBook-Air" → "MacBook Air"
        model = model.replace("-", " ").replace("  ", " ").strip()
        # Title case
        return " ".join(w.capitalize() if w.lower() not in ("imac",) else "iMac" for w in model.split())

    # Windows: "DESKTOP-XXXXXXX" pattern
    if re.match(r'^DESKTOP-[A-Z0-9]+$', name, re.IGNORECASE):
        return "Windows PC"

    # Platform-based fallback
    plat = device_platform.lower()
    if plat in ("darwin", "macos"):
        return "Mac"
    if plat in ("win32", "windows"):
        return "Windows PC"
    if plat in ("linux",):
        return "Linux"

    # If name is short enough and readable, keep it
    if len(name) <= 20 and name.replace("-", "").replace("_", "").isalnum():
        return name

    return raw_name[:20]


def get_device_display_name(device_id: str) -> str:
    """Get the human-readable name of a device by its ID."""
    with _lock:
        devices = _load_devices()
    for d in devices:
        if d["device_id"] == device_id:
            return d.get("name", device_id)
    return device_id


def get_local_device_id() -> str:
    """Generate a stable device ID for the local Mac/PC based on hostname."""
    hostname = platform.node() or "unknown"
    h = hashlib.md5(hostname.encode()).hexdigest()[:8]
    return f"local_{h}"


def get_lan_ip() -> str:
    """Get the machine's LAN IP address (private/RFC1918 preferred).

    Scans all network interfaces for private IPs (192.168.x.x, 10.x.x.x,
    172.16-31.x.x). Falls back to the socket-connect trick if no private
    IP is found. This avoids returning VPN/virtual adapter addresses like
    198.18.x.x that phones on the same WiFi can't reach.
    """
    import re
    import subprocess

    # Step 1: Try to find a real private LAN IP from network interfaces
    try:
        if sys.platform == "darwin":
            result = subprocess.run(
                ["ifconfig"], capture_output=True, text=True, timeout=5,
            )
            # Find all "inet x.x.x.x" lines
            for match in re.finditer(r"inet (\d+\.\d+\.\d+\.\d+)", result.stdout):
                ip = match.group(1)
                if ip.startswith("127."):
                    continue
                # Check if it's a private IP (RFC 1918)
                if (ip.startswith("192.168.") or ip.startswith("10.") or
                        _is_172_private(ip)):
                    return ip
        elif sys.platform == "win32":
            result = subprocess.run(
                ["ipconfig"], capture_output=True, text=True, timeout=5,
            )
            for match in re.finditer(r"IPv4.*?:\s*(\d+\.\d+\.\d+\.\d+)", result.stdout):
                ip = match.group(1)
                if ip.startswith("127."):
                    continue
                if (ip.startswith("192.168.") or ip.startswith("10.") or
                        _is_172_private(ip)):
                    return ip
    except Exception:
        pass

    # Step 2: Fallback to socket-connect trick
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        pass

    # Step 3: Last resort
    try:
        return socket.gethostbyname(socket.gethostname())
    except Exception:
        return "127.0.0.1"


def _is_172_private(ip: str) -> bool:
    """Check if IP is in 172.16.0.0 - 172.31.255.255 range."""
    try:
        parts = ip.split(".")
        return parts[0] == "172" and 16 <= int(parts[1]) <= 31
    except (IndexError, ValueError):
        return False


def remove_device(device_id: str) -> bool:
    """Remove a device from the registry."""
    with _lock:
        devices = _load_devices()
        before = len(devices)
        devices = [d for d in devices if d["device_id"] != device_id]
        if len(devices) < before:
            _save_devices(devices)
            return True
    return False
