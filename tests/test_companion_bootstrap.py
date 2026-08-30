import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as app_module
import auth
import companion
import core
from flask import g


def _fake_check_request(req=None):
    """Bypass auth in tests — set a dummy user context."""
    g.user_id = "u_test000000"
    g.user_data_dir = os.path.join(auth._BASE_DATA_DIR, "users", "u_test000000")
    g.is_admin = False
    return None


def test_build_companion_bootstrap_aggregates_runtime_state(monkeypatch):
    class DummyConfig:
        name = "AiriMiku"
        user_address = "Master"
        appearance = "Silver hair"
        personality = "Calm and helpful"
        speech_patterns = "Soft"
        backstory = "Virtual guide"
        interests = "Planning"

    monkeypatch.setattr(companion, "get_config", lambda: DummyConfig())
    monkeypatch.setattr(companion.core, "_build_chat_context", lambda: "当前时间: 2026-03-08 10:00")
    monkeypatch.setattr(
        companion.storage,
        "get_today_schedule",
        lambda: [{"time": "09:00", "task": "写融合文档", "type": "focus"}],
    )
    # Mock memory module to return active commitments
    import memory as mem_module
    monkeypatch.setattr(
        mem_module,
        "read_file",
        lambda path: "# Active Commitments\n- [ ] 接入 AIRI -- 先做 runtime bridge\n" if "active" in path else None,
    )
    monkeypatch.setattr(
        companion.storage,
        "get_chat_history",
        lambda limit=50: [{"role": "user", "text": "在吗？"}] if limit == 5 else [],
    )
    monkeypatch.setattr(companion.storage, "UPLOADS_DIR", "/tmp/does-not-exist")

    payload = companion.build_bootstrap(chat_limit=5)

    assert payload["character"]["name"] == "AiriMiku"
    assert payload["character"]["appearance"] == "Silver hair"
    assert payload["context_summary"] == "当前时间: 2026-03-08 10:00"
    assert payload["schedule"][0]["task"] == "写融合文档"
    assert payload["active_commitment_count"] == 1
    assert payload["recent_chat_count"] == 1
    assert payload["generated_at"]


def test_companion_bootstrap_route_clamps_chat_limit(monkeypatch):
    called = {}

    def fake_build_bootstrap(chat_limit=20):
        called["chat_limit"] = chat_limit
        return {"ok": True, "chat_limit": chat_limit}

    monkeypatch.setattr(app_module.companion, "build_bootstrap", fake_build_bootstrap)
    monkeypatch.setattr(auth, "check_request", _fake_check_request)

    client = app_module.app.test_client()
    response = client.get("/api/companion/bootstrap?chat_limit=999")

    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "chat_limit": 100}
    assert called["chat_limit"] == 100


def test_core_memory_route_returns_review_blocks(monkeypatch, tmp_path):
    """The Memory UI can review human/persona blocks even though they are not slots."""
    import json

    user_dir = tmp_path / "user_data"
    user_dir.mkdir()
    (user_dir / "core_memory.json").write_text(json.dumps({
        "human": {"label": "human", "value": "用户喜欢自然陪伴。", "limit": 5000},
        "persona": {"label": "persona", "value": "我正在学习更自然地靠近用户。", "limit": 5000},
    }, ensure_ascii=False), encoding="utf-8")

    def fake_check_request(req=None):
        g.user_id = "u_test000000"
        g.user_data_dir = str(user_dir)
        g.is_admin = False
        return None

    monkeypatch.setattr(auth, "check_request", fake_check_request)

    client = app_module.app.test_client()
    response = client.get("/api/core-memory")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["blocks"]["human"]["content"] == "用户喜欢自然陪伴。"
    assert payload["blocks"]["persona"]["content"] == "我正在学习更自然地靠近用户。"


def test_companion_history_normalizes_reminder_image(monkeypatch, tmp_path):
    uploads_dir = tmp_path / "uploads"
    uploads_dir.mkdir()
    (uploads_dir / "reminder_1.webp").write_bytes(b"fake")

    monkeypatch.setattr(companion.storage, "UPLOADS_DIR", str(uploads_dir))
    monkeypatch.setattr(companion.storage, "_uploads_dir", lambda: str(uploads_dir))
    monkeypatch.setattr(
        companion.storage,
        "get_chat_history",
        lambda limit=50: [
            {"id": "u1", "role": "user", "text": "hello", "time": "2026-03-08 10:00:00"},
            {
                "id": "r1",
                "role": "assistant",
                "type": "reminder",
                "text": "记得吃饭",
                "image": "reminder_1.png",
                "time": "2026-03-08 10:01:00",
            },
        ],
    )

    history = companion.build_chat_history(limit=10)

    assert history[1]["image_url"] == "/data/uploads/reminder_1.webp"


def test_companion_runtime_route_returns_manifest(monkeypatch):
    monkeypatch.setattr(
        app_module.companion,
        "build_runtime_snapshot",
        lambda chat_limit=20, plan_limit=7, version="dev": {
            "manifest": {"version": version},
            "bootstrap": {"recent_chat_count": chat_limit},
        },
    )
    monkeypatch.setattr(auth, "check_request", _fake_check_request)

    client = app_module.app.test_client()
    response = client.get("/api/companion/runtime?chat_limit=3&plan_limit=2")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["manifest"]["version"]
    assert payload["bootstrap"]["recent_chat_count"] == 3


def test_companion_chat_route_returns_adapter_payload(monkeypatch):
    monkeypatch.setattr(
        app_module.companion,
        "send_chat",
        lambda text, history_limit=20: {
            "reply": f"echo:{text}",
            "tool_calls": [],
            "history": [{"role": "assistant", "text": "echo"}],
            "history_count": 1,
            "assistant_message": {"role": "assistant", "text": "echo"},
        },
    )
    monkeypatch.setattr(auth, "check_request", _fake_check_request)

    client = app_module.app.test_client()
    response = client.post("/api/companion/chat", json={"text": "test", "history_limit": 10})

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["reply"] == "echo:test"
    assert payload["history_count"] == 1


def test_companion_extracts_latest_openai_user_message():
    text = companion.extract_latest_user_text([
        {"role": "system", "content": "sys"},
        {"role": "user", "content": [{"type": "text", "text": "first"}]},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "latest"},
    ])

    assert text == "latest"


def test_airi_models_route_returns_openai_shape(monkeypatch):
    monkeypatch.setattr(auth, "check_request", _fake_check_request)
    client = app_module.app.test_client()
    response = client.get("/api/airi/v1/models")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["object"] == "list"
    assert payload["data"][0]["id"] == "contextlife-companion"


def test_airi_chat_completions_route_returns_openai_shape(monkeypatch):
    monkeypatch.setattr(auth, "check_request", _fake_check_request)
    monkeypatch.setattr(
        app_module.companion,
        "build_openai_chat_completion",
        lambda messages, model="contextlife-companion", history_limit=20: {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 123,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "hello from contextlife"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        },
    )

    client = app_module.app.test_client()
    response = client.post(
        "/api/airi/v1/chat/completions",
        json={
            "model": "contextlife-companion",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["object"] == "chat.completion"
    assert payload["choices"][0]["message"]["content"] == "hello from contextlife"


def test_airi_chat_completions_streams_sse(monkeypatch):
    monkeypatch.setattr(auth, "check_request", _fake_check_request)
    monkeypatch.setattr(
        app_module.companion,
        "build_openai_chat_completion",
        lambda messages, model="contextlife-companion", history_limit=20: {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 123,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "stream hello"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        },
    )

    client = app_module.app.test_client()
    response = client.post(
        "/api/airi/v1/chat/completions",
        json={
            "model": "contextlife-companion",
            "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "chat.completion.chunk" in body
    assert "data: [DONE]" in body


def test_core_companion_bootstrap_backwards_compatible(monkeypatch):
    class DummyConfig:
        name = "AiriMiku"
        user_address = "Master"
        appearance = "Silver hair"
        personality = "Calm and helpful"

    monkeypatch.setattr(core, "get_config", lambda: DummyConfig())
    monkeypatch.setattr(core, "_build_chat_context", lambda: "当前时间: 2026-03-08 10:00")
    monkeypatch.setattr(core.storage, "get_today_schedule", lambda: [])
    import memory as mem_module
    monkeypatch.setattr(mem_module, "read_file", lambda path: None)
    monkeypatch.setattr(core.storage, "get_chat_history", lambda limit=50: [])

    payload = core.build_companion_bootstrap(chat_limit=5)

    assert payload["character"]["name"] == "AiriMiku"
    assert payload["recent_chat_count"] == 0
