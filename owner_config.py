"""Instance-owner helpers for private-server settings.

Miru private-server v1 treats one Docker instance as one user's home. Some
settings, such as the 3-tier AI provider config, are stored once per instance
under ``data/_admin``. The normal app user therefore needs a narrow "owner"
permission to edit those instance settings without exposing the broader
``/api/admin/*`` surface.

The first active user who touches an owner endpoint claims the instance. If a
reset deletes that user while preserving ``_admin`` config, the next active
user may reclaim ownership; this keeps ``reset.sh --user-data`` usable.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime

import auth


_lock = threading.RLock()


def _owner_path() -> str:
    return os.path.join(auth._BASE_DATA_DIR, "_admin", "owner.json")


def _load_owner() -> dict:
    path = _owner_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_owner(data: dict) -> None:
    path = _owner_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _is_active_user(user_id: str | None) -> bool:
    if not user_id or user_id == "_admin":
        return False
    user = auth.get_user(user_id)
    return bool(user and user.get("status") == "active")


def ensure_instance_owner(user_id: str | None) -> dict:
    """Claim or verify instance ownership for ``user_id``.

    Returns a small result dict:
      - ok=True when the user is the owner after this call
      - ok=False with error/status_code otherwise

    No secrets are read or written here.
    """
    if not _is_active_user(user_id):
        return {"ok": False, "error": "active user required", "status_code": 401}

    now = datetime.now().isoformat(timespec="seconds")
    with _lock:
        data = _load_owner()
        owner_id = (data.get("owner_user_id") or "").strip()

        if owner_id and _is_active_user(owner_id):
            if owner_id != user_id:
                return {
                    "ok": False,
                    "error": "instance owner only",
                    "status_code": 403,
                    "owner_user_id": owner_id,
                }
            return {"ok": True, "owner_user_id": owner_id, "claimed": False}

        # First claim, or reclaim after reset/delete left a stale owner file.
        new_data = {
            "version": 1,
            "owner_user_id": user_id,
            "claimed_at": data.get("claimed_at") or now,
            "updated_at": now,
        }
        if owner_id and owner_id != user_id:
            new_data["reclaimed_from"] = owner_id
            new_data["reclaimed_at"] = now
        _save_owner(new_data)
        return {"ok": True, "owner_user_id": user_id, "claimed": True}


def get_public_owner_state() -> dict:
    """Return owner metadata safe for diagnostics/UI."""
    data = _load_owner()
    owner_id = (data.get("owner_user_id") or "").strip()
    return {
        "owner_user_id": owner_id or None,
        "has_owner": bool(owner_id),
        "claimed_at": data.get("claimed_at"),
        "updated_at": data.get("updated_at"),
    }
