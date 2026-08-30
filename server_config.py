"""Server-level configuration (global, shared across all users).

Stores: port, flask_debug, VAPID keypair (push notifications are signed with
ONE keypair for all users — browser subscriptions are bound to this public key),
and default_timezone (fallback when a user has not set their own).

Location: {DATA_DIR}/_admin/server_config.json

Unlike user_settings, this module does NOT read Flask `g` — it's global and
safe to call at startup / from any background thread without a request context.

Admin-edit protocol: no HTTP endpoint exposed (yet). To change these values,
edit the JSON file directly and restart the server.
"""

from __future__ import annotations

import json
import os
import platform


def _base_data_dir() -> str:
    return os.environ.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))


def _admin_dir() -> str:
    return os.path.join(_base_data_dir(), "_admin")


def _config_path() -> str:
    return os.path.join(_admin_dir(), "server_config.json")


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _load() -> dict:
    path = _config_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(data: dict) -> None:
    os.makedirs(_admin_dir(), exist_ok=True)
    path = _config_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


SERVER_FIELDS = {
    "port",
    "flask_debug",
    "vapid_public_key",
    "vapid_private_key",
    "default_timezone",
}


def get() -> dict:
    """Return server config with defaults + env fallback. Ensures VAPID exists."""
    saved = _load()

    # port: saved > env > 5001
    port_env = os.environ.get("PORT")
    if "port" in saved:
        try:
            port = int(saved["port"] or 5001)
        except Exception:
            port = 5001
    elif port_env:
        try:
            port = int(port_env)
        except Exception:
            port = 5001
    else:
        port = 5001
    port = min(max(port, 1), 65535)

    # flask_debug: saved > env > False
    if "flask_debug" in saved:
        flask_debug = bool(saved["flask_debug"])
    else:
        flask_debug = _bool_env("FLASK_DEBUG", False)

    # VAPID: saved > env > auto-generate
    vapid_pub = saved.get("vapid_public_key") or os.environ.get("VAPID_PUBLIC_KEY") or ""
    vapid_pri = saved.get("vapid_private_key") or os.environ.get("VAPID_PRIVATE_KEY") or ""
    if not vapid_pub or not vapid_pri or vapid_pub == "test-key" or vapid_pri == "test-private":
        new_pub, new_pri = _generate_vapid()
        if new_pub and new_pri:
            vapid_pub, vapid_pri = new_pub, new_pri
            saved["vapid_public_key"] = vapid_pub
            saved["vapid_private_key"] = vapid_pri
            _save(saved)
            print("[server_config] Auto-generated new VAPID keypair")

    default_tz = saved.get("default_timezone") or os.environ.get("TIMEZONE", "") or ""

    return {
        "port": port,
        "flask_debug": flask_debug,
        "vapid_public_key": vapid_pub,
        "vapid_private_key": vapid_pri,
        "default_timezone": default_tz,
    }


def update(payload: dict) -> dict:
    """Filter payload to server fields and persist. Returns updated config."""
    if not isinstance(payload, dict):
        return {"ok": False, "error": "invalid payload"}
    saved = _load()

    if "port" in payload and payload["port"] is not None:
        try:
            port = int(payload["port"])
            if not (1 <= port <= 65535):
                raise ValueError()
            saved["port"] = port
        except Exception:
            return {"ok": False, "error": "port must be an integer between 1 and 65535"}

    if "flask_debug" in payload:
        saved["flask_debug"] = bool(payload["flask_debug"])

    if payload.get("clear_vapid_public_key"):
        saved["vapid_public_key"] = ""
    elif "vapid_public_key" in payload and payload["vapid_public_key"] is not None:
        saved["vapid_public_key"] = str(payload["vapid_public_key"]).strip()

    if payload.get("clear_vapid_private_key"):
        saved["vapid_private_key"] = ""
    elif "vapid_private_key" in payload and payload["vapid_private_key"] is not None:
        v = str(payload["vapid_private_key"]).strip()
        if v:
            saved["vapid_private_key"] = v

    if "default_timezone" in payload:
        tz = str(payload.get("default_timezone") or "").strip()
        if tz:
            try:
                from zoneinfo import ZoneInfo
                ZoneInfo(tz)
                saved["default_timezone"] = tz
            except Exception:
                return {"ok": False, "error": f"Invalid timezone: {tz}"}
        else:
            saved["default_timezone"] = ""

    _save(saved)
    return {"ok": True, "config": get()}


def _generate_vapid() -> tuple[str, str]:
    """Generate a new VAPID keypair. Returns (public, private_pem) or empty strs on failure."""
    try:
        import base64
        from py_vapid import Vapid
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        vp = Vapid()
        vp.generate_keys()
        priv = vp.private_pem().decode().strip()
        raw_pub = vp.public_key.public_bytes(encoding=Encoding.X962, format=PublicFormat.UncompressedPoint)
        pub = base64.urlsafe_b64encode(raw_pub).rstrip(b"=").decode()
        return pub, priv
    except Exception as e:
        print(f"[server_config] VAPID generation failed: {e}")
        return "", ""


# ---------------------------------------------------------------------------
# Legacy migration — runs once at startup, idempotent
# ---------------------------------------------------------------------------

def migrate_legacy_settings() -> dict:
    """Migrate old app_settings.json (mixed server+user fields) into two layers.

    Splits:
      - Server fields (port, flask_debug, vapid_*) → data/_admin/server_config.json
      - User fields (timezone, auto_screenshot_interval, pet_*) → data/users/<uid>/user_settings.json

    VAPID conflict resolution: prefers user-dir keypair (browser already
    subscribed to that public key).

    Idempotent: if server_config.json already exists, skips server migration.
    Per-user: if user_settings.json already exists, skips that user.

    Old files are renamed to .bak (not deleted), for one-version recovery window.

    Returns a summary dict for logging.
    """
    summary = {
        "server_migrated": False,
        "users_migrated": [],
        "vapid_source": None,
        "backups": [],
    }

    base = _base_data_dir()
    admin_dir = _admin_dir()
    users_dir = os.path.join(base, "users")

    # Candidate legacy files to scan for server fields
    candidates = []
    user_candidates = []  # (uid, path)
    for legacy in (os.path.join(base, "app_settings.json"),
                   os.path.join(admin_dir, "app_settings.json")):
        if os.path.exists(legacy):
            candidates.append(("root_or_admin", legacy))

    if os.path.isdir(users_dir):
        for uid in sorted(os.listdir(users_dir)):
            u_path = os.path.join(users_dir, uid, "app_settings.json")
            if os.path.exists(u_path):
                candidates.append(("user", u_path))
                user_candidates.append((uid, u_path))

    # --- Server migration -----------------------------------------------------
    if not os.path.exists(_config_path()):
        # Pass 1: read every candidate file once, collect payloads
        parsed: list[tuple[str, str, dict]] = []  # (kind, path, data)
        for kind, path in candidates:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                continue
            if isinstance(data, dict):
                parsed.append((kind, path, data))

        merged_server: dict = {}

        # Non-VAPID fields — first-seen wins, scan everything
        for _, _, data in parsed:
            for k in ("port", "flask_debug", "default_timezone"):
                if k in data and k not in merged_server:
                    merged_server[k] = data[k]

        # default_timezone fallback: any user-level `timezone` that's set
        if "default_timezone" not in merged_server:
            for _, _, data in parsed:
                tz = data.get("timezone")
                if tz:
                    merged_server["default_timezone"] = tz
                    break

        # VAPID — prefer a user-dir keypair (browser already subscribed there)
        best_vapid: tuple[str, str] | None = None
        best_vapid_source = None
        for kind, path, data in parsed:
            pub = data.get("vapid_public_key") or ""
            pri = data.get("vapid_private_key") or ""
            if not (pub and pri):
                continue
            if kind == "user":
                best_vapid = (pub, pri)
                best_vapid_source = path
                break  # lock in first user-dir keypair
            if best_vapid is None:
                best_vapid = (pub, pri)
                best_vapid_source = path
        if best_vapid:
            merged_server["vapid_public_key"] = best_vapid[0]
            merged_server["vapid_private_key"] = best_vapid[1]
            summary["vapid_source"] = best_vapid_source

        if merged_server:
            os.makedirs(admin_dir, exist_ok=True)
            _save(merged_server)
            summary["server_migrated"] = True
            print(f"[Migration] Wrote server_config.json with {len(merged_server)} fields "
                  f"(vapid source: {best_vapid_source or 'none'})")

    # --- Per-user migration ---------------------------------------------------
    USER_FIELDS = {
        "timezone",
        "auto_screenshot_interval",
        "pet_hotkey",
        "pet_collapse_delay",
        "pet_chat_position",
        "pet_toolbar_position",
    }
    for uid, old_path in user_candidates:
        new_path = os.path.join(os.path.dirname(old_path), "user_settings.json")
        if os.path.exists(new_path):
            continue  # already migrated
        try:
            with open(old_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        user_cfg = {k: data[k] for k in data if k in USER_FIELDS}
        if user_cfg:
            tmp = new_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(user_cfg, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, new_path)
            summary["users_migrated"].append(uid)
            print(f"[Migration] Wrote {new_path} ({len(user_cfg)} fields)")

    # --- Backup & remove old files -------------------------------------------
    for _, old_path in candidates:
        if not os.path.exists(old_path):
            continue
        bak = old_path + ".bak"
        try:
            if os.path.exists(bak):
                os.remove(bak)  # overwrite stale backup
            os.rename(old_path, bak)
            summary["backups"].append(bak)
            print(f"[Migration] Backed up {old_path} -> {bak}")
        except Exception as e:
            print(f"[Migration] Failed to backup {old_path}: {e}")

    return summary
