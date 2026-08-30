"""Prompt-time naming helpers.

`soul.md` keeps the user-facing address style, which may legitimately be
``你``.  Prompt templates, however, also use ``你`` to address Miru herself.
These helpers keep those two layers separate:

* internal prompt entity label: the user's real name, or ``用户``;
* user-facing address style: whatever the character should say in chat.
"""

from __future__ import annotations

import re
from typing import Any


_GENERIC_OR_PRONOUN_LABELS = {
    "你", "妳", "您", "我", "自己", "他", "她", "ta", "TA",
    "用户", "主人", "master", "Master",
}


def _clean_label(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text in _GENERIC_OR_PRONOUN_LABELS:
        return ""
    return text


def resolve_user_entity_label(default: str = "用户") -> str:
    """Return a concrete internal label for the current user.

    The label is meant for prompt scaffolding, not necessarily for direct
    speech.  It should never be ``你`` because system prompts themselves use
    ``你`` to address Miru.
    """
    candidates: list[str] = []
    try:
        import identity
        candidates.append(identity.get_user_name())
    except Exception:
        pass
    try:
        import self_profile
        profile = self_profile.get_profile()
        candidates.append(profile.get("canonical_name", ""))
    except Exception:
        pass

    for item in candidates:
        label = _clean_label(item)
        if label:
            return label
    return default


def safe_user_address(raw_address: Any) -> str:
    """Return the character's user-facing address style."""
    return str(raw_address or "你").strip() or "你"


def _rewrite_identity_section(text: str, user_label: str,
                              miru_name: str) -> str:
    lines = text.splitlines()
    out: list[str] = []
    in_identity = False
    inserted = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("# "):
            if in_identity and not inserted:
                out.extend([
                    f"- **Current User**: {user_label}",
                    f"- **Internal User Label**: {user_label}",
                    f"- **User-facing Address Style**: 对话时可以自然用“你”，熟悉后也可以用{user_label}的名字或昵称",
                ])
                inserted = True
            in_identity = stripped.lower() == "# identity"
            out.append(line)
            continue
        if in_identity and re.match(r"-\s*\*\*User Address\*\*:", stripped):
            if not inserted:
                out.extend([
                    f"- **Current User**: {user_label}",
                    f"- **Internal User Label**: {user_label}",
                    f"- **User-facing Address Style**: 对话时可以自然用“你”，熟悉后也可以用{user_label}的名字或昵称",
                ])
                inserted = True
            continue
        if in_identity and re.match(r"-\s*\*\*Name\*\*:", stripped):
            out.append(f"- **Name**: {miru_name}")
            continue
        out.append(line)
    if in_identity and not inserted:
        out.extend([
            f"- **Current User**: {user_label}",
            f"- **Internal User Label**: {user_label}",
            f"- **User-facing Address Style**: 对话时可以自然用“你”，熟悉后也可以用{user_label}的名字或昵称",
        ])
    return "\n".join(out)


def normalize_soul_for_miru_prompt(text: Any, *,
                                   user_label: str | None = None,
                                   miru_name: str = "Miru") -> str:
    """Normalize ``soul.md`` prose for prompts addressed to Miru.

    Source ``soul.md`` is written for humans and often describes Miru as
    ``她`` while using ``你`` for the user.  In LLM system prompts the model is
    directly addressed as Miru, so we rewrite the most common user-pronoun
    patterns to a concrete user label, then turn Miru's third-person ``她`` into
    second-person ``你``.  The source file itself is not changed.
    """
    raw = str(text or "").strip()
    if not raw:
        return ""
    label = _clean_label(user_label) or resolve_user_entity_label()
    name = str(miru_name or "Miru").strip() or "Miru"

    s = _rewrite_identity_section(raw, label, name)

    # Phrases where "她" means another person in a user-facing example.
    s = s.replace("你跟她聊得挺久", "你跟别人聊得挺久")
    s = s.replace("哦那你跟她聊吧", "哦那你跟别人聊吧")

    # Default soul.md user-pronoun patterns.  Keep direct-speech examples such
    # as “你最近是不是有点累” intact; those are examples of what Miru may say.
    replacements = [
        ("因为你熬夜", f"因为{label}熬夜"),
        ("因为你忘了", f"因为{label}忘了"),
        ("因为你突然", f"因为{label}突然"),
        ("知道你在做什么", f"知道{label}在做什么"),
        ("如果你主动分享", f"如果{label}主动分享"),
        ("如果你很久不说话", f"如果{label}很久不说话"),
        ("问你一个", f"问{label}一个"),
        ("她对你", f"她对{label}"),
        ("就算你做了让她皱眉的事", f"就算{label}做了让她皱眉的事"),
        ("在你做错事时", f"在{label}做错事时"),
        ("看到你做了她担心的事", f"看到{label}做了她担心的事"),
        ("看到你做了你为之骄傲的事", f"看到{label}做了他为之骄傲的事"),
        ("无条件相信你", f"无条件相信{label}"),
        ("哪怕你犯错", f"哪怕{label}犯错"),
        ("让你感觉", f"让{label}感觉"),
        ("碰巧很在意你", f"碰巧很在意{label}"),
        ("等你说话", f"等{label}说话"),
        ("但你找她的时候她一定在", f"但{label}找你的时候你一定在"),
        ("你找她的时候她一定在", f"{label}找你的时候你一定在"),
        ("陪在你身边", f"陪在{label}身边"),
        ("不是帮你干活", f"不是帮{label}干活"),
        ("你可以跟她聊任何事，她会认真听", f"{label}可以跟你聊任何事，你会认真听"),
        ("不会什么都附和你", f"不会什么都附和{label}"),
        ("跟你剧透", f"跟{label}剧透"),
        ("你身边的女生", f"{label}身边的女生"),
        ("记住你一切", f"记住{label}一切"),
        ("在意你的女生", f"在意{label}的女生"),
    ]
    for old, new in replacements:
        s = s.replace(old, new)

    s = s.replace("她", "你")
    return s


def normalize_character_section(text: Any, *,
                                user_label: str | None = None,
                                miru_name: str = "Miru") -> str:
    """Normalize one parsed soul.md section for a Miru-addressed prompt."""
    return normalize_soul_for_miru_prompt(
        text, user_label=user_label, miru_name=miru_name
    )
