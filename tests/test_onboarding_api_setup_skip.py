import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def test_app_bootstrap_cleans_api_setup_redirect_marker_without_skip_state():
    html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")

    assert "params.get('api_setup')" in html
    assert "apiSetupState === 'configured'" in html
    assert "localStorage.setItem('miru_api_setup_skipped'" not in html
    assert "apiSetupState === 'skipped'" not in html
    assert "localStorage.removeItem('miru_api_setup_skipped')" in html
    assert "params.delete('api_setup')" in html


def test_explicit_skip_greeting_flag_uses_default_first_greeting_without_llm():
    import core
    import storage

    tmp = tempfile.mkdtemp(prefix="miru_onboarding_api_skip_")
    try:
        with patch.object(storage, "get_data_dir", return_value=tmp), \
             patch("prompt._call_llm_text", side_effect=AssertionError("LLM must not be called")):
            msg = core._inject_custom_first_greeting({
                "name": "测试用户",
                "_skip_llm_greeting": True,
            })

            assert msg is not None
            assert msg["text"] == "……我是 Miru。以后就由我陪在你旁边了。"
            history = storage.read_json(storage.chat_history_path())
            assert history and history[0]["text"] == msg["text"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_chat_tier_readiness_uses_real_ai_config(monkeypatch):
    import ai_config
    import core

    tmp = tempfile.mkdtemp(prefix="miru_onboarding_chat_ready_")
    try:
        monkeypatch.setenv("DATA_DIR", tmp)
        ai_config.invalidate_cache()

        assert core._chat_tier_ready_for_greeting() is False

        result = ai_config.update_tier_config("chat", {
            "host": "api.deepseek.com",
            "model": "deepseek-chat",
            "api_key": "dummy-chat-key",
        })
        assert result["ok"] is True
        ai_config.invalidate_cache()

        assert core._chat_tier_ready_for_greeting() is True
    finally:
        ai_config.invalidate_cache()
        shutil.rmtree(tmp, ignore_errors=True)


def test_no_chat_tier_config_uses_default_first_greeting_without_llm():
    import core
    import storage

    tmp = tempfile.mkdtemp(prefix="miru_onboarding_no_chat_key_")
    try:
        with patch.object(storage, "get_data_dir", return_value=tmp), \
             patch.object(core, "_chat_tier_ready_for_greeting", return_value=False), \
             patch("prompt._call_llm_text", side_effect=AssertionError("LLM must not be called")):
            msg = core._inject_custom_first_greeting({"name": "测试用户"})

            assert msg is not None
            assert msg["text"] == "……我是 Miru。以后就由我陪在你旁边了。"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_first_greeting_prompt_and_timestamp_use_user_timezone():
    import core
    import storage

    tmp = tempfile.mkdtemp(prefix="miru_onboarding_tz_")
    calls = []

    def fake_llm(system_prompt, user_prompt, **_kwargs):
        calls.append((system_prompt, user_prompt))
        return "李垦，晚上好。我刚记住你最近在忙具身智能和世界模型，先陪你把今晚收个尾。"

    try:
        with patch.object(storage, "get_data_dir", return_value=tmp), \
             patch.object(core, "_user_now", return_value=datetime(2026, 6, 24, 23, 49, 42)), \
             patch.object(core, "_chat_tier_ready_for_greeting", return_value=True), \
             patch("prompt._call_llm_text", side_effect=fake_llm):
            msg = core._inject_custom_first_greeting({
                "name": "李垦",
                "focus": "具身智能和世界模型",
            })

            assert msg is not None
            assert msg["id"] == "greeting_20260624_234942"
            assert msg["time"] == "2026-06-24 23:49:42"
            rendered = "\n".join(calls[0])
            assert "现在是晚上" in rendered
            assert "现在 下午" not in rendered
            assert "现在是下午" not in rendered
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
