"""Model library — manages Live2D model files for the companion system.

Storage:
  - data/model_library.json  — model registry + active model ID
  - data/models/             — downloaded .zip model files
  - data/personas/<model_id>/ — per-model persona files (soul.md + avatar)
"""

from __future__ import annotations

import json
import os
import filecmp
import shutil
import uuid
import urllib.request
import zipfile
from datetime import datetime
from typing import Optional

import storage

def _data_dir_or_none() -> Optional[str]:
    """Resolve user data dir, returning None for anonymous/admin contexts.

    Avatar / soul lookups can be hit by exempt routes (login overlay
    fetches /assets/character-avatar.png before any auth) where Flask g
    has user_data_dir=None. Without this guard, os.path.join crashes
    with TypeError.
    """
    try:
        d = storage.get_data_dir()
        return d if isinstance(d, str) and d else None
    except Exception:
        return None


def _library_path() -> Optional[str]:
    d = _data_dir_or_none()
    return os.path.join(d, "model_library.json") if d else None


def _models_dir() -> Optional[str]:
    d = _data_dir_or_none()
    return os.path.join(d, "models") if d else None


def _personas_dir() -> Optional[str]:
    d = _data_dir_or_none()
    return os.path.join(d, "personas") if d else None


# Default soul.md and avatar (project root)
_PROJECT_ROOT = os.path.dirname(__file__)
_DEFAULT_SOUL_PATH = os.path.join(_PROJECT_ROOT, "soul.md")
_DEFAULT_AVATAR_PATH = os.path.join(_PROJECT_ROOT, "assets", "brand", "miru-avatar.png")
_LEGACY_DEFAULT_AVATAR_PATH = os.path.join(_PROJECT_ROOT, "airi.jpeg")


def _is_legacy_default_avatar(path: str) -> bool:
    """Return True when a persona avatar is the old bundled default copy."""
    if not path or not os.path.exists(path) or not os.path.exists(_LEGACY_DEFAULT_AVATAR_PATH):
        return False
    if os.path.abspath(path) == os.path.abspath(_LEGACY_DEFAULT_AVATAR_PATH):
        return True
    try:
        return filecmp.cmp(path, _LEGACY_DEFAULT_AVATAR_PATH, shallow=False)
    except OSError:
        return False


def _ensure_models_dir():
    os.makedirs(_models_dir(), exist_ok=True)


def _ensure_personas_dir():
    os.makedirs(_personas_dir(), exist_ok=True)


def _persona_dir(model_id: str) -> str:
    """Return the persona directory for a given model ID."""
    return os.path.join(_personas_dir(), model_id)


def _extract_thumbnail_from_zip(zip_path: str) -> bytes | None:
    """Try to extract icon.png from a Live2D zip as avatar.

    Note: texture spritesheets (texture_00.png etc.) are NOT used as thumbnails
    because they contain disassembled body parts. Real rendered previews come
    from the frontend via the Live2D auto-avatar capture mechanism.
    """
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            names = zf.namelist()
            for pattern in ("icon.png", "icon.jpg", "icon.jpeg"):
                for n in names:
                    if n.lower().endswith(pattern) or os.path.basename(n).lower() == pattern:
                        return zf.read(n)
    except Exception:
        pass
    return None


def _fetch_thumbnail_from_url(source_url: str) -> bytes | None:
    """Try to fetch icon.png from the same CDN directory as a model3.json URL.

    Note: texture spritesheets are NOT useful as thumbnails (they contain
    disassembled body parts). Real rendered previews come from the frontend
    via the auto-avatar capture mechanism.
    """
    if not source_url or not source_url.endswith(".json"):
        return None
    base_url = source_url.rsplit("/", 1)[0]
    for icon_name in ("icon.png", "icon.jpg"):
        try:
            url = f"{base_url}/{icon_name}"
            req = urllib.request.Request(url, headers={"User-Agent": "ContextLife"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    return resp.read()
        except Exception:
            continue
    return None


def _try_set_model_avatar(model_id: str, model: dict | None = None):
    """Try to extract a real thumbnail for a model from its zip or URL source."""
    pdir = _persona_dir(model_id)
    os.makedirs(pdir, exist_ok=True)

    if model is None:
        lib = _load_library()
        for m in lib.get("models", []):
            if m.get("id") == model_id:
                model = m
                break
    if not model:
        return

    thumb_data = None
    ext = "png"

    # Try zip extraction
    local_path = model.get("local_path", "")
    if local_path:
        full_path = os.path.join(storage.DATA_DIR, local_path) if not os.path.isabs(local_path) else local_path
        if os.path.exists(full_path):
            thumb_data = _extract_thumbnail_from_zip(full_path)

    # Try URL fetch
    if not thumb_data:
        source_url = model.get("source_url", "")
        if source_url:
            thumb_data = _fetch_thumbnail_from_url(source_url)

    if thumb_data:
        # Remove old default avatar if present
        for old_ext in ("jpeg", "jpg", "png", "webp"):
            old = os.path.join(pdir, f"avatar.{old_ext}")
            if os.path.exists(old):
                os.remove(old)
        dest = os.path.join(pdir, f"avatar.{ext}")
        with open(dest, "wb") as f:
            f.write(thumb_data)


def _init_persona_files(model_id: str, model: dict | None = None):
    """Create persona dir with copies of the default soul.md and avatar."""
    pdir = _persona_dir(model_id)
    os.makedirs(pdir, exist_ok=True)

    soul_dest = os.path.join(pdir, "soul.md")
    if not os.path.exists(soul_dest) and os.path.exists(_DEFAULT_SOUL_PATH):
        shutil.copy2(_DEFAULT_SOUL_PATH, soul_dest)

    # Try to extract a real thumbnail from the model data
    _try_set_model_avatar(model_id, model)

    # Fall back to default avatar if nothing was extracted
    avatar_exists = any(
        os.path.exists(os.path.join(pdir, f"avatar.{ext}"))
        for ext in ("jpeg", "jpg", "png", "webp")
    )
    if not avatar_exists and os.path.exists(_DEFAULT_AVATAR_PATH):
        ext = os.path.splitext(_DEFAULT_AVATAR_PATH)[1].lstrip(".") or "png"
        shutil.copy2(_DEFAULT_AVATAR_PATH, os.path.join(pdir, f"avatar.{ext}"))


def _cleanup_persona_files(model_id: str):
    """Remove persona directory for a deleted model."""
    pdir = _persona_dir(model_id)
    if os.path.isdir(pdir):
        shutil.rmtree(pdir, ignore_errors=True)


def _load_library() -> dict:
    """Load model_library.json; return empty structure if missing."""
    path = _library_path()
    if path and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {"active_model_id": None, "models": []}


def _save_library(lib: dict):
    path = _library_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(lib, f, ensure_ascii=False, indent=2)


def check_compatibility(source_url: str = "", local_path: str = "") -> dict:
    """Check Live2D model version (Cubism2 and Cubism4 are both compatible).

    Returns {"compatible": bool, "cubism": "4"|"2"|"unknown", "detail": str}.
    """
    # Check remote model.json / model3.json
    if source_url and source_url.endswith(".json"):
        try:
            req = urllib.request.Request(source_url, headers={"User-Agent": "ContextLife"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            # Cubism4: has FileReferences with .moc3
            if "FileReferences" in data:
                moc = data["FileReferences"].get("Moc", "")
                if moc.endswith(".moc3"):
                    return {"compatible": True, "cubism": "4", "detail": "Cubism4 model3.json"}
            # Cubism4 alt: Version == 3
            if data.get("Version") == 3:
                return {"compatible": True, "cubism": "4", "detail": "Cubism4 (Version 3)"}
            # Cubism2: has "model" key pointing to .moc
            if "model" in data and isinstance(data["model"], str):
                return {"compatible": True, "cubism": "2",
                        "detail": "Cubism2 格式 (model.json + .moc)"}
            return {"compatible": False, "cubism": "unknown",
                    "detail": "无法确定模型格式，可能不兼容桌宠渲染器"}
        except Exception as e:
            return {"compatible": False, "cubism": "unknown",
                    "detail": f"无法检测模型格式: {e}"}

    # Check local zip
    if local_path:
        full_path = os.path.join(storage.DATA_DIR, local_path) if not os.path.isabs(local_path) else local_path
        if os.path.exists(full_path):
            try:
                with zipfile.ZipFile(full_path, "r") as zf:
                    names = zf.namelist()
                    has_moc3 = any(n.endswith(".moc3") for n in names)
                    has_moc = any(n.endswith(".moc") and not n.endswith(".moc3") for n in names)
                    if has_moc3:
                        return {"compatible": True, "cubism": "4", "detail": "Cubism4 zip (.moc3)"}
                    if has_moc:
                        return {"compatible": True, "cubism": "2",
                                "detail": "Cubism2 格式 (.moc)"}
            except zipfile.BadZipFile:
                return {"compatible": False, "cubism": "unknown",
                        "detail": "文件不是有效的 zip 格式"}

    return {"compatible": True, "cubism": "unknown", "detail": "无法检测，假定兼容"}


# ---------- Public API ----------


def list_models() -> list:
    """Return all registered models."""
    return _load_library().get("models", [])


def get_active_model() -> dict | None:
    """Return the currently active model dict, or None."""
    lib = _load_library()
    active_id = lib.get("active_model_id")
    if not active_id:
        return None
    for m in lib.get("models", []):
        if m.get("id") == active_id:
            return m
    return None


def set_active_model(model_id: str) -> dict:
    """Set active model by ID. Returns the model dict or raises ValueError."""
    lib = _load_library()
    for m in lib.get("models", []):
        if m.get("id") == model_id:
            lib["active_model_id"] = model_id
            _save_library(lib)
            return m
    raise ValueError(f"Model '{model_id}' not found")


def add_model(name: str, format: str = "live2d-zip", local_path: str = "",
              source_url: str = "", preview_url: str = "", tags: list = None) -> dict:
    """Register a new model in the library and create its persona files."""
    _ensure_personas_dir()
    lib = _load_library()
    model_id = f"model-{uuid.uuid4().hex[:8]}"
    model = {
        "id": model_id,
        "name": name,
        "format": format,
        "source_url": source_url,
        "local_path": local_path,
        "preview_url": preview_url,
        "added_at": datetime.now().isoformat(),
        "tags": tags or [],
    }
    lib["models"].append(model)
    _save_library(lib)

    # Create persona files (soul.md + avatar extracted from model)
    _init_persona_files(model_id, model)

    return model


def remove_model(model_id: str) -> bool:
    """Remove a model from the library (and delete its file if present)."""
    lib = _load_library()
    models = lib.get("models", [])
    target = None
    for m in models:
        if m.get("id") == model_id:
            target = m
            break
    if not target:
        return False

    # Remove file
    local_path = target.get("local_path", "")
    if local_path:
        full_path = os.path.join(storage.DATA_DIR, local_path)
        if os.path.exists(full_path):
            try:
                os.remove(full_path)
            except OSError:
                pass

    models.remove(target)
    lib["models"] = models
    if lib.get("active_model_id") == model_id:
        lib["active_model_id"] = None
    _save_library(lib)

    # Clean up persona files
    _cleanup_persona_files(model_id)

    return True


def download_model(url: str, name: str) -> dict:
    """Download a .zip model from URL into data/models/ and register it."""
    _ensure_models_dir()

    # Sanitize filename
    safe_name = "".join(c if c.isalnum() or c in "._-" else "_" for c in name)
    if not safe_name.endswith(".zip"):
        safe_name += ".zip"
    dest_path = os.path.join(_models_dir(), safe_name)

    # Download
    urllib.request.urlretrieve(url, dest_path)

    # Register
    relative_path = os.path.join("models", safe_name)
    model = add_model(
        name=name,
        format="live2d-zip",
        local_path=relative_path,
        source_url=url,
    )
    return model


def get_model_layout(model_id: str) -> dict:
    """Return layout params for a model. Defaults: scale=1, x=0, y=0."""
    lib = _load_library()
    for m in lib.get("models", []):
        if m.get("id") == model_id:
            return m.get("layout", {"scale": 1, "x": 0, "y": 0})
    return {"scale": 1, "x": 0, "y": 0}


def save_model_layout(model_id: str, layout: dict) -> dict:
    """Save layout params (scale, x, y) for a model. Returns updated model."""
    lib = _load_library()
    for m in lib.get("models", []):
        if m.get("id") == model_id:
            m["layout"] = {
                "scale": float(layout.get("scale", 1)),
                "x": float(layout.get("x", 0)),
                "y": float(layout.get("y", 0)),
            }
            _save_library(lib)
            return m
    raise ValueError(f"Model '{model_id}' not found")


def rename_model(model_id: str, new_name: str) -> dict:
    """Rename a model. Returns the updated model dict or raises ValueError."""
    new_name = new_name.strip()
    if not new_name:
        raise ValueError("名称不能为空")
    lib = _load_library()
    for m in lib.get("models", []):
        if m.get("id") == model_id:
            m["name"] = new_name
            _save_library(lib)
            return m
    raise ValueError(f"Model '{model_id}' not found")


def find_models_by_name(query: str) -> list:
    """Fuzzy match models by name (case-insensitive substring)."""
    query_lower = query.lower()
    return [m for m in list_models() if query_lower in m.get("name", "").lower()]


# ---------- Persona helpers ----------


def get_persona_soul_path(model_id: str | None = None) -> str:
    """Return the soul.md path for a model. Falls back to project default."""
    if model_id:
        p = os.path.join(_persona_dir(model_id), "soul.md")
        if os.path.exists(p):
            return p
    return _DEFAULT_SOUL_PATH


def get_persona_avatar_path(model_id: str | None = None) -> str:
    """Return the avatar path for a model. Falls back to project default."""
    if model_id:
        pdir = _persona_dir(model_id)
        # Check for any common avatar extension
        legacy_default_seen = False
        for ext in ("jpeg", "jpg", "png", "webp"):
            p = os.path.join(pdir, f"avatar.{ext}")
            if os.path.exists(p):
                if _is_legacy_default_avatar(p):
                    legacy_default_seen = True
                    continue
                return p
        if legacy_default_seen:
            return _DEFAULT_AVATAR_PATH
    return _DEFAULT_AVATAR_PATH


def get_active_soul_path() -> str:
    """Return the soul.md path for the currently active model."""
    active = get_active_model()
    mid = active.get("id") if active else None
    return get_persona_soul_path(mid)


def get_active_avatar_path() -> str:
    """Return the avatar file path for the currently active model."""
    active = get_active_model()
    mid = active.get("id") if active else None
    return get_persona_avatar_path(mid)


def save_persona_avatar(model_id: str, file_data: bytes, ext: str = "jpeg") -> str:
    """Save uploaded avatar for a model. Returns the saved file path."""
    _ensure_personas_dir()
    pdir = _persona_dir(model_id)
    os.makedirs(pdir, exist_ok=True)
    # Remove old avatars
    for old_ext in ("jpeg", "jpg", "png", "webp"):
        old = os.path.join(pdir, f"avatar.{old_ext}")
        if os.path.exists(old):
            os.remove(old)
    dest = os.path.join(pdir, f"avatar.{ext}")
    with open(dest, "wb") as f:
        f.write(file_data)
    return dest


def save_persona_soul(model_id: str, content: str):
    """Write soul.md content for a specific model."""
    _ensure_personas_dir()
    pdir = _persona_dir(model_id)
    os.makedirs(pdir, exist_ok=True)
    with open(os.path.join(pdir, "soul.md"), "w", encoding="utf-8") as f:
        f.write(content)


def read_persona_soul(model_id: str) -> str:
    """Read soul.md content for a specific model. Returns empty string if missing."""
    p = get_persona_soul_path(model_id)
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8") as f:
            return f.read()
    return ""


def ensure_existing_models_have_personas():
    """Backfill persona files for models registered before the persona system."""
    _ensure_personas_dir()
    for m in list_models():
        mid = m.get("id", "")
        if mid and not os.path.isdir(_persona_dir(mid)):
            _init_persona_files(mid, m)


def is_default_avatar(model_id: str) -> bool:
    """Check if a model is still using the default avatar."""
    current = get_persona_avatar_path(model_id)
    if current == _DEFAULT_AVATAR_PATH:
        return True
    if not os.path.exists(_DEFAULT_AVATAR_PATH):
        return False
    default_size = os.path.getsize(_DEFAULT_AVATAR_PATH)
    return os.path.exists(current) and os.path.getsize(current) == default_size


def refresh_all_model_avatars() -> dict:
    """Re-extract thumbnails from model sources for all models that still use the default avatar."""
    results = {}
    default_size = None
    if os.path.exists(_DEFAULT_AVATAR_PATH):
        default_size = os.path.getsize(_DEFAULT_AVATAR_PATH)

    for m in list_models():
        mid = m.get("id", "")
        if not mid:
            continue
        pdir = _persona_dir(mid)
        # Check if current avatar is the bundled default.
        current = get_persona_avatar_path(mid)
        is_default = False
        if current == _DEFAULT_AVATAR_PATH:
            is_default = True
        elif default_size and os.path.exists(current):
            is_default = os.path.getsize(current) == default_size

        if is_default:
            _try_set_model_avatar(mid, m)
            new_path = get_persona_avatar_path(mid)
            results[mid] = "updated" if new_path != _DEFAULT_AVATAR_PATH else "no_source"
        else:
            results[mid] = "already_custom"
    return results
