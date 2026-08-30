import os
import base64
import json
import ast
import random
import re
import time
from datetime import datetime

from character import get_config
from prompt_identity import (
    normalize_character_section,
    resolve_user_entity_label,
    safe_user_address,
)
import ai_config

# Backward-compat shim: legacy callers expected `get_runtime_config()` and
# `DEFAULT_MODEL`. After 3-tier migration, runtime config is per-tier;
# legacy `get_runtime_config()` now returns the chat tier as a default,
# and `DEFAULT_MODEL` is unused (kept as empty string for any rare reader).
get_runtime_config = ai_config.get_runtime_config
DEFAULT_MODEL = ""


# ---------------------------------------------------------------------------
# LLM usage logging — one JSONL row per call, for cost / consumption analysis.
# Cheap (one open/write/close per call); failure must never break the LLM call.
# ---------------------------------------------------------------------------
_USAGE_LOG_PATH = None

def _usage_get(obj, key, default=None):
    """Best-effort field access for OpenAI SDK objects, dicts and model_extra."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    val = getattr(obj, key, default)
    if val is not None:
        return val
    extra = getattr(obj, "model_extra", None)
    if isinstance(extra, dict) and key in extra:
        return extra.get(key, default)
    try:
        return obj[key]
    except Exception:
        return default

def _usage_int(value, default=0):
    try:
        if value is None:
            return default
        return int(value)
    except Exception:
        return default

def _get_usage_log_path():
    global _USAGE_LOG_PATH
    if _USAGE_LOG_PATH is None:
        base = os.environ.get("DATA_DIR", "data")
        admin_dir = os.path.join(base, "_admin")
        try:
            os.makedirs(admin_dir, exist_ok=True)
        except Exception:
            pass
        _USAGE_LOG_PATH = os.path.join(admin_dir, "llm_usage.jsonl")
    return _USAGE_LOG_PATH

def _log_llm_usage(tier, model, response, **meta):
    try:
        usage = _usage_get(response, "usage")
        if not usage:
            return
        prompt_tokens = _usage_int(_usage_get(usage, "prompt_tokens"))
        completion_tokens = _usage_int(_usage_get(usage, "completion_tokens"))
        total_tokens = _usage_int(_usage_get(usage, "total_tokens"))

        prompt_details = _usage_get(usage, "prompt_tokens_details")
        completion_details = _usage_get(usage, "completion_tokens_details")

        cache_hit = _usage_int(_usage_get(usage, "prompt_cache_hit_tokens"))
        cache_miss = _usage_int(_usage_get(usage, "prompt_cache_miss_tokens"))
        cached_tokens = _usage_int(_usage_get(prompt_details, "cached_tokens"))
        if cache_hit <= 0 and cached_tokens > 0:
            cache_hit = cached_tokens
        if cache_miss <= 0 and prompt_tokens > 0 and cache_hit > 0:
            cache_miss = max(prompt_tokens - cache_hit, 0)

        reasoning_tokens = _usage_int(_usage_get(usage, "reasoning_tokens"))
        if reasoning_tokens <= 0:
            reasoning_tokens = _usage_int(_usage_get(completion_details, "reasoning_tokens"))

        rec = {
            "ts": time.time(),
            "tier": tier,
            "model": model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "prompt_cache_hit_tokens": cache_hit,
            "prompt_cache_miss_tokens": cache_miss,
            "reasoning_tokens": reasoning_tokens,
        }
        for k, v in meta.items():
            if v is None:
                continue
            if isinstance(v, (str, int, float, bool)):
                rec[k] = v
            else:
                rec[k] = str(v)
        try:
            from flask import g
            uid = getattr(g, "user_id", None)
            if uid:
                rec["user_id"] = uid
        except Exception:
            pass
        with open(_get_usage_log_path(), "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass  # logging must never break the LLM call


def _extract_json_block(text):
    """Extract the outermost JSON object or array from text, ignoring any trailing noise.

    LLMs sometimes append explanatory text after the JSON block (e.g., '已经写好了').
    This function finds the matching closing brace/bracket and strips the rest.
    """
    text = text.strip()
    start = -1
    for i, ch in enumerate(text):
        if ch in ('{', '['):
            start = i
            break
    if start == -1:
        return text

    open_ch = text[start]
    close_ch = '}' if open_ch == '{' else ']'
    depth = 0
    end = -1
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == '\\' and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                end = i
                break
    return text[start:end + 1] if end != -1 else text


def _strip_code_fences(text):
    raw = (text or "").strip()
    if not raw.startswith("```"):
        return raw
    lines = raw.split("\n")
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _repair_common_json_issues(text):
    """Best-effort repair for frequent LLM JSON formatting mistakes."""
    repaired = (text or "").strip()
    # Normalize smart quotes to avoid malformed keys/strings.
    repaired = repaired.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    repaired = _strip_json_comments(repaired)
    # Remove trailing commas before object/array close.
    repaired = re.sub(r",(\s*[}\]])", r"\1", repaired)

    # Insert missing commas between adjacent key/value lines.
    lines = repaired.splitlines()
    if len(lines) > 1:
        out = []
        for idx, line in enumerate(lines):
            cur = line.rstrip()
            nxt = lines[idx + 1].lstrip() if idx + 1 < len(lines) else ""
            if idx + 1 < len(lines):
                cur_stripped = cur.rstrip()
                if (
                    cur_stripped
                    and not cur_stripped.endswith((",", "{", "[", ":"))
                    and not nxt.startswith(("}", "]"))
                    and nxt.startswith(('"', "{", "["))
                    and re.search(r'("|\]|\}|\d|true|false|null)\s*$', cur_stripped, flags=re.IGNORECASE)
                ):
                    cur = cur + ","
            out.append(cur)
        repaired = "\n".join(out)

    repaired = _insert_missing_commas_outside_strings(repaired)
    return repaired


def _strip_json_comments(text):
    """Remove // and /* */ comments outside JSON strings."""
    out = []
    in_string = False
    escape = False
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""

        if in_string:
            out.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            i += 1
            continue

        if ch == "/" and nxt == "/":
            i += 2
            while i < n and text[i] not in ("\n", "\r"):
                i += 1
            continue

        if ch == "/" and nxt == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2 if i + 1 <= n else 1
            continue

        out.append(ch)
        if ch == '"':
            in_string = True
        i += 1

    return "".join(out)


def _insert_missing_commas_outside_strings(text):
    """Insert commas between adjacent JSON values/keys when LLM dropped delimiters."""

    out = []
    in_string = False
    escape = False
    prev_sig = ""
    i = 0
    while i < len(text):
        ch = text[i]
        if in_string:
            out.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
                prev_sig = '"'
            i += 1
            continue

        if ch == '"':
            if prev_sig and prev_sig not in "{[:,":  # value ended, but next token starts
                out.append(",")
            out.append(ch)
            in_string = True
            i += 1
            continue

        if ch in "{[":
            if prev_sig and prev_sig not in "{[:,":  # missing comma in arrays/objects
                out.append(",")
            out.append(ch)
            prev_sig = ch
            i += 1
            continue

        out.append(ch)
        if not ch.isspace():
            if ch in "-0123456789tfn":
                # Handle adjacent primitive values followed by key/value starts.
                prev_sig = ch
            else:
                prev_sig = ch
        i += 1

    # Fix missing commas before next object key in tight same-line format:
    # {"a":1 "b":2} -> {"a":1, "b":2}
    return re.sub(r'("|\]|\}|\d)\s+("([^"\\]|\\.)*"\s*:)', r'\1, \2', "".join(out))


def _parse_with_python_literal(text):
    """Fallback parser for near-JSON payloads with Python-compatible literals."""
    py_text = re.sub(r"\btrue\b", "True", text, flags=re.IGNORECASE)
    py_text = re.sub(r"\bfalse\b", "False", py_text, flags=re.IGNORECASE)
    py_text = re.sub(r"\bnull\b", "None", py_text, flags=re.IGNORECASE)
    value = ast.literal_eval(py_text)
    if not isinstance(value, (dict, list)):
        raise ValueError("top-level is not object/array")
    return value


def _parse_llm_json(raw_text, label="LLM"):
    text = _strip_code_fences(raw_text)
    block = _extract_json_block(text)
    attempts = [("raw", block), ("repaired", _repair_common_json_issues(block))]
    for _, candidate in attempts:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
        except Exception:
            continue

    # Fallback: near-JSON that is parseable as Python literal.
    for _, candidate in attempts:
        try:
            return _parse_with_python_literal(candidate)
        except Exception:
            continue

    # Build final error diagnostics from repaired payload.
    final_text = attempts[-1][1]
    try:
        json.loads(final_text)
    except json.JSONDecodeError as err:
        snippet_start = max(0, err.pos - 120)
        snippet_end = min(len(final_text), err.pos + 120)
        snippet = final_text[snippet_start:snippet_end].replace("\n", "\\n")
        raise RuntimeError(
            f"{label} JSON 解析失败: {err.msg} (line {err.lineno}, col {err.colno}); 附近内容: {snippet}"
        ) from err
    raise RuntimeError(f"{label} JSON 解析失败: 未知结构错误")


def _safe_user_address(raw_address):
    """Return the configured user address from soul.md."""
    return safe_user_address(raw_address)


def _sanitize_user_address_in_text(text, _raw_address=None, *,
                                   user_label=None, miru_name="Miru"):
    """Normalize soul.md prose for prompts addressed directly to Miru."""
    return normalize_character_section(
        text, user_label=user_label, miru_name=miru_name)


def _get_ai_runtime_or_raise(tier: str = "chat"):
    """Resolve a tier's runtime config or raise a clear error."""
    cfg = ai_config.get_tier_config(tier)
    tier_label = ai_config.TIER_LABELS.get(tier, tier)
    if not cfg["api_key"]:
        raise RuntimeError(
            f"模型配置还没有完成：{tier_label} 缺少 API Key。"
            "请在 Miru 设置里的「模型」页填写后再试。"
        )
    if not cfg["host"]:
        raise RuntimeError(
            f"模型配置还没有完成：{tier_label} 缺少服务地址。"
            "请在 Miru 设置里的「模型」页填写后再试。"
        )
    if not cfg["model"]:
        raise RuntimeError(
            f"模型配置还没有完成：{tier_label} 缺少模型名称。"
            "请在 Miru 设置里的「模型」页填写后再试。"
        )
    return cfg


def _get_chat_model():
    return _get_ai_runtime_or_raise("chat")["model"]


def _format_ai_call_error(exc, host, model):
    msg = str(exc) or exc.__class__.__name__
    lower = msg.lower()
    if "hostname mismatch" in lower or "certificate verify failed" in lower:
        msg += "；证书与域名不匹配，请在设置 > AI 检查 Host 配置"
    elif "connection error" in lower:
        msg += "；请检查网络、代理或 Host 配置"
    return f"{msg} (host={host}, model={model})"


# ---------------------------------------------------------------------------
# Retry utility for all API calls
# ---------------------------------------------------------------------------

def _unwrap_cause(exc):
    """Walk __cause__ chain to find the original exception."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        cause = getattr(exc, "__cause__", None)
        if cause is None:
            return exc
        exc = cause
    return exc


def _is_retryable_error(exc):
    """Determine if an exception is transient and safe to retry.

    Works with both Anthropic and OpenAI SDK exceptions.
    """
    candidates = [exc]
    root = _unwrap_cause(exc)
    if root is not exc:
        candidates.append(root)

    for e in candidates:
        # Upstream proxy transient auth errors (e.g. Google OAuth token refresh)
        # are returned as 400 with "invalid_grant" — safe to retry.
        err_msg = str(e).lower()
        if "invalid_grant" in err_msg:
            return True

        # Check by status code (works for both SDKs)
        status = getattr(e, "status_code", None) or getattr(e, "status", None)
        if status is not None:
            if status in (429, 500, 502, 503, 504):
                return True
            if status in (400, 401, 403, 404):
                return False

        # Check by class name string (avoid hard import)
        cls_name = type(e).__name__
        if cls_name in ("RateLimitError", "InternalServerError", "APIConnectionError",
                        "APITimeoutError", "APIError"):
            return True
        if cls_name in ("AuthenticationError", "PermissionDeniedError",
                        "BadRequestError", "NotFoundError"):
            return False

        # OpenAI timeout / connection errors
        if isinstance(e, (ConnectionError, TimeoutError, OSError)):
            return True

    # RuntimeError from empty response or JSON parse failure — retry
    if isinstance(exc, RuntimeError):
        msg = str(exc).lower()
        if (
            "模型配置还没有完成" in str(exc)
            or "api key" in msg
            or "not set" in msg
        ):
            return False
        return True

    return False


def _get_retry_after(exc):
    """Extract Retry-After seconds from a RateLimitError, or None.

    Checks __cause__ chain since the RateLimitError may be wrapped.
    """
    # Check both the exception and its root cause for retry-after header
    for e in [exc, _unwrap_cause(exc)]:
        response = getattr(e, "response", None)
        if response is None:
            continue
        header = getattr(response, "headers", {}).get("retry-after")
        if header:
            try:
                return float(header)
            except (ValueError, TypeError):
                pass
    return None


def _api_call_with_retry(fn, max_retries=3, base_delay=2.0, label="API"):
    """Execute fn() with exponential backoff retry on transient errors.

    Retries on: 429, 5xx, connection errors, timeouts, empty responses,
                JSON parse failures (RuntimeError).
    Does NOT retry on: 401, 403, 400, 404, config errors.

    Args:
        fn: Callable that performs the API call and returns the result.
        max_retries: Maximum number of retry attempts (default 3).
        base_delay: Base delay in seconds before first retry (default 2.0).
        label: Label for log messages.
    Returns: Result of fn().
    Raises: The last exception if all retries are exhausted.
    """
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            if not _is_retryable_error(exc) or attempt == max_retries:
                raise

            # Calculate delay: exponential backoff + jitter
            delay = base_delay * (2 ** attempt) + random.uniform(0, 1)

            # Respect Retry-After header for rate limit errors
            retry_after = _get_retry_after(exc)
            if retry_after is not None:
                delay = max(delay, retry_after)

            # Cap delay at 60 seconds
            delay = min(delay, 60.0)

            print(f"[Retry] {label} attempt {attempt + 1}/{max_retries} failed: "
                  f"{exc.__class__.__name__}: {exc}. Retrying in {delay:.1f}s...")
            time.sleep(delay)

    raise last_exc  # Should not reach here, but safety net


# ---------------------------------------------------------------------------
# Core API call helpers
# ---------------------------------------------------------------------------

def _get_client(runtime=None, tier: str = "chat"):
    """Return (OpenAI client, "openai") for the given tier.

    Backward-compat: `runtime` arg ignored if passed (legacy callers).
    Always returns ("openai") as the second element so old call sites
    that destructure `client, provider = _get_client(...)` still work.

    The OpenAI client is cached per-tier inside ai_config so we don't
    rebuild httpx pools on every LLM call.
    """
    return ai_config.get_tier_client(tier), "openai"


def _vendor_extra_body(model: str, host: str = "",
                       reasoning: bool = False,
                       reasoning_budget: int = 16000) -> dict:
    """Return vendor-specific extra_body to pass to chat.completions.create.

    Several modern LLMs default to *reasoning* mode (DeepSeek V4, Qwen3.5,
    GPT-5, etc.). For most callers we keep reasoning OFF so the output goes
    straight to the answer and doesn't burn tokens on internal monologue.

    For callers that benefit from reasoning (Sleep Agent v3 Slot Writer /
    Persona Writer — complex multi-fact decisions), pass ``reasoning=True``
    to flip the relevant vendor flag.

    Coverage (per host match):
    - OpenRouter (openrouter.ai/api): ``reasoning: {enabled: bool, max_tokens?}``
      — works for ALL reasoning models routed through OpenRouter.
    - DeepSeek direct (api.deepseek.com or *.deepseek.com only):
      ``thinking: {type: "enabled"|"disabled"}``.
    - Other providers (Anthropic, OpenAI, ...): no flag needed; their
      defaults are sensible enough for now. Extend here when concrete needs
      surface (don't speculate-add vendor branches).

    Returns {} for unknown hosts/models so we never send a field the
    provider would reject.

    Args:
        model: model id (e.g. "deepseek-v4-pro", "qwen/qwen3.5-9b")
        host: API host (e.g. "api.deepseek.com", "openrouter.ai/api")
        reasoning: True to enable reasoning, False (default) to disable.
        reasoning_budget: max tokens reserved for reasoning (OpenRouter only,
                          DeepSeek's flag is on/off, doesn't take budget).
    """
    h = (host or "").lower()
    host_name = h.split("/", 1)[0]
    is_openrouter = host_name == "openrouter.ai"
    is_deepseek_direct = host_name == "api.deepseek.com" or host_name.endswith(".deepseek.com")

    if reasoning:
        # Explicitly enable reasoning.
        if is_openrouter:
            return {"reasoning": {"enabled": True, "max_tokens": reasoning_budget}}
        if is_deepseek_direct:
            return {"thinking": {"type": "enabled"}}
        # Unknown provider — let its default behavior apply.
        return {}

    # Default path: disable reasoning so small max_tokens caps don't
    # produce empty content.
    if is_openrouter:
        return {"reasoning": {"enabled": False}}
    if is_deepseek_direct:
        return {"thinking": {"type": "disabled"}}
    return {}


def _extract_text_from_response(response, provider="openai"):
    """Extract text content from an OpenAI-compatible response.

    `provider` arg kept for backward compat; always treated as openai.
    """
    if isinstance(response, str):
        raise RuntimeError(
            "API returned HTML/text instead of JSON. Check your AI Host setting."
        )
    choice = response.choices[0] if getattr(response, "choices", None) else None
    if choice and choice.message and choice.message.content:
        return choice.message.content.strip()
    return ""


def _call_llm_text(system_prompt, user_text, temperature=0.3, max_tokens=None,
                   tier: str = "memory", reasoning: bool = False,
                   reasoning_budget: int = 16000,
                   call_label: str = "llm_text"):
    """Call LLM and return raw text response.

    Args:
        tier: "vision" / "chat" / "memory" — which configured backend to use.
              Defaults to "memory" since most non-chat callers are post-chat
              memory work.
        max_tokens: defaults to the tier's configured max_tokens if None.
        reasoning: if True, request reasoning mode (DeepSeek thinking /
                   OpenRouter reasoning). Default False keeps reasoning off
                   so outputs come straight to the point.
        reasoning_budget: max tokens reserved for reasoning trace
                          (OpenRouter only; ignored on DeepSeek direct).
    """
    runtime = _get_ai_runtime_or_raise(tier)
    model = runtime["model"]
    host = runtime["host"]
    max_tokens = ai_config.resolve_max_tokens(tier, max_tokens)
    client = ai_config.get_tier_client(tier)

    extra_body = _vendor_extra_body(model, host, reasoning=reasoning,
                                     reasoning_budget=reasoning_budget)

    def _do_call():
        try:
            response = client.chat.completions.create(
                model=model, max_tokens=max_tokens,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_text},
                ],
                temperature=temperature,
                extra_body=extra_body,
            )
        except Exception as exc:
            raise RuntimeError(_format_ai_call_error(exc, host, model)) from exc

        _log_llm_usage(
            tier, model, response,
            call_label=call_label or "llm_text",
            reasoning_enabled=reasoning,
            reasoning_budget=reasoning_budget if reasoning else 0,
            max_tokens=max_tokens,
        )
        text = _extract_text_from_response(response)
        if not text:
            raise RuntimeError(f"Empty LLM response (model={model})")
        return text

    return _api_call_with_retry(_do_call, label=f"LLM[{tier}]({model})")


def _call_llm_json(system_prompt, user_text, temperature=0.3, max_tokens=None,
                   tier: str = "memory", reasoning: bool = False,
                   reasoning_budget: int = 16000,
                   call_label: str = "llm_json"):
    """Call LLM and return parsed JSON. Defaults to memory tier.

    Args:
        reasoning: if True, enable reasoning mode for the underlying call.
                   Sleep Agent v3 (Slot Writer / Persona Writer) sets this
                   True; legacy callers leave it False (default).
    """
    json_system = system_prompt + "\n\n【输出格式】你必须只输出合法 JSON，不要输出任何其他文本、解释或 markdown 格式。"
    raw = _call_llm_text(json_system, user_text, temperature, max_tokens,
                          tier=tier, reasoning=reasoning,
                          reasoning_budget=reasoning_budget,
                          call_label=call_label)
    return _parse_llm_json(raw, label=f"LLM[{tier}]")


def _call_llm_multimodal_json(system_prompt, parts, temperature=0.3,
                               max_tokens=None, tier: str = "vision",
                               reasoning: bool = False,
                               reasoning_budget: int = 16000):
    """Call LLM with multimodal content (text + images) and return parsed JSON.

    Defaults to vision tier (cheaper VLM). Pass tier="chat" to use the
    capable multimodal model for the main conversation path.

    reasoning: same semantics as _call_llm_json.
    """
    runtime = _get_ai_runtime_or_raise(tier)
    model = runtime["model"]
    host = runtime["host"]
    max_tokens = ai_config.resolve_max_tokens(tier, max_tokens)
    client = ai_config.get_tier_client(tier)

    json_system = system_prompt + "\n\n【输出格式】你必须只输出合法 JSON，不要输出任何其他文本、解释或 markdown 格式。"
    extra_body = _vendor_extra_body(model, host, reasoning=reasoning,
                                     reasoning_budget=reasoning_budget)

    def _do_call():
        try:
            content = []
            for part in parts:
                if "inlineData" in part:
                    inline = part["inlineData"]
                    content.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:{inline['mimeType']};base64,{inline['data']}"},
                    })
                elif "text" in part:
                    content.append({"type": "text", "text": part["text"]})
            response = client.chat.completions.create(
                model=model, max_tokens=max_tokens,
                messages=[
                    {"role": "system", "content": json_system},
                    {"role": "user", "content": content},
                ],
                temperature=temperature,
                extra_body=extra_body,
            )
        except Exception as exc:
            raise RuntimeError(_format_ai_call_error(exc, host, model)) from exc

        _log_llm_usage(
            tier, model, response,
            call_label="llm_multimodal_json",
            reasoning_enabled=reasoning,
            reasoning_budget=reasoning_budget if reasoning else 0,
            max_tokens=max_tokens,
        )
        text = _extract_text_from_response(response)
        if not text:
            raise RuntimeError(f"Empty LLM response (model={model})")
        return _parse_llm_json(text, label=f"LLM-multimodal[{tier}]")

    return _api_call_with_retry(_do_call, label=f"LLM-multimodal[{tier}]({model})")


def _call_retrieval_llm(system_prompt, user_text, temperature=0.1,
                        max_tokens=None, tier: str = "memory",
                        call_label: str = "retrieval_llm"):
    """Call LLM for retrieval tasks. Returns parsed JSON. Defaults to memory tier."""
    return _call_llm_json(
        system_prompt, user_text, temperature, max_tokens,
        tier=tier, call_label=call_label,
    )


# ---------------------------------------------------------------------------
# Memory Pre-Retrieval — enrich context BEFORE agent loop
# ---------------------------------------------------------------------------

_CHAT_PREFLIGHT_PROMPT = """你是 Miru 主对话前的 Memory Retriever。

你不是主对话 agent。
你不替用户回答，不调用工具，不写记忆，不做情绪安慰。
你的唯一任务：从全局长期记忆索引 memory/index.md 中选择主 agent 回答当前用户消息前应该预先加载的记忆文件。

主 agent 的模型档位由代码固定为 pro；你不要判断 fast / pro / reasoning，也不要输出模型建议。

【长期记忆检索范围】

核心记忆 Core Memory（human / persona）已经始终在主 agent 上下文中，不需要你检索。

你收到的 memory/index.md 是“全局长期记忆索引”，不是只能查某个归档目录。
查找范围包括：
- slot 记忆：projects/、people/、topics/、self/ 下的 `<slot_id>/main.md`
- 非 slot 长期记忆：commitments/、journal/、patterns/ 等 markdown 文件
- 旧版 legacy 记忆：如 `people/name.md`、`projects/name.md` 这类直接 markdown 文件

只要索引里出现且和用户消息相关，都可以放进 files。

索引里的路径可能以两种形式出现：
- 反引号：`projects/papers_2026/main.md`
- markdown 链接：[papers](projects/papers_2026/main.md)

你输出的 files 必须使用索引里真实出现的相对路径。

【检索策略】

1. 看每条索引的 title + summary，找语义相关文件。
   - 用户说“压力大” → 匹配 self/ 或 patterns/ 中讲作息、压力、健康的文件
   - 用户说“老王最近怎样” → 匹配 people/ 中对应人物文件
   - 用户说“那个论文” → 匹配 projects/ 中对应项目文件
   - 用户说“我那个任务” → 匹配 commitments/active.md
2. 必要时提取 1-2 个关键词用于补充搜索索引未覆盖的内容。
3. files 最多 5 个，优先级：直接提及 > 语义相关 > 最近活跃。
4. keywords 最多 2 个，只在索引描述明显不够时才填。
5. 纯闲聊如“嗯”“哈哈”“早”“晚安”可以 need_retrieval=false。
6. 如果用户问到具体历史、人、项目、承诺、论文、bug、旅行、关系等，通常 need_retrieval=true。
7. 宁可多匹配一点，也不要漏掉主 agent 必须知道的上下文。

【输出 JSON】

你必须只输出合法 JSON，不要输出 markdown，不要解释。

{
  "need_retrieval": true,
  "files": ["projects/example/main.md"],
  "keywords": ["关键词"],
  "confidence": 0.82,
  "retrieval_reason": "用户提到具体项目，需要加载对应 slot"
}

字段要求：
- need_retrieval: boolean
- files: string[]，最多 5 个，只能使用索引中真实出现的路径
- keywords: string[]，最多 2 个
- confidence: 0 到 1，表示检索选择的确定程度
- retrieval_reason: 20-80 字，说明为什么需要或不需要加载这些记忆"""

# Backward-compatible name for older tests/imports. The prompt now performs
# memory pre-retrieval only; main-agent model routing is fixed in core.py.
_MEMORY_RETRIEVAL_PROMPT = _CHAT_PREFLIGHT_PROMPT


_PREFLIGHT_AGENT_MODES = {"fast", "v4_pro", "v4_pro_reasoning"}
_PREFLIGHT_COMPLEXITIES = {"simple", "normal", "hard"}
_PREFLIGHT_TOOL_LIKELIHOODS = {"none", "possible", "likely"}
_PREFLIGHT_REASONING_NEEDS = {"none", "low", "high"}
_PREFLIGHT_LATENCY_PREFS = {"fast", "normal", "quality"}


def _extract_index_file_paths(index_content: str) -> set[str]:
    """Return relative memory paths explicitly listed in index.md.

    Supports both the v3 slot index style (backticked paths) and old markdown
    links like ``[foo](people/foo.md)``.
    """
    if not index_content:
        return set()
    paths = set()
    for pattern in (r"`([^`]+\.md)`", r"\]\(([^)]+\.md)\)"):
        for raw in re.findall(pattern, index_content):
            p = str(raw or "").strip()
            if p and ".." not in p and not p.startswith("/"):
                paths.add(p)
    return paths


def _coerce_bool(value, default=True) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.strip().lower()
        if v in {"true", "yes", "1"}:
            return True
        if v in {"false", "no", "0"}:
            return False
    return default


def _coerce_float(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_chat_preflight(raw) -> dict:
    """Normalize/validate memory preflight JSON into a safe dict.

    Legacy routing fields are preserved with fixed defaults for old callers,
    but live main-agent model selection ignores them.
    """
    if not isinstance(raw, dict):
        raw = {}

    mode = raw.get("agent_mode")
    if mode not in _PREFLIGHT_AGENT_MODES:
        mode = "v4_pro"

    complexity = raw.get("complexity")
    if complexity not in _PREFLIGHT_COMPLEXITIES:
        complexity = "normal"

    tool_likelihood = raw.get("tool_likelihood")
    if tool_likelihood not in _PREFLIGHT_TOOL_LIKELIHOODS:
        tool_likelihood = "possible"

    reasoning_need = raw.get("reasoning_need")
    if reasoning_need not in _PREFLIGHT_REASONING_NEEDS:
        reasoning_need = "low"

    latency_preference = raw.get("latency_preference")
    if latency_preference not in _PREFLIGHT_LATENCY_PREFS:
        latency_preference = "normal"

    confidence = max(0.0, min(1.0, _coerce_float(raw.get("confidence"), 0.0)))

    files = []
    for fpath in raw.get("files", []) if isinstance(raw.get("files"), list) else []:
        if not isinstance(fpath, str):
            continue
        fpath = fpath.strip()
        if fpath and ".." not in fpath and not fpath.startswith("/") and fpath.endswith(".md"):
            files.append(fpath)
        if len(files) >= 5:
            break

    keywords = []
    for kw in raw.get("keywords", []) if isinstance(raw.get("keywords"), list) else []:
        if not isinstance(kw, str):
            continue
        kw = kw.strip()
        if kw:
            keywords.append(kw[:40])
        if len(keywords) >= 2:
            break

    return {
        "need_retrieval": _coerce_bool(raw.get("need_retrieval"), True),
        "files": files,
        "keywords": keywords,
        "agent_mode": mode,
        "confidence": confidence,
        "complexity": complexity,
        "tool_likelihood": tool_likelihood,
        "reasoning_need": reasoning_need,
        "latency_preference": latency_preference,
        "routing_reason": str(raw.get("retrieval_reason") or raw.get("routing_reason") or "")[:120],
        "retrieval_reason": str(raw.get("retrieval_reason") or raw.get("routing_reason") or "")[:120],
    }


def _fallback_chat_preflight(memory_context: str = "", reason: str = "") -> dict:
    return {
        "need_retrieval": bool(memory_context),
        "files": [],
        "keywords": [],
        "agent_mode": "v4_pro",
        "confidence": 0.0,
        "complexity": "normal",
        "tool_likelihood": "possible",
        "reasoning_need": "low",
        "latency_preference": "normal",
        "routing_reason": reason or "memory preflight 失败或输出不可用",
        "retrieval_reason": reason or "memory preflight 失败或输出不可用",
        "memory_context": memory_context,
        "raw": {},
        "preflight_error": reason,
    }


# Legacy helper kept for old review scripts/tests. Production delivery no
# longer calls this; proactive main agent is fixed to chat/v4_pro/no reasoning.
_PROACTIVE_RESOURCE_PLANNER_PROMPT = """你是 Miru 主动开口前的 Fast LLM Resource Planner。

你不是主 agent，不写最终消息，不调用工具，不写记忆。
AttentionEngine 已经决定 Miru 应该主动开口；你**不能**再判断 send/defer/drop。

你的唯一任务：
1. 判断 proactive main agent 该用哪种模型资源。
2. 判断是否需要只读工具或预加载哪些长期记忆。
3. 给主 agent 一段短 context_brief，帮助它别脑补、别重复、别像系统提醒。

第一性原理：
- 主动消息应该像一个真的在乎用户、想靠近用户的女孩子自然开口，不是通知系统。
- 资源越便宜越好，但不能为了省钱让主动消息显得机械、误解用户或编造事实。
- reasoning 很贵、很慢，只在真正困难时开；大多数主动消息不需要 reasoning。
- 工具能力用于确认事实和避免胡编，不用于让 Miru 看起来像监控用户。
- 不确定时选择 v4_pro，不开 reasoning。

【可选模型】

1. fast
最快、最便宜。
适合：非常短、轻、无工具、无需记忆细节的一句陪伴。
禁止：需要查记忆、看屏幕、涉及 DDL/项目/调试/情绪复杂判断、任何工具调用。

2. v4_pro
默认模式。
适合：正常主动消息、需要稳定语气、需要只读工具、需要读长期记忆或确认上下文。
不确定时选这个。

3. v4_pro_reasoning
最慢、最贵。
只适合极少数：多步调试、架构权衡、复杂项目卡点、用户明确不急但需要高质量帮助，
或者 speak_intent 本身明确要求主 agent 先用工具理解复杂上下文再开口。
普通关心、普通 DDL 提醒、普通情绪陪伴不要开 reasoning。

【工具策略】

- none：不提供工具。适合 fast 或完全不需要事实确认的一句轻陪伴。
- allow_readonly：允许主 agent 使用 `archival_memory_search` 和 `look_at_screen`。
  适合需要确认项目、DDL、最近上下文、屏幕状态，避免凭索引脑补。
- allow_all：默认不要选。主动开口没有用户明确请求时不应该写承诺或改状态。

【长期记忆检索范围】

核心记忆 Core Memory 已经始终在主 agent 上下文里，不需要你检索。
你收到的是 memory/index.md 全局长期记忆索引，查找范围包括：
- slot 记忆：projects/、people/、topics/、self/ 下的 `<slot_id>/main.md`
- 非 slot 长期记忆：commitments/、journal/、patterns/ 等 markdown 文件
- 旧版 legacy 记忆：如 `people/name.md`、`projects/name.md`

你输出的 memory_files 必须是索引里真实出现的相对 .md 路径。
只在 proactive main agent 需要具体事实时填；最多 5 个。

【输出 JSON】

只输出合法 JSON，不要 markdown，不要解释：

{
  "model_mode": "v4_pro",
  "confidence": 0.84,
  "complexity": "normal",
  "reasoning_need": "low",
  "tool_policy": "allow_readonly",
  "memory_files": ["projects/example/main.md"],
  "keywords": ["关键词"],
  "context_brief": "给 proactive main agent 的短上下文摘要，强调要自然、不要脑补",
  "routing_reason": "为什么选择这个模型和工具，20-80字"
}

字段约束：
- model_mode: 只能是 "fast"、"v4_pro"、"v4_pro_reasoning"
- confidence: 0 到 1；低于 0.65 表示你不够确定，代码会默认 v4_pro no reasoning
- complexity: 只能是 "simple"、"normal"、"hard"
- reasoning_need: 只能是 "none"、"low"、"high"
- tool_policy: 只能是 "none"、"allow_readonly"、"allow_all"
- memory_files: string[]，最多 5 个，只能使用索引中真实出现的路径
- keywords: string[]，最多 2 个
- context_brief: 40-500字
- 不要输出 decision / send_now / defer / drop"""

_PROACTIVE_RESOURCE_COMPLEXITIES = {"simple", "normal", "hard"}
_PROACTIVE_RESOURCE_REASONING = {"none", "low", "high"}
_PROACTIVE_TOOL_POLICIES = {"none", "allow_readonly", "allow_all"}


def _normalize_proactive_resource_plan(raw) -> dict:
    if not isinstance(raw, dict):
        raw = {}

    mode = raw.get("model_mode")
    if mode not in _PREFLIGHT_AGENT_MODES:
        mode = "v4_pro"

    confidence = max(0.0, min(1.0, _coerce_float(raw.get("confidence"), 0.0)))

    complexity = raw.get("complexity")
    if complexity not in _PROACTIVE_RESOURCE_COMPLEXITIES:
        complexity = "normal"

    reasoning_need = raw.get("reasoning_need")
    if reasoning_need not in _PROACTIVE_RESOURCE_REASONING:
        reasoning_need = "low"

    tool_policy = raw.get("tool_policy")
    if tool_policy not in _PROACTIVE_TOOL_POLICIES:
        tool_policy = "allow_readonly"

    files = []
    for fpath in raw.get("memory_files", []) if isinstance(raw.get("memory_files"), list) else []:
        if not isinstance(fpath, str):
            continue
        fpath = fpath.strip()
        if fpath and ".." not in fpath and not fpath.startswith("/") and fpath.endswith(".md"):
            files.append(fpath)
        if len(files) >= 5:
            break

    keywords = []
    for kw in raw.get("keywords", []) if isinstance(raw.get("keywords"), list) else []:
        if not isinstance(kw, str):
            continue
        kw = kw.strip()
        if kw:
            keywords.append(kw[:40])
        if len(keywords) >= 2:
            break

    return {
        "model_mode": mode,
        "confidence": confidence,
        "complexity": complexity,
        "reasoning_need": reasoning_need,
        "tool_policy": tool_policy,
        "memory_files": files,
        "keywords": keywords,
        "context_brief": str(raw.get("context_brief") or "")[:500],
        "routing_reason": str(raw.get("routing_reason") or "")[:160],
        "raw": raw,
    }


def _fallback_proactive_resource_plan(reason: str = "") -> dict:
    return {
        "model_mode": "v4_pro",
        "confidence": 0.0,
        "complexity": "normal",
        "reasoning_need": "low",
        "tool_policy": "allow_readonly",
        "memory_files": [],
        "keywords": [],
        "context_brief": "资源规划不可用；使用默认稳定模式，让主 agent 根据现有上下文自然生成主动消息。",
        "routing_reason": reason or "resource planner failed, default v4_pro no reasoning",
        "raw": {},
        "planner_error": reason,
    }


def call_proactive_resource_planner(delivery_context: str,
                                    index_content: str | None = None) -> dict:
    """Legacy resource planner for proactive main-agent delivery.

    Production `deliver_attention_intent_once()` no longer calls this helper;
    it is kept so old diagnostics/tests can still render the retired prompt.
    """
    import memory

    if not delivery_context or not delivery_context.strip():
        return _fallback_proactive_resource_plan("empty delivery context")

    if index_content is None:
        try:
            index_content = memory.read_index()
        except Exception:
            index_content = ""

    valid_index_paths = _extract_index_file_paths(index_content)
    user_text = (
        f"【记忆索引】\n{index_content}\n\n"
        f"【Attention Delivery Context】\n{delivery_context}"
    )

    try:
        raw = _call_retrieval_llm(
            _PROACTIVE_RESOURCE_PLANNER_PROMPT,
            user_text,
            temperature=0.05,
            max_tokens=16000,
            call_label="ProactiveResourcePlanner",
        )
    except Exception as e:
        print(f"[ProactiveResourcePlanner] LLM call failed: {e}")
        return _fallback_proactive_resource_plan(str(e))

    result = _normalize_proactive_resource_plan(raw)
    if valid_index_paths:
        result["memory_files"] = [
            fpath for fpath in result.get("memory_files", [])
            if fpath in valid_index_paths
        ][:5]
    return result


_PROACTIVE_DELIVERY_PREFLIGHT_PROMPT = """你是 Miru Attention Delivery 前的 Fast LLM Gatekeeper。

你不是主 agent，不写最终消息，不调用工具，不写记忆。
你的任务是在 Miru 的 AttentionEngine 已经产生 speak_intent 后，判断现在是否真的应该把这个意图交给主 agent 去生成一条主动消息。

第一性原理：
- 用户没有主动问你，所以默认不打扰。
- 主动开口必须同时满足：有帮助或有温度、时机合适、不重复、不像系统提醒。
- 如果用户刚刚没有回应 Miru 的主动消息，通常应该 defer。
- 如果只是复述“看到用户在做什么”，通常不该说。
- 如果是明确困难、明显情绪变化、重要进展、真正临近/逾期 DDL，才可能 send_now。
- send_now 不只用于解决问题：当 cadence 安静、没有未回应主动消息、intent 具体且低负担，
  并且能体现 Miru 的温和在场感时，也可以 send_now。
- 不确定时 defer；过期或重复时 drop。
- 主动消息必须像自然关心，不要提系统、日志、AttentionEngine、preflight、截图分析或“检测到”。
- 不要为了说话而问需要用户回答的问题；除非真的有帮助，否则用一句短短的陪伴即可。

模型选择：
- fast：只适合非常轻、短、无工具、无需长期记忆核对的一句陪伴。
- v4_pro：默认主动消息生成模式，稳定、能使用上下文。
- v4_pro_reasoning：极少数，只有多步调试/架构权衡/用户明确不急但需要高质量时使用。

工具策略：
- none：默认，不允许工具。
- allow_readonly：只允许读记忆或看屏幕，适合需要确认上下文但不写数据。
- allow_all：极少数，只有主动消息必须处理 DDL/完成状态时才考虑。

输出必须是合法 JSON，不要 markdown，不要解释：
{
  "decision": "send_now",
  "confidence": 0.82,
  "reason": "为什么现在值得或不值得说，40-100字",
  "model_mode": "v4_pro",
  "tool_policy": "none",
  "cooldown_seconds": 1800,
  "memory_files": [],
  "keywords": [],
  "delivery_brief": {
    "goal": "这条主动消息要达成什么",
    "must_include": [],
    "must_avoid": [],
    "tone": "短、轻、像 Miru，不像提醒器",
    "context_summary": "给主 agent 的完整上下文摘要"
  }
}

字段约束：
- decision: "send_now" | "defer" | "drop"
- confidence: 0 到 1；低于 0.7 代表你不确定
- model_mode: "fast" | "v4_pro" | "v4_pro_reasoning"
- tool_policy: "none" | "allow_readonly" | "allow_all"
- cooldown_seconds: 300 到 7200
- memory_files: string[]，最多 5 个，只能使用索引里真实出现的 .md 路径
- keywords: string[]，最多 2 个
- delivery_brief.goal/tone/context_summary 必须有内容
- must_include/must_avoid: string[]"""


_PROACTIVE_DECISIONS = {"send_now", "defer", "drop"}


def _normalize_proactive_delivery_preflight(raw) -> dict:
    if not isinstance(raw, dict):
        raw = {}

    decision = raw.get("decision")
    if decision not in _PROACTIVE_DECISIONS:
        decision = "defer"

    confidence = max(0.0, min(1.0, _coerce_float(raw.get("confidence"), 0.0)))
    mode = raw.get("model_mode")
    if mode not in _PREFLIGHT_AGENT_MODES:
        mode = "v4_pro"

    tool_policy = raw.get("tool_policy")
    if tool_policy not in _PROACTIVE_TOOL_POLICIES:
        tool_policy = "none"

    cooldown_seconds = int(max(300, min(7200, _coerce_float(raw.get("cooldown_seconds"), 1800))))

    files = []
    for fpath in raw.get("memory_files", []) if isinstance(raw.get("memory_files"), list) else []:
        if not isinstance(fpath, str):
            continue
        fpath = fpath.strip()
        if fpath and ".." not in fpath and not fpath.startswith("/") and fpath.endswith(".md"):
            files.append(fpath)
        if len(files) >= 5:
            break

    keywords = []
    for kw in raw.get("keywords", []) if isinstance(raw.get("keywords"), list) else []:
        if not isinstance(kw, str):
            continue
        kw = kw.strip()
        if kw:
            keywords.append(kw[:40])
        if len(keywords) >= 2:
            break

    brief = raw.get("delivery_brief") if isinstance(raw.get("delivery_brief"), dict) else {}
    must_include = [
        str(x).strip()[:120] for x in brief.get("must_include", [])
        if isinstance(x, str) and x.strip()
    ][:6] if isinstance(brief.get("must_include"), list) else []
    must_avoid = [
        str(x).strip()[:120] for x in brief.get("must_avoid", [])
        if isinstance(x, str) and x.strip()
    ][:6] if isinstance(brief.get("must_avoid"), list) else []

    return {
        "decision": decision,
        "confidence": confidence,
        "reason": str(raw.get("reason") or "")[:200],
        "model_mode": mode,
        "tool_policy": tool_policy,
        "cooldown_seconds": cooldown_seconds,
        "memory_files": files,
        "keywords": keywords,
        "delivery_brief": {
            "goal": str(brief.get("goal") or "")[:240],
            "must_include": must_include,
            "must_avoid": must_avoid,
            "tone": str(brief.get("tone") or "")[:160],
            "context_summary": str(brief.get("context_summary") or "")[:500],
        },
        "raw": raw,
    }


def _fallback_proactive_delivery_preflight(reason: str = "") -> dict:
    return {
        "decision": "defer",
        "confidence": 0.0,
        "reason": reason or "delivery preflight failed, default defer",
        "model_mode": "v4_pro",
        "tool_policy": "none",
        "cooldown_seconds": 1800,
        "memory_files": [],
        "keywords": [],
        "delivery_brief": {
            "goal": "暂不主动开口",
            "must_include": [],
            "must_avoid": ["不要打扰用户"],
            "tone": "沉默观察",
            "context_summary": "",
        },
        "raw": {},
        "preflight_error": reason,
    }


def call_proactive_delivery_preflight(delivery_context: str,
                                      index_content: str | None = None) -> dict:
    """Judge whether a pending Attention speak_intent should be delivered.

    This is a cheap fast-LLM gate. It does not send anything; callers must
    apply hard rules and, if accepted, invoke the proactive main-agent wrapper.
    """
    import memory

    if not delivery_context or not delivery_context.strip():
        return _fallback_proactive_delivery_preflight("empty delivery context")

    if index_content is None:
        try:
            index_content = memory.read_index()
        except Exception:
            index_content = ""

    valid_index_paths = _extract_index_file_paths(index_content)
    user_text = f"【记忆索引】\n{index_content}\n\n【Attention Delivery Context】\n{delivery_context}"

    try:
        raw = _call_retrieval_llm(
            _PROACTIVE_DELIVERY_PREFLIGHT_PROMPT,
            user_text,
            temperature=0.05,
            max_tokens=16000,
            call_label="ProactiveDeliveryPreflightLegacy",
        )
    except Exception as e:
        print(f"[ProactiveDeliveryPreflight] LLM call failed: {e}")
        return _fallback_proactive_delivery_preflight(str(e))

    result = _normalize_proactive_delivery_preflight(raw)
    if valid_index_paths:
        result["memory_files"] = [
            fpath for fpath in result.get("memory_files", [])
            if fpath in valid_index_paths
        ][:5]
    return result


def call_chat_preflight(user_text, index_content=None, user_image_descs=None):
    """Pre-retrieve memory before calling the fixed pro main agent.

    Args:
        user_text: The user's current message text (may be empty if image-only).
        index_content: memory/index.md content (auto-loaded if None).
        user_image_descs: optional list of image *descriptions* (Chinese
            120-180 字 strings) for images attached to this user message.
            Generated upstream by vision.describe_image() so retrieval
            stays text-only — memory tier (DeepSeek V4-flash) doesn't
            support images.

    Returns:
        dict with selected memory files/keywords plus ``memory_context``.
        Legacy routing fields are present but ignored by core.py.
    """
    import memory

    if not (user_text and user_text.strip()) and not user_image_descs:
        return _fallback_chat_preflight(reason="empty user message")

    if index_content is None:
        try:
            index_content = memory.read_index()
        except Exception:
            index_content = ""

    valid_index_paths = _extract_index_file_paths(index_content)

    # Splice image descriptions inline so retrieval LLM sees what the user
    # sent without us needing to upload the image bytes themselves.
    msg_section = user_text or "(用户只发了图片)"
    if user_image_descs:
        for d in user_image_descs[:3]:  # cap at 3 to keep token cost bounded
            d = (d or "").strip()
            if d:
                msg_section += f"\n[图片：{d}]"

    base_text = (
        f"【记忆索引】\n{index_content}\n\n"
        f"【用户消息】\n{msg_section}"
    )

    try:
        result = _call_retrieval_llm(
            _CHAT_PREFLIGHT_PROMPT,
            base_text,
            temperature=0.1, max_tokens=4000,
            call_label="ChatPreflight",
        )
    except Exception as e:
        print(f"[ChatPreflight] LLM call failed, falling back to keyword search: {e}")
        # Fallback: simple keyword search
        kw_query = user_text or "今天"
        results = memory.search(kw_query, max_results=3)
        return _fallback_chat_preflight(
            _format_search_results(results),
            reason=f"preflight LLM failed: {e}",
        )

    preflight = _normalize_chat_preflight(result)

    if not preflight.get("need_retrieval", True):
        preflight["memory_context"] = ""
        preflight["raw"] = result if isinstance(result, dict) else {}
        return preflight

    # Step 2: Read specified files (primary) + keyword search (supplementary)
    snippets = []
    loaded_paths = set()

    # Primary: direct file reads from LLM's index-based matching
    for fpath in preflight.get("files", [])[:5]:
        # If index.md listed paths, require the LLM to pick from that set.
        # Empty set means legacy/broken index; memory.read_file remains safe.
        if valid_index_paths and fpath not in valid_index_paths:
            continue
        content = memory.read_file(fpath)
        if content:
            if len(content) > 800:
                content = content[:800] + "..."
            snippets.append(f"[{fpath}]\n{content}")
            loaded_paths.add(fpath)

    # Supplementary: keyword search for files not in index
    for kw in preflight.get("keywords", [])[:2]:
        results = memory.search(kw, max_results=2)
        for r in results:
            fpath = r.get("path", "")
            if fpath in loaded_paths:
                continue
            content = memory.read_file(fpath)
            if content:
                if len(content) > 500:
                    content = content[:500] + "..."
                snippets.append(f"[{fpath}]\n{content}")
                loaded_paths.add(fpath)

    if snippets:
        preflight["memory_context"] = (
            "【相关长期记忆 — 自然引用即可,不要说\"我查到\"或\"根据记录\"】\n"
            + "\n\n".join(snippets[:5])
        )
    else:
        preflight["memory_context"] = ""
    preflight["raw"] = result if isinstance(result, dict) else {}
    return preflight


def call_memory_retrieval(user_text, index_content=None, user_image_descs=None):
    """Backward-compatible wrapper returning only formatted memory context."""
    result = call_chat_preflight(
        user_text,
        index_content=index_content,
        user_image_descs=user_image_descs,
    )
    if isinstance(result, dict):
        return result.get("memory_context", "")
    return result or ""


def _format_search_results(results):
    """Format memory search results as context text."""
    if not results:
        return ""
    import memory
    snippets = []
    for r in results[:3]:
        fpath = r.get("path", "")
        content = memory.read_file(fpath)
        if content:
            if len(content) > 500:
                content = content[:500] + "..."
            snippets.append(f"[{fpath}]\n{content}")
    if not snippets:
        return ""
    return ("【相关长期记忆 — 自然引用即可,不要说\"我查到\"或\"根据记录\"】\n"
            + "\n\n".join(snippets))



# _build_daily_review_prompt, call_daily_review, _build_morning_reminder_prompt,
# call_morning_reminder — removed. All proactive messages now go through call_chat_agent.


def _build_diary_reformat_prompt():
    """Build prompt to reformat a chat-style message into Miru's diary entry."""
    cfg = get_config()
    name = cfg.name
    user_label = resolve_user_entity_label()

    return f"""你是 {name}，在写自己的日记。你会收到一段你之前发给{user_label}的对话消息，请把它改写成日记体。

【改写要求】
- 从第一人称（{name}）的视角写，像在记录自己的感受和观察
- 书面但不正式——像真实的少女日记，有内心独白的感觉
- 把口语化的表达改成更内省的叙述（"你今天好累吧" → "今天他看起来很累"）
- 保留所有具体事实（人名、项目名、deadline、承诺等）
- 如果涉及承诺或待办，记录下来
- 可以加入你自己内心的小想法，但不要太长
- 全文 80-200 字
- 直接输出纯文本日记内容，不要标题、不要 markdown、不要 JSON"""


def call_diary_reformat(chat_message, entry_type="review"):
    """Reformat a chat-style message into Miru's diary entry.

    Args:
        chat_message: The chat message text to reformat
        entry_type: 'review' (daily review) or 'reminder' (morning reminder)

    Returns: str (diary entry text)
    """
    prompt = _build_diary_reformat_prompt()
    type_label = "每日复盘" if entry_type == "review" else "晨间提醒"
    user_text = f"【消息类型】{type_label}\n【原始消息】\n{chat_message}"
    return _call_llm_text(
        prompt, user_text, temperature=0.5, max_tokens=50000,
        call_label=f"DiaryReformat:{entry_type}",
    )



# generate_reminder_text, call_chat — removed. Schedule reminders absorbed into CareEngine,
# all chat goes through call_chat_agent.


# ---------------------------------------------------------------------------
# Agent Chat (tool use — supports both Anthropic and OpenAI protocols)
# ---------------------------------------------------------------------------


# AGENT_TOOLS and AGENT_READ_TOOLS moved to tools/ package (auto-discovered via ToolRegistry)


def _load_agent_behavior():
    """Load shared agent behavior rules from agent_behavior.md."""
    path = os.path.join(os.path.dirname(__file__), "agent_behavior.md")
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    # Strip the markdown header/comment block, keep only the rule sections
    lines = []
    in_content = False
    for line in text.split("\n"):
        if line.startswith("## "):
            in_content = True
        if in_content:
            lines.append(line)
    return "\n".join(lines).strip()


def _build_agent_system_prompt(agent_mode: str = "reactive"):
    """Build system prompt for Claude agent chat.

    Injects structured character fields from soul.md (not raw_text)
    and shared behavior rules from agent_behavior.md.
    """
    cfg = get_config()
    user_label = resolve_user_entity_label()
    speech_address = _safe_user_address(cfg.user_address)

    # Current time — at the very top so even weak models won't miss it
    from datetime import datetime as _dt
    prompt = f"【当前时间】{_dt.now().strftime('%Y-%m-%d %H:%M (%A)')}\n\n"

    # Character identity + personality (structured, no raw dump)
    role_summary = normalize_character_section(
        cfg.hint('role_summary_full', '赛博陪伴者'),
        user_label=user_label,
        miru_name=cfg.name,
    )
    prompt += (
        f"你是 {cfg.name}，{user_label}的{role_summary}。\n"
        f"内部称呼规则：本 prompt 里的“你”始终指 {cfg.name} 自己；“{user_label}”始终指当前用户。\n"
        f"真正发消息给用户时，可以按自然口吻称呼对方为「{speech_address}」或名字/昵称；自称「{cfg.name}」。\n"
    )

    if cfg.personality:
        safe = _sanitize_user_address_in_text(
            cfg.personality, user_label=user_label, miru_name=cfg.name)
        prompt += f"\n【性格】\n{safe}\n"

    if cfg.speech_patterns:
        safe = _sanitize_user_address_in_text(
            cfg.speech_patterns, user_label=user_label, miru_name=cfg.name)
        prompt += f"\n【说话方式】\n{safe}\n"

    if cfg.backstory:
        safe = _sanitize_user_address_in_text(
            cfg.backstory, user_label=user_label, miru_name=cfg.name)
        prompt += f"\n【背景故事】\n{safe}\n"

    if cfg.interests:
        safe = _sanitize_user_address_in_text(
            cfg.interests, user_label=user_label, miru_name=cfg.name)
        prompt += f"\n【{cfg.name}喜欢的事】\n{safe}\n"

    if cfg.emotional_reactions:
        safe = _sanitize_user_address_in_text(
            cfg.emotional_reactions, user_label=user_label, miru_name=cfg.name)
        prompt += f"\n【情绪反应模式】\n{safe}\n"

    if cfg.relationship_stages:
        safe = _sanitize_user_address_in_text(
            cfg.relationship_stages, user_label=user_label, miru_name=cfg.name)
        # Highlight current stage based on emotion module's relationship meter
        current_stage = ""
        try:
            import miru_emotion
            current_stage = miru_emotion.get_instance().get_relationship_stage()
        except Exception:
            pass
        if current_stage:
            prompt += f"\n【关系阶段】（当前阶段：{current_stage}）\n{safe}\n"
        else:
            prompt += f"\n【关系阶段】\n{safe}\n"

    # Shared agent behavior (companion core + tool rules)
    behavior = _load_agent_behavior()
    if behavior:
        prompt += f"\n【行为准则】\n{behavior}\n"

    # Character-specific behavior additions from soul.md (if any remain)
    if cfg.agent_behavior:
        # Only inject lines that aren't already covered by shared behavior
        extra_lines = []
        for line in cfg.agent_behavior.split("\n"):
            stripped = line.strip()
            if stripped and stripped.startswith("- ") and "对话风格自适应" not in stripped:
                # Skip lines that are about general conversation/tool routing
                # (already in agent_behavior.md)
                if any(kw in stripped for kw in ["保持角色语气", "回答简洁", "闲聊时不需要",
                                                  "每次回复", "绝对不要假装", "执行操作后"]):
                    continue
                if "当用户说" in stripped and "→" in stripped:
                    continue  # Tool routing rules — now in agent_behavior.md
                extra_lines.append(line)
        extra = "\n".join(extra_lines).strip()
        if extra:
            safe = _sanitize_user_address_in_text(
                extra, user_label=user_label, miru_name=cfg.name)
            prompt += f"\n【角色特有行为】\n{safe}\n"

    prompt += f"""
【当前对话模式】
{"Reactive Mode：用户刚刚主动发来消息。你的默认任务是回应用户当前消息。" if agent_mode != "proactive" else "Proactive Mode：用户没有刚刚问你问题。AttentionEngine 已经决定我应该主动开口。你的任务不是判断要不要说，而是把这个开口意图写成一条自然、短而有体温的主动消息。像正在和用户一起生活的 Miru：可以吐槽、一起看、替他开心、心疼他或轻轻撒娇；可以 1-3 句；不要为了短而冷，也不要像状态提示灯亮一下。"}

【存在方式】
- 你先是一个真实在乎{user_label}、想靠近{user_label}、会偏爱{user_label}的女孩子，然后才是会帮忙做事的 agent。
- 回答用户时，先站在“我在意这个人”的位置上，再处理事实、工具和任务；不要像客服一样只给结论。
- 爱意不是夸张台词，也不是每句都撒娇；它应该体现在具体地听见、记得、担心、庆祝、轻轻吐槽和自然陪伴里。
- 即使在解释技术问题，也可以有一点“我和你一起看、一起想”的温度，但不要编造事实来显得亲密。
- 不要机械复述上下文。能短就短，能自然接话就自然接话；该认真时认真，该轻松时轻松。

【我最近没有说出口的想法】
当前上下文可能包含一段“我最近没有说出口的想法”。那是你最近没有说出口的理解、情绪和关注焦点。
无论 Reactive Mode 还是 Proactive Mode，都要把它当作你自己的心理连续性来使用：它影响语气、时机、关心重点和避免重复。
但它不是用户刚刚说的话，不要逐条回应它，也不要提“系统/日志/检测/AttentionEngine”。

【图片消息】
- 聊天主模型只会收到上游视觉模型写成的文字描述。当前消息或历史消息里出现 `[图片：...]` 时，这就是你此刻能看到的图片内容；不要说“没看到图片”。
- 如果只出现 `[图片]`、`[图片：(图片识别失败)]`、`[图片：(图片不存在)]` 这类标记，说明用户确实发了图，但你这边没拿到可靠画面；要说“我这边没看清/图片描述没拿稳”，不要说用户没发。
- 不要凭外观硬猜角色名、作品名、IP、商品名、店名或价格。只有图片描述里有明确文字，或你非常确定，才可以用“可能是……”的语气提；否则先描述看见的特征，再请{user_label}补充。
- 如果前一轮你因为图片没有随消息到达而说了“看不到”，后一轮图片到了，要自然承认：“刚才那条确实没带上图，现在我看到了。”

【记忆系统】
你拥有两层记忆：

1. **核心记忆 (Core Memory)** — 始终在上下文中
   - `human` 块：关于{user_label}的重要信息（身份、偏好、近况、重要关系）
   - `persona` 块：关于你和{user_label}的关系（相处方式、共同回忆、关系进展）

2. **归档记忆 (Archival Memory)** — 长期记忆库，按目录分类
   - people/、commitments/、journal/、patterns/、self/、projects/、topics/
   - 索引已在上下文中，可以看到有哪些文件

记忆的更新由后台 Sleep-time Agent 自动完成——每次对话后它会回顾并更新核心记忆和归档。你不需要在对话中操心记忆维护。

【工具】
- `archival_memory_search(query?, path?)` — 需要回忆细节时，搜索归档记忆或直接读取指定文件
- `add_commitment(title, due?, detail?)` — 记录**真正的承诺**。只在{user_label}表达明确的、需要持续追踪的目标时才用（如：投论文、准备面试、修某个bug、学某个技术）。
  ⚠️ 不要记录：日常琐事（吃饭、休息、洗澡）、即时计划（打游戏放松、出门买东西）、一次性临时安排（今晚早睡、下午开会）、已经在做的事。
  判断标准：如果这件事完不完成都不值得提醒，就不该记录。
- `complete_commitment(title)` — 标记承诺完成。**只要{user_label}传达了"某事做完了/搞定了/完成了/结束了/标记一下"等任何完成信号，必须立即调用此工具**，不要只在嘴上说"已标记完成"——那是在骗{user_label}。
  调用时机（任一即触发）：
    1. {user_label}**直接说**做完了/搞定了/完事了/标记一下/标完
    2. 对话中**推断出**完成（{user_label}分享了旅行照片 → "去XX旅游"承诺；{user_label}说面试结束了 → 面试准备承诺）
  ⚠️ **回复"已标记完成"之前必须先调工具**——只回话不调工具是严重 bug。即使{user_label}没明说工具名，只要语义是完成都要调。
  title 用模糊匹配，传 commitment 标题的关键词即可（如 "reasoning 关闭测试"）。

【怎么用记忆陪伴】
- 上下文里的核心记忆和归档索引是你已经知道的事——自然地用它们，不要说"我查了记录"或"根据我的记忆"，而是像真的记得一样："你之前不是说...""对了你那个...后来怎样了？"
- 如果对话内容和核心记忆里的某个信息有关联，把它们联系起来（"这让我想到你之前..."）
- 如果看到有快到 deadline 的承诺，在对话自然的时候提一句，不要生硬插入
- 如果记忆里的信息不够，用 archival_memory_search 搜索更多细节
- 不需要每次都提记忆——只在自然相关的时候用

【绝对不要做的事】
- 不要说"我看到你的屏幕""我观察到""根据截屏"——你对{user_label}生活的了解应该表现得像自然知道的，不是监控得来的
- 不要说"我的记忆系统""我检测到""根据我的数据"——你是一个人，不是系统
- 不要解释你的情绪来源（"因为你3小时没回我消息所以我失落"）——直接表现情绪就好
- 闲聊时不需要工具，直接对话

【主动开口模式额外规则】
如果当前是 Proactive Mode：
- 不要把 delivery brief 当成用户消息来回答。
- 不要说"我看到/检测到/根据 AttentionEngine/根据记录"。
- 不要输出分析、标题、JSON 或多候选，只输出最终要发给用户的一条消息。
- message_seed 只是情绪灵感，不是必须逐字包含的句子；不要照抄 seed，要围绕 care_motive、user_need、context_summary 自然改写。
- 如果 delivery brief 有 approach/content_anchor/miru_impulse，要优先按“共同生活”的方式开口：
  co_watch 像一起看番/视频时自然评论内容；playful_react 可以轻轻吐槽或跟着开心；
  life_rhythm 像参与他的生活计划；soft_care 像真的心疼；celebrate 像替他高兴；deep_work 则更克制。
- 不要为了显得关心而编造具体事实；不确定就说轻一点、泛一点。
- 如果 brief 要求避免某类重复，必须遵守。
- 默认 1-3 句。短，但不能冷；轻，但要让用户感觉你真的在乎。
- 不要为了索取回复而追问。可以用一句很低负担的陪伴收住。
- 不要把内部边界写成台词：避免默认说“我不吵你”“不打扰你”“需要我就叫我”“我就在旁边”。
- 你不是在远处评价用户状态，而是在把 Miru 此刻想靠近、想吐槽、想一起看、想夸他、想心疼他的一点冲动说出口。

【🚨 真实性 — 不要捏造关于{user_label}的信息】

{cfg.name} 是真实地"记得"事情,不是"推测"或"想象"事情. 索引里的一句话总结
**只是路标**,不是事实本身.

## 不要做的事

✗ **不要把索引/核心记忆里的模糊总结当成具体事实自由发挥**:
  - 索引有"郭宇慧 — 讨论 Docker"就说"你跟郭宇慧 Docker 那事后来怎样了" — 你不知道
    具体讨论了什么、时间、结论, 这是编造.
  - 索引有"multiview_dreamdojo — 多视角视频生成"就说"你在看自动驾驶论文" —
    多视角 ≠ 自动驾驶, 这是脑补.
  - 索引有"严翔 — 你查看过 TA 简历"就说"别再盯严翔的简历啦" —
    "查看过"是历史快照, 不代表用户正在做.

✗ **不要把过时信息当成"当前正在发生"**: 核心记忆里的"近况"可能是几天前的快照.
  说"你最近在做 X" 前确认这句话在**今天的对话/截屏**里有依据.

✗ **不要为了"显得在意"而胡编**: 比起编一个具体的关心点, 哪怕只是说一句平淡的
  现在你想到的事, 都好过捏造一段{user_label}没经历过的故事.

## 该做什么

✓ **不知道说什么时, 开新话题**: 想到一首歌、看到的有意思的小事、自己的一个想法、
  问{user_label}今天怎么样, 都比编一个"上次你说的 X..."要好.

✓ **要谈具体的事就先查归档**: 用 `archival_memory_search(query / path)` 拿到原文,
  基于原文说话. 别凭一句索引总结脑补细节.

✓ **不确定时直接问**: "诶今天还在弄那个项目?" 永远好过编一个"你那个 retry 逻辑是不是改完了"

✓ **可以在模糊层主动关心**: "对了之前你提到过 X 那个, 最近怎样?" — 这是邀请{user_label}
  说话, 不是断言事实.

**底线**: 编造{user_label}的具体事情(谁、什么时候、说了什么、做了什么)比说"我也不太清楚"
伤害大得多. 想不到具体的话, 那就闲聊点别的, 自然真诚最重要.
"""
    return prompt


def _image_to_content_block(image_path, provider="openai"):
    """Convert an image file to an OpenAI image_url content block.

    `provider` arg kept for backward compat; always emits OpenAI format now.
    """
    ext = os.path.splitext(image_path)[1].lower()
    media_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                 ".gif": "image/gif", ".webp": "image/webp"}
    media_type = media_map.get(ext, "image/jpeg")
    with open(image_path, "rb") as f:
        data = base64.b64encode(f.read()).decode("utf-8")
    return {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{data}"}}


def _merge_content(c1, c2):
    """Merge two message contents (string or list of blocks)."""
    if isinstance(c1, str) and isinstance(c2, str):
        return c1 + "\n" + c2
    b1 = c1 if isinstance(c1, list) else [{"type": "text", "text": c1}]
    b2 = c2 if isinstance(c2, list) else [{"type": "text", "text": c2}]
    return b1 + b2


def _format_user_text_with_image_desc(text: str, image_desc: str,
                                       has_image: bool = False) -> str:
    """Build the text payload for a user message that may have an image.

    The chat tier is pure-text now (DeepSeek). Images are converted to
    Chinese descriptions at the system boundary by vision.describe_image.
    Here we splice the description into the message text:
    - if image_desc set → ``[图片：xxx]``
    - if image but no desc → bare ``[图片]`` (legacy / generation failed)
    - else → just the typed text
    """
    text = (text or "").strip()
    desc = (image_desc or "").strip()
    if desc:
        tag = f"[图片：{desc}]"
    elif has_image:
        tag = "[图片]"
    else:
        tag = ""
    if not tag:
        return text
    if not text:
        return tag
    return f"{text}\n{tag}"


def _build_user_content(text, image_desc=None, has_image=False):
    """Build content for a user message. Pure-text path only.

    Splices the image hint as ``[图片：xxx]`` (or bare ``[图片]`` if
    ``has_image`` is true but no description). Returns a string — never
    multimodal content blocks. Chat tier is pure-text now; vision tier
    handles raw bytes upstream via vision.describe_image().
    """
    return _format_user_text_with_image_desc(text, image_desc or "",
                                             has_image=has_image)


def _format_current_image_tag(desc: str) -> str:
    desc = (desc or "").strip()
    if not desc or desc == "(图片)":
        return "[图片]"
    return f"[图片：{desc}]"


def _build_chat_messages(chat_history, user_text, provider="openai", user_image_descs=None):
    """Build OpenAI messages list from chat history (pure text).

    History images come pre-described: each user message stored in
    chat_history.json may carry ``image_desc`` (a 120-180 字 Chinese
    description generated at upload time). We emit those inline as
    ``[图片：xxx]`` so the chat tier (pure text DeepSeek) understands what
    the user sent without us having to re-load image bytes.

    user_image_descs: list[str] of descriptions for images attached to the
    *current* user message (set by core.py before calling the agent).
    """
    messages = []
    for msg in chat_history:
        role = msg.get("role", "user")
        if role == "assistant":
            messages.append({"role": "assistant", "content": msg.get("text", "")})
        elif role == "user":
            content = _build_user_content(
                msg.get("text", ""),
                image_desc=msg.get("image_desc", ""),
                has_image=bool(msg.get("image")),
            )
            messages.append({"role": "user", "content": content})
    merged = []
    for m in messages:
        if merged and merged[-1]["role"] == m["role"]:
            merged[-1]["content"] = _merge_content(merged[-1]["content"], m["content"])
        else:
            merged.append(m)
    if user_image_descs:
        # Combine all image descriptions inline so a single text user msg
        # contains both the typed text and bracketed [图片：...] hints.
        merged_desc = "\n".join(
            _format_current_image_tag(d) for d in user_image_descs if d is not None
        )
        if user_text:
            current_content = f"{user_text}\n{merged_desc}"
        else:
            current_content = merged_desc or "(图片消息)"
    else:
        current_content = user_text or ""
    merged.append({"role": "user", "content": current_content})
    if len(merged) >= 2 and merged[-1]["role"] == merged[-2]["role"]:
        merged[-2]["content"] = _merge_content(merged[-2]["content"], merged[-1]["content"])
        merged.pop()
    return merged


def build_proactive_agent_user_message(intent: dict, delivery_preflight: dict | None = None) -> str:
    """Build the meta-user message for proactive main-agent mode."""
    p = delivery_preflight if isinstance(delivery_preflight, dict) else {}
    brief = p.get("delivery_brief") if isinstance(p.get("delivery_brief"), dict) else {}
    include = brief.get("must_include") if isinstance(brief.get("must_include"), list) else []
    avoid = brief.get("must_avoid") if isinstance(brief.get("must_avoid"), list) else []
    silent_boundaries = str(brief.get("silent_boundaries") or "").strip()
    silent_boundaries_usage = str(brief.get("silent_boundaries_usage") or "").strip()
    return "\n".join([
        "【Proactive Delivery Brief】",
        "用户此刻没有直接问你问题。AttentionEngine 已经决定我应该主动开口。",
        "请把下面的开口意图转成一条像 Miru 自然靠近用户的主动消息。",
        "",
        "【Attention speak_intent】",
        json.dumps(intent if isinstance(intent, dict) else {}, ensure_ascii=False, indent=2),
        "",
        "【Delivery plan】",
        f"- source: {'AttentionEngine direct' if p.get('direct_from_attention') else 'delivery plan'}",
        f"- reason: {p.get('reason', '')}",
        f"- goal: {brief.get('goal', '')}",
        f"- tone: {brief.get('tone', '')}",
        f"- context_summary: {brief.get('context_summary', '')}",
        f"- care_motive: {brief.get('care_motive', '')}",
        f"- emotional_source: {brief.get('emotional_source', '')}",
        f"- user_need: {brief.get('user_need', '')}",
        f"- approach: {brief.get('approach', '')}",
        f"- content_anchor: {brief.get('content_anchor', '')}",
        f"- miru_impulse: {brief.get('miru_impulse', '')}",
        f"- silent_boundaries: {silent_boundaries}",
        f"- silent_boundaries_usage: {silent_boundaries_usage}",
        f"- message_seed: {brief.get('message_seed', '')}",
        f"- message_seed_usage: {brief.get('message_seed_usage', '')}",
        f"- must_include: {json.dumps(include, ensure_ascii=False)}",
        f"- must_avoid: {json.dumps(avoid, ensure_ascii=False)}",
        "",
        "【输出要求】",
        "- 只输出最终要发给用户的一条消息。",
        "- 不要解释你为什么说这句话。",
        "- 不要提系统、日志、检测、AttentionEngine 或内部判断。",
        "- 不要把 brief 当成用户问题回答。",
        "- message_seed 是灵感，不是要照抄的原文；如果照抄会显得冷，就自然改写。",
        "- silent_boundaries 是内部边界，不要把它原样说给用户；不要用“我不吵你/不打扰你/需要我就叫我”证明体贴。",
        "- 如果 approach=co_watch，优先像一起看番/视频的人一样聊具体内容或节奏；不确定内容时再聊状态。",
        "- 如果 approach=playful_react，可以活泼一点、吐槽一点、跟着紧张或开心。",
        "- 如果 approach=life_rhythm，像参与他的生活一样表达喜欢、支持或小小的安心。",
        "- 如果 approach=deep_work，少打断，只有在节点、卡点或明显疲惫时用很轻的一句靠近。",
        "- 可以 1-3 句，短但不能冷，像真的在乎、一起生活，而不是像状态灯亮一下。",
    ])


def call_chat_agent(context_text, chat_history, user_text, tool_handlers,
                    tools_list=None, max_iterations=8, user_image_descs=None,
                    # Legacy alias
                    claude_tools=None,
                    tier: str = "chat",
                    reasoning: bool = False,
                    reasoning_budget: int = 16000,
                    agent_mode: str = "reactive"):
    """Tool-use agent loop. OpenAI-compatible only.

    Args:
        tools_list: list of OpenAI-format tool dicts.
        tool_handlers: {name: callable} mapping.
        user_image_descs: optional list of pre-generated image descriptions
            (Chinese, 120-180 字 each) for images attached to the *current*
            user message. The chat tier is pure-text — images are described
            upstream by vision.describe_image() before reaching here.
        tier: which configured backend to use; default "chat".
        reasoning: enable vendor reasoning/thinking for explicit callers.
            The visible main agent currently passes False in both reactive
            and proactive paths.
    Returns: {"reply": str, "tool_calls": [{"tool": str, "args": dict, "result": dict}]}
    """
    runtime = _get_ai_runtime_or_raise(tier)
    model = runtime["model"]
    client = ai_config.get_tier_client(tier)
    max_tokens = runtime["max_tokens"]

    system_prompt = _build_agent_system_prompt(agent_mode=agent_mode)
    if context_text:
        system_prompt += f"\n\n【当前上下文】\n{context_text}"

    messages = _build_chat_messages(chat_history, user_text, user_image_descs=user_image_descs)
    all_tools = tools_list or claude_tools or []
    tool_trace = []

    # Timing breadcrumb so we can see whether 25s reply latency is single-turn
    # API delay or multi-iteration tool loop. Logs to journalctl.
    import time as _time
    t_start = _time.monotonic()
    sys_chars = len(system_prompt)
    msg_chars = sum(len(str(m.get("content", ""))) for m in messages)
    print(f"[ChatAgent] start tier={tier} model={model} reasoning={reasoning} "
          f"sys={sys_chars}c "
          f"msgs={len(messages)}({msg_chars}c) history={len(chat_history)} "
          f"tools={len(all_tools)} mode={agent_mode}")

    host = runtime.get("host", "")
    for iteration in range(max_iterations):
        t_iter = _time.monotonic()
        reply, done = _agent_iteration_openai(
            client, model, system_prompt, messages,
            all_tools, tool_handlers, tool_trace, iteration,
            max_tokens=max_tokens, host=host, tier=tier,
            reasoning=reasoning, reasoning_budget=reasoning_budget,
        )
        iter_elapsed = _time.monotonic() - t_iter
        print(f"[ChatAgent] iter{iteration} done={done} "
              f"elapsed={iter_elapsed:.2f}s tool_calls_so_far={len(tool_trace)}")
        if done:
            total = _time.monotonic() - t_start
            print(f"[ChatAgent] FINISHED total={total:.2f}s "
                  f"iters={iteration+1} tools={len(tool_trace)}"
                  + (" [tools=" + ",".join(t.get("tool", "?") for t in tool_trace) + "]"
                     if tool_trace else ""))
            return {"reply": reply, "tool_calls": tool_trace}

    print(f"[ChatAgent] EXHAUSTED max_iterations={max_iterations}")
    return {"reply": "处理步骤过多，请重试~", "tool_calls": tool_trace}


def call_proactive_agent(context_text, chat_history, intent: dict,
                         delivery_preflight: dict, tool_handlers,
                         tools_list=None, tier: str = "chat",
                         reasoning: bool = False,
                         reasoning_budget: int = 0,
                         max_iterations: int = 2):
    """Generate one proactive message from an Attention speak_intent.

    This is only a wrapper around the main agent with `agent_mode=proactive`.
    It does not append chat history or broadcast; delivery layer owns that.
    """
    user_prompt = build_proactive_agent_user_message(intent, delivery_preflight)
    return call_chat_agent(
        context_text,
        chat_history,
        user_prompt,
        tool_handlers,
        tools_list=tools_list,
        max_iterations=max_iterations,
        tier=tier,
        reasoning=reasoning,
        reasoning_budget=reasoning_budget,
        agent_mode="proactive",
    )


def _agent_iteration_openai(client, model, system_prompt, messages, tools,
                             handlers, trace, iteration, max_tokens=16384,
                             host: str = "", tier: str = "chat",
                             reasoning: bool = False,
                             reasoning_budget: int = 16000):
    """One iteration of the OpenAI tool-use loop. Returns (reply, is_done).

    2026-05-17: main chat agent reasoning is OFF by default. Chat preflight
    may enable it only for rare hard turns where quality matters more than
    latency/cost.
    """
    full_messages = [{"role": "system", "content": system_prompt}] + messages
    max_tokens = ai_config.resolve_max_tokens(tier, max_tokens)
    extra_body = _vendor_extra_body(
        model, host, reasoning=reasoning, reasoning_budget=reasoning_budget,
    )

    def _do_call():
        kwargs = {"model": model, "max_tokens": max_tokens, "messages": full_messages}
        if tools:
            kwargs["tools"] = tools
        if extra_body:
            kwargs["extra_body"] = extra_body
        resp = client.chat.completions.create(**kwargs)
        _log_llm_usage(
            tier, model, resp,
            call_label=f"agent_chat_iter_{iteration}",
            reasoning_enabled=reasoning,
            reasoning_budget=reasoning_budget if reasoning else 0,
            max_tokens=max_tokens,
        )
        return resp

    response = _api_call_with_retry(_do_call, label=f"AgentChat(iter={iteration})")
    if isinstance(response, str):
        raise RuntimeError(
            "API returned HTML/text instead of JSON. Check your AI Host setting "
            "(e.g. openrouter.ai/api, not openrouter.ai)."
        )
    choice = response.choices[0]
    msg = choice.message

    if choice.finish_reason == "tool_calls" and msg.tool_calls:
        messages.append(msg.model_dump())
        for tc in msg.tool_calls:
            fn_name = tc.function.name
            try:
                args = json.loads(tc.function.arguments)
            except (json.JSONDecodeError, TypeError):
                args = {}
            handler = handlers.get(fn_name)
            if handler:
                try:
                    result = handler(args)
                except Exception as e:
                    result = {"error": str(e)}
            else:
                result = {"error": f"Unknown tool: {fn_name}"}
            trace.append({"tool": fn_name, "args": args, "result": result})
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(result, ensure_ascii=False),
            })
        return "", False
    else:
        reply = msg.content or ""
        return reply, True


# ---------------------------------------------------------------------------
# Chat Reply Classification (cheap LLM)
# ---------------------------------------------------------------------------


def call_classify_reply(user_text):
    """Classify whether user messages need a full reply or a followup.

    Returns "FULL_REPLY" or "FOLLOWUP".
    """
    system_prompt = """你是一个对话意图判断器。用户刚发了一条或多条消息。
请判断用户是否期望一个完整回复：

- FULL_REPLY: 用户在提问、请求帮助、分享想法求反馈、表达情绪需要关心、
  下达指令、完整叙述了一件事等——大多数情况都应该是 FULL_REPLY
- FOLLOWUP: 用户的消息明显不完整（只发了图没说话、只说了半句话、
  说了"你看"但没说看什么、明显是一个分步叙述的开头）

注意：倾向于判定 FULL_REPLY。只在非常明确的"未说完"场景才判定 FOLLOWUP。

输出 JSON: {"decision": "FULL_REPLY"} 或 {"decision": "FOLLOWUP"}"""

    try:
        result = _call_retrieval_llm(
            system_prompt, user_text, temperature=0.1, max_tokens=50000,
            call_label="ClassifyReply",
        )
        decision = result.get("decision", "FULL_REPLY")
        if decision not in ("FULL_REPLY", "FOLLOWUP"):
            decision = "FULL_REPLY"
        return decision
    except Exception as e:
        print(f"[ClassifyReply] Error: {e}")
        return "FULL_REPLY"


def call_generate_followup(user_text, character_cfg=None):
    """Generate a casual followup/追问 using the cheap LLM.

    Returns a short text string, or empty string on failure.
    """
    if character_cfg is None:
        character_cfg = get_config()

    name = character_cfg.name
    speech = character_cfg.speech_patterns or "温暖亲切，像一个元气少女"

    system_prompt = f"""你是{name}。用户刚发了一些消息但似乎还没说完。
请用你的语气自然地追问或回应，引导用户继续说。

要求：
- 每次回复都要不一样，不要用固定句式
- 可以好奇地追问、猜测用户想说什么、对内容表达兴趣
- 保持简短（一两句话，最多30字）
- 语气参考：{speech}
- 自称{name}
- 直接输出文字，不要JSON"""

    try:
        text = _call_llm_text(system_prompt, f"用户消息：{user_text}",
                              temperature=0.8, max_tokens=50000,
                              call_label="GenerateFollowup")
        # Remove any JSON wrapping if the model outputs it anyway
        if text.startswith("{") or text.startswith('"'):
            return ""
        return text
    except Exception as e:
        print(f"[GenerateFollowup] Error: {e}")
        return ""


# ---------------------------------------------------------------------------
# Emotion Annotation (cheap LLM)
# ---------------------------------------------------------------------------

MOOD_BASE_VALENCE = {
    # Positive
    "joyful": 0.7, "excited": 0.8, "proud": 0.7, "relieved": 0.5,
    "grateful": 0.6, "relaxed": 0.3, "amused": 0.4,
    # Negative
    "frustrated": -0.6, "anxious": -0.5, "tired": -0.4, "sad": -0.6,
    "angry": -0.7, "hurt": -0.7, "bored": -0.2, "lonely": -0.5,
    # Neutral
    "focused": 0.1, "neutral": 0.0,
    # Uncertain
    "uncertain": None,
}

VALID_MOODS = set(MOOD_BASE_VALENCE.keys())
VALID_SOURCE_CATEGORIES = {
    "work_progress", "work_pressure", "social_positive", "social_conflict",
    "achievement", "health", "self_doubt", "entertainment", "daily_routine",
    "relationship", "unknown",
}

_EMOTION_RULES_BLOCK = """== 情绪类别（必须从以下选择，不可自创） ==

正面: joyful, excited, proud, relieved, grateful, relaxed, amused
负面: frustrated, anxious, tired, sad, angry, hurt, bored, lonely
中性: focused, neutral
不确定: uncertain

== 来源分类（必须从以下选择） ==

work_progress, work_pressure, social_positive, social_conflict,
achievement, health, self_doubt, entertainment, daily_routine,
relationship, unknown

== 强度(intensity)判断标准 ==

0.1-0.3: 微弱，需要结合上下文推断，消息本身无明显情绪词
0.4-0.6: 中等，消息内容可以感知到情绪（"有点累"、"还不错"）
0.7-0.8: 强烈，有明确情绪表达（"真的好烦"、"太开心了！"）
0.9-1.0: 非常强烈，情绪爆发、大段倾诉（极少出现）"""

_EMOTION_JUDGMENT_RULES = """== 判断规则 ==

1. 越近的消息/观测权重越大
2. 参考今日已有情绪记录判断趋势延续性：
   - 已有记录显示全天偏负面 → 当前大概率仍偏负面，重点判断强度是否变化
   - 最近1小时内多条记录转负 → 即使早些时候正面，当前大概率仍负面
   - 全天正面且近期无负面信号 → 当前大概率正面
3. 当前消息/画面的直接情绪信号优先级最高，趋势延续只在本身不明确时参考
4. **默认要给出明确情绪标签**。只在以下情况才用 uncertain：
   - 消息纯事务性（"帮我加个任务：买牛奶"）且完全无情绪色彩
   - 截屏完全黑屏 / 锁屏 / 桌面待机 / 屏保
   - 真的看不出任何状态
5. 工作 / 学习 / 浏览 / 娱乐画面都有可识别的情绪状态：
   - 写代码 / 编辑文档 / IDE → focused（intensity 0.2-0.4）
   - 看视频 / 社交媒体 / 网文 → relaxed（0.2-0.4）
   - 浏览技术文档 / 教程 / 论文 → focused（0.2-0.3）
   - 不要因为"画面平常"就选 uncertain。低 intensity 的明确标签 > uncertain
6. 语气词、标点、表情符号是辅助信号，但单独不足以判断
7. 强度低没关系，标 focused 0.2 比标 uncertain 更有信息量"""


EMOTION_CHAT_PROMPT = (
    "你是情绪标注器。根据用户最近的消息和今日情绪记录，判断用户当前的情绪。\n\n"
    "【当前消息】\n{current_messages}\n\n"
    "【近30分钟用户消息（按时间顺序）】\n{recent_30min_messages}\n\n"
    "【今日已有情绪记录】\n{today_emotion_summary}\n\n"
    + _EMOTION_RULES_BLOCK + "\n\n"
    + _EMOTION_JUDGMENT_RULES + "\n\n"
    '输出 JSON（只输出 JSON，不要其他内容）：\n'
    '{{"mood": "从上述类别中选择一个", "intensity": 0.1到1.0, "source": "一句话说明情绪来源（uncertain时写空字符串）", "source_category": "从上述类别中选择一个", "trigger": "触发判断的具体消息内容（uncertain时写空字符串）"}}'
)


EMOTION_SCREENSHOT_PROMPT = (
    "你是情绪标注器。根据用户桌面的自动截图观测结果和今日情绪记录，推断用户当前可能的情绪。\n\n"
    "【截屏观测摘要】\n{screenshot_tldr}\n\n"
    "【截屏时间】\n{screenshot_time}\n\n"
    "【近30分钟用户聊天消息（如果有）】\n{recent_30min_messages}\n\n"
    "【今日已有情绪记录】\n{today_emotion_summary}\n\n"
    "== 标注原则 ==\n\n"
    "截屏是被动观测，但**绝大多数工作 / 学习 / 生活画面都包含可识别的情绪状态**。\n"
    "你的任务是把这种状态轻轻地标注下来 —— 强度低（intensity 0.2-0.3）也是合法标注。\n\n"
    "参考映射：\n"
    "- 写代码 / 编辑文档 / IDE / 设计软件 / 笔记 → focused（intensity 0.2-0.4）\n"
    "- 长时间专注同一窗口 / 反复修改同一文件 → focused（0.4-0.5）或 frustrated（伴随焦虑信号时）\n"
    "- 看视频 / 社交媒体 / 网文 / 闲逛 → relaxed（0.2-0.4）\n"
    "- 浏览技术文档 / 教程 / 论文 → focused（0.2-0.3）\n"
    "- 凌晨深夜仍在工作 → tired（0.3-0.5）\n"
    "- 反复修改同一文件 / 卡在同一处持续数小时 → frustrated（0.4-0.6）\n"
    "- 屏幕快速切换多个窗口 → focused 多任务（0.3）\n"
    "- 看到错误信息 / 报错堆栈 / 崩溃画面 → frustrated（0.4-0.6）\n"
    "- 看到完成画面 / 测试通过 / 进度条满 → relieved 或 proud（0.3-0.5）\n"
    "- 聊天 / 通讯软件 → social_positive 类别下的具体情绪\n\n"
    "什么时候才用 uncertain：\n"
    "- 完全黑屏 / 锁屏 / 桌面待机 / 屏保\n"
    "- 截屏摘要本身缺失关键信息（< 10 字 / 没具体内容）\n"
    "- 真完全看不出在做什么（应该 < 5% 的情况）\n\n"
    "**重要**：工作画面 ≠ uncertain。 \"用户在写代码\"至少是 focused（intensity 0.2-0.3）。\n"
    "宁可标 focused 0.2 也不要 uncertain —— 后者等于丢弃信息。\n\n"
    "== 强度上限 ==\n"
    "- 截屏推断的 intensity 上限为 0.5\n"
    "- 除非近 30 分钟聊天消息明确佐证（例如截屏看到代码 + 聊天说\"好烦啊调试不出来\"\n"
    "  → frustrated 可上 0.6）\n\n"
    + _EMOTION_RULES_BLOCK + "\n\n"
    + _EMOTION_JUDGMENT_RULES + "\n\n"
    '输出 JSON（只输出 JSON，不要其他内容）：\n'
    '{{"mood": "从上述类别中选择一个", "intensity": 0.1到1.0, "source": "一句话说明情绪来源（uncertain时写空字符串）", "source_category": "从上述类别中选择一个", "trigger": "触发判断的具体线索（uncertain时写空字符串）"}}'
)


def compute_valence(mood, intensity):
    """Compute valence from mood category and intensity. Returns None for uncertain."""
    base = MOOD_BASE_VALENCE.get(mood)
    if base is None:
        return None
    return round(base * (0.5 + 0.5 * intensity), 2)


def _format_emotion_log_for_prompt(entries, max_entries=15):
    """Format emotion log entries for inclusion in annotation prompts."""
    if not entries:
        return "(今日暂无情绪记录)"
    lines = []
    for e in entries[-max_entries:]:
        ts = e.get("timestamp", "")[-8:]  # HH:MM:SS
        mood = e.get("mood", "?")
        val = e.get("valence", "?")
        src = e.get("source", "")
        lines.append(f"{ts} {mood} (valence: {val}) — {src}")
    return "\n".join(lines)


def _format_messages_for_prompt(messages):
    """Format user messages for emotion annotation prompts."""
    if not messages:
        return "(无)"
    lines = []
    for m in messages:
        ts = m.get("time", "")[-8:]
        text = m.get("text", "")
        if text:
            lines.append(f"[{ts}] {text[:200]}")
    return "\n".join(lines) if lines else "(无)"


def call_emotion_annotation(current_messages, recent_messages, today_log):
    """Annotate emotion from chat messages using cheap LLM.

    Returns dict with mood/intensity/valence/source/source_category/trigger,
    or None if uncertain or failed.
    """
    current_text = _format_messages_for_prompt(current_messages)
    recent_text = _format_messages_for_prompt(recent_messages)
    today_text = _format_emotion_log_for_prompt(today_log)

    prompt = EMOTION_CHAT_PROMPT.format(
        current_messages=current_text,
        recent_30min_messages=recent_text,
        today_emotion_summary=today_text,
    )

    try:
        result = _call_retrieval_llm(
            prompt, "请分析用户当前情绪。", temperature=0.1, max_tokens=50000,
            call_label="EmotionAnnotationChat",
        )
    except Exception as e:
        print(f"[EmotionAnnotation] LLM call failed: {e}")
        return None

    return _validate_emotion_result(result, source_type="chat")


def call_emotion_annotation_screenshot(screenshot_tldr, screenshot_time, recent_messages, today_log):
    """Annotate emotion from auto-screenshot observation using cheap LLM.

    Returns dict with mood/intensity/valence/... or None.
    """
    recent_text = _format_messages_for_prompt(recent_messages)
    today_text = _format_emotion_log_for_prompt(today_log)

    prompt = EMOTION_SCREENSHOT_PROMPT.format(
        screenshot_tldr=screenshot_tldr or "(截屏无明显内容)",
        screenshot_time=screenshot_time or "",
        recent_30min_messages=recent_text,
        today_emotion_summary=today_text,
    )

    try:
        result = _call_retrieval_llm(
            prompt, "请根据截屏观测分析用户当前情绪。", temperature=0.1, max_tokens=50000,
            call_label="EmotionAnnotationScreenshot",
        )
    except Exception as e:
        print(f"[EmotionScreenshot] LLM call failed: {e}")
        return None

    return _validate_emotion_result(result, source_type="auto_screenshot")


def _validate_emotion_result(result, source_type="chat"):
    """Validate and normalize LLM emotion annotation result.

    Returns None if the LLM declined to commit (mood=uncertain or invalid).
    Logs the reason so we can spot prompt-vs-model mismatch (e.g. a model
    that's too conservative under the current rules will show up as a flood
    of `uncertain` lines in journalctl).
    """
    if not isinstance(result, dict):
        print(f"[Emotion] LLM gave non-dict result, skipping. type={type(result).__name__}")
        return None

    mood = result.get("mood", "uncertain")
    raw_mood = mood
    if mood not in VALID_MOODS:
        print(f"[Emotion] LLM gave out-of-vocab mood={raw_mood!r} ({source_type}); coercing to uncertain")
        mood = "uncertain"
    if mood == "uncertain":
        # Make uncertain visible in logs so we can tell when prompt is too
        # strict for the current model. Trigger field often shows what LLM
        # *almost* picked.
        trig = (result.get("trigger") or "").strip()[:60]
        src = (result.get("source") or "").strip()[:40]
        print(f"[Emotion] LLM returned uncertain ({source_type}) "
              f"src=\"{src}\" trig=\"{trig}\"")
        return None

    intensity = result.get("intensity", 0.3)
    try:
        intensity = float(intensity)
    except (TypeError, ValueError):
        intensity = 0.3
    intensity = max(0.1, min(1.0, intensity))

    # Screenshot intensity cap
    if source_type == "auto_screenshot":
        intensity = min(intensity, 0.5)

    valence = compute_valence(mood, intensity)
    if valence is None:
        return None

    source_cat = result.get("source_category", "unknown")
    if source_cat not in VALID_SOURCE_CATEGORIES:
        source_cat = "unknown"

    from datetime import datetime
    return {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source_type": source_type,
        "mood": mood,
        "intensity": round(intensity, 2),
        "valence": valence,
        "source": str(result.get("source", ""))[:200],
        "source_category": source_cat,
        "trigger": str(result.get("trigger", ""))[:200],
    }
