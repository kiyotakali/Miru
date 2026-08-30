"""Tests for renderer.py — the unified slot view layer (v3).

After the slot migration, the slot's content lives in main.md as a free-text
body plus append records. The renderer reads slot meta + main.md body — no
fact list / confidence scoring anymore.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


@pytest.fixture
def env(monkeypatch):
    """Isolated data dir + memory_dir for one test."""
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("MIRU_DATA_DIR", tmp)
    monkeypatch.setenv("DATA_DIR", tmp)
    import memory
    monkeypatch.setattr(memory, "_memory_dir", lambda: os.path.join(tmp, "memory"))
    os.makedirs(os.path.join(tmp, "memory"), exist_ok=True)
    yield tmp
    shutil.rmtree(tmp, ignore_errors=True)


def _seed_slot(domain, slot_id, *, title="Test Slot", icon="🧪",
               summary="测试 slot", pinned=False, status="active",
               aliases=None, people_refs=None, body=""):
    """Write slot meta into the registry and (optionally) seed main.md."""
    import memory_router
    import memory
    slots = memory_router.load_all_slots(domain)
    rel = memory_router._slot_main_file_rel(domain, slot_id)
    new = {
        "id": slot_id, "title": title, "icon": icon,
        "status": status, "pinned": pinned, "summary": summary,
        "aliases": aliases or [], "people_refs": people_refs or [],
        "main_file": rel,
        "last_active": "2026-05-05T10:00:00",
        "created": "2026-04-01T10:00:00",
    }
    slots.append(new)
    memory_router._save_all_slots_atomic(domain, slots)
    if body:
        memory.write_file(rel, f"# {title}\n\n{body}\n")
    return new


# ─────────────────────────────────────────────────────────────────────
# render_slot — 4 modes
# ─────────────────────────────────────────────────────────────────────

def test_render_card_basic(env):
    import renderer
    _seed_slot("person", "mentor", title="导师", icon="👤",
               summary="用户的导师",
               body="导师是用户的论文指导教授，研究方向是世界模型蒸馏。")
    card = renderer.render_slot("person", "mentor", mode="card")
    assert card["title"] == "导师"
    assert card["icon"] == "👤"
    assert card["summary"] == "用户的导师"
    assert "研究方向" in card["body"]
    assert card["body_len"] > 0
    assert card["status"] == "active"
    assert card["domain"] == "person"
    assert card["slot_id"] == "mentor"
    assert "revisions" not in card  # revisions system removed


def test_render_markdown(env):
    import renderer
    _seed_slot("project", "ctx", title="ContextLife", summary="记忆系统重构",
               body="正在重写 fact_resolver。Pass 4 改用 AppendEditor。")
    md = renderer.render_slot("project", "ctx", mode="markdown")
    assert isinstance(md, str)
    assert "# ContextLife" in md
    assert "> 记忆系统重构" in md
    assert "正在重写 fact_resolver" in md
    # Revisions system was removed — markdown no longer has edit-history footer
    assert "编辑历史" not in md


def test_render_overview_uses_summary(env):
    import renderer
    _seed_slot("person", "alice", title="Alice", summary="好朋友",
               body="Alice 喜欢咖啡。周末经常一起爬山。")
    line = renderer.render_slot("person", "alice", mode="overview")
    assert isinstance(line, str)
    assert "**Alice**" in line
    assert "好朋友" in line


def test_render_overview_falls_back_to_body(env):
    import renderer
    _seed_slot("person", "bob", title="Bob", summary="",
               body="Bob 是同事，最近在做后端重构。")
    line = renderer.render_slot("person", "bob", mode="overview")
    assert "Bob 是同事" in line


def test_render_overview_empty_slot(env):
    import renderer
    _seed_slot("person", "ghost", title="Ghost", summary="", body="")
    line = renderer.render_slot("person", "ghost", mode="overview")
    assert "(暂无信息)" in line


def test_render_ground_truth_self_identity(env):
    import renderer
    _seed_slot("self", "identity", title="用户身份",
               body="姓名: 李垦\n身份: 研究生\n职业: 蒸馏研究")
    gt = renderer.render_slot("self", "identity", mode="ground_truth")
    assert "李垦" in gt
    assert "研究生" in gt
    assert "不可质疑" in gt
    assert "用户已声明的事实" in gt


def test_render_ground_truth_falls_back_to_identity_json(env, monkeypatch):
    import renderer
    # Seed slot but with empty body — should fall back to identity.json
    _seed_slot("self", "identity", title="用户身份", body="")
    import identity
    identity.update({"name": "李垦", "role": "研究生"}, source="onboarding")
    gt = renderer.render_slot("self", "identity", mode="ground_truth")
    # cascade_identity wrote into the slot, so either source is fine —
    # but the ground truth must contain the canonical name.
    assert "李垦" in gt


def test_render_ground_truth_returns_empty_for_other_slots(env):
    import renderer
    _seed_slot("person", "anyone", title="Someone", body="some body")
    gt = renderer.render_slot("person", "anyone", mode="ground_truth")
    assert gt == ""


# ─────────────────────────────────────────────────────────────────────
# render_domain_overview
# ─────────────────────────────────────────────────────────────────────

def test_render_domain_overview(env):
    import renderer
    _seed_slot("person", "a", title="Alice", summary="好朋友", body="x")
    _seed_slot("person", "b", title="Bob", summary="同事", body="y")
    _seed_slot("person", "c", title="Carol", summary="老同学",
               status="archived", body="z")
    lines = renderer.render_domain_overview("person")
    # archived dropped
    assert len(lines) == 2
    assert any("Alice" in l for l in lines)
    assert any("Bob" in l for l in lines)
    assert not any("Carol" in l for l in lines)


def test_render_unknown_mode_raises(env):
    import renderer
    with pytest.raises(ValueError):
        renderer.render_slot("person", "x", mode="weird")


def test_render_handles_missing_slot(env):
    """Fall back gracefully when slot meta isn't registered yet."""
    import renderer
    # No _seed_slot call → meta is None
    card = renderer.render_slot("person", "ghost", mode="card")
    assert card["title"] == "ghost"  # Falls back to slot_id
    assert card["body"] == ""
    assert card["body_len"] == 0
    assert card["status"] == "active"
