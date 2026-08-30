#!/usr/bin/env python3
"""Render AttentionEngine + agent-mode prompt/context review HTML.

This does not call any LLM and does not send messages. It builds the exact
system/user/message payload shapes for:
  1. reactive memory preflight LLM
  2. reactive main agent
  3. proactive direct delivery plan
  4. proactive main agent wrapper
"""

from __future__ import annotations

import argparse
import html
import json
import os
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, g

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


DEFAULT_REACTIVE_TEXT = (
    "我明白了，那么我们现在就是思考如何让fast llm和主agent能够有两种模式，"
    "一种是根据用户消息去回答，另一种就是根据attention engine的输出去回答。"
)
DEFAULT_CLIENT_CONFIG = Path.home() / "Library" / "Application Support" / "Miru" / "config.json"


def esc(value) -> str:
    return html.escape(str(value), quote=True)


def jdump(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _choose_user_id(explicit: str | None) -> str:
    if explicit:
        return explicit
    raise SystemExit("Pass --user-id explicitly; prompt review contains private context.")


def _synthetic_intent() -> dict:
    now = datetime.now()
    return {
        "id": "intent_review_preview",
        "status": "pending",
        "delivery_status": "queued",
        "created_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "updated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "expires_at": (now + timedelta(minutes=75)).strftime("%Y-%m-%d %H:%M:%S"),
        "priority": "medium",
        "topic_key": "attention_agent_mode_design",
        "why_now": "用户正在设计 AttentionEngine 如何接入主 agent，需要保持上下文连续。",
        "suggested_tone": "直接、细致、不要像提醒器。",
        "avoid": "不要假装用户刚问了一个普通问题；不要提系统日志。",
        "context_summary": "用户在讨论 reactive/proactive 两种 agent 模式，希望看到完整上下文。",
        "episode_id": "ep_review_preview",
        "trigger": "review_dump",
        "repeat_count": 1,
    }


def _normalize_invitation_code(code: str) -> str:
    return (code or "").strip().upper().replace(" ", "")


def _local_invitation_code(full_or_local: str) -> str:
    code = _normalize_invitation_code(full_or_local)
    if not code.startswith("MIRU-"):
        return code
    body = code[5:].replace("-", "")
    if len(body) == 16:
        return "MIRU-" + body[-6:]
    return code


def _mask_invitation_code(full_or_local: str) -> str:
    local = _local_invitation_code(full_or_local)
    if not local.startswith("MIRU-") or len(local) < 8:
        return "(not recorded)"
    suffix = local[-2:]
    return f"MIRU-****{suffix}"


def _load_client_config(path: str | os.PathLike) -> dict:
    cfg_path = Path(path).expanduser()
    if not cfg_path.exists():
        raise SystemExit(f"Client config not found: {cfg_path}")
    with cfg_path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not data.get("server_url") or not data.get("auth_token"):
        raise SystemExit(f"Client config lacks server_url/auth_token: {cfg_path}")
    return data


def _http_json(url: str, token: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "miru-attention-review/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:500]
        raise SystemExit(f"HTTP {e.code} for {url}: {body}") from e
    except Exception as e:
        raise SystemExit(f"Failed to fetch {url}: {e}") from e
    return json.loads(raw)


def _fetch_export_bundle_from_client_config(config_path: str | os.PathLike,
                                            expected_invitation_code: str | None = None) -> tuple[dict, dict]:
    cfg = _load_client_config(config_path)
    if expected_invitation_code:
        expected = _local_invitation_code(expected_invitation_code)
        actual = _local_invitation_code(cfg.get("invitation_code", ""))
        if actual != expected:
            raise SystemExit(
                "Client config invitation_code mismatch: "
                f"expected {expected}, got {actual or '(empty)'}"
            )
    base = str(cfg["server_url"]).rstrip("/")
    me = _http_json(base + "/api/auth/me", cfg["auth_token"])
    bundle = _http_json(base + "/api/auth/export", cfg["auth_token"])
    uid = bundle.get("user_id") or me.get("user_id") or cfg.get("user_id")
    if not uid:
        raise SystemExit("Export bundle has no user_id")
    if me.get("user_id") and me.get("user_id") != uid:
        raise SystemExit(f"/api/auth/me user_id mismatch: {me.get('user_id')} vs export {uid}")
    safe_meta = {
        "source": "client_config_export",
        "server_url": base,
        "client_user_id": cfg.get("user_id", ""),
        "auth_user_id": me.get("user_id", ""),
        "invitation_code_hint": _mask_invitation_code(cfg.get("invitation_code", "")),
        "exported_at": bundle.get("exported_at", ""),
    }
    return bundle, safe_meta


def _materialize_export_bundle(bundle: dict) -> tuple[str, str]:
    user_id = bundle.get("user_id")
    files = bundle.get("files")
    if not user_id or not isinstance(files, dict):
        raise SystemExit("Invalid export bundle: expected user_id and files{}")
    tmp_root = tempfile.mkdtemp(prefix="miru_attention_review_")
    user_dir = os.path.join(tmp_root, user_id)
    os.makedirs(user_dir, exist_ok=True)
    for rel, content in files.items():
        if not isinstance(rel, str) or rel.startswith("/") or ".." in Path(rel).parts:
            continue
        full = os.path.join(user_dir, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, indent=2))
    os.makedirs(os.path.join(user_dir, "uploads"), exist_ok=True)
    return tmp_root, user_dir


def _build_payload(user_id: str, reactive_text: str, user_data_dir: str | None = None,
                   source_meta: dict | None = None) -> dict:
    import auth
    import core
    import memory
    import prompt
    import storage
    from tools import get_registry

    if user_data_dir is None:
        user_data_dir = auth.get_user_data_dir(user_id)
        auth.ensure_account_manifest(user_id)

    index_content = memory.read_index() or ""
    chat_history = storage.get_chat_history(limit=20) or []
    registry = get_registry()

    reactive_preflight_user = f"【记忆索引】\n{index_content}\n\n【用户消息】\n{reactive_text}"
    reactive_context = core._build_chat_context()
    reactive_system = prompt._build_agent_system_prompt(agent_mode="reactive")
    reactive_full_system = reactive_system
    if reactive_context:
        reactive_full_system += f"\n\n【当前上下文】\n{reactive_context}"
    reactive_messages = prompt._build_chat_messages(chat_history, reactive_text)

    intent = _synthetic_intent()
    delivery_context = core._build_attention_delivery_context(intent)
    delivery_plan = core._build_direct_attention_delivery_plan(intent, None)
    delivery_policy = core._direct_attention_delivery_policy(intent, None)
    proactive_user_prompt = prompt.build_proactive_agent_user_message(intent, delivery_plan)
    proactive_context = reactive_context + "\n【Attention Delivery Context】\n" + delivery_context
    proactive_system = prompt._build_agent_system_prompt(agent_mode="proactive")
    proactive_full_system = proactive_system
    if proactive_context:
        proactive_full_system += f"\n\n【当前上下文】\n{proactive_context}"
    proactive_messages = prompt._build_chat_messages(chat_history, proactive_user_prompt)

    return {
        "meta": {
            "user_id": user_id,
            "user_data_dir": user_data_dir,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "reactive_text": reactive_text,
            "note": "No LLM call, no message delivery.",
            "source": (source_meta or {}).get("source", "local_user_dir"),
            "source_server_url": (source_meta or {}).get("server_url", ""),
            "source_exported_at": (source_meta or {}).get("exported_at", ""),
            "source_invitation_code_hint": (source_meta or {}).get("invitation_code_hint", ""),
        },
        "architecture": {
            "attention_inputs": [
                "soul.md + agent_behavior Core",
                "activity state",
                "conversation_window",
                "proactive_cadence",
                "current_focus (stored compatibly as current_episode)",
                "speak_intent_queue",
                "recent_signals",
                "attention_log",
                "attention_delivery_log",
                "emotion_log",
                "miru_emotion",
                "identity facts",
                "core_memory human/persona guarded by account_manifest",
                "active commitments",
            ],
            "attention_outputs": [
                "attention_log.json",
                "attention_state.json",
                "attention_intent_queue.json",
                "attention_delivery_log.json",
                "emotion_log.json",
                "miru_emotion.json",
            ],
            "agent_modes": [
                "Reactive: user message -> memory preflight -> fixed pro main agent reply",
                "Proactive: AttentionEngine speak_intent -> direct delivery plan -> fixed pro proactive main agent -> append chat/SSE/push",
            ],
        },
        "reactive_memory_preflight": {
            "system": prompt._CHAT_PREFLIGHT_PROMPT,
            "user": reactive_preflight_user,
        },
        "reactive_main_agent": {
            "system": reactive_full_system,
            "messages": reactive_messages,
            "tool_count": len(registry.to_tools("openai")),
            "mode": "reactive",
        },
        "proactive_delivery_plan": {
            "plan": delivery_plan,
            "policy": delivery_policy,
            "context": delivery_context,
            "intent_preview": intent,
        },
        "proactive_main_agent": {
            "system": proactive_full_system,
            "messages": proactive_messages,
            "delivery_plan_preview": delivery_plan,
            "tool_policy": delivery_policy.get("tool_policy"),
            "mode": "proactive",
        },
    }


def _raw_section(title: str, body: str, note: str = "") -> str:
    return f"""
    <details class="raw">
      <summary><strong>{esc(title)}</strong><span>{esc(note)}</span></summary>
      <pre>{esc(body)}</pre>
    </details>
    """


def _render_html(payload: dict) -> str:
    meta = payload["meta"]
    architecture = payload["architecture"]
    input_rows = "".join(f"<li>{esc(x)}</li>" for x in architecture["attention_inputs"])
    output_rows = "".join(f"<li>{esc(x)}</li>" for x in architecture["attention_outputs"])

    style = """
    :root {
      --paper:#f3f1eb; --panel:#fffefa; --ink:#1b1b18; --muted:#6d6a61;
      --line:#d8d0c1; --rule:#24231f; --green:#2f6f55; --amber:#b46a24;
      --rose:#a94949; --blue:#315f82; --code:#171717; --code-ink:#f4efe4;
    }
    * { box-sizing:border-box; }
    body {
      margin:0; background:var(--paper); color:var(--ink);
      font-family: ui-serif, Georgia, "Times New Roman", "Songti SC", serif;
      line-height:1.58;
    }
    header {
      background:var(--panel); border-bottom:2px solid var(--rule);
      padding:28px clamp(16px, 4vw, 48px) 20px;
    }
    h1 { margin:0 0 8px; font-size:clamp(30px, 5vw, 56px); line-height:1.02; letter-spacing:0; }
    h2 { margin:0; font-size:22px; letter-spacing:0; }
    h3 { margin:0 0 8px; font-size:16px; letter-spacing:0; }
    p { margin:0; }
    .sub { max-width:900px; color:var(--muted); font-size:15px; }
    .nav {
      display:flex; flex-wrap:wrap; gap:8px; margin-top:18px;
      font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size:13px;
    }
    .nav a {
      color:var(--ink); text-decoration:none; border:1px solid var(--line);
      padding:6px 10px; background:#f8f4ea;
    }
    main { max-width:1280px; margin:0 auto; padding:24px clamp(14px, 3vw, 32px) 64px; }
    section, details.raw {
      background:var(--panel); border:1px solid var(--line); margin:16px 0;
    }
    .section-head {
      display:flex; align-items:flex-end; justify-content:space-between; gap:16px;
      padding:16px 18px; border-bottom:1px solid var(--line);
    }
    .section-head span, summary span { color:var(--muted); font-size:12px; }
    .hero-grid { display:grid; grid-template-columns:1.2fr .8fr; gap:16px; padding:18px; }
    .thesis {
      border:2px solid var(--rule); padding:18px; background:#fbf6e8;
      font-size:22px; line-height:1.42;
    }
    .thesis strong { color:var(--green); }
    .facts { display:grid; gap:10px; }
    .fact {
      border-left:5px solid var(--rule); padding:12px 14px; background:#f8f4ea;
      font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size:14px;
    }
    .fact b { display:block; font-size:13px; text-transform:uppercase; letter-spacing:.04em; margin-bottom:3px; }
    .cards { display:grid; grid-template-columns:repeat(4, minmax(0, 1fr)); gap:12px; padding:18px; }
    .card {
      min-height:130px; padding:15px; border:1px solid var(--line); background:#fffaf0;
      font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    .card .k { color:var(--muted); font-size:12px; margin-bottom:10px; }
    .card .v { font-size:22px; line-height:1.12; font-weight:700; margin-bottom:10px; }
    .card p { font-size:13px; color:#3c3a34; }
    .flow { display:grid; grid-template-columns:repeat(7, minmax(120px, 1fr)); gap:0; padding:18px; }
    .step {
      min-height:150px; padding:12px; border:1px solid var(--line); background:#fffaf0;
      position:relative; font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    .step + .step { border-left:0; }
    .step .n { font-size:12px; color:var(--muted); margin-bottom:8px; }
    .step .t { font-weight:800; font-size:15px; margin-bottom:8px; }
    .step .d { font-size:13px; color:#3e3b34; }
    .step.green { border-top:5px solid var(--green); }
    .step.amber { border-top:5px solid var(--amber); }
    .step.rose { border-top:5px solid var(--rose); }
    .step.blue { border-top:5px solid var(--blue); }
    .lanes { display:grid; grid-template-columns:repeat(2, minmax(0, 1fr)); gap:16px; padding:18px; }
    .lane { border:1px solid var(--line); background:#fbf6e8; }
    .lane h3 { padding:13px 14px; border-bottom:1px solid var(--line); background:#f4ead9; }
    .lane ol { margin:0; padding:14px 18px 16px 34px; }
    .lane li { margin:8px 0; }
    .cols { display:grid; grid-template-columns:1fr 1fr; gap:16px; padding:18px; }
    ul.clean { margin:0; padding-left:20px; }
    ul.clean li { margin:7px 0; }
    table { width:100%; border-collapse:collapse; font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; font-size:14px; }
    th, td { text-align:left; vertical-align:top; border-bottom:1px solid var(--line); padding:10px 12px; }
    th { background:#f4ead9; font-size:12px; text-transform:uppercase; letter-spacing:.04em; }
    .table-wrap { padding:18px; overflow:auto; }
    .callouts { display:grid; grid-template-columns:repeat(3, minmax(0, 1fr)); gap:12px; padding:18px; }
    .callout { border:1px solid var(--line); padding:14px; background:#fffaf0; }
    .callout b { display:block; margin-bottom:6px; }
    .callout.ok { border-top:5px solid var(--green); }
    .callout.warn { border-top:5px solid var(--amber); }
    .callout.stop { border-top:5px solid var(--rose); }
    dl { display:grid; grid-template-columns:210px 1fr; gap:8px 14px; padding:16px; margin:0; font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; font-size:13px; }
    dt { color:var(--muted); } dd { margin:0; overflow-wrap:anywhere; }
    details.raw { font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
    details.raw summary {
      cursor:pointer; list-style:none; display:flex; justify-content:space-between; gap:16px;
      padding:14px 16px; border-bottom:1px solid var(--line); background:#f8f4ea;
    }
    details.raw summary::-webkit-details-marker { display:none; }
    details.raw summary:before { content:"+"; font-weight:900; margin-right:10px; }
    details.raw[open] summary:before { content:"-"; }
    pre { margin:0; padding:18px; max-height:72vh; overflow:auto; white-space:pre-wrap; word-break:break-word; background:var(--code); color:var(--code-ink); font:12.5px/1.55 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; }
    code { font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; background:#efe7d8; padding:1px 4px; }
    @media (max-width:980px) {
      .hero-grid,.cards,.flow,.lanes,.cols,.callouts { grid-template-columns:1fr; }
      .step + .step { border-left:1px solid var(--line); border-top-width:5px; }
      dl { grid-template-columns:1fr; }
    }
    """

    meta_rows = "".join(
        f"<dt>{esc(k)}</dt><dd>{esc(v)}</dd>"
        for k, v in meta.items()
    )

    return f"""<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Attention Engine Map</title><style>{style}</style></head>
<body>
<header>
  <h1>Attention Engine Map</h1>
  <div class="sub">先看设计地图，再按需展开完整 prompt/context。这个页面不调用 LLM，不发送消息，数据来自当前账号快照。</div>
  <nav class="nav">
    <a href="#core">核心</a><a href="#flow">流程</a><a href="#inputs">输入输出</a><a href="#modes">两种 agent mode</a><a href="#cases">真实场景</a><a href="#raw">完整原文</a>
  </nav>
</header>
<main>
  <section id="core">
    <div class="section-head"><h2>一眼看懂</h2><span>AttentionEngine 不是聊天 agent</span></div>
    <div class="hero-grid">
      <div class="thesis">
        AttentionEngine 是 Miru 的<strong>持续内心循环</strong>：它看见信号，理解用户和 Miru 的状态，并自己决定是否形成 <code>speak_intent</code>。只要形成意图，就直接交给主 agent 写成一句自然主动消息。
      </div>
      <div class="facts">
        <div class="fact"><b>它做什么</b>更新 thought、用户情绪、Miru 情绪、attention state、speak_intent queue。</div>
        <div class="fact"><b>它不做什么</b>不回答用户、不写长期记忆、不直接 SSE/push、不把截图事实硬塞给主 agent。</div>
        <div class="fact"><b>为什么存在</b>让 Miru 像一个持续在场的人一样“先理解”，而不是每 10 分钟机械提醒一次。</div>
      </div>
    </div>
    <div class="cards">
      <div class="card"><div class="k">Layer 1</div><div class="v">收信号</div><p>聊天、截图、活跃状态、DDL、heartbeat 统一进入 salience buffer。</p></div>
      <div class="card"><div class="k">Layer 2</div><div class="v">形成内心</div><p>完整 soul.md + snapshot 交给轻量 LLM，输出 thought / emotion / attention。</p></div>
      <div class="card"><div class="k">Layer 3</div><div class="v">决定开口</div><p>speak_intent 就是 Miru 判断“应该说”的结构化意图，进入队列并去重。</p></div>
      <div class="card"><div class="k">Layer 4</div><div class="v">自然表达</div><p>proactive main agent 只负责把意图写成一句自然的话，然后 chat/SSE/push。</p></div>
    </div>
  </section>

  <section id="flow">
    <div class="section-head"><h2>主流程</h2><span>从外界信号到“可能开口”</span></div>
    <div class="flow">
      <div class="step green"><div class="n">01</div><div class="t">Signals</div><div class="d">用户消息、Miru 回复、截图 sig≥2、活跃状态、DDL、heartbeat。</div></div>
      <div class="step green"><div class="n">02</div><div class="t">Salience</div><div class="d">sig=2 weak；sig=3 normal；sig≥4/chat/DDL strong；sig&lt;2 丢弃。</div></div>
      <div class="step blue"><div class="n">03</div><div class="t">Snapshot</div><div class="d">组合人格、对话窗口、节奏、current focus、队列、情绪、DDL、近期信号。</div></div>
      <div class="step blue"><div class="n">04</div><div class="t">Attention LLM</div><div class="d">memory tier，无 reasoning。任务是“想清楚”，不是“说出口”。</div></div>
      <div class="step amber"><div class="n">05</div><div class="t">State Writes</div><div class="d">写 attention_log/state，并 dual-write emotion_log 与 miru_emotion。</div></div>
      <div class="step amber"><div class="n">06</div><div class="t">Intent Queue</div><div class="d">若有 speak_intent，同 topic 30 分钟内去重或升级。</div></div>
      <div class="step rose"><div class="n">07</div><div class="t">Delivery Dry-run</div><div class="d">fast gate + hard rules 写 attention_delivery_log；真实发送仍关闭。</div></div>
    </div>
  </section>

  <section id="inputs">
    <div class="section-head"><h2>输入与输出</h2><span>哪些东西真的进入 AttentionEngine</span></div>
    <div class="cols">
      <div><h3>Inputs</h3><ul class="clean">{input_rows}</ul></div>
      <div><h3>Outputs</h3><ul class="clean">{output_rows}</ul></div>
    </div>
    <div class="callouts">
      <div class="callout ok"><b>Journal 不读</b>Attention tick 不再读取日记，避免旧叙事反复污染实时判断。</div>
      <div class="callout warn"><b>DDL 只读活跃项</b>只看未完成、带 deadline、当前相关的承诺；陈旧逾期项会过滤。</div>
      <div class="callout stop"><b>sig=2 不写 memory</b>弱截图只影响内心和情绪，不进入长期 slot。</div>
    </div>
  </section>

  <section id="modes">
    <div class="section-head"><h2>Memory Preflight 与主 Agent 的两种模式</h2><span>这是最容易混的地方</span></div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>链路</th><th>谁触发</th><th>Preflight 做什么</th><th>Main Agent 做什么</th><th>当前是否会发给用户</th></tr></thead>
        <tbody>
          <tr><td>Reactive</td><td>用户发消息</td><td>只读记忆索引 + 当前用户消息，选择要预加载的记忆；不再建议模型档位。</td><td>固定用 pro 回答用户当前问题；上下文里额外读一段紧凑 Attention 状态。</td><td>会，这是普通聊天。</td></tr>
          <tr><td>Proactive</td><td>AttentionEngine 产生 speak_intent</td><td>不再调用 cheap LLM 做资源路由，也不做二次 send/defer/drop 判断。</td><td>固定用 pro，把 AttentionEngine 的 speak_intent 和上下文转成一句自然主动消息并发送。</td><td>会，只要 AttentionEngine 决定开口。</td></tr>
        </tbody>
      </table>
    </div>
  </section>

  <section id="cases">
    <div class="section-head"><h2>真实场景怎么走</h2><span>按事件理解比按代码理解快</span></div>
    <div class="lanes">
      <div class="lane">
        <h3>用户发消息</h3>
        <ol>
          <li>消息写入 chat history，并作为 <code>chat_in</code> strong signal 给 AttentionEngine。</li>
          <li>memory preflight 只决定要不要预加载长期记忆。</li>
          <li>reactive main agent 固定 pro、工具开启，回答用户并读到紧凑 Attention 状态。</li>
          <li>回复后作为 <code>chat_out</code> signal，再更新 Miru 的内心和情绪。</li>
        </ol>
      </div>
      <div class="lane">
        <h3>自动截图</h3>
        <ol>
          <li><code>sig&lt;2</code> 直接丢弃，Attention 不看。</li>
          <li><code>sig=2</code> 只进 Attention，影响“内心/情绪”，不写 memory slot。</li>
          <li><code>sig≥3</code> 进 Attention，也可能进入 ScreenSlotWriter。</li>
          <li><code>sig≥4</code>、DDL、项目变化才更可能用强模型/更强写入策略。</li>
        </ol>
      </div>
      <div class="lane">
        <h3>Attention 想开口</h3>
        <ol>
          <li>LLM 输出 <code>speak_intent</code>，例如“用户像是卡住了，想轻轻问一句”。</li>
          <li>队列按 topic 去重；同类意图 30 分钟内不会无限新增。</li>
          <li>生成 direct delivery plan，不再用分数、二次 gate 或 cheap LLM 资源路由拦截。</li>
          <li>proactive main agent 固定 pro + 只读工具，写最终一句话；随后写 chat/SSE/push，并记录 <code>attention_delivery_log.json</code>。</li>
        </ol>
      </div>
      <div class="lane">
        <h3>没有新信号</h3>
        <ol>
          <li>heartbeat 大约 12 分钟兜底 tick。</li>
          <li>主要更新时间感、等待感、情绪惯性。</li>
          <li>它不是为了硬找话说。</li>
          <li>如果证据很弱，不会刷可见 emotion river。</li>
        </ol>
      </div>
    </div>
  </section>

  <section>
    <div class="section-head"><h2>当前账号快照</h2><span>来源信息，不含 token</span></div>
    <dl>{meta_rows}</dl>
  </section>

  <section id="raw">
    <div class="section-head"><h2>完整原文审计区</h2><span>默认折叠，需要时展开</span></div>
    {_raw_section("Reactive Memory Preflight messages[0] system", payload["reactive_memory_preflight"]["system"], "memory preflight prompt")}
    {_raw_section("Reactive Memory Preflight messages[1] user", payload["reactive_memory_preflight"]["user"], "memory index + current user message")}
    {_raw_section("Reactive Main Agent messages[0] system", payload["reactive_main_agent"]["system"], "system prompt + 当前上下文")}
    {_raw_section("Reactive Main Agent messages[1..] array", jdump(payload["reactive_main_agent"]["messages"]), "chat history + current user message")}
    {_raw_section("Proactive Direct Delivery Plan", jdump(payload["proactive_delivery_plan"]), "AttentionEngine speak_intent is the decision; main agent policy is fixed pro")}
    {_raw_section("Proactive Direct Delivery Context", payload["proactive_delivery_plan"]["context"], "passed to proactive main agent context")}
    {_raw_section("Proactive Main Agent messages[0] system", payload["proactive_main_agent"]["system"], "proactive mode system + context")}
    {_raw_section("Proactive Main Agent messages[1..] array", jdump(payload["proactive_main_agent"]["messages"]), "chat history + proactive delivery brief")}
    {_raw_section("Full Raw Payload JSON", jdump(payload), "all sections above as data")}
  </section>
</main>
</body>
</html>"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Dump Attention + two agent-mode contexts to HTML.")
    parser.add_argument("--user-id")
    parser.add_argument("--from-client-config", action="store_true",
                        help="Fetch the current DMG/APK account via /api/auth/export and build prompts from that snapshot.")
    parser.add_argument("--client-config", default=str(DEFAULT_CLIENT_CONFIG))
    parser.add_argument("--expect-invitation-code", default="",
                        help="Optional guard; fail if the selected account is not this invite/local code.")
    parser.add_argument("--reactive-user-text", default=DEFAULT_REACTIVE_TEXT)
    parser.add_argument("--output", default=str(ROOT / "data" / "_admin" / "attention_agent_modes_review.html"))
    args = parser.parse_args()

    tmp_root = None
    source_meta = None
    try:
        if args.from_client_config:
            bundle, source_meta = _fetch_export_bundle_from_client_config(
                args.client_config,
                expected_invitation_code=args.expect_invitation_code or None,
            )
            user_id = bundle["user_id"]
            tmp_root, user_data_dir = _materialize_export_bundle(bundle)
        else:
            import auth
            user_id = _choose_user_id(args.user_id)
            user_data_dir = auth.get_user_data_dir(user_id)
            if not os.path.isdir(user_data_dir):
                raise SystemExit(f"User data dir does not exist: {user_data_dir}")
            if args.expect_invitation_code:
                manifest_path = auth.account_manifest_path(user_id)
                manifest = {}
                if os.path.exists(manifest_path):
                    with open(manifest_path, "r", encoding="utf-8") as f:
                        manifest = json.load(f)
                expected = _local_invitation_code(args.expect_invitation_code)
                actual = _local_invitation_code(manifest.get("invitation_code", ""))
                if actual != expected:
                    raise SystemExit(
                        "Local user invitation_code mismatch: "
                        f"expected {expected}, got {actual or '(empty)'}"
                    )

        app = Flask("attention_agent_modes_dump")
        with app.app_context():
            g.user_id = user_id
            g.user_data_dir = user_data_dir
            g.is_admin = False
            payload = _build_payload(user_id, args.reactive_user_text,
                                     user_data_dir=user_data_dir,
                                     source_meta=source_meta)
    finally:
        if tmp_root:
            shutil.rmtree(tmp_root, ignore_errors=True)

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(_render_html(payload), encoding="utf-8")
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
