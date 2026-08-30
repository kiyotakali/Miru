import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import memory_prompts_v2  # noqa: E402
import curator  # noqa: E402


def test_curator_planner_keeps_chat_tier_but_disables_reasoning(monkeypatch):
    captured = {}

    def fake_call(**kwargs):
        captured.update(kwargs)
        return memory_prompts_v2.CuratorPlannerOutput(
            reasoning="no confident cleanup",
            ops=[],
        )

    monkeypatch.setattr(memory_prompts_v2, "_call_llm_with_retry", fake_call)

    result = memory_prompts_v2.call_curator_planner(
        domain="project",
        slots=[
            {
                "id": "paper_a",
                "title": "Paper A",
                "summary": "论文 A",
                "aliases": ["a"],
                "status": "active",
                "pinned": False,
                "last_active": "2026-05-17T10:00:00",
                "body_excerpt": "A",
            },
            {
                "id": "paper_b",
                "title": "Paper B",
                "summary": "论文 B",
                "aliases": ["b"],
                "status": "active",
                "pinned": False,
                "last_active": "2026-05-17T10:00:00",
                "body_excerpt": "B",
            },
        ],
    )

    assert result.ops == []
    assert captured["pass_label"] == "CuratorPlanner"
    assert captured["tier"] == "chat"
    assert captured["reasoning"] is False
    assert captured["reasoning_budget"] == 0


def test_curator_normal_cooldown_is_one_hour():
    assert curator.CURATOR_COOLDOWN_SECONDS == 3600
