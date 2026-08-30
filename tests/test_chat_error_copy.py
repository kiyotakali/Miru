from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_chat_error_copy_hides_internal_ai_tier_wording():
    html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")

    assert "friendlyChatErrorMessage" in html
    assert "Miru 还没有配置好模型" in html
    assert "视觉、聊天、记忆三类 API Key" in html
    chat_send_block = html[html.index("async function sendCompanionMessage"):html.index("let _companionLastMsgId")]
    assert "friendlyChatErrorMessage" in chat_send_block
    assert "出错了:" not in chat_send_block
    assert "网络错误:" not in chat_send_block


def test_backend_missing_model_copy_does_not_use_admin_ui_language():
    prompt_py = (ROOT / "prompt.py").read_text(encoding="utf-8")
    memory_prompts_py = (ROOT / "memory_prompts.py").read_text(encoding="utf-8")

    user_facing_blocks = prompt_py[prompt_py.index("def _get_ai_runtime_or_raise"):prompt_py.index("def _get_chat_model")]
    user_facing_blocks += "\n" + memory_prompts_py[memory_prompts_py.index("def call_screen_observation"):memory_prompts_py.index("model = runtime")]

    assert "模型配置还没有完成" in user_facing_blocks
    assert "Miru 设置" in user_facing_blocks
    assert "AI tier" not in user_facing_blocks
    assert "Admin UI" not in user_facing_blocks
    assert "Vision tier API key not set" not in user_facing_blocks


def test_backend_friendly_chat_error_mapping_hides_internal_terms():
    import core

    samples = [
        RuntimeError("模型配置还没有完成：聊天模型 缺少服务地址。请在 Miru 设置里的「模型」页填写后再试。"),
        RuntimeError("AI tier 'chat' API Key is not set. Configure via Admin UI > AI 配置."),
        TimeoutError("API timeout after 30s"),
        ConnectionError("connection refused"),
        RuntimeError("unexpected provider failure"),
    ]

    replies = [core._friendly_chat_generation_error(exc) for exc in samples]

    assert "还没有配置好模型" in replies[0]
    assert "还没有配置好模型" in replies[1]
    assert "没连上模型服务" in replies[2]
    assert "没连上模型服务" in replies[3]
    assert "出了点小问题" in replies[4]
    for reply in replies:
        lowered = reply.lower()
        assert "ai tier" not in lowered
        assert "admin ui" not in lowered
        assert "api timeout" not in lowered
