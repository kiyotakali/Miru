"""Tests for Pass 4 AppendEditor validation.

Daily chat/screenshot match writes no longer rewrite the whole slot body.
Pass 4 is a small semantic-delta gate: decide append/skip, return one short
entry, and optionally patch title/summary only when the slot's main line changed.
"""

import json
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import memory_prompts_v2  # noqa: E402


def _mock_append_call(
    *,
    should_append=True,
    entry="auth.py 阻塞解除，准备对齐论文段落。",
    title_update=None,
    summary_update=None,
    skip_reason="重复现有内容",
    entry_kind="progress",
):
    payload = {
        "should_append": should_append,
        "append_entry": entry if should_append else "",
        "entry_kind": entry_kind,
        "title_update": title_update,
        "summary_update": summary_update,
        "alias_additions": [],
        "skip_reason": "" if should_append else skip_reason,
    }
    return json.dumps(payload, ensure_ascii=False)


def _call_append(source_type="chat", **kwargs):
    return memory_prompts_v2.call_pass4_append_slot(
        domain="project",
        current_title="Motus v2",
        current_summary="用户在推进 Motus v2 实验。",
        current_body="用户正在推进 Motus v2。\n\n## 近期记录\n- [2026-05-19 10:12] 整理了 ablation。",
        current_aliases=["Motus v2"],
        new_content=kwargs.pop("new_content", "auth.py 阻塞解除，准备对齐论文段落。"),
        source_type=source_type,
        source_context=kwargs.pop("source_context", ""),
        **kwargs,
    )


def test_pass4_append_returns_patch_fields_default_null():
    with patch("prompt._call_llm_text", return_value=_mock_append_call()):
        result = _call_append()
    assert result is not None
    assert result["should_append"] is True
    assert result["append_entry"] == "auth.py 阻塞解除，准备对齐论文段落。"
    assert result["title_update"] is None
    assert result["summary_update"] is None


def test_pass4_append_accepts_summary_patch_when_needed():
    summary = "用户在推进 Motus v2，auth.py 阻塞已解除，下一步对齐论文段落。"
    with patch("prompt._call_llm_text",
               return_value=_mock_append_call(summary_update=summary)):
        result = _call_append()
    assert result is not None
    assert result["summary_update"] == summary
    assert result["title_update"] is None


def test_pass4_append_summary_patch_over_200_truncated():
    summary = "李" * 201
    with patch("prompt._call_llm_text",
               return_value=_mock_append_call(summary_update=summary)) as mock_llm:
        result = _call_append()
    assert result is not None
    assert result["summary_update"] == "李" * 199 + "…"
    assert mock_llm.call_count == 1


def test_pass4_append_long_entry_truncated_to_90():
    entry = "李" * 140
    with patch("prompt._call_llm_text",
               return_value=_mock_append_call(entry=entry)) as mock_llm:
        result = _call_append()
    assert result is not None
    assert len(result["append_entry"]) == 90
    assert result["append_entry"] == "李" * 90
    assert mock_llm.call_count == 1


def test_pass4_append_skip_requires_reason():
    with patch("prompt._call_llm_text",
               return_value=_mock_append_call(should_append=False)):
        result = _call_append(source_type="screenshot")
    assert result is not None
    assert result["should_append"] is False
    assert result["skip_reason"] == "重复现有内容"
    assert result["append_entry"] == ""


def test_pass4_append_skip_without_reason_retries():
    payload = {
        "should_append": False,
        "append_entry": "",
        "entry_kind": "other",
        "title_update": None,
        "summary_update": None,
        "alias_additions": [],
        "skip_reason": "",
    }
    with patch("prompt._call_llm_text",
               return_value=json.dumps(payload, ensure_ascii=False)) as mock_llm:
        result = _call_append()
    assert result is None
    assert mock_llm.call_count >= 2


def test_pass4_append_prompt_explains_miru_slot_and_new_path():
    captured = {}

    def _capture_call(system, user, **kwargs):
        captured["system"] = system
        captured["user"] = user
        captured.update(kwargs)
        return _mock_append_call()

    with patch("prompt._call_llm_text", side_effect=_capture_call):
        _call_append(source_type="screenshot")

    system = captured["system"]
    user = captured["user"]
    assert "Miru 是一个会陪用户生活" in system
    assert "当前记忆卡(slot)" in system
    assert "新建记忆卡(kind=new)不会调用你" in system
    assert "append_entry 目标 10-60" in system
    assert "- source: screenshot" in user
    assert "source_note" in user


def test_pass4_append_defaults_to_memory_no_reasoning_and_low_max_tokens():
    captured = {}

    def _capture_call(*args, **kwargs):
        captured.update(kwargs)
        return _mock_append_call()

    with patch("prompt._call_llm_text", side_effect=_capture_call):
        _call_append(max_tokens=4000)
    assert captured.get("reasoning") is False
    assert captured.get("tier") == "memory"
    assert captured.get("max_tokens") == 1000
    assert captured.get("call_label") == "Pass4Append:chat"
