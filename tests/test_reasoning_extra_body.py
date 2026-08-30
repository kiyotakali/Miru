"""Unit tests for prompt._vendor_extra_body reasoning switch (Sleep Agent v3 A1).

The function decides which vendor flags to send so reasoning mode is
explicitly on or off. Default behavior (reasoning=False) must remain
backward-compatible with the pre-v3 implementation.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from prompt import _vendor_extra_body  # noqa: E402


# ─────────────────────────────────────────────────────────────────────
# reasoning=False (default) — backward-compatible disable
# ─────────────────────────────────────────────────────────────────────

def test_default_disables_openrouter_reasoning():
    out = _vendor_extra_body("qwen/qwen3.5-9b", "openrouter.ai/api")
    assert out == {"reasoning": {"enabled": False}}


def test_default_disables_deepseek_direct_thinking():
    out = _vendor_extra_body("deepseek-v4-pro", "api.deepseek.com")
    assert out == {"thinking": {"type": "disabled"}}


def test_default_does_not_send_deepseek_flag_to_unknown_proxy_by_model_name():
    """Unknown proxies may reject DeepSeek-only fields even for DeepSeek models."""
    out = _vendor_extra_body("deepseek-v4-flash", "some.other.host")
    assert out == {}


def test_default_unknown_host_returns_empty():
    """Unknown provider: no flag sent (don't reject calls to e.g. OpenAI)."""
    out = _vendor_extra_body("gpt-4o", "api.openai.com")
    assert out == {}


def test_default_empty_input_returns_empty():
    out = _vendor_extra_body("", "")
    assert out == {}


# ─────────────────────────────────────────────────────────────────────
# reasoning=True — Sleep Agent v3 path
# ─────────────────────────────────────────────────────────────────────

def test_reasoning_on_openrouter():
    out = _vendor_extra_body("qwen/qwen3.5-9b", "openrouter.ai/api",
                             reasoning=True)
    assert out == {"reasoning": {"enabled": True, "max_tokens": 16000}}


def test_reasoning_on_openrouter_custom_budget():
    out = _vendor_extra_body("qwen/qwen3.5-9b", "openrouter.ai/api",
                             reasoning=True, reasoning_budget=8000)
    assert out == {"reasoning": {"enabled": True, "max_tokens": 8000}}


def test_reasoning_on_deepseek_direct():
    out = _vendor_extra_body("deepseek-v4-pro", "api.deepseek.com",
                             reasoning=True)
    assert out == {"thinking": {"type": "enabled"}}


def test_reasoning_on_deepseek_model_via_unknown_proxy_returns_empty():
    out = _vendor_extra_body("deepseek-v4-flash", "some.other.host",
                             reasoning=True)
    assert out == {}


def test_reasoning_on_unknown_provider_returns_empty():
    """Unknown provider: don't send vendor-specific flag. The provider's
    default behavior applies (e.g. OpenAI o-series auto-reasons)."""
    out = _vendor_extra_body("gpt-4o", "api.openai.com", reasoning=True)
    assert out == {}


# ─────────────────────────────────────────────────────────────────────
# Backward compat: positional call still works
# ─────────────────────────────────────────────────────────────────────

def test_positional_call_backward_compatible():
    """Old call sites that don't know about reasoning should still work."""
    out = _vendor_extra_body("deepseek-v4-pro", "api.deepseek.com")
    assert "thinking" in out
    assert out["thinking"]["type"] == "disabled"


if __name__ == "__main__":
    import unittest
    unittest.main()
