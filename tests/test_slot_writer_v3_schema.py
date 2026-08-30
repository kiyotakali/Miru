"""Unit tests for Slot Writer v3 schema (no LLM, pure Pydantic validation)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from pydantic import ValidationError

from memory_prompts_v3 import (  # noqa: E402
    NewSlotMeta,
    IntegrationSpec,
    SlotWrite,
    SlotWriterOutput,
    _format_slot_index_for_prompt,
    _format_chat_messages_for_prompt,
)


# ─────────────────────────────────────────────────────────────────────
# NewSlotMeta validators
# ─────────────────────────────────────────────────────────────────────

def test_new_slot_meta_valid():
    m = NewSlotMeta(
        id="zhang_san",
        title="张三",
        icon="👨‍💻",
        summary="后端同事, 偏好深夜",
        aliases=["张三", "zhangsan", "san"],
    )
    assert m.id == "zhang_san"


def test_new_slot_meta_rejects_uppercase_id():
    with pytest.raises(ValidationError):
        NewSlotMeta(
            id="ZhangSan", title="张三", icon="👨", summary="后端",
            aliases=["a", "b", "c"],
        )


def test_new_slot_meta_rejects_too_short_id():
    with pytest.raises(ValidationError):
        NewSlotMeta(
            id="ab", title="张三", icon="👨", summary="后端",
            aliases=["a", "b", "c"],
        )


def test_new_slot_meta_rejects_too_few_aliases():
    # 2026-05-16: aliases lower bound relaxed from 3 to 1 (natural cases like
    # "CVPR rebuttal" project have only 1-2 core words; forcing >=3 made the
    # LLM either invent suspicious aliases or trigger a retry).
    # Empty list still rejected.
    with pytest.raises(ValidationError):
        NewSlotMeta(
            id="zhang_san", title="张三", icon="👨", summary="后端",
            aliases=[],   # 0 aliases — still rejected
        )


def test_new_slot_meta_rejects_too_many_aliases():
    with pytest.raises(ValidationError):
        NewSlotMeta(
            id="zhang_san", title="张三", icon="👨", summary="后端",
            aliases=["a", "b", "c", "d", "e", "f", "g", "h", "i"],  # 9
        )


def test_new_slot_meta_rejects_duplicate_aliases():
    with pytest.raises(ValidationError):
        NewSlotMeta(
            id="zhang_san", title="张三", icon="👨", summary="后端",
            aliases=["张三", "张三", "zhangsan"],
        )


def test_new_slot_meta_rejects_long_title():
    with pytest.raises(ValidationError):
        NewSlotMeta(
            id="zhang_san", title="a" * 31, icon="👨", summary="后端",
            aliases=["a", "b", "c"],
        )


@pytest.mark.parametrize("length", [80, 81, 150, 200])
def test_new_slot_meta_accepts_summary_up_to_200(length):
    meta = NewSlotMeta(
        id="zhang_san", title="张三", icon="👨", summary="x" * length,
        aliases=["a", "b", "c"],
    )
    assert len(meta.summary) == length


def test_new_slot_meta_truncates_summary_over_200():
    meta = NewSlotMeta(
        id="zhang_san", title="张三", icon="👨", summary="x" * 201,
        aliases=["a", "b", "c"],
    )
    assert len(meta.summary) == 200
    assert meta.summary.endswith("…")


def test_new_slot_meta_rejects_empty_summary():
    with pytest.raises(ValidationError):
        NewSlotMeta(
            id="zhang_san", title="张三", icon="👨", summary=" ",
            aliases=["a", "b", "c"],
        )


# ─────────────────────────────────────────────────────────────────────
# IntegrationSpec validators
# ─────────────────────────────────────────────────────────────────────

def test_integration_spec_valid():
    s = IntegrationSpec(
        content_to_integrate="2026-05-13 李垦遇到 stream disconnected, 嘉骏建议调小 chunk size.",
    )
    assert "李垦" in s.content_to_integrate


def test_integration_spec_accepts_short_identity_fact():
    s = IntegrationSpec(content_to_integrate="梁嘉骏是中山大学大三学生。")
    assert s.content_to_integrate == "梁嘉骏是中山大学大三学生。"


def test_integration_spec_empty_rejected():
    with pytest.raises(ValidationError):
        IntegrationSpec(content_to_integrate="   ")


def test_integration_spec_too_long():
    with pytest.raises(ValidationError):
        IntegrationSpec(content_to_integrate="x" * 700)


# ─────────────────────────────────────────────────────────────────────
# SlotWrite consistency
# ─────────────────────────────────────────────────────────────────────

def test_slot_write_match_valid():
    sw = SlotWrite(
        kind="match", domain="project", slot_id="papers_2026",
        integration=IntegrationSpec(
            content_to_integrate="2026-05-13 李垦做完了 ECCV multi-view ablation 实验.",
        ),
    )
    assert sw.slot_id == "papers_2026"
    assert sw.new_slot_meta is None


def test_slot_write_match_missing_slot_id_rejected():
    with pytest.raises(ValidationError):
        SlotWrite(
            kind="match", domain="project", slot_id=None,
            integration=IntegrationSpec(
                content_to_integrate="2026-05-13 李垦做完了实验, 准备给阿明 review.",
            ),
        )


def test_slot_write_new_valid():
    sw = SlotWrite(
        kind="new", domain="person",
        new_slot_meta=NewSlotMeta(
            id="zhang_san", title="张三", icon="👨",
            summary="后端同事", aliases=["张三", "zhangsan", "san"],
        ),
        integration=IntegrationSpec(
            content_to_integrate="2026-05-13 李垦认识同事张三, 后端开发, 也偏好深夜写代码.",
        ),
    )
    assert sw.new_slot_meta.id == "zhang_san"


def test_slot_write_new_missing_meta_rejected():
    with pytest.raises(ValidationError):
        SlotWrite(
            kind="new", domain="person", new_slot_meta=None,
            integration=IntegrationSpec(
                content_to_integrate="2026-05-13 李垦认识同事张三, 后端开发.",
            ),
        )


def test_slot_write_new_with_slot_id_rejected():
    with pytest.raises(ValidationError):
        SlotWrite(
            kind="new", domain="person", slot_id="zhang_san",
            new_slot_meta=NewSlotMeta(
                id="zhang_san", title="张三", icon="👨",
                summary="后端", aliases=["a", "b", "c"],
            ),
            integration=IntegrationSpec(
                content_to_integrate="2026-05-13 李垦认识同事张三, 后端开发.",
            ),
        )


def test_slot_write_self_identity_rejected_match():
    with pytest.raises(ValidationError):
        SlotWrite(
            kind="match", domain="self", slot_id="identity",
            integration=IntegrationSpec(
                content_to_integrate="李垦是研究生, 这是身份事实.",
            ),
        )


def test_slot_write_self_identity_rejected_new():
    with pytest.raises(ValidationError):
        SlotWrite(
            kind="new", domain="self",
            new_slot_meta=NewSlotMeta(
                id="identity", title="用户身份", icon="🪪",
                summary="身份", aliases=["a", "b", "c"],
            ),
            integration=IntegrationSpec(
                content_to_integrate="李垦是研究生, 这是身份事实.",
            ),
        )


# ─────────────────────────────────────────────────────────────────────
# SlotWriterOutput cap + parsing
# ─────────────────────────────────────────────────────────────────────

def _make_match(slot_id="papers_2026"):
    return SlotWrite(
        kind="match", domain="project", slot_id=slot_id,
        integration=IntegrationSpec(
            content_to_integrate="2026-05-13 李垦做完了实验. 准备给阿明 review.",
        ),
    )


def test_slot_writer_output_caps_at_6():
    SlotWriterOutput(slot_writes=[_make_match() for _ in range(6)])
    with pytest.raises(ValidationError):
        SlotWriterOutput(slot_writes=[_make_match() for _ in range(7)])


def test_slot_writer_output_empty_ok():
    out = SlotWriterOutput(
        slot_writes=[], skipped_reason="纯寒暄, 没值得记的"
    )
    assert out.slot_writes == []


def test_slot_writer_output_parses_from_json():
    raw = {
        "slot_writes": [
            {
                "kind": "match", "domain": "project", "slot_id": "papers_2026",
                "integration": {
                    "content_to_integrate":
                        "2026-05-13 李垦完成 ECCV multi-view ablation 实验, 效果不错. 准备 2026-05-14 给阿明 review.",
                },
            }
        ],
        "completed_commitments": [],
        "skipped_reason": "",
    }
    out = SlotWriterOutput.model_validate(raw)
    assert len(out.slot_writes) == 1
    assert out.slot_writes[0].slot_id == "papers_2026"


def test_slot_writer_output_accepts_short_person_correction():
    raw = {
        "slot_writes": [
            {
                "kind": "match", "domain": "person", "slot_id": "liu_yu_cheng",
                "integration": {
                    "content_to_integrate": "刘垣呈是中山大学大三学生。",
                },
            },
            {
                "kind": "new", "domain": "person", "slot_id": None,
                "new_slot_meta": {
                    "id": "liang_jia_jun",
                    "title": "梁嘉骏",
                    "icon": "👤",
                    "summary": "中山大学大三学生",
                    "aliases": ["梁嘉骏", "嘉骏"],
                },
                "integration": {
                    "content_to_integrate": "梁嘉骏是中山大学大三学生。",
                },
            },
        ],
        "completed_commitments": [],
        "skipped_reason": "",
    }
    out = SlotWriterOutput.model_validate(raw)
    assert len(out.slot_writes) == 2
    assert out.slot_writes[0].integration.content_to_integrate == "刘垣呈是中山大学大三学生。"
    assert out.slot_writes[1].new_slot_meta.title == "梁嘉骏"


# ─────────────────────────────────────────────────────────────────────
# Formatters
# ─────────────────────────────────────────────────────────────────────

def test_format_slot_index():
    idx = {
        "project": [
            {"id": "papers_2026", "title": "2026 论文投稿",
             "summary": "...", "aliases": ["论文"],
             "last_active": "2026-05-13T10:00:00"},
        ],
        "person": [],
        "topic": [],
        "self": [],
    }
    out = _format_slot_index_for_prompt(idx)
    assert "## project" in out
    assert "papers_2026" in out
    assert "## person (无)" in out


def test_format_chat_messages():
    msgs = [
        {"role": "user", "text": "调 AgiBot 数据加载", "time": "2026-05-13T14:23:00"},
        {"role": "assistant", "text": "嗯嗯", "time": "2026-05-13T14:24:00"},
        {"role": "user", "text": "我跟嘉骏说了", "time": "2026-05-13T14:25:00"},
    ]
    out = _format_chat_messages_for_prompt(msgs, miru_name="Miru", user_name="李垦")
    assert "李垦: 调 AgiBot 数据加载" in out
    assert "Miru: 嗯嗯" in out


def test_format_chat_messages_with_image():
    msgs = [
        {"role": "user", "text": "看这个", "time": "2026-05-13T14:23:00",
         "image": "x.png", "image_desc": "一只白色英短猫"},
    ]
    out = _format_chat_messages_for_prompt(msgs, miru_name="Miru", user_name="李垦")
    assert "[图片: 一只白色英短猫]" in out


if __name__ == "__main__":
    import unittest
    unittest.main()
