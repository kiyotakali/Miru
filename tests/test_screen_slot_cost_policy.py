import core
import memory_prompts_v2


def test_sig3_routine_screenshot_uses_low_cost_memory_tier():
    policy = core._screen_slot_writer_policy(
        "用户在 VS Code 浏览一个 Python 文件, 看起来是在阅读代码。",
        3,
    )
    assert policy["tier"] == "memory"
    assert policy["reasoning"] is False
    assert policy["pass4_tier"] == "memory"
    assert policy["pass4_reasoning"] is False
    assert policy["pass4_max_tokens"] == 1000


def test_sig4_screenshot_without_ddl_or_project_change_stays_low_cost():
    policy = core._screen_slot_writer_policy(
        "用户在微信和朋友聊天, 对方发来一张旅行照片, 氛围轻松。",
        4,
    )
    assert policy["tier"] == "memory"
    assert policy["reasoning"] is False
    assert policy["pass4_tier"] == "memory"
    assert policy["pass4_reasoning"] is False
    assert policy["pass4_max_tokens"] == 1000
    assert policy["name"] == "routine_sig4_low_cost"


def test_sig3_deadline_screenshot_uses_strong_chat_tier():
    policy = core._screen_slot_writer_policy(
        "日历页面显示明天 15:00 有组会, 旁边写着提交 rebuttal DDL。",
        3,
    )
    assert policy["tier"] == "chat"
    assert policy["reasoning"] is False
    assert policy["reasoning_budget"] == 0
    assert policy["pass4_tier"] == "memory"
    assert policy["pass4_reasoning"] is False
    assert policy["pass4_max_tokens"] == 1000
    assert "ddl" in policy["name"]


def test_sig3_concrete_project_update_uses_strong_chat_tier():
    policy = core._screen_slot_writer_policy(
        "GitHub 页面显示论文项目的 bug 已修复, CI test passed。",
        3,
    )
    assert policy["tier"] == "chat"
    assert policy["reasoning"] is False
    assert policy["reasoning_budget"] == 0
    assert policy["pass4_tier"] == "memory"
    assert policy["pass4_reasoning"] is False
    assert policy["pass4_max_tokens"] == 1000
    assert "project_update" in policy["name"]


def test_manual_legacy_slot_merge_defaults_to_no_reasoning():
    defaults = memory_prompts_v2.call_legacy_slot_merge_rewrite.__kwdefaults__ or {}
    assert defaults["reasoning"] is False
    assert defaults["reasoning_budget"] == 0
