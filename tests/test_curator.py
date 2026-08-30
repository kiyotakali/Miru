"""Tests for curator.cascade_identity (v3) — the only remaining writer."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


@pytest.fixture
def env(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("MIRU_DATA_DIR", tmp)
    monkeypatch.setenv("DATA_DIR", tmp)
    import memory
    monkeypatch.setattr(memory, "_memory_dir", lambda: os.path.join(tmp, "memory"))
    os.makedirs(os.path.join(tmp, "memory"), exist_ok=True)
    yield tmp
    shutil.rmtree(tmp, ignore_errors=True)


def test_cascade_identity_writes_body_and_meta(env):
    import curator
    import memory_router
    import memory

    res = curator.cascade_identity({
        "name": "李垦",
        "role": "研究生",
        "occupation": "蒸馏研究",
        "schedule": "夜猫子",
    })
    assert res["ok"] is True
    assert res["domain"] == "self"
    assert res["slot_id"] == "identity"
    assert "李垦" in res["body"]

    # Slot meta exists, pinned, summary populated
    slot = memory_router.get_slot("self", "identity")
    assert slot is not None
    assert slot["pinned"] is True
    assert slot["status"] == "active"
    assert slot["icon"] == "🪪"
    assert "李垦" in slot["summary"]

    # main.md body has all four labels
    md = memory.read_file(slot["main_file"])
    assert md is not None
    assert "# 用户身份" in md
    assert "姓名: 李垦" in md
    assert "身份: 研究生" in md
    assert "职业 / 研究方向: 蒸馏研究" in md
    assert "作息: 夜猫子" in md


def test_cascade_identity_skips_empty_fields(env):
    import curator
    import memory

    curator.cascade_identity({"name": "李垦", "role": "", "occupation": None})
    slot = __import__("memory_router").get_slot("self", "identity")
    md = memory.read_file(slot["main_file"])
    assert "姓名: 李垦" in md
    # The slot title still contains "身份" (用户身份); body's label "- 身份: " must not.
    assert "- 身份:" not in md
    assert "- 职业" not in md


def test_cascade_identity_no_op_on_empty_payload(env):
    import curator
    res = curator.cascade_identity({})
    assert res["ok"] is True
    assert res.get("no_op") is True


def test_cascade_identity_overwrites_old_body(env):
    """Settings page change must replace the old body, not append."""
    import curator
    import memory

    curator.cascade_identity({"name": "旧名", "role": "学生"})
    curator.cascade_identity({"name": "新名", "role": "研究生"})

    slot = __import__("memory_router").get_slot("self", "identity")
    md = memory.read_file(slot["main_file"])
    assert "新名" in md
    assert "旧名" not in md


def test_cascade_identity_renderer_ground_truth_picks_up_body(env):
    """End-to-end: cascade → renderer ground_truth includes the body."""
    import curator
    import renderer

    curator.cascade_identity({"name": "李垦", "role": "研究生"})
    gt = renderer.render_slot("self", "identity", mode="ground_truth")
    assert "李垦" in gt
    assert "研究生" in gt
    assert "不可质疑" in gt
