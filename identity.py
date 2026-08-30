"""Authoritative identity store for the user.

This is the **ground truth** layer of the memory system. Anything written
here comes directly from the user (onboarding answers, settings page
edits) and is treated as immutable fact by every downstream consumer:

  · Main agent system prompt
  · Journal generation
  · memory_router 4-pass LLM (injected as ground-truth block in Pass 1+4)
  · VLM screenshot prompt (so OCR doesn't mis-read the user's name)

Why a separate file from ``self_profile.json`` and ``core_memory.json``:

  · ``self_profile`` exists as alias-resolution config (aliases / notes).
  · ``core_memory`` is a free-form text block consumed by the main agent.
  · Neither is structured enough to drive automatic ground-truth checks
    in the Bayesian fact pipeline. We need a typed, structured schema
    that tells the resolver "user.name is X with confidence 1.0, never
    let any fact contradict it".

Schema (data/users/<uid>/identity.json)::

    {
      "version": 1,
      "name": "<user-supplied>",
      "role": "<student/grad/working/...>",   # 学生 / 研究生 / 工作 / ...
      "occupation": "<free text>",            # 用户填写的研究/工作方向
      "schedule": "<early/standard/night/...>",  # 早起 / 标准 / 夜猫子 / 弹性
      "style": "<interaction-style>",          # interaction style preference
      "aliases": ["alex", "小明A", ...],       # 别名 / 多平台用户名
      "notes": "",                 # free user notes
      "updated_at": "...",
      "source_versions": {         # which input wrote each field last
        "name": "onboarding",
        "role": "settings",
        ...
      }
    }

All fields are optional — empty string / empty list means "user hasn't
told us yet". ``get_authoritative_facts()`` only emits facts for fields
that are populated, so we never assert facts we don't actually know.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from typing import Optional


_lock = threading.RLock()

# Canonical fields the identity file knows about. Order matters — it
# determines the order facts are emitted into prompts.
#
# 2026-05-08: aliases is a LIST (not str) — multi-platform usernames the
# user goes by. All other fields are strings. update() and
# get_authoritative_facts() special-case this.
_FIELDS = ("name", "role", "occupation", "schedule", "style", "aliases", "notes")
_LIST_FIELDS = frozenset({"aliases"})


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


def _path() -> str:
    return os.path.join(_data_dir(), "identity.json")


def _empty() -> dict:
    return {
        "version": 1,
        "name": "",
        "role": "",
        "occupation": "",
        "schedule": "",
        "style": "",
        "aliases": [],
        "notes": "",
        "updated_at": "",
        "source_versions": {},
    }


def _load() -> dict:
    p = _path()
    if not os.path.exists(p):
        return _empty()
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return _empty()
        # Fill in missing keys (forward-compat)
        base = _empty()
        base.update({k: v for k, v in data.items() if k in base})
        # source_versions is a dict, special-case
        sv = data.get("source_versions") or {}
        base["source_versions"] = sv if isinstance(sv, dict) else {}
        return base
    except (json.JSONDecodeError, OSError):
        return _empty()


def _save(data: dict) -> bool:
    os.makedirs(_data_dir(), exist_ok=True)
    p = _path()
    tmp = p + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
        return True
    except OSError as e:
        print(f"[identity] save failed: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────

def get() -> dict:
    """Load identity dict. Always returns the full schema (empty strings
    for fields the user hasn't told us yet)."""
    with _lock:
        return _load()


def update(payload: dict, *, source: str = "settings") -> dict:
    """Update one or more fields. ``source`` tags which input wrote the
    field (onboarding / settings / chat / ...) for downstream
    introspection.

    Empty-string values are *not* written (they would erase the user's
    real answer). Pass ``None`` if you genuinely want to clear a field
    (rare).
    """
    if not isinstance(payload, dict):
        return {"ok": False, "error": "invalid payload"}

    with _lock:
        data = _load()
        changed = []
        for k, v in payload.items():
            if k not in _FIELDS:
                continue

            # List-valued fields (currently only "aliases") follow a
            # different normalization path: dedupe + strip + drop empties.
            if k in _LIST_FIELDS:
                if v is None:
                    if data.get(k):
                        data[k] = []
                        data["source_versions"][k] = source
                        changed.append(k)
                    continue
                if not isinstance(v, (list, tuple)):
                    continue  # ignore mistyped input
                cleaned = []
                seen = set()
                for item in v:
                    s = str(item or "").strip()
                    if not s:
                        continue
                    key = s.lower()
                    if key in seen:
                        continue
                    seen.add(key)
                    cleaned.append(s)
                if data.get(k) == cleaned:
                    continue  # no-op
                data[k] = cleaned
                data["source_versions"][k] = source
                changed.append(k)
                continue

            # String-valued fields
            if v is None:
                # explicit clear
                if data.get(k):
                    data[k] = ""
                    data["source_versions"][k] = source
                    changed.append(k)
                continue
            v_str = str(v).strip()
            if not v_str:
                # empty string treated as "no answer this turn"
                continue
            if data.get(k) == v_str:
                continue  # no-op
            data[k] = v_str
            data["source_versions"][k] = source
            changed.append(k)

        if changed:
            data["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            _save(data)

        # Cascade to slots/self/identity facts.json — single store for
        # UI display and prompt injection. identity.json is kept as a
        # backup / migration shim, not the authoritative read source.
        if changed:
            try:
                import curator
                slot_payload = {k: data.get(k, "") for k in _FIELDS if data.get(k)}
                curator.cascade_identity(slot_payload)
            except Exception as e:
                print(f"[identity] slot cascade failed: {e}")

        return {"ok": True, "changed": changed, "identity": data}


def get_authoritative_facts() -> list[dict]:
    """Return a list of facts derived from the populated identity fields.

    Compatibility shim: kept for the ground_truth fallback path used by
    pre-cascade users (their slot doesn't have a body yet, so the
    renderer falls back to building a block from this list directly).

    Each fact has fields::

        {category, text, source_type, source_origin, confidence, pinned, asserted_at}

    Empty fields produce no facts.
    """
    data = _load()
    facts = []
    field_to_category = {
        "name": "name",
        "role": "role",
        "occupation": "occupation",
        "schedule": "schedule",
        "style": "style",
        "aliases": "aliases",
        "notes": "notes",
    }
    for f in _FIELDS:
        raw = data.get(f, "")
        # List-valued fields: emit ONE fact whose text is "a, b, c".
        if f in _LIST_FIELDS:
            if not isinstance(raw, list):
                continue
            cleaned = [str(x).strip() for x in raw if str(x).strip()]
            if not cleaned:
                continue
            v_text = ", ".join(cleaned)
        else:
            if not isinstance(raw, str):
                continue
            v_text = raw.strip()
            if not v_text:
                continue
        facts.append({
            "category": field_to_category[f],
            "text": v_text,
            "source_type": "identity",
            "source_origin": data.get("source_versions", {}).get(f, "user"),
            "confidence": 1.0,
            "pinned": True,
            "asserted_at": data.get("updated_at", ""),
        })
    return facts


def compose_ground_truth_block(*, header: bool = True) -> str:
    """Produce a markdown block ready to inject into LLM prompts.

    Source of truth: pinned facts in slots/self/identity (single store
    for both UI display and LLM prompts — see renderer.py docstring).

    The legacy identity.json store is consulted as a fallback only when
    the slot is empty (e.g. just-migrated user); cascade_from_* writes
    to BOTH the slot AND identity.json so they stay in sync.
    """
    # Primary: render from the slot (what the user sees on screen)
    try:
        import renderer
        block = renderer.render_slot("self", "identity", mode="ground_truth")
        if isinstance(block, str) and block.strip():
            return block if header else _strip_header(block)
    except Exception:
        pass

    # Fallback: render from identity.json directly (pre-cascade users)
    facts = get_authoritative_facts()
    if not facts:
        return ""
    label_map = {
        "name": "姓名",
        "role": "身份",
        "occupation": "职业 / 研究方向",
        "schedule": "作息",
        "style": "偏好的说话风格",
        "notes": "用户备注",
    }
    lines = []
    if header:
        lines.append("# 用户已声明的事实（不可质疑，必须遵循）")
    for f in facts:
        label = label_map.get(f["category"], f["category"])
        lines.append(f"- {label}: {f['text']}")
    if header:
        lines.append("")
        lines.append("⚠️ 任何与上述事实冲突的内容，必须按上述事实写。"
                     "VLM 对 OCR / 手写 / 模糊文字的识别可能出错，"
                     "若疑似涉及姓名等已声明字段，直接采用上述声明值。")
    return "\n".join(lines)


def _strip_header(block: str) -> str:
    """Remove the leading '# ...' header line from a rendered block."""
    lines = block.split("\n")
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    return "\n".join(lines).lstrip()


def get_user_name() -> str:
    """Convenience: return canonical name or empty string if unset."""
    return _load().get("name", "").strip()


# ─────────────────────────────────────────────────────────────────────
# Cascade hooks — called by onboarding submit and settings update.
# These are thin wrappers that translate caller-side field names into
# the canonical schema above.
# ─────────────────────────────────────────────────────────────────────

def cascade_from_onboarding(answers: dict) -> dict:
    """Called from core.initialize_from_questionnaire after onboarding submit.

    `answers` is the raw onboarding payload; we map the relevant fields
    into the identity schema.
    """
    if not isinstance(answers, dict):
        return {"ok": False, "error": "invalid answers"}

    payload = {
        "name": answers.get("name"),
        "role": answers.get("occupation"),  # onboarding calls it 'occupation'
        "occupation": answers.get("focus"),
        "schedule": answers.get("schedule"),
        "style": answers.get("style"),
    }
    # drop None / empty
    payload = {k: v for k, v in payload.items() if v}
    return update(payload, source="onboarding")


def cascade_from_settings(profile_payload: dict) -> dict:
    """Called from self_profile.update_profile / settings page.

    Settings primarily sets canonical_name. We also accept a few other
    fields if the settings UI ever exposes them.

    2026-05-08: aliases now cascade through too — they end up as a fact
    in slots/self/identity/main.md so the main agent + journal + VLM
    prompts all see the user's multi-platform usernames.
    """
    if not isinstance(profile_payload, dict):
        return {"ok": False, "error": "invalid payload"}

    payload = {}
    if profile_payload.get("canonical_name"):
        payload["name"] = profile_payload["canonical_name"]
    if profile_payload.get("notes"):
        payload["notes"] = profile_payload["notes"]
    # aliases is a list field; pass through verbatim (update() will
    # dedupe + strip). Pass even when empty so users can clear aliases
    # via settings (None → explicit-clear semantics in update()).
    if "aliases" in profile_payload:
        aliases = profile_payload["aliases"]
        if isinstance(aliases, list):
            payload["aliases"] = aliases
    if not payload:
        return {"ok": True, "changed": []}
    return update(payload, source="settings")
