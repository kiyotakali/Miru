import json
import os
from types import SimpleNamespace

import prompt
import memory_prompts
import memory_prompts_v2


def _reset_usage_log(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    prompt._USAGE_LOG_PATH = None
    return tmp_path / "_admin" / "llm_usage.jsonl"


def _fake_response(content="ok", usage=None):
    if usage is None:
        usage = SimpleNamespace(
            prompt_tokens=100,
            completion_tokens=20,
            total_tokens=120,
            prompt_cache_hit_tokens=80,
            prompt_cache_miss_tokens=20,
            completion_tokens_details={"reasoning_tokens": 7},
        )
    return SimpleNamespace(
        usage=usage,
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content),
            )
        ],
    )


def test_log_llm_usage_records_cache_and_reasoning(tmp_path, monkeypatch):
    path = _reset_usage_log(tmp_path, monkeypatch)

    prompt._log_llm_usage(
        "chat",
        "deepseek-v4-pro",
        _fake_response(),
        call_label="unit",
        reasoning_enabled=True,
    )

    row = json.loads(path.read_text().strip())
    assert row["prompt_tokens"] == 100
    assert row["completion_tokens"] == 20
    assert row["total_tokens"] == 120
    assert row["prompt_cache_hit_tokens"] == 80
    assert row["prompt_cache_miss_tokens"] == 20
    assert row["reasoning_tokens"] == 7
    assert row["call_label"] == "unit"
    assert row["reasoning_enabled"] is True


def test_call_llm_json_uses_explicit_call_label(tmp_path, monkeypatch):
    path = _reset_usage_log(tmp_path, monkeypatch)

    class FakeCompletions:
        def create(self, **kwargs):
            return _fake_response(content='{"ok": true}')

    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=FakeCompletions())
    )
    monkeypatch.setattr(
        "ai_config.get_tier_config",
        lambda tier: {
            "model": "deepseek-v4-flash",
            "host": "api.deepseek.com",
            "api_key": "x",
            "max_tokens": 10000,
        },
    )
    monkeypatch.setattr("ai_config.get_tier_client", lambda tier: fake_client)

    out = prompt._call_llm_json(
        "system", "user", tier="memory", call_label="UnitJsonLabel"
    )

    assert out == {"ok": True}
    row = json.loads(path.read_text().strip())
    assert row["call_label"] == "UnitJsonLabel"


def test_call_llm_json_clips_max_tokens_to_tier_config(tmp_path, monkeypatch):
    path = _reset_usage_log(tmp_path, monkeypatch)
    calls = []

    class FakeCompletions:
        def create(self, **kwargs):
            calls.append(kwargs)
            return _fake_response(content='{"ok": true}')

    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=FakeCompletions())
    )
    monkeypatch.setattr(
        "ai_config.get_tier_config",
        lambda tier: {
            "model": "generic-memory-model",
            "host": "example.com/v1",
            "api_key": "x",
            "max_tokens": 50,
        },
    )
    monkeypatch.setattr("ai_config.get_tier_client", lambda tier: fake_client)

    out = prompt._call_llm_json(
        "system",
        "user",
        tier="memory",
        max_tokens=123,
        call_label="UnitJsonClipped",
    )

    assert out == {"ok": True}
    assert calls[0]["max_tokens"] == 50
    row = json.loads(path.read_text().strip())
    assert row["max_tokens"] == 50


def test_agent_iteration_clips_max_tokens_to_tier_config(tmp_path, monkeypatch):
    path = _reset_usage_log(tmp_path, monkeypatch)
    calls = []

    class FakeCompletions:
        def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                usage=SimpleNamespace(
                    prompt_tokens=10,
                    completion_tokens=5,
                    total_tokens=15,
                ),
                choices=[
                    SimpleNamespace(
                        finish_reason="stop",
                        message=SimpleNamespace(content="收到", tool_calls=None),
                    )
                ],
            )

    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=FakeCompletions())
    )
    monkeypatch.setattr(
        "ai_config.get_tier_config",
        lambda tier: {
            "model": "generic-chat-model",
            "host": "example.com/v1",
            "api_key": "x",
            "max_tokens": 88,
        },
    )

    reply, done = prompt._agent_iteration_openai(
        fake_client,
        "generic-chat-model",
        "system",
        [{"role": "user", "content": "hi"}],
        [],
        {},
        [],
        0,
        max_tokens=50000,
        host="example.com/v1",
        tier="chat",
    )

    assert reply == "收到"
    assert done is True
    assert calls[0]["max_tokens"] == 88
    row = json.loads(path.read_text().strip())
    assert row["max_tokens"] == 88


def test_memory_prompts_retry_helper_logs_successful_response(
    tmp_path, monkeypatch
):
    path = _reset_usage_log(tmp_path, monkeypatch)
    calls = []

    class FakeCompletions:
        def create(self, **kwargs):
            calls.append(kwargs)
            return _fake_response(content="hello")

    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=FakeCompletions())
    )

    monkeypatch.setattr(
        "ai_config.get_tier_config",
        lambda tier: {
            "model": "deepseek-v4-flash",
            "host": "api.deepseek.com",
        },
    )
    monkeypatch.setattr("ai_config.get_tier_client", lambda tier: fake_client)

    out = memory_prompts_v2._call_llm_with_retry(
        messages=[{"role": "user", "content": "hi"}],
        output_schema=None,
        pass_label="UnitPass",
        tier="memory",
        reasoning=False,
        max_tokens=123,
    )

    assert out == "hello"
    assert calls, "fake client should be called"
    row = json.loads(path.read_text().strip())
    assert row["tier"] == "memory"
    assert row["model"] == "deepseek-v4-flash"
    assert row["call_label"] == "UnitPass"
    assert row["max_tokens"] == 123
    assert row["reasoning_enabled"] is False
    assert row["prompt_cache_hit_tokens"] == 80


def test_memory_prompts_retry_helper_clips_max_tokens_to_tier_config(
    tmp_path, monkeypatch
):
    path = _reset_usage_log(tmp_path, monkeypatch)
    calls = []

    class FakeCompletions:
        def create(self, **kwargs):
            calls.append(kwargs)
            return _fake_response(content="hello")

    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=FakeCompletions())
    )

    monkeypatch.setattr(
        "ai_config.get_tier_config",
        lambda tier: {
            "model": "generic-memory-model",
            "host": "example.com/v1",
            "api_key": "x",
            "max_tokens": 77,
        },
    )
    monkeypatch.setattr("ai_config.get_tier_client", lambda tier: fake_client)

    out = memory_prompts_v2._call_llm_with_retry(
        messages=[{"role": "user", "content": "hi"}],
        output_schema=None,
        pass_label="UnitPassClipped",
        tier="memory",
        reasoning=False,
        max_tokens=1000,
    )

    assert out == "hello"
    assert calls[0]["max_tokens"] == 77
    row = json.loads(path.read_text().strip())
    assert row["max_tokens"] == 77


def test_screen_observation_clips_max_tokens_to_vision_config(
    tmp_path, monkeypatch
):
    path = _reset_usage_log(tmp_path, monkeypatch)
    calls = []

    class FakeCompletions:
        def create(self, **kwargs):
            calls.append(kwargs)
            return _fake_response(content="4|用户正在做一次发布前截图测试")

    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=FakeCompletions())
    )

    monkeypatch.setattr(
        "ai_config.get_tier_config",
        lambda tier: {
            "model": "generic-vision-model",
            "host": "example.com/v1",
            "api_key": "x",
            "supports_images": True,
            "max_tokens": 66,
        },
    )
    monkeypatch.setattr("ai_config.get_tier_client", lambda tier: fake_client)

    desc, score = memory_prompts.call_screen_observation(
        "AAAA",
        media_type="image/png",
    )

    assert desc == "用户正在做一次发布前截图测试"
    assert score == 4
    assert calls[0]["max_tokens"] == 66
    row = json.loads(path.read_text().strip())
    assert row["max_tokens"] == 66
