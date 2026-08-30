"""AttentionEngine persona alignment after CareEngine retirement."""

import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def _fake_cfg(**overrides):
    cfg = MagicMock()
    cfg.name = overrides.get("name", "Miru")
    cfg.user_address = overrides.get("user_address", "你")
    cfg.raw_text = overrides.get(
        "raw_text",
        "【性格】安静但在意\n【说话方式】短、轻、像真实陪伴",
    )
    return cfg


def test_attention_prompt_injects_full_soul_and_love_principle():
    import attention_engine

    cfg = _fake_cfg(raw_text="完整 soul 签名 SOUL_ATTENTION_123")
    with patch("character.get_config", return_value=cfg), \
         patch("prompt_identity.resolve_user_entity_label", return_value="用户"):
        prompt = attention_engine._build_attention_system_prompt()

    assert "完整 soul 签名 SOUL_ATTENTION_123" in prompt
    assert "爱与偏爱" in prompt
    assert "真的在乎" in prompt
    assert "第一人称" in prompt
    assert "speak_intent" in prompt


def test_attention_prompt_uses_entity_label_not_user_address():
    import attention_engine

    cfg = _fake_cfg(user_address="kiyo")
    with patch("character.get_config", return_value=cfg), \
         patch("prompt_identity.resolve_user_entity_label", return_value="李垦"):
        prompt = attention_engine._build_attention_system_prompt()

    assert "master" not in prompt
    assert "李垦的赛博陪伴者" in prompt
    assert "kiyo 的" not in prompt


def test_main_agent_proactive_mode_keeps_attention_continuity():
    import prompt

    system = prompt._build_agent_system_prompt(agent_mode="proactive")

    assert "Proactive Mode" in system
    assert "我最近没有说出口的想法" in system
    assert "AttentionEngine" in system
    assert "只输出最终要发给用户的一条消息" in system
    assert "短而有体温" in system


def test_main_agent_prompt_normalizes_user_entity_label():
    import prompt

    cfg = _fake_cfg(
        user_address="你",
        raw_text="",
    )
    cfg.hint.return_value = "会记住你一切的女生"
    cfg.personality = "她会因为你熬夜而有点生气。"
    cfg.speech_patterns = "称呼用户：刚认识时用\"你\"。"
    cfg.backstory = "她不是 AI 助手，碰巧很在意你。"
    cfg.interests = ""
    cfg.emotional_reactions = ""
    cfg.relationship_stages = ""
    cfg.agent_behavior = ""

    with patch("prompt.get_config", return_value=cfg), \
         patch("prompt.resolve_user_entity_label", return_value="李垦"):
        system = prompt._build_agent_system_prompt(agent_mode="reactive")

    assert "你是 Miru，李垦的" in system
    assert "记住李垦一切" in system
    assert "你会因为李垦熬夜" in system
    assert "碰巧很在意李垦" in system
    assert "称呼对方为「你」" in system
    assert "先是一个真实在乎李垦" in system
    assert "不要凭外观硬猜角色名" in system
    assert "不要说“没看到图片”" in system
    assert "你 的" not in system


def test_retired_care_prompt_builder_is_gone():
    import care_engine

    assert not hasattr(care_engine, "_build_care_system_prompt")
    assert not hasattr(care_engine, "build_care_snapshot_prompt")
