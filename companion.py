import os
import time
import uuid
from datetime import datetime

import core
import storage
from character import get_config


def coerce_limit(value, default=20, minimum=1, maximum=100):
    try:
        limit = int(value)
    except (TypeError, ValueError):
        limit = default
    return max(minimum, min(limit, maximum))


def _resolve_upload_url(filename):
    if not filename:
        return ""

    safe_name = os.path.basename(filename)
    base, ext = os.path.splitext(safe_name)

    candidates = []
    if ext:
        candidates.append(safe_name)
    for candidate_ext in (".png", ".jpg", ".jpeg", ".webp"):
        candidate = f"{base}{candidate_ext}"
        if candidate not in candidates:
            candidates.append(candidate)

    for candidate in candidates:
        if os.path.exists(os.path.join(storage._uploads_dir(), candidate)):
            return f"/data/uploads/{candidate}"
    return ""


def build_character_payload():
    cfg = get_config()
    return {
        "name": cfg.name,
        "user_address": cfg.user_address,
        "avatar_url": "/assets/character-avatar.jpeg",
        "appearance": cfg.appearance,
        "personality": cfg.personality,
        "speech_patterns": cfg.speech_patterns,
        "backstory": cfg.backstory,
        "interests": cfg.interests,
    }


def build_context_summary():
    return core._build_chat_context()


def build_schedule_payload():
    return storage.get_today_schedule()


def build_commitments_payload():
    import memory
    content = memory.read_file("commitments/active.md") or ""
    result = []
    for line in content.split("\n"):
        line = line.strip()
        if line.startswith("- [ ]"):
            result.append({"title": line[6:].strip(), "status": "active"})
    return result


def normalize_chat_message(msg):
    payload = {
        "id": msg.get("id", ""),
        "role": msg.get("role", ""),
        "type": msg.get("type", "chat"),
        "text": msg.get("text", ""),
        "time": msg.get("time", ""),
        "tool_calls": msg.get("tool_calls", []),
    }
    if msg.get("source"):
        payload["source"] = msg.get("source")

    image_url = _resolve_upload_url(msg.get("image"))
    if image_url:
        payload["image_url"] = image_url

    reminder_key = msg.get("reminder_key")
    if reminder_key:
        payload["reminder_key"] = reminder_key

    scene_description = msg.get("scene_description")
    if scene_description:
        payload["scene_description"] = scene_description

    return payload


def build_chat_history(limit=50):
    limit = coerce_limit(limit, default=50, minimum=1, maximum=200)
    return [normalize_chat_message(msg) for msg in storage.get_chat_history(limit=limit)]


def normalize_reminder(reminder):
    image_filename = reminder.get("image_filename") or reminder.get("image")
    payload = {
        "id": reminder.get("key", "") or reminder.get("id", ""),
        "text": reminder.get("text", ""),
        "image_url": _resolve_upload_url(image_filename),
        "created_at": reminder.get("created_at", "") or reminder.get("time", ""),
    }
    scene_description = reminder.get("scene_description")
    if scene_description:
        payload["scene_description"] = scene_description
    return payload


def build_today_reminders(date_str=None):
    if not date_str:
        date_str = datetime.now().strftime("%Y-%m-%d")
    return [normalize_reminder(r) for r in storage.get_reminders_for_date(date_str)]


def build_manifest(version="dev"):
    return {
        "name": "ContextLife Companion Adapter",
        "version": version,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "capabilities": {
            "chat": True,
            "tool_calls": True,
            "chat_history": True,
            "schedule": True,
            "commitments": True,
            "reminders": True,
            "daily_reminder_plan": True,
            "live2d_runtime": False,
            "vrm_runtime": False,
            "native_voice_runtime": False,
        },
        "endpoints": {
            "manifest": "/api/companion/manifest",
            "runtime": "/api/companion/runtime",
            "bootstrap": "/api/companion/bootstrap",
            "character": "/api/companion/character",
            "schedule": "/api/companion/schedule",
            "commitments": "/api/companion/commitments",
            "chat_send": "/api/companion/chat",
            "chat_history": "/api/companion/chat/history",
            "reminders": "/api/companion/reminders",
        },
    }


def build_bootstrap(chat_limit=20):
    history = build_chat_history(limit=chat_limit)
    commitments = build_commitments_payload()
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "character": build_character_payload(),
        "context_summary": build_context_summary(),
        "schedule": build_schedule_payload(),
        "active_commitments": commitments,
        "active_commitment_count": len(commitments),
        "recent_chat": history,
        "recent_chat_count": len(history),
    }


def build_runtime_snapshot(chat_limit=20, plan_limit=7, version="dev"):
    bootstrap = build_bootstrap(chat_limit=chat_limit)
    reminders = build_today_reminders()
    try:
        import model_library
        active_model = model_library.get_active_model()
    except Exception:
        active_model = None
    return {
        "manifest": build_manifest(version=version),
        "bootstrap": bootstrap,
        "character": bootstrap["character"],
        "schedule": bootstrap["schedule"],
        "active_commitments": bootstrap["active_commitments"],
        "reminders": reminders,
        "reminder_count": len(reminders),
        "tomorrow_plan": storage.load_tomorrow_plan(),
        "daily_reminder_plan": storage.load_daily_reminder_plan(),
        "active_model": active_model,
    }


def send_chat(text, history_limit=20):
    result = core.chat_with_companion(text)
    history = build_chat_history(limit=history_limit)
    assistant_message = history[-1] if history and history[-1].get("role") == "assistant" else None
    return {
        "reply": result.get("reply", ""),
        "tool_calls": result.get("tool_calls", []),
        "history": history,
        "history_count": len(history),
        "assistant_message": assistant_message,
    }


def _flatten_openai_message_content(content):
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = str(item.get("text", "")).strip()
                if text:
                    parts.append(text)
        return "\n".join(parts).strip()
    return ""


def extract_latest_user_text(messages):
    if not isinstance(messages, list):
        return ""

    for item in reversed(messages):
        if not isinstance(item, dict):
            continue
        if item.get("role") != "user":
            continue
        text = _flatten_openai_message_content(item.get("content"))
        if text:
            return text
    return ""


def list_openai_models():
    return {
        "object": "list",
        "data": [
            {
                "id": "contextlife-companion",
                "object": "model",
                "created": 0,
                "owned_by": "contextlife",
            }
        ],
    }


def build_openai_chat_completion(messages, model="contextlife-companion", history_limit=20):
    user_text = extract_latest_user_text(messages)
    if not user_text:
        raise ValueError("No user message found in payload")

    result = send_chat(user_text, history_limit=history_limit)
    reply = result.get("reply", "")
    now_ts = int(time.time())
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"

    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": now_ts,
        "model": model or "contextlife-companion",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": reply,
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
        "contextlife": {
            "tool_calls": result.get("tool_calls", []),
            "history_count": result.get("history_count", 0),
        },
    }


def stream_openai_chat_completion_payload(completion):
    chunk_id = completion["id"]
    created = completion["created"]
    model = completion["model"]
    reply = completion["choices"][0]["message"]["content"]
    first_chunk = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {
                    "role": "assistant",
                    "content": reply,
                },
                "finish_reason": None,
            }
        ],
    }
    final_chunk = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {},
                "finish_reason": "stop",
            }
        ],
    }
    return [first_chunk, final_chunk]
