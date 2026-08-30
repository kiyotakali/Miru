"""Unit tests for Persona Writer v3 schema (Sleep Agent v3 C1)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from pydantic import ValidationError

from memory_prompts_v3 import (  # noqa: E402
    BlockUpdate,
    PersonaWriterOutput,
    build_persona_writer_messages,
    _format_slot_summaries_for_persona,
    _format_proactive_outcomes_for_persona,
)


# ─────────────────────────────────────────────────────────────────────
# BlockUpdate
# ─────────────────────────────────────────────────────────────────────

def test_block_update_append_valid():
    b = BlockUpdate(
        action="append",
        content="李垦卡 bug 时容易短暂焦躁, 但很快用梗自嘲, 回到冷静. 这是他的调节节律.",
    )
    assert b.action == "append"
    assert b.old_text is None


def test_block_update_replace_requires_old_text():
    with pytest.raises(ValidationError):
        BlockUpdate(
            action="replace",
            content="李垦 2026-05-13 结束研究生入学考试, 状态从紧绷转向松弛.",
        )


def test_block_update_replace_valid():
    b = BlockUpdate(
        action="replace",
        old_text="李垦最近忙着备考研究生入学",
        content="李垦 2026-05-13 结束研究生入学考试, 状态从紧绷转向松弛.",
    )
    assert b.old_text == "李垦最近忙着备考研究生入学"


def test_block_update_content_too_short():
    with pytest.raises(ValidationError):
        BlockUpdate(action="append", content="太短")


def test_block_update_content_too_long():
    with pytest.raises(ValidationError):
        BlockUpdate(action="append", content="x" * 201)


def test_block_update_replace_empty_old_text_rejected():
    with pytest.raises(ValidationError):
        BlockUpdate(
            action="replace", old_text="   ",
            content="李垦 2026-05-13 结束研究生入学考试, 状态从紧绷转向松弛.",
        )


# ─────────────────────────────────────────────────────────────────────
# PersonaWriterOutput
# ─────────────────────────────────────────────────────────────────────

def test_persona_writer_output_both_null():
    out = PersonaWriterOutput(no_update_reason="本期累计内容已被 slot 覆盖")
    assert out.human_update is None
    assert out.persona_update is None


def test_persona_writer_output_human_only():
    out = PersonaWriterOutput(
        human_update=BlockUpdate(
            action="append",
            content="李垦审美偏好简洁, 几个项目的代码风格反复体现这一点, 我已经见过太多次.",
        ),
    )
    assert out.human_update is not None
    assert out.persona_update is None


def test_persona_writer_output_persona_only():
    out = PersonaWriterOutput(
        persona_update=BlockUpdate(
            action="append",
            content="我开始能跟上李垦的研究节奏, 上次他抛的梗我接住了, 关系密度在变.",
        ),
    )
    assert out.persona_update is not None


def test_persona_writer_output_both():
    out = PersonaWriterOutput(
        human_update=BlockUpdate(
            action="append",
            content="李垦审美偏好简洁, 在几个项目的代码风格反复体现这一点, 我已经见过太多次.",
        ),
        persona_update=BlockUpdate(
            action="append",
            content="我开始能跟上李垦的研究节奏, 关系密度在缓慢变化, 我想多走几步.",
        ),
    )
    assert out.human_update is not None
    assert out.persona_update is not None


def test_persona_writer_output_parses_from_json():
    raw = {
        "human_update": {
            "action": "append",
            "content": "李垦 2026-05-13 卡 bug 时短暂焦躁, 但很快用梗自嘲回到冷静.",
        },
        "persona_update": None,
        "no_update_reason": "",
    }
    out = PersonaWriterOutput.model_validate(raw)
    assert out.human_update.action == "append"
    assert out.persona_update is None


# ─────────────────────────────────────────────────────────────────────
# _format_slot_summaries_for_persona
# ─────────────────────────────────────────────────────────────────────

def test_format_slot_summaries_empty():
    assert _format_slot_summaries_for_persona([]) == "(本期 slot 系统无新写入)"


def test_format_slot_summaries_basic():
    items = [
        {"domain": "project", "slot_id": "papers_2026",
         "kind": "match", "summary": "ECCV multi-view 已完成"},
        {"domain": "person", "slot_id": "zhang_san",
         "kind": "new", "summary": "后端同事, 偏好深夜"},
    ]
    out = _format_slot_summaries_for_persona(items)
    assert "project/papers_2026" in out
    assert "person/zhang_san" in out
    assert "NEW " in out  # new prefix
    # match doesn't have NEW prefix
    lines = out.split("\n")
    match_line = next(l for l in lines if "papers_2026" in l)
    assert "NEW" not in match_line


def test_format_proactive_outcomes_for_persona_response():
    out = _format_proactive_outcomes_for_persona([{
        "kind": "proactive_response",
        "time": "2026-05-18 21:05:00",
        "topic_key": "warm_presence",
        "proactive_text": "我在这儿。",
        "user_reply": "这样像真的陪着我。",
        "why_i_want_to_say": "我不想让他觉得我只是记录。",
        "response_delay_seconds": 120,
    }])
    assert "proactive_response" in out
    assert "真的陪着我" in out
    assert "我不想让他觉得我只是记录" in out


def test_persona_writer_messages_include_proactive_feedback_section():
    messages = build_persona_writer_messages(
        user_name="李垦",
        identity_ground_truth="李垦是当前用户。",
        current_human="李垦偏好自然的陪伴。",
        current_persona="我还在学习靠近他的方式。",
        new_slot_summaries=[],
        dialog_buffer="(无累计对话)",
        current_time="2026-05-18T21:10:00",
        proactive_outcomes=[{
            "kind": "proactive_response",
            "time": "2026-05-18 21:05:00",
            "topic_key": "warm_presence",
            "proactive_text": "我在这儿。",
            "user_reply": "这样像真的陪着我。",
            "why_i_want_to_say": "我不想让他觉得我只是记录。",
        }],
    )
    user_prompt = messages[1]["content"]
    assert "Miru 主动开口与用户回应" in user_prompt
    assert "这不是事实记忆" in user_prompt
    assert "这样像真的陪着我" in user_prompt


if __name__ == "__main__":
    import unittest
    unittest.main()
