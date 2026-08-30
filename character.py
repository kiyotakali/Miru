"""Character configuration loader — parses soul.md as single source of truth.

Usage:
    from character import get_config, SOUL_PATH
    cfg = get_config()
    print(cfg.name)          # "Airi"
    print(cfg.user_address)  # "Master"
    print(cfg.hint("archetype"))  # "元気少女"
    print(cfg.raw_text)      # full soul.md content for LLM injection
"""

from __future__ import annotations

import os
import re

SOUL_PATH = os.path.join(os.path.dirname(__file__), "soul.md")

# Keep internal alias for backward compat
_SOUL_PATH = SOUL_PATH



class CharacterConfig:
    """Parsed character configuration from soul.md."""

    def __init__(self):
        # Defaults (used when soul.md is missing or unparseable)
        self.name = "Airi"
        self.user_address = "Master"
        self.personality = ""
        self.speech_patterns = ""
        self.appearance = ""
        self.backstory = ""
        self.interests = ""
        self.emotional_reactions = ""
        self.relationship_stages = ""
        self.agent_behavior = ""
        self.avatar_filename = "airi.jpeg"
        self.reference_filename = "airi_reference.webp"
        self.raw_text = ""
        self.prompt_hints: dict[str, str] = {}

    def hint(self, key: str, fallback: str = "") -> str:
        """Get a prompt hint value by key, with optional fallback."""
        return self.prompt_hints.get(key, fallback)

    def _parse(self, text):
        """Parse soul.md markdown into structured fields."""
        self.raw_text = text

        sections = {}
        current_heading = None
        current_lines = []

        for line in text.split("\n"):
            m = re.match(r'^#\s+(.+)', line)
            if m:
                if current_heading is not None:
                    sections[current_heading] = "\n".join(current_lines).strip()
                current_heading = m.group(1).strip().lower()
                current_lines = []
            else:
                current_lines.append(line)

        if current_heading is not None:
            sections[current_heading] = "\n".join(current_lines).strip()

        # Identity
        identity = sections.get("identity", "")
        name_m = re.search(r'\*\*Name\*\*:\s*(.+)', identity)
        if name_m:
            self.name = name_m.group(1).strip()
        addr_m = re.search(r'\*\*User Address\*\*:\s*(.+)', identity)
        if addr_m:
            self.user_address = addr_m.group(1).strip()

        # Simple text sections
        self.personality = sections.get("personality", "")
        self.speech_patterns = sections.get("speech patterns", "")
        self.appearance = sections.get("appearance", "")
        self.backstory = sections.get("backstory", "")
        self.interests = sections.get("interests", "")
        self.emotional_reactions = sections.get("emotional reactions", "")
        self.relationship_stages = sections.get("relationship stages", "")
        self.agent_behavior = sections.get("agent behavior", "")

        # Prompt Hints — key: value per line
        hints_raw = sections.get("prompt hints", "")
        self.prompt_hints = {}
        for line in hints_raw.split("\n"):
            hint_m = re.match(r'^-\s*(\w+):\s*(.+)', line)
            if hint_m:
                self.prompt_hints[hint_m.group(1).strip()] = hint_m.group(2).strip()

        # Reference images
        ref = sections.get("reference images", "")
        avatar_m = re.search(r'\*\*Avatar\*\*:\s*(.+)', ref)
        if avatar_m:
            self.avatar_filename = avatar_m.group(1).strip()
        ref_m = re.search(r'\*\*Reference\*\*:\s*(.+)', ref)
        if ref_m:
            self.reference_filename = ref_m.group(1).strip()


# Per-user config cache: {user_id: (CharacterConfig, soul_path)}
_configs: dict[str, tuple[CharacterConfig, str]] = {}


def _current_user_id() -> str:
    try:
        from flask import g
        return getattr(g, "user_id", "_admin")
    except (RuntimeError, ImportError):
        return "_admin"


def _resolve_soul_path() -> str:
    """Determine which soul.md to load: active model's persona or project default."""
    try:
        import model_library
        return model_library.get_active_soul_path()
    except Exception:
        return _SOUL_PATH


def get_config():
    """Return per-user CharacterConfig. Loads soul.md on first call.

    Automatically uses the active model's persona soul.md if available.
    Cached per-user to avoid cross-user config leaks.
    """
    uid = _current_user_id()
    soul_path = _resolve_soul_path()

    cached = _configs.get(uid)
    if cached is not None:
        cfg, cached_path = cached
        # Auto-invalidate if the active model changed
        if cached_path == soul_path:
            return cfg

    cfg = CharacterConfig()
    if os.path.exists(soul_path):
        with open(soul_path, "r", encoding="utf-8") as f:
            cfg._parse(f.read())
    _configs[uid] = (cfg, soul_path)
    return cfg


def reload_config():
    """Force reload soul.md for current user (e.g. after editing or model switch)."""
    uid = _current_user_id()
    _configs.pop(uid, None)
    return get_config()
