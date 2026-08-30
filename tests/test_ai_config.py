"""Tests for the 3-tier ai_config module.

Verifies:
- TIERS list, env-var fallback, mask, host normalization
- update_tier_config persists + invalidates cache (hot reload)
- get_tier_client raises clean error when api_key missing
- get_runtime_config / update_public_config / ping_provider backward-compat
"""
from __future__ import annotations

import base64
import importlib
import io
import json
import os
from types import SimpleNamespace

from PIL import Image


def _setup(monkeypatch, tmp_path):
    """Reload ai_config pointing at a fresh DATA_DIR."""
    data_dir = str(tmp_path / "data")
    monkeypatch.setenv("DATA_DIR", data_dir)
    os.makedirs(os.path.join(data_dir, "_admin"), exist_ok=True)
    # Clear all tier env vars so tests don't leak from the host environment
    for v in (
        "AI_VISION_HOST", "AI_VISION_KEY", "AI_VISION_MODEL",
        "AI_CHAT_HOST", "AI_CHAT_KEY", "AI_CHAT_MODEL",
        "AI_MEMORY_HOST", "AI_MEMORY_KEY", "AI_MEMORY_MODEL",
        "AI_API_KEY", "AI_HOST", "AI_MODEL", "CLAUDE_CHAT_MODEL", "AI_PROVIDER",
    ):
        monkeypatch.delenv(v, raising=False)
    import ai_config
    importlib.reload(ai_config)
    return ai_config, data_dir


# ---------------------------------------------------------------------------
# Schema basics
# ---------------------------------------------------------------------------

def test_tiers_list(monkeypatch, tmp_path):
    ai_config, _ = _setup(monkeypatch, tmp_path)
    assert ai_config.TIERS == ("vision", "chat", "memory")


def test_empty_config_returns_empty_strings(monkeypatch, tmp_path):
    ai_config, _ = _setup(monkeypatch, tmp_path)
    cfg = ai_config.get_tier_config("chat")
    assert cfg["host"] == ""
    assert cfg["api_key"] == ""
    assert cfg["model"] == ""
    assert cfg["supports_images"] is True  # chat default
    assert cfg["max_tokens"] > 0


def test_get_tier_client_raises_when_no_key(monkeypatch, tmp_path):
    ai_config, _ = _setup(monkeypatch, tmp_path)
    try:
        ai_config.get_tier_client("memory")
    except RuntimeError as e:
        assert "记忆模型" in str(e)
        assert "模型" in str(e)
        assert "AI tier" not in str(e)
        assert "Admin UI" not in str(e)
        return
    raise AssertionError("expected RuntimeError when api_key missing")


def test_ping_tier_missing_config_uses_user_facing_copy(monkeypatch, tmp_path):
    ai_config, _ = _setup(monkeypatch, tmp_path)

    result = ai_config.ping_tier("vision")

    assert result["ok"] is False
    assert result["error"] == "视觉模型 缺少 API Key"
    assert "API key not set" not in result["error"]
    assert "tier" not in result["error"].lower()


def test_missing_model_settings_are_not_retryable():
    import prompt

    messages = [
        "模型配置还没有完成：聊天模型 缺少 API Key。请在 Miru 设置里的「模型」页填写后再试。",
        "模型配置还没有完成：聊天模型 缺少服务地址。请在 Miru 设置里的「模型」页填写后再试。",
        "模型配置还没有完成：聊天模型 缺少模型名称。请在 Miru 设置里的「模型」页填写后再试。",
    ]

    for message in messages:
        assert prompt._is_retryable_error(RuntimeError(message)) is False
    assert prompt._is_retryable_error(RuntimeError("empty response")) is True


# ---------------------------------------------------------------------------
# Persistence + hot reload
# ---------------------------------------------------------------------------

def test_update_persists_and_returns_public_config(monkeypatch, tmp_path):
    ai_config, _ = _setup(monkeypatch, tmp_path)
    r = ai_config.update_tier_config("memory", {
        "host": "openrouter.ai/api",
        "api_key": "sk-or-v1-test123",
        "model": "deepseek/deepseek-v4-flash",
        "max_tokens": 50000,
    })
    assert r["ok"] is True
    cfg = ai_config.get_tier_config("memory")
    assert cfg["host"] == "openrouter.ai/api"
    assert cfg["api_key"] == "sk-or-v1-test123"
    assert cfg["model"] == "deepseek/deepseek-v4-flash"
    assert cfg["max_tokens"] == 50000


def test_update_invalidates_cache_for_hot_reload(monkeypatch, tmp_path):
    ai_config, _ = _setup(monkeypatch, tmp_path)
    ai_config.update_tier_config("memory", {
        "host": "host1", "api_key": "key1", "model": "model1"
    })
    assert ai_config.get_tier_config("memory")["host"] == "host1"
    ai_config.update_tier_config("memory", {"host": "host2"})
    cfg2 = ai_config.get_tier_config("memory")
    assert cfg2["host"] == "host2"
    # api_key + model preserved through update
    assert cfg2["api_key"] == "key1"
    assert cfg2["model"] == "model1"


def test_clear_api_key(monkeypatch, tmp_path):
    ai_config, _ = _setup(monkeypatch, tmp_path)
    ai_config.update_tier_config("chat", {
        "host": "h", "api_key": "secret", "model": "m"
    })
    assert ai_config.get_tier_config("chat")["api_key"] == "secret"
    ai_config.update_tier_config("chat", {"clear_api_key": True})
    assert ai_config.get_tier_config("chat")["api_key"] == ""


# ---------------------------------------------------------------------------
# Env-var fallback
# ---------------------------------------------------------------------------

def test_env_var_fallback(monkeypatch, tmp_path):
    """Missing tier in saved config falls back to env vars."""
    data_dir = str(tmp_path / "data")
    monkeypatch.setenv("DATA_DIR", data_dir)
    monkeypatch.setenv("AI_VISION_HOST", "env-vision-host")
    monkeypatch.setenv("AI_VISION_KEY", "env-vision-key")
    monkeypatch.setenv("AI_VISION_MODEL", "env-vision-model")
    os.makedirs(os.path.join(data_dir, "_admin"), exist_ok=True)
    import ai_config
    importlib.reload(ai_config)
    cfg = ai_config.get_tier_config("vision")
    assert cfg["host"] == "env-vision-host"
    assert cfg["api_key"] == "env-vision-key"
    assert cfg["model"] == "env-vision-model"


def test_saved_overrides_env(monkeypatch, tmp_path):
    data_dir = str(tmp_path / "data")
    monkeypatch.setenv("DATA_DIR", data_dir)
    monkeypatch.setenv("AI_CHAT_HOST", "env-host")
    os.makedirs(os.path.join(data_dir, "_admin"), exist_ok=True)
    with open(os.path.join(data_dir, "_admin", "ai_config.json"), "w") as f:
        json.dump({"tiers": {"chat": {"host": "saved-host"}}}, f)
    import ai_config
    importlib.reload(ai_config)
    cfg = ai_config.get_tier_config("chat")
    assert cfg["host"] == "saved-host"


# ---------------------------------------------------------------------------
# Host normalization + mask
# ---------------------------------------------------------------------------

def test_normalize_host_preserves_explicit_base_paths(monkeypatch, tmp_path):
    ai_config, _ = _setup(monkeypatch, tmp_path)
    assert ai_config._normalize_host("https://example.com/v1") == "example.com/v1"
    assert ai_config._normalize_host("https://example.com/") == "example.com"
    assert ai_config._normalize_host("example.com") == "example.com"
    assert ai_config._openai_base_url_from_host("example.com") == "https://example.com/v1"
    assert ai_config._openai_base_url_from_host("example.com/v1") == "https://example.com/v1"


def test_normalize_host_strips_common_chat_completions_endpoint(monkeypatch, tmp_path):
    ai_config, _ = _setup(monkeypatch, tmp_path)

    assert ai_config._normalize_host(
        "https://compat-proxy.example.test/v1/chat/completions"
    ) == "compat-proxy.example.test/v1"
    assert ai_config._openai_base_url_from_host(
        "https://compat-proxy.example.test/v1/chat/completions"
    ) == "https://compat-proxy.example.test/v1"
    assert ai_config._normalize_host(
        "https://openrouter.ai/api/v1/chat/completions"
    ) == "openrouter.ai/api/v1"
    assert ai_config._normalize_host(
        "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    ) == "generativelanguage.googleapis.com/v1beta/openai"
    assert ai_config._normalize_host(
        "https://example.openai.azure.com/openai/v1/chat/completions"
    ) == "example.openai.azure.com/openai/v1"


def test_normalize_host_strips_final_chat_completions_from_provider_path(monkeypatch, tmp_path):
    ai_config, _ = _setup(monkeypatch, tmp_path)

    assert ai_config._normalize_host(
        "https://example.openai.azure.com/openai/deployments/foo/chat/completions"
    ) == "example.openai.azure.com/openai/deployments/foo"


def test_normalize_host_openrouter_auto_prefix(monkeypatch, tmp_path):
    ai_config, _ = _setup(monkeypatch, tmp_path)
    assert ai_config._normalize_host("openrouter.ai") == "openrouter.ai/api"
    assert ai_config._normalize_host("https://openrouter.ai/v1") == "openrouter.ai/api"
    assert ai_config._openai_base_url_from_host("openrouter.ai") == "https://openrouter.ai/api/v1"


def test_normalize_host_gemini_and_azure_compatible_paths(monkeypatch, tmp_path):
    ai_config, _ = _setup(monkeypatch, tmp_path)
    assert ai_config._normalize_host("generativelanguage.googleapis.com") == (
        "generativelanguage.googleapis.com/v1beta/openai"
    )
    assert ai_config._normalize_host(
        "https://generativelanguage.googleapis.com/v1beta/openai/"
    ) == "generativelanguage.googleapis.com/v1beta/openai"
    assert ai_config._openai_base_url_from_host("generativelanguage.googleapis.com") == (
        "https://generativelanguage.googleapis.com/v1beta/openai"
    )
    assert ai_config._openai_base_url_from_host("example.openai.azure.com/openai") == (
        "https://example.openai.azure.com/openai/v1"
    )
    assert ai_config._openai_base_url_from_host("example.openai.azure.com/openai/v1") == (
        "https://example.openai.azure.com/openai/v1"
    )


def test_openai_base_url_candidates_cover_provider_path_rules(monkeypatch, tmp_path):
    ai_config, _ = _setup(monkeypatch, tmp_path)

    assert ai_config._openai_base_url_candidates("open.bigmodel.cn/api/paas/v4") == [
        "https://open.bigmodel.cn/api/paas/v4",
        "https://open.bigmodel.cn/api/paas/v4/v1",
    ]
    assert ai_config._openai_base_url_candidates("open.bigmodel.cn") == [
        "https://open.bigmodel.cn/api/paas/v4",
        "https://open.bigmodel.cn/api/paas/v4/v1",
    ]
    assert ai_config._openai_base_url_candidates("api.deepseek.com") == [
        "https://api.deepseek.com/v1",
        "https://api.deepseek.com",
    ]
    assert ai_config._openai_base_url_candidates("openrouter.ai/api") == [
        "https://openrouter.ai/api/v1",
        "https://openrouter.ai/api",
    ]
    assert ai_config._openai_base_url_candidates(
        "generativelanguage.googleapis.com/v1beta/openai"
    ) == ["https://generativelanguage.googleapis.com/v1beta/openai"]
    assert ai_config._openai_base_url_candidates("proxy.example.test/custom/oai") == [
        "https://proxy.example.test/custom/oai",
        "https://proxy.example.test/custom/oai/v1",
    ]


def test_mask_secret(monkeypatch, tmp_path):
    ai_config, _ = _setup(monkeypatch, tmp_path)
    assert ai_config._mask_secret("") == ""
    assert ai_config._mask_secret("short") == "*****"
    assert ai_config._mask_secret("sk-or-v1-12345678") == "sk-o***5678"


def test_safe_error_message_redacts_api_key(monkeypatch, tmp_path):
    ai_config, _ = _setup(monkeypatch, tmp_path)
    exc = RuntimeError("request failed for sk-or-v1-secretkey")
    msg = ai_config._safe_error_message(exc, "sk-or-v1-secretkey")
    assert "sk-or-v1-secretkey" not in msg
    assert "sk-o***tkey" in msg


def test_get_public_config_masks_keys(monkeypatch, tmp_path):
    ai_config, _ = _setup(monkeypatch, tmp_path)
    ai_config.update_tier_config("memory", {
        "host": "h", "api_key": "sk-or-v1-secretkey", "model": "m"
    })
    pub = ai_config.get_public_config()
    assert pub["tiers"]["memory"]["api_key_masked"] == "sk-o***tkey"
    assert pub["tiers"]["memory"]["has_api_key"] is True
    assert "api_key" not in pub["tiers"]["memory"]  # raw key not exposed


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def test_invalid_tier_name(monkeypatch, tmp_path):
    ai_config, _ = _setup(monkeypatch, tmp_path)
    r = ai_config.update_tier_config("not_a_tier", {"host": "x"})
    assert r["ok"] is False
    assert "unknown tier" in r["error"]


def test_empty_host_rejected(monkeypatch, tmp_path):
    ai_config, _ = _setup(monkeypatch, tmp_path)
    r = ai_config.update_tier_config("vision", {"host": ""})
    assert r["ok"] is False
    assert "host" in r["error"].lower()


def test_max_tokens_validated(monkeypatch, tmp_path):
    ai_config, _ = _setup(monkeypatch, tmp_path)
    r = ai_config.update_tier_config("memory", {"host": "h", "api_key": "k",
                                                  "model": "m", "max_tokens": -5})
    assert r["ok"] is False
    r2 = ai_config.update_tier_config("memory", {"max_tokens": "not_int"})
    assert r2["ok"] is False


def test_resolve_max_tokens_uses_config_when_requested_none(monkeypatch, tmp_path):
    ai_config, _ = _setup(monkeypatch, tmp_path)
    ai_config.update_tier_config("chat", {
        "host": "h",
        "api_key": "k",
        "model": "m",
        "max_tokens": 12345,
    })

    assert ai_config.resolve_max_tokens("chat") == 12345


def test_resolve_max_tokens_clips_requested_to_config(monkeypatch, tmp_path):
    ai_config, _ = _setup(monkeypatch, tmp_path)
    ai_config.update_tier_config("memory", {
        "host": "h",
        "api_key": "k",
        "model": "m",
        "max_tokens": 800,
    })

    assert ai_config.resolve_max_tokens("memory", 50000) == 800
    assert ai_config.resolve_max_tokens("memory", 300) == 300
    assert ai_config.resolve_max_tokens("memory", "bad") == 800


# ---------------------------------------------------------------------------
# Backward-compat shims (legacy single-config API routes to chat tier)
# ---------------------------------------------------------------------------

def test_get_runtime_config_returns_chat_tier(monkeypatch, tmp_path):
    ai_config, _ = _setup(monkeypatch, tmp_path)
    ai_config.update_tier_config("chat", {
        "host": "chat-h", "api_key": "chat-k", "model": "chat-m"
    })
    runtime = ai_config.get_runtime_config()
    assert runtime["host"] == "chat-h"
    assert runtime["api_key"] == "chat-k"
    assert runtime["model"] == "chat-m"
    assert runtime["provider"] == "openai"


def test_legacy_update_public_config_routes_to_chat(monkeypatch, tmp_path):
    ai_config, _ = _setup(monkeypatch, tmp_path)
    r = ai_config.update_public_config({
        "host": "h", "api_key": "k", "model": "m"
    })
    assert r["ok"] is True
    assert ai_config.get_tier_config("chat")["host"] == "h"


def test_get_tier_client_uses_provider_base_url(monkeypatch, tmp_path):
    ai_config, _ = _setup(monkeypatch, tmp_path)
    captured = {}

    class FakeOpenAI:
        def __init__(self, api_key, base_url, timeout):
            captured["api_key"] = api_key
            captured["base_url"] = str(base_url)
            captured["timeout"] = timeout

    monkeypatch.setattr("openai.OpenAI", FakeOpenAI)
    ai_config.update_tier_config("chat", {
        "host": "generativelanguage.googleapis.com",
        "api_key": "gemini-key",
        "model": "gemini-test",
    })

    ai_config.get_tier_client("chat")

    assert captured["api_key"] == "gemini-key"
    assert captured["base_url"] == "https://generativelanguage.googleapis.com/v1beta/openai"


def test_ping_vision_sends_real_image_probe(monkeypatch, tmp_path):
    ai_config, _ = _setup(monkeypatch, tmp_path)
    captured = {}

    class FakeCompletions:
        def create(self, **kwargs):
            captured["kwargs"] = kwargs
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="OK"))]
            )

    class FakeOpenAI:
        def __init__(self, api_key, base_url, timeout):
            captured["api_key"] = api_key
            captured["base_url"] = str(base_url)
            captured["timeout"] = timeout
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr("openai.OpenAI", FakeOpenAI)
    ai_config.update_tier_config("vision", {
        "host": "generativelanguage.googleapis.com",
        "api_key": "gemini-key",
        "model": "gemini-vision-test",
        "supports_images": True,
    })

    result = ai_config.ping_tier("vision")

    assert result["ok"] is True
    assert captured["base_url"] == "https://generativelanguage.googleapis.com/v1beta/openai"
    content = captured["kwargs"]["messages"][0]["content"]
    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    assert content[1]["type"] == "image_url"
    data_url = content[1]["image_url"]["url"]
    assert data_url.startswith("data:image/png;base64,")

    encoded = data_url.split(",", 1)[1]
    raw = base64.b64decode(encoded, validate=True)
    with Image.open(io.BytesIO(raw)) as probe:
        probe.load()
        assert probe.format == "PNG"
        assert probe.size == (128, 128)
        assert probe.mode == "RGB"
        colors = probe.getcolors(maxcolors=128 * 128)
        assert colors is not None
        assert len(colors) > 1


def test_ping_explicit_provider_path_succeeds_without_v1_and_persists(
    monkeypatch, tmp_path
):
    ai_config, _ = _setup(monkeypatch, tmp_path)
    attempted = []

    class FakeCompletions:
        def create(self, **kwargs):
            return SimpleNamespace()

    class FakeOpenAI:
        def __init__(self, api_key, base_url, timeout):
            attempted.append(str(base_url))
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr("openai.OpenAI", FakeOpenAI)
    ai_config.update_tier_config("vision", {
        "host": "open.bigmodel.cn/api/paas/v4",
        "api_key": "zhipu-key",
        "model": "glm-4.6v-flashx",
        "supports_images": True,
    })

    result = ai_config.ping_tier("vision")

    assert result["ok"] is True
    assert attempted == ["https://open.bigmodel.cn/api/paas/v4"]
    assert result["host"] == "open.bigmodel.cn/api/paas/v4"
    assert ai_config.get_tier_config("vision")["host"] == (
        "open.bigmodel.cn/api/paas/v4"
    )


def test_ping_falls_back_on_path_error_and_persists_winner(monkeypatch, tmp_path):
    ai_config, _ = _setup(monkeypatch, tmp_path)
    attempted = []

    class PathError(RuntimeError):
        status_code = 404

    class FakeCompletions:
        def __init__(self, base_url):
            self.base_url = base_url

        def create(self, **kwargs):
            if self.base_url.endswith("/custom/oai"):
                raise PathError("not found")
            return SimpleNamespace()

    class FakeOpenAI:
        def __init__(self, api_key, base_url, timeout):
            base_url = str(base_url)
            attempted.append(base_url)
            self.chat = SimpleNamespace(completions=FakeCompletions(base_url))

    monkeypatch.setattr("openai.OpenAI", FakeOpenAI)
    ai_config.update_tier_config("chat", {
        "host": "proxy.example.test/custom/oai",
        "api_key": "proxy-key",
        "model": "proxy-model",
    })

    result = ai_config.ping_tier("chat")

    assert result["ok"] is True
    assert attempted == [
        "https://proxy.example.test/custom/oai",
        "https://proxy.example.test/custom/oai/v1",
    ]
    assert result["host"] == "proxy.example.test/custom/oai/v1"
    assert ai_config.get_tier_config("chat")["host"] == (
        "proxy.example.test/custom/oai/v1"
    )
    ai_config.get_tier_client("chat")
    assert attempted[-1] == "https://proxy.example.test/custom/oai/v1"


def test_ping_falls_back_on_method_not_allowed_from_response(monkeypatch, tmp_path):
    ai_config, _ = _setup(monkeypatch, tmp_path)
    attempted = []

    class MethodError(RuntimeError):
        response = SimpleNamespace(status_code=405)

    class FakeCompletions:
        def __init__(self, base_url):
            self.base_url = base_url

        def create(self, **kwargs):
            if self.base_url.endswith("/custom/oai"):
                raise MethodError("method not allowed")
            return SimpleNamespace()

    class FakeOpenAI:
        def __init__(self, api_key, base_url, timeout):
            base_url = str(base_url)
            attempted.append(base_url)
            self.chat = SimpleNamespace(completions=FakeCompletions(base_url))

    monkeypatch.setattr("openai.OpenAI", FakeOpenAI)
    ai_config.update_tier_config("memory", {
        "host": "proxy.example.test/custom/oai",
        "api_key": "proxy-key",
        "model": "proxy-model",
    })

    result = ai_config.ping_tier("memory")

    assert result["ok"] is True
    assert attempted == [
        "https://proxy.example.test/custom/oai",
        "https://proxy.example.test/custom/oai/v1",
    ]
    assert ai_config.get_tier_config("memory")["host"] == (
        "proxy.example.test/custom/oai/v1"
    )


def test_ping_does_not_fallback_on_non_path_errors(monkeypatch, tmp_path):
    ai_config, _ = _setup(monkeypatch, tmp_path)

    for status in (None, 400, 401, 403, 429, 500):
        attempted = []

        class ProviderError(RuntimeError):
            status_code = status

        class FakeCompletions:
            def create(self, **kwargs):
                raise ProviderError(f"provider error {status}")

        class FakeOpenAI:
            def __init__(self, api_key, base_url, timeout):
                attempted.append(str(base_url))
                self.chat = SimpleNamespace(completions=FakeCompletions())

        monkeypatch.setattr("openai.OpenAI", FakeOpenAI)
        ai_config.update_tier_config("vision", {
            "host": "open.bigmodel.cn/api/paas/v4",
            "api_key": "zhipu-key",
            "model": "glm-4.6v-flashx",
            "supports_images": True,
        })

        result = ai_config.ping_tier("vision")

        assert result["ok"] is False
        assert result["http_status"] == status
        assert attempted == ["https://open.bigmodel.cn/api/paas/v4"]


def test_ping_vision_rejects_text_only_config(monkeypatch, tmp_path):
    ai_config, _ = _setup(monkeypatch, tmp_path)
    ai_config.update_tier_config("vision", {
        "host": "api.openai.com",
        "api_key": "key",
        "model": "text-only",
        "supports_images": False,
    })

    result = ai_config.ping_tier("vision")

    assert result["ok"] is False
    assert "图片输入" in result["error"]
