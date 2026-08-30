from unittest.mock import patch


def test_chat_preflight_reads_slot_and_legacy_paths_from_index(monkeypatch):
    import memory
    import prompt

    index = """
## Projects
- 🧪 **Miru 项目** — `projects/miru/main.md`
  - 桌宠与主 agent 成本优化

## People
- [小张](people/xiao_zhang.md) — 合作伙伴
"""
    files = {
        "projects/miru/main.md": "# Miru 项目\n\n正在做 agent 路由。",
        "people/xiao_zhang.md": "# 小张\n\n一起讨论过模型成本。",
    }

    monkeypatch.setattr(memory, "read_index", lambda: index)
    monkeypatch.setattr(memory, "read_file", lambda path: files.get(path))
    monkeypatch.setattr(memory, "search", lambda *args, **kwargs: [])

    def fake_llm(system_prompt, user_text, **kwargs):
        assert "全局长期记忆索引" in system_prompt
        assert "不要判断 fast / pro / reasoning" in system_prompt
        assert "projects/miru/main.md" in user_text
        return {
            "need_retrieval": True,
            "files": [
                "projects/miru/main.md",
                "people/xiao_zhang.md",
                "projects/not_in_index/main.md",
            ],
            "keywords": [],
            "confidence": 0.88,
            "retrieval_reason": "需要读相关长期记忆",
        }

    with patch("prompt._call_retrieval_llm", side_effect=fake_llm):
        result = prompt.call_chat_preflight("继续看 Miru 项目的模型成本")

    assert result["confidence"] == 0.88
    assert result["retrieval_reason"] == "需要读相关长期记忆"
    assert "正在做 agent 路由" in result["memory_context"]
    assert "一起讨论过模型成本" in result["memory_context"]
    assert "not_in_index" not in result["memory_context"]


def test_memory_retrieval_wrapper_stays_string_compatible(monkeypatch):
    import prompt

    monkeypatch.setattr(
        prompt,
        "call_chat_preflight",
        lambda *args, **kwargs: {"memory_context": "【相关长期记忆】\nhello"},
    )

    assert prompt.call_memory_retrieval("hello") == "【相关长期记忆】\nhello"


def test_chat_policy_always_uses_pro_for_simple_turn():
    import core

    policy = core._resolve_chat_agent_policy(
        {
            "agent_mode": "fast",
            "confidence": 0.92,
            "complexity": "simple",
            "tool_likelihood": "none",
            "reasoning_need": "none",
            "latency_preference": "fast",
            "need_retrieval": False,
            "files": [],
            "memory_context": "",
        },
        "早呀",
    )

    assert policy["mode"] == "v4_pro"
    assert policy["tier"] == "chat"
    assert policy["allow_tools"] is True
    assert policy["max_iterations"] == 8
    assert policy["reason"] == "main_agent_fixed_pro"


def test_chat_policy_defaults_low_confidence_to_v4_pro():
    import core

    policy = core._resolve_chat_agent_policy(
        {"agent_mode": "fast", "confidence": 0.2},
        "早呀",
    )

    assert policy["mode"] == "v4_pro"
    assert policy["tier"] == "chat"
    assert policy["reasoning"] is False
    assert policy["allow_tools"] is True


def test_chat_policy_keeps_pro_when_tool_or_memory_needed():
    import core

    policy = core._resolve_chat_agent_policy(
        {
            "agent_mode": "fast",
            "confidence": 0.9,
            "complexity": "simple",
            "tool_likelihood": "none",
            "reasoning_need": "none",
            "latency_preference": "fast",
            "need_retrieval": False,
            "files": [],
        },
        "提醒我明天提交论文",
    )

    assert policy["mode"] == "v4_pro"
    assert policy["allow_tools"] is True


def test_chat_policy_never_auto_enables_reasoning():
    import core

    from_reasoning_request = core._resolve_chat_agent_policy(
        {
            "agent_mode": "v4_pro_reasoning",
            "confidence": 0.9,
            "complexity": "simple",
            "tool_likelihood": "none",
            "reasoning_need": "high",
            "latency_preference": "quality",
        },
        "陪我聊两句",
    )
    assert from_reasoning_request["mode"] == "v4_pro"
    assert from_reasoning_request["reasoning"] is False

    hard_case = core._resolve_chat_agent_policy(
        {
            "agent_mode": "v4_pro",
            "confidence": 0.9,
            "complexity": "hard",
            "tool_likelihood": "likely",
            "reasoning_need": "high",
            "latency_preference": "quality",
        },
        "不急，请仔细检查这个架构和代码 bug 的修复方案",
    )
    assert hard_case["mode"] == "v4_pro"
    assert hard_case["reasoning"] is False
    assert hard_case["reasoning_budget"] == 0


def test_chat_with_companion_calls_main_agent_fixed_pro_even_if_preflight_says_fast(tmp_path, monkeypatch):
    import core
    import prompt
    import storage

    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(core, "_build_chat_context", lambda: "BASE_CONTEXT")
    monkeypatch.setattr(
        prompt,
        "call_chat_preflight",
        lambda *args, **kwargs: {
            "agent_mode": "fast",
            "confidence": 0.99,
            "memory_context": "【相关长期记忆】\nPRELOADED_MEMORY",
        },
    )

    captured = {}

    def fake_agent(context_text, history, user_text, tool_handlers, **kwargs):
        captured.update(kwargs)
        captured["context_text"] = context_text
        captured["tool_handler_names"] = set(tool_handlers.keys())
        return {"reply": "好，我在。", "tool_calls": []}

    monkeypatch.setattr(core, "call_chat_agent", fake_agent)

    class FakeSleep:
        def enqueue(self, *args, **kwargs):
            return None

    with patch("sleep_agent.get_sleep_agent", return_value=FakeSleep()), \
         patch("attention_engine.get_attention_engine") as fake_engine:
        fake_engine.return_value.record_signal.return_value = None
        result = core.chat_with_companion("早呀")

    assert result["reply"] == "好，我在。"
    assert "PRELOADED_MEMORY" in captured["context_text"]
    assert captured["tier"] == "chat"
    assert captured["reasoning"] is False
    assert captured["reasoning_budget"] == 0
    assert captured["max_iterations"] == 8
    assert captured["tools_list"]
    assert {"archival_memory_search", "look_at_screen"} <= captured["tool_handler_names"]


def test_csm_full_reply_calls_main_agent_fixed_pro_even_if_preflight_says_reasoning(tmp_path, monkeypatch):
    import core
    import prompt
    import storage

    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(core, "_build_chat_context", lambda: "BASE_CONTEXT")
    monkeypatch.setattr(
        prompt,
        "call_chat_preflight",
        lambda *args, **kwargs: {
            "agent_mode": "v4_pro_reasoning",
            "confidence": 0.99,
            "complexity": "hard",
            "reasoning_need": "high",
            "memory_context": "【相关长期记忆】\nCSM_MEMORY",
        },
    )

    captured = {}

    def fake_agent(context_text, history, user_text, tool_handlers, **kwargs):
        captured.update(kwargs)
        captured["context_text"] = context_text
        captured["user_text"] = user_text
        return {"reply": "我会认真看，但不开 reasoning 路由。", "tool_calls": []}

    delivered = {}

    def fake_deliver(reply_msg, user_id, device_id=""):
        delivered["reply_msg"] = reply_msg
        delivered["user_id"] = user_id
        delivered["device_id"] = device_id

    monkeypatch.setattr(core, "call_chat_agent", fake_agent)
    monkeypatch.setattr(core, "_csm_try_deliver", fake_deliver)

    import app as app_mod

    with app_mod.app.app_context():
        core._csm_do_full_reply([{
            "id": "u_csm",
            "role": "user",
            "text": "不急，请完整检查这个复杂问题。",
            "time": "2026-05-30 18:00:00",
            "_user_data_dir": str(tmp_path),
            "_device_id": "dev_test",
        }], "u_test")

    assert delivered["user_id"] == "u_test"
    assert delivered["device_id"] == "dev_test"
    assert delivered["reply_msg"]["text"] == "我会认真看，但不开 reasoning 路由。"
    assert "CSM_MEMORY" in captured["context_text"]
    assert captured["user_text"] == "不急，请完整检查这个复杂问题。"
    assert captured["tier"] == "chat"
    assert captured["reasoning"] is False
    assert captured["reasoning_budget"] == 0
    assert captured["max_iterations"] == 8
    assert captured["tools_list"]


def test_chat_with_companion_model_config_error_is_user_facing(tmp_path, monkeypatch):
    import core
    import prompt
    import storage

    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(core, "_build_chat_context", lambda: "BASE_CONTEXT")
    monkeypatch.setattr(
        prompt,
        "call_chat_preflight",
        lambda *args, **kwargs: {"memory_context": ""},
    )
    monkeypatch.setattr(
        core,
        "call_chat_agent",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("AI tier 'chat' API Key is not set. Configure via Admin UI > AI 配置.")
        ),
    )

    class FakeSleep:
        def enqueue(self, *args, **kwargs):
            return None

    with patch("sleep_agent.get_sleep_agent", return_value=FakeSleep()), \
         patch("attention_engine.get_attention_engine") as fake_engine:
        fake_engine.return_value.record_signal.return_value = None
        result = core.chat_with_companion("你好")

    assert "模型" in result["reply"]
    assert "设置" in result["reply"]
    assert "AI tier" not in result["reply"]
    assert "Admin UI" not in result["reply"]


def test_csm_generation_error_is_user_facing(tmp_path, monkeypatch):
    import core
    import prompt
    import storage

    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(core, "_build_chat_context", lambda: "BASE_CONTEXT")
    monkeypatch.setattr(
        prompt,
        "call_chat_preflight",
        lambda *args, **kwargs: {"memory_context": ""},
    )
    monkeypatch.setattr(
        core,
        "call_chat_agent",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("AI tier 'chat' API Key is not set. Configure via Admin UI > AI 配置.")
        ),
    )
    delivered = {}
    monkeypatch.setattr(core, "_csm_try_deliver", lambda reply_msg, user_id, device_id="": delivered.update(reply_msg=reply_msg))

    import app as app_mod

    with app_mod.app.app_context():
        core._csm_do_full_reply([{
            "id": "u_csm_err",
            "role": "user",
            "text": "你好",
            "time": "2026-06-12 18:00:00",
            "_user_data_dir": str(tmp_path),
            "_device_id": "dev_test",
        }], "u_test")

    reply = delivered["reply_msg"]["text"]
    assert "模型" in reply
    assert "设置" in reply
    assert "AI tier" not in reply
    assert "Admin UI" not in reply
