"""Unit tests for the curator 1h-cooldown collision override.

The bug being fixed: two slots with identical title (e.g. `weijiazhe` +
`weijiazhe_2`, both "魏佳哲") that the user is actively chatting with will
have last_active < 1h on every Curator tick. Without override, they get
filtered out of `_planner_candidates` forever — Planner never sees them,
duplicate never merges.

Override rule: if a slot collides with another slot on normalized title
OR has alias overlap, it bypasses the 1h cooldown.
"""

import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import curator  # noqa: E402


def _now_iso(minutes_ago: float = 0) -> str:
    return (datetime.now() - timedelta(minutes=minutes_ago)).isoformat()


def test_title_collision_bypasses_cooldown():
    """Two slots with identical title both enter candidates even when both
    last_active < 1h."""
    slots = [
        {
            "id": "weijiazhe",
            "title": "魏佳哲",
            "aliases": ["魏佳哲", "佳哲"],
            "summary": "微信好友",
            "status": "active",
            "pinned": False,
            "last_active": _now_iso(minutes_ago=10),  # 10 min ago
        },
        {
            "id": "weijiazhe_2",
            "title": "魏佳哲",
            "aliases": ["魏佳哲", "小魏"],
            "summary": "毕设群同学",
            "status": "active",
            "pinned": False,
            "last_active": _now_iso(minutes_ago=5),  # 5 min ago
        },
    ]
    out = curator._planner_candidates("person", slots)
    ids = {s["id"] for s in out}
    assert "weijiazhe" in ids, "title 重复必须 bypass cooldown"
    assert "weijiazhe_2" in ids, "title 重复必须 bypass cooldown"


def test_alias_overlap_bypasses_cooldown():
    """Different titles but overlapping aliases also trigger bypass."""
    slots = [
        {
            "id": "wjz",
            "title": "魏佳哲 (朋友)",
            "aliases": ["魏佳哲", "jiazhewei"],
            "summary": "微信好友",
            "status": "active",
            "pinned": False,
            "last_active": _now_iso(minutes_ago=10),
        },
        {
            "id": "weijiazhe_alt",
            "title": "魏佳哲 (毕设)",  # different title
            "aliases": ["jiazhewei", "佳哲"],  # but jiazhewei overlaps
            "summary": "毕设群同学",
            "status": "active",
            "pinned": False,
            "last_active": _now_iso(minutes_ago=5),
        },
    ]
    out = curator._planner_candidates("person", slots)
    ids = {s["id"] for s in out}
    assert "wjz" in ids
    assert "weijiazhe_alt" in ids


def test_normalized_title_matches_with_whitespace_and_case():
    """Title comparison is case + whitespace insensitive."""
    slots = [
        {
            "id": "alpha",
            "title": "Dr. Chen",
            "aliases": [],
            "summary": "x",
            "status": "active", "pinned": False,
            "last_active": _now_iso(minutes_ago=20),
        },
        {
            "id": "beta",
            "title": "dr.chen",  # case + space difference
            "aliases": [],
            "summary": "y",
            "status": "active", "pinned": False,
            "last_active": _now_iso(minutes_ago=15),
        },
    ]
    out = curator._planner_candidates("person", slots)
    ids = {s["id"] for s in out}
    assert "alpha" in ids and "beta" in ids


def test_no_collision_cooldown_still_applies():
    """Non-colliding slot with last_active < 1h is still filtered out."""
    slots = [
        {
            "id": "alpha",
            "title": "魏佳哲",
            "aliases": ["魏佳哲"],
            "summary": "x",
            "status": "active", "pinned": False,
            "last_active": _now_iso(minutes_ago=5),  # < 1h
        },
        {
            "id": "beta",
            "title": "张三",   # 不同 title, 无 alias 重叠
            "aliases": ["张三"],
            "summary": "y",
            "status": "active", "pinned": False,
            "last_active": _now_iso(minutes_ago=5),  # < 1h
        },
    ]
    out = curator._planner_candidates("person", slots)
    ids = {s["id"] for s in out}
    assert "alpha" not in ids, "无碰撞的 slot 应该被 cooldown 过滤"
    assert "beta" not in ids, "无碰撞的 slot 应该被 cooldown 过滤"


def test_old_slot_still_enters_candidates():
    """A slot with last_active > 1h ago always enters candidates regardless
    of collision status (preserves old behavior for non-colliding cases)."""
    slots = [
        {
            "id": "alpha",
            "title": "周深",
            "aliases": [],
            "summary": "歌手",
            "status": "active", "pinned": False,
            "last_active": _now_iso(minutes_ago=120),  # 2h ago
        },
        {
            "id": "beta",
            "title": "Sam Altman",
            "aliases": [],
            "summary": "OpenAI",
            "status": "active", "pinned": False,
            "last_active": _now_iso(minutes_ago=90),  # 1.5h ago
        },
    ]
    out = curator._planner_candidates("person", slots)
    ids = {s["id"] for s in out}
    assert "alpha" in ids and "beta" in ids


def test_archived_and_pinned_still_excluded_even_if_colliding():
    """Hard exclusions (archived / pinned) still apply over collision override."""
    slots = [
        {
            "id": "wjz_active",
            "title": "魏佳哲",
            "aliases": ["魏佳哲"],
            "summary": "正常 slot",
            "status": "active", "pinned": False,
            "last_active": _now_iso(minutes_ago=5),
        },
        {
            "id": "wjz_archived",
            "title": "魏佳哲",  # collides
            "aliases": ["魏佳哲"],
            "summary": "已归档",
            "status": "archived",  # ← excluded
            "pinned": False,
            "last_active": _now_iso(minutes_ago=5),
        },
        {
            "id": "wjz_pinned",
            "title": "魏佳哲",  # collides
            "aliases": ["魏佳哲"],
            "summary": "锁定",
            "status": "active",
            "pinned": True,  # ← excluded
            "last_active": _now_iso(minutes_ago=5),
        },
    ]
    out = curator._planner_candidates("person", slots)
    ids = {s["id"] for s in out}
    assert "wjz_archived" not in ids, "archived 永远排除"
    assert "wjz_pinned" not in ids, "pinned 永远排除"
    # wjz_active collides with archived/pinned but those are themselves
    # excluded from the collision set, so wjz_active alone shouldn't bypass.
    # The current collision computation correctly only looks at non-archived
    # non-pinned pairs, so wjz_active stays under cooldown if it's alone.
    assert "wjz_active" not in ids, (
        "wjz_active 没有 active 的 collision partner, cooldown 仍生效")


def test_self_identity_always_excluded():
    """self/identity is reserved — never enters candidates."""
    slots = [
        {
            "id": "identity",
            "title": "Identity",
            "aliases": [],
            "summary": "user identity",
            "status": "active", "pinned": False,
            "last_active": _now_iso(minutes_ago=200),  # old, would pass cooldown
        },
        {
            "id": "habits",
            "title": "Habits",
            "aliases": [],
            "summary": "x",
            "status": "active", "pinned": False,
            "last_active": _now_iso(minutes_ago=200),
        },
    ]
    out = curator._planner_candidates("self", slots)
    ids = {s["id"] for s in out}
    assert "identity" not in ids
    assert "habits" in ids


def test_empty_aliases_dont_trigger_false_collision():
    """Two slots both with empty aliases must not be considered colliding via
    aliases (empty set ∩ empty set = empty, which would have been a bug)."""
    slots = [
        {
            "id": "alpha",
            "title": "A",
            "aliases": [],
            "summary": "x",
            "status": "active", "pinned": False,
            "last_active": _now_iso(minutes_ago=5),
        },
        {
            "id": "beta",
            "title": "B",
            "aliases": [],
            "summary": "y",
            "status": "active", "pinned": False,
            "last_active": _now_iso(minutes_ago=5),
        },
    ]
    out = curator._planner_candidates("person", slots)
    ids = {s["id"] for s in out}
    assert "alpha" not in ids and "beta" not in ids


if __name__ == "__main__":
    import unittest
    unittest.main()
