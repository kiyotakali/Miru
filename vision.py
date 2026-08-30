"""Vision helper — single entry point for image → text description.

All image inputs (user-uploaded chat images, ad-hoc bytes from any source)
flow through here. Downstream modules (chat agent, memory router, sleep
agent, emotion eval, care engine, journal) only ever see *text*
descriptions, never raw bytes.

Why this exists: vision tier is the only place where images get sent to
an LLM. By converting images to text at the system boundary, the
chat/memory tiers can be pure-text models (e.g. DeepSeek V4-flash),
which is 5-10× cheaper than multimodal models.

Note: ScreenAnalyzer.analyze() handles screenshots separately — it has
its own salience-aware prompt and writes to screenshot_log + memory_router
directly. This module is for ad-hoc image→text conversion (chat uploads).
"""
from __future__ import annotations

import base64
import os


# Prompt asks for 120-180 字 detailed Chinese description, JSON-wrapped so
# we can reuse _call_llm_multimodal_json (no need for a parallel text helper).
_DESCRIBE_SYSTEM = """你是 Miru 的图像观察助手。请用 120-180 字详细描述这张图片，覆盖以下角度（缺哪一项就跳过哪一项）：
1. 主要内容：人物 / 物品 / 场景
2. 显眼文字：UI 标题、聊天截图原文、应用名 等
3. 氛围与情绪：人物表情和姿态、整体光感 / 色调 / 构图
4. 关键细节：颜色、装饰、特殊符号

风格要客观、具体、像素级关注重点元素。不要主观评价、不要加问候语和解释。

如果图片是动漫、手办、谷子、商品图：
- 只有在画面文字明确写出名称，或你极高置信度能识别时，才写角色名/作品名。
- 不确定时不要硬猜；写“未看到明确作品名/角色名，只能确认外观特征……”。
- 如果只是相似，必须写“可能像……但不确定”，不要当成事实。

输出严格 JSON：
{"description": "你的描述文字"}
"""


def _detect_mime(b: bytes) -> str:
    """Cheap magic-byte mime detection."""
    if not b:
        return "image/jpeg"
    if b[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if b[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if b[:4] == b"RIFF" and len(b) >= 12 and b[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def describe_image(jpeg_bytes: bytes, hint: str = "") -> str:
    """Convert image bytes to a Chinese description via vision tier.

    Args:
        jpeg_bytes: raw JPEG / PNG / GIF / WEBP bytes.
        hint: optional context hint inserted into the system prompt
              ("用户在聊天里发的图" / "屏幕截图" / "头像扫描" 等).

    Returns:
        120-180 字 description. On any failure (vision tier down,
        API error, malformed response) returns "(图片识别失败)" so
        callers can always persist *something* and downstream readers
        keep working.
    """
    if not jpeg_bytes:
        return "(空图片)"

    mime = _detect_mime(jpeg_bytes)
    try:
        b64 = base64.b64encode(jpeg_bytes).decode("ascii")
    except Exception:
        return "(图片编码失败)"

    system = _DESCRIBE_SYSTEM
    if hint:
        system = f"{system}\n\n【背景提示】{hint}"

    parts = [
        {"text": "请描述这张图片。"},
        {"inlineData": {"mimeType": mime, "data": b64}},
    ]

    try:
        from prompt import _call_llm_multimodal_json
        result = _call_llm_multimodal_json(
            system, parts,
            temperature=0.2, max_tokens=10000, tier="vision",
        )
    except Exception as e:
        print(f"[vision.describe_image] vision tier call failed: {e}")
        return "(图片识别失败)"

    if not isinstance(result, dict):
        print(f"[vision.describe_image] unexpected result type: {type(result)}")
        return "(图片识别失败)"

    desc = result.get("description") or result.get("desc") or ""
    desc = (desc or "").strip()
    if not desc:
        return "(图片识别失败)"

    # Cap at ~250 chars in case the model went verbose. Hard cap, no LLM
    # roundtrip — better to truncate cleanly than burn another token cycle.
    if len(desc) > 250:
        desc = desc[:247] + "…"
    return desc


def describe_image_file(path: str, hint: str = "") -> str:
    """Convenience wrapper: read file bytes from disk, then describe.

    Returns the same fallback strings as describe_image() on any disk-read
    or vision-tier failure.
    """
    if not path or not os.path.exists(path):
        return "(图片不存在)"
    try:
        with open(path, "rb") as f:
            data = f.read()
    except Exception as e:
        print(f"[vision.describe_image_file] read failed {path}: {e}")
        return "(图片读取失败)"
    return describe_image(data, hint=hint)
