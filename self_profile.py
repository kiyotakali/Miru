"""Self identity profile for alias resolution."""

from __future__ import annotations

import json
import os
from datetime import datetime


def _data_dir() -> str:
    """Multi-tenant aware: prefer validated Flask user context."""
    fallback = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
    try:
        from flask import g
    except ImportError:
        return fallback
    try:
        data_dir = getattr(g, "user_data_dir", None)
    except RuntimeError:
        return fallback
    if data_dir:
        import storage
        return storage.get_data_dir()
    return fallback


def _config_path() -> str:
    return os.path.join(_data_dir(), "self_profile.json")


def _ensure_dir() -> None:
    os.makedirs(_data_dir(), exist_ok=True)


def _load_saved() -> dict:
    path = _config_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_saved(data: dict) -> None:
    _ensure_dir()
    with open(_config_path(), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _split_tokens(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        raw = value
    else:
        raw = str(value).replace("\n", ",").split(",")
    out = []
    seen = set()
    for item in raw:
        token = str(item or "").strip()
        if not token:
            continue
        key = token.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(token)
    return out


def _defaults() -> dict:
    return {
        "ok": True,
        "config_file": _config_path(),
        "canonical_name": "",
        "aliases": [],
        "notes": "",
        "updated_at": "",
    }


def _migrate_legacy(saved: dict) -> dict:
    """Migrate old format (display_name, platform_handles, checkboxes) into new."""
    changed = False

    # Merge display_name into canonical_name if canonical is empty
    if not saved.get("canonical_name") and saved.get("display_name"):
        saved["canonical_name"] = saved["display_name"]
        changed = True

    # Merge platform_handles into aliases
    handles = saved.pop("platform_handles", None)
    if handles and isinstance(handles, list):
        existing = _split_tokens(saved.get("aliases"))
        existing_lower = {a.lower() for a in existing}
        for item in handles:
            if isinstance(item, dict):
                handle = str(item.get("handle") or "").strip()
            else:
                handle = str(item or "").strip()
            if handle and handle.lower() not in existing_lower:
                existing.append(handle)
                existing_lower.add(handle.lower())
        saved["aliases"] = existing
        changed = True

    # Remove deprecated fields
    for key in ("display_name", "auto_resolve_aliases", "ask_when_uncertain"):
        if key in saved:
            saved.pop(key)
            changed = True

    if changed:
        saved["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _save_saved(saved)

    return saved


def get_profile() -> dict:
    saved = _load_saved()
    saved = _migrate_legacy(saved)
    cfg = _defaults()
    cfg.update({k: v for k, v in saved.items() if k in cfg})
    cfg["aliases"] = _split_tokens(cfg.get("aliases"))
    cfg["canonical_name"] = str(cfg.get("canonical_name") or "").strip()
    cfg["notes"] = str(cfg.get("notes") or "").strip()
    return cfg


def update_profile(payload: dict) -> dict:
    if not isinstance(payload, dict):
        return {"ok": False, "error": "invalid payload"}

    saved = _load_saved()
    saved = _migrate_legacy(saved)

    if "canonical_name" in payload:
        saved["canonical_name"] = str(payload.get("canonical_name") or "").strip()

    if "aliases" in payload:
        saved["aliases"] = _split_tokens(payload.get("aliases"))

    if "notes" in payload:
        saved["notes"] = str(payload.get("notes") or "").strip()

    saved["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _save_saved(saved)

    # Cascade authoritative fields into identity.json so the fact pipeline
    # picks up the user's declared name as confidence=1.0 ground truth.
    try:
        import identity
        identity.cascade_from_settings(payload)
    except Exception as e:
        print(f"[self_profile] identity cascade failed: {e}")

    # 2026-05-08: Broadcast data_changed so the memory hub on every open
    # tab / device invalidates its memory_slots + memory_tree cache and
    # re-fetches. Without this, you save aliases in settings and the
    # "用户身份" card on the memory hub still shows stale data until the
    # cache TTL expires or the user manually refreshes.
    try:
        import sse
        from flask import g
        uid = getattr(g, "user_id", None)
        sse.broadcast("data_changed", {"scope": "memory"}, user_id=uid)
    except Exception as e:
        print(f"[self_profile] data_changed broadcast failed: {e}")

    return {"ok": True, "profile": get_profile()}


def add_alias(alias: str) -> dict:
    token = str(alias or "").strip()
    if not token:
        return {"ok": False, "error": "alias cannot be empty"}
    current = get_profile()
    aliases = current.get("aliases", [])
    lowered = {a.lower() for a in aliases}
    if token.lower() not in lowered:
        aliases.append(token)
    return update_profile({"aliases": aliases})


def canonical_name(profile: dict | None = None) -> str:
    cfg = profile or get_profile()
    return str(cfg.get("canonical_name") or "").strip() or "我"


def alias_set(profile: dict | None = None) -> set[str]:
    cfg = profile or get_profile()
    names = set()
    names.add(canonical_name(cfg))
    for a in cfg.get("aliases", []):
        token = str(a or "").strip()
        if token:
            names.add(token)
            if token.startswith("@"):
                names.add(token[1:])
            elif not token.startswith("@"):
                names.add("@" + token)
    names.update({"我", "本人", "自己"})
    return {x for x in names if x}
