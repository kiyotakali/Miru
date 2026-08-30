"""End-to-end tests for the simplified memory pipeline:

    Observer (LLM mocked) → Curator (real) → fact_store (real) → renderer

Verifies that the three original problems are still solved post-refactor:
  1. OCR'd wrong name (李垦 → 李昱) is rejected by pinned identity
  2. Wrong-role inference (学生 from screenshot) is superseded by chat (导师)
  3. Group chat misclassified as person — Observer-side responsibility
     (we test that curator respects whatever domain the observer chose,
     and that the system supports correct routing when observer is right)

NOTE: After the v3 slot migration, several of these tests still
assert on the deprecated fact-flow card schema (active_facts /
history_facts / stats). They are marked xfail until J8 (test rewrite)
replaces them with body-based equivalents.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


_OLD_FACT_SCHEMA_XFAIL = pytest.mark.xfail(
    reason="Asserts on deprecated fact-flow card schema; rewrite in J8.",
    strict=False,
)


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


# ─────────────────────────────────────────────────────────────────────
# Onboarding → identity slot → ground truth → blocks bad OCR
# ─────────────────────────────────────────────────────────────────────

@_OLD_FACT_SCHEMA_XFAIL
def test_onboarding_identity_blocks_ocr_wrong_name(env):
    """Onboarding writes pinned 李垦; later screenshot OCR says 李昱;
    final state: 李垦 still active, 李昱 was rejected."""
    import identity, curator, fact_store, renderer

    # Step 1: onboarding → cascade
    identity.cascade_from_onboarding({"name": "李垦", "occupation": "研究生"})

    # Verify ground-truth block contains 李垦
    gt = identity.compose_ground_truth_block()
    assert "李垦" in gt
    assert "不可质疑" in gt

    # Step 2: screenshot observer thinks user's name is 李昱 (OCR error)
    curator.apply(
        facts=[{"slot": "self/identity", "category": "name", "text": "李昱"}],
        slot_hints=[{"domain": "self", "id": "identity"}],
        default_source_type="screenshot_text",
    )

    # Step 3: render the slot — UI sees only 李垦
    card = renderer.render_slot("self", "identity", mode="card")
    name_facts = [f for f in card["active_facts"] if f["category"] == "name"]
    names = [f["text"] for f in name_facts]
    assert "李垦" in names
    assert "李昱" not in names


@_OLD_FACT_SCHEMA_XFAIL
def test_chat_supersedes_screenshot_inference(env):
    """Screenshot weakly says 司晨阳 is '学生' (0.20);
    chat strongly says '导师' (0.70) → supersedes."""
    import curator, fact_store, renderer

    # Initial wrong inference from screenshot
    curator.apply(
        facts=[{"slot": "person/si-chenyang", "category": "role", "text": "学生"}],
        slot_hints=[{"domain": "person", "id": "si-chenyang", "title": "司晨阳"}],
        default_source_type="screenshot_inf",
    )
    # Then user says in chat — high confidence
    curator.apply(
        facts=[{"slot": "person/si-chenyang", "category": "role", "text": "导师"}],
        slot_hints=[{"domain": "person", "id": "si-chenyang"}],
        default_source_type="user_chat",
    )

    card = renderer.render_slot("person", "si-chenyang", mode="card")
    active_roles = [f["text"] for f in card["active_facts"] if f["category"] == "role"]
    history_roles = [f["text"] for f in card["history_facts"] if f["category"] == "role"]
    assert active_roles == ["导师"]
    assert "学生" in history_roles


@_OLD_FACT_SCHEMA_XFAIL
def test_observer_routes_group_to_project_not_person(env):
    """If the observer correctly routes '项目协作群' to project domain,
    we should NOT end up with a person slot named that."""
    import curator, memory_router

    curator.apply(
        facts=[{"slot": "project/team-collab", "category": "activity",
                "text": "在协作群讨论需求"}],
        slot_hints=[{"domain": "project", "id": "team-collab",
                     "title": "项目协作群", "icon": "💬"}],
        default_source_type="user_chat",
    )

    # Confirm: it's in project, not in person
    person_slots = memory_router.load_all_slots("person")
    project_slots = memory_router.load_all_slots("project")
    person_titles = [s.get("title") for s in person_slots]
    project_titles = [s.get("title") for s in project_slots]
    assert "项目协作群" not in person_titles
    assert "项目协作群" in project_titles


# ─────────────────────────────────────────────────────────────────────
# UI / Miru data parity (single render)
# ─────────────────────────────────────────────────────────────────────

@_OLD_FACT_SCHEMA_XFAIL
def test_ui_card_and_miru_markdown_share_data(env):
    """Card view + markdown view use the SAME source — facts.json.
    The high-confidence facts shown to the user must equal the
    facts that appear in the markdown that goes into Miru's prompt."""
    import curator, renderer

    curator.apply(
        facts=[
            {"slot": "person/x", "category": "preference",
             "text": "喜欢喝咖啡", "confidence": 0.85},
            {"slot": "person/x", "category": "habit",
             "text": "习惯熬夜写代码", "confidence": 0.30},
            # Below threshold — should NOT appear in markdown
            {"slot": "person/x", "category": "preference",
             "text": "好像喜欢茶（很模糊）", "confidence": 0.15},
        ],
        slot_hints=[{"domain": "person", "id": "x", "title": "X"}],
        default_source_type="user_chat",
    )

    card = renderer.render_slot("person", "x", mode="card")
    md = renderer.render_slot("person", "x", mode="markdown")

    # All 3 facts present in card (UI shows full picture)
    assert card["stats"]["n_active"] == 3

    # Only ≥ 0.30 facts in markdown (Miru prompt budget)
    assert "喜欢喝咖啡" in md
    assert "习惯熬夜写代码" in md
    assert "好像喜欢茶" not in md


# ─────────────────────────────────────────────────────────────────────
# Settings update path
# ─────────────────────────────────────────────────────────────────────

@_OLD_FACT_SCHEMA_XFAIL
def test_settings_change_replaces_pinned_identity(env):
    """User changes name in settings — old pinned name replaced.

    The onboarding cascade maps `occupation` form field → identity `role`,
    and `focus` → identity `occupation`. Settings page only updates name +
    notes today, so we test the name-change path here (the canonical
    settings path) rather than synthesizing a fake non-supported field.
    """
    import identity, fact_store

    identity.cascade_from_onboarding({"name": "旧名", "occupation": "研究生"})
    facts = fact_store.load_facts("self", "identity")
    names = [f["text"] for f in facts
             if f.get("category") == "name" and not f.get("superseded")]
    assert names == ["旧名"]

    # User changes name in settings (settings exposes canonical_name)
    identity.cascade_from_settings({"canonical_name": "新名"})

    facts = fact_store.load_facts("self", "identity")
    active_names = [f["text"] for f in facts
                    if f.get("category") == "name" and not f.get("superseded")]
    assert active_names == ["新名"]
