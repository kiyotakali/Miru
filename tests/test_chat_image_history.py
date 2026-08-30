"""Chat history image handling — text-only path (post 2026-05-10).

After the vision redesign, _build_chat_messages no longer reads image
bytes from disk. Instead, each user message in chat_history may carry
an ``image_desc`` field (120-180 字 Chinese description, generated at
upload time by vision.describe_image). The history builder splices
that description inline as ``[图片：xxx]`` so the chat tier (pure-text
DeepSeek V4-flash) understands what the user sent.

Tests verify:
- history with image_desc → text contains [图片：xxx]
- history with image but no image_desc (legacy) → text contains [图片]
- pure text history → clean string content (no list, no image blocks)
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def test_history_image_with_desc_spliced_inline():
    """Image + image_desc → user msg contains [图片：xxx] inline."""
    from prompt import _build_chat_messages

    history = [
        {"role": "user", "text": "看这只猫",
         "image": "chat_test_001.jpg",
         "image_desc": "一只橘色的猫趴在键盘上，眼神慵懒"},
        {"role": "assistant", "text": "好可爱"},
    ]
    msgs = _build_chat_messages(history, user_text="再看一张")

    # First message is the user's history line, content should be a string
    # containing both the typed text AND the [图片：xxx] tag.
    assert len(msgs) >= 2, f"expected ≥2 messages, got {len(msgs)}: {msgs}"
    user_msg = msgs[0]
    assert user_msg["role"] == "user"
    content = user_msg["content"]
    assert isinstance(content, str), \
        f"text-only path should produce str content, got {type(content).__name__}"
    assert "看这只猫" in content
    assert "[图片：一只橘色的猫趴在键盘上" in content


def test_history_image_no_desc_uses_generic_placeholder():
    """Old messages without image_desc fall back to plain [图片] tag."""
    from prompt import _build_chat_messages

    history = [
        {"role": "user", "text": "看",
         "image": "ghost_does_not_exist.jpg"},  # no image_desc
    ]
    msgs = _build_chat_messages(history, user_text="hi")
    # The last 'user' msg merges history user msg with current user_text.
    # Either it's a single user message (merged) or two — both fine.
    text_blob = " ".join(
        m.get("content", "") if isinstance(m.get("content"), str)
        else " ".join(b.get("text", "") for b in m.get("content", []) if isinstance(b, dict))
        for m in msgs if m["role"] == "user"
    )
    assert "看" in text_blob
    assert "[图片]" in text_blob  # generic fallback when image_desc absent


def test_no_image_history_clean_text_messages():
    """Pure-text history should produce string content (not list)."""
    from prompt import _build_chat_messages

    history = [
        {"role": "user", "text": "hello"},
        {"role": "assistant", "text": "hi"},
        {"role": "user", "text": "how are you"},
    ]
    msgs = _build_chat_messages(history, user_text="bye")
    last_user = [m for m in msgs if m["role"] == "user"][-1]
    assert isinstance(last_user["content"], str)


def test_no_multimodal_blocks_emitted():
    """Sanity guard: chat history builder must NEVER emit image_url blocks
    after the 2026-05-10 vision redesign. Vision tier handles raw bytes
    upstream; chat tier is pure-text. If this fails it means someone
    re-introduced the multimodal path — which would break DeepSeek calls.
    """
    from prompt import _build_chat_messages

    history = [
        {"role": "user", "text": "with desc",
         "image": "f.jpg", "image_desc": "a thing"},
        {"role": "user", "text": "without desc", "image": "g.jpg"},
    ]
    msgs = _build_chat_messages(history, user_text="end")

    for m in msgs:
        content = m.get("content", "")
        if isinstance(content, list):
            image_blocks = [b for b in content if isinstance(b, dict)
                            and b.get("type") == "image_url"]
            assert not image_blocks, \
                f"FAIL: image_url block leaked into chat tier — model would reject it"


def test_user_image_descs_param_inlined_in_current_message():
    """call_chat_agent passes user_image_descs=[...] for the *current*
    user message; verify they're spliced into the trailing user content."""
    from prompt import _build_chat_messages

    msgs = _build_chat_messages(
        chat_history=[],
        user_text="看这两张图",
        user_image_descs=["一只橘猫", "一片蓝天"],
    )
    assert len(msgs) == 1
    content = msgs[0]["content"]
    assert isinstance(content, str)
    assert "看这两张图" in content
    assert "[图片：一只橘猫]" in content
    assert "[图片：一片蓝天]" in content


def test_current_image_placeholder_stays_plain_when_desc_missing():
    """A legacy/current image placeholder should not become [图片：(图片)]."""
    from prompt import _build_chat_messages

    msgs = _build_chat_messages(
        chat_history=[],
        user_text="这是我想找的谷子",
        user_image_descs=["(图片)"],
    )
    content = msgs[0]["content"]
    assert "[图片]" in content
    assert "[图片：(图片)]" not in content


def test_vision_prompt_for_merch_discourages_hard_guessing():
    import vision

    assert "动漫、手办、谷子、商品图" in vision._DESCRIBE_SYSTEM
    assert "不确定时不要硬猜" in vision._DESCRIBE_SYSTEM
