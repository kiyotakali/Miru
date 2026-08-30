#!/usr/bin/env python3
"""Dump the exact AttentionEngine prompts for review.

This script does not call the LLM. It builds the same system prompt and
snapshot prompt that AttentionEngine._evaluate() would pass to _call_llm_json.
Output is written under data/_admin/ by default because it can contain private
user context.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from flask import Flask, g

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _choose_user_id(explicit: str | None, *, allow_first_local_user: bool) -> str:
    if explicit:
        return explicit
    if not allow_first_local_user:
        raise SystemExit(
            "Refusing to auto-pick a local user because Attention prompts contain private account context. "
            "Pass --user-id <uid>, or pass --first-local-user only for local dev review."
        )
    import auth
    engaged = auth.get_engaged_user_ids(days=30)
    if engaged:
        return engaged[0]
    active = auth.get_active_user_ids()
    if active:
        return active[0]
    raise SystemExit("No active user found. Pass --user-id explicitly.")


def _json_default(value):
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    return str(value)


def _add_review_signals(engine):
    """Seed non-persistent signals so reviewers can inspect signal formatting."""
    engine.record_signal("screenshot", {
        "device_id": "review-mac",
        "device_name": "Mac review signal",
        "observation": "【review signal】用户正在检查 Attention Engine 的上下文 prompt，旁边有测试结果和 proposal 文档。",
        "significance": 4,
        "source": "dump_attention_prompt",
    })
    engine.record_signal("chat_in", {
        "text": "【review signal】我想看到实际调用时完整没有压缩的上下文 prompt。",
        "device_id": "review",
    })


def _metadata(*, user_id: str, user_data_dir: str, trigger: str,
              with_review_signals: bool) -> dict:
    return {
        "user_id": user_id,
        "user_data_dir": user_data_dir,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "trigger": trigger,
        "with_review_signals": bool(with_review_signals),
        "llm_call": {
            "helper": "prompt._call_llm_json",
            "tier": "memory",
            "reasoning": False,
            "temperature": 0.45,
            "max_tokens": 12000,
        },
    }


def _write_markdown(out_path: Path, *, meta: dict, system_prompt: str,
                    user_prompt: str, snapshot: dict):
    out_path.write_text(
        "\n".join([
            "# AttentionEngine Prompt Review",
            "",
            f"- user_id: `{meta['user_id']}`",
            f"- user_data_dir: `{meta['user_data_dir']}`",
            f"- generated_at: `{meta['generated_at']}`",
            f"- trigger: `{meta['trigger']}`",
            f"- with_review_signals: `{meta['with_review_signals']}`",
            f"- llm_call: `{json.dumps(meta['llm_call'], ensure_ascii=False)}`",
            "",
            "## Messages Sent To LLM",
            "",
            "### messages[0] role=system",
            "",
            "```text",
            system_prompt,
            "```",
            "",
            "### messages[1] role=user",
            "",
            "```text",
            user_prompt,
            "```",
            "",
            "## Raw Snapshot JSON",
            "",
            "```json",
            json.dumps(snapshot, ensure_ascii=False, indent=2, default=_json_default),
            "```",
            "",
        ]),
        encoding="utf-8",
    )


def _html_escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def _write_html(out_path: Path, *, meta: dict, system_prompt: str,
                user_prompt: str, snapshot: dict):
    raw_snapshot = json.dumps(snapshot, ensure_ascii=False, indent=2, default=_json_default)
    messages_json = json.dumps({
        **meta["llm_call"],
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }, ensure_ascii=False, indent=2)
    style = """
    :root {
      color-scheme: light;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --ink: #171a1f;
      --muted: #667085;
      --line: #d8dee8;
      --accent: #335cff;
      --code-bg: #0f172a;
      --code-ink: #e5e7eb;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--ink);
      line-height: 1.55;
    }
    header {
      position: sticky;
      top: 0;
      z-index: 2;
      border-bottom: 1px solid var(--line);
      background: rgba(246, 247, 249, 0.96);
      backdrop-filter: blur(10px);
      padding: 18px 28px;
    }
    h1 { margin: 0 0 6px; font-size: 24px; letter-spacing: 0; }
    .sub { color: var(--muted); font-size: 13px; }
    main {
      max-width: 1280px;
      margin: 0 auto;
      padding: 22px 28px 56px;
    }
    section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      margin: 18px 0;
      overflow: hidden;
    }
    .section-head {
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 16px;
      padding: 14px 16px;
      border-bottom: 1px solid var(--line);
    }
    h2 { margin: 0; font-size: 17px; letter-spacing: 0; }
    .count { color: var(--muted); font-size: 12px; white-space: nowrap; }
    dl {
      display: grid;
      grid-template-columns: 180px 1fr;
      gap: 8px 14px;
      margin: 0;
      padding: 16px;
    }
    dt { color: var(--muted); }
    dd { margin: 0; min-width: 0; overflow-wrap: anywhere; }
    pre {
      margin: 0;
      padding: 18px;
      overflow: auto;
      white-space: pre-wrap;
      word-break: break-word;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 12.5px;
      line-height: 1.55;
      background: var(--code-bg);
      color: var(--code-ink);
    }
    .prompt pre { max-height: none; }
    .note {
      padding: 12px 16px;
      color: var(--muted);
      border-top: 1px solid var(--line);
      font-size: 13px;
    }
    .pill {
      display: inline-block;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 2px 8px;
      margin-right: 6px;
      color: var(--muted);
      background: #fff;
      font-size: 12px;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }
    th, td {
      text-align: left;
      vertical-align: top;
      border-bottom: 1px solid var(--line);
      padding: 10px 12px;
    }
    th {
      color: var(--muted);
      font-weight: 600;
      background: #fafbfc;
    }
    td { overflow-wrap: anywhere; }
    @media (max-width: 720px) {
      header, main { padding-left: 14px; padding-right: 14px; }
      dl { grid-template-columns: 1fr; }
      .section-head { display: block; }
      .count { margin-top: 4px; display: block; }
    }
    """
    rows = "\n".join(
        f"<dt>{_html_escape(k)}</dt><dd>{_html_escape(v)}</dd>"
        for k, v in [
            ("user_id", meta["user_id"]),
            ("user_data_dir", meta["user_data_dir"]),
            ("generated_at", meta["generated_at"]),
            ("trigger", meta["trigger"]),
            ("with_review_signals", meta["with_review_signals"]),
            ("llm helper", meta["llm_call"]["helper"]),
            ("tier", meta["llm_call"]["tier"]),
            ("reasoning", meta["llm_call"]["reasoning"]),
            ("temperature", meta["llm_call"]["temperature"]),
            ("max_tokens", meta["llm_call"]["max_tokens"]),
        ]
    )
    source_rows = [
        ("System soul/personality", "character.get_config().raw_text", "soul.md / active persona soul", "完整注入，不压缩"),
        ("Shared behavior core", "_load_agent_behavior_core_section()", "agent_behavior.md ## Core", "存在时附加"),
        ("Activity", "sleep_inference.infer_activity_state()", "screenshot_log/chat/device activity", "当前活跃/idle/作息窗口"),
        ("Conversation window", "storage.get_chat_history(limit=240) -> _build_conversation_window", "chat_history.json", "近 7 天 / 最多 80 条，标注 reply/proactive/care 与用户回应"),
        ("Proactive cadence", "_build_proactive_cadence(history)", "chat_history.json", "近 7 天主动次数、24h 次数、未回应数量、上次主动是否被回应"),
        ("Current episode", "storage.load_attention_state().current_episode", "attention_state.json", "连续关注主题、tick_count、speak_count、last_thought"),
        ("Speak intent queue", "storage.load_attention_intent_queue()", "attention_intent_queue.json", "待交付但未发送的 speak_intent，Phase 1 仅记录"),
        ("Message distances", "_message_distances(history)", "chat_history.json", "距用户/assistant/主动消息多久"),
        ("New signals", "AttentionEngine._signals deque", "内存中的 screenshot/chat/state signals", "最近 1 小时，最多 40 条"),
        ("Inner history", "storage.get_recent_attention_log(limit=30)", "attention_log.json", "最近 30 条 tick"),
        ("User affect", "storage.get_today_emotion_log()", "emotion_log.json", "当天情绪旧状态与 3h bucket"),
        ("Miru emotion", "miru_emotion.get_instance().get_state()", "miru_emotion.json", "Miru 自己旧状态"),
        ("Human/persona blocks", "core_memory.get_all_blocks()", "core_memory.json", "仅 account_manifest.json 绑定当前 user_id 后注入"),
        ("Identity facts", "identity.compose_ground_truth_block(header=False)", "memory/self/identity/main.md 或 identity.json fallback", "权威身份事实"),
        ("Commitments", "_format_relevant_commitments(now)", "memory/commitments/active.md", "只注入未完成、当前相关、有 deadline 的 DDL；很旧逾期项会跳过"),
    ]
    source_table = "\n".join(
        "<tr>"
        f"<td>{_html_escape(name)}</td>"
        f"<td>{_html_escape(call)}</td>"
        f"<td>{_html_escape(src)}</td>"
        f"<td>{_html_escape(note)}</td>"
        "</tr>"
        for name, call, src, note in source_rows
    )
    out_path.write_text(f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AttentionEngine Prompt Review</title>
  <style>{style}</style>
</head>
<body>
  <header>
    <h1>AttentionEngine Prompt Review</h1>
    <div class="sub">
      <span class="pill">完整未压缩</span>
      <span class="pill">不调用 LLM</span>
      <span class="pill">messages[0]=system / messages[1]=user</span>
    </div>
  </header>
  <main>
    <section>
      <div class="section-head">
        <h2>调用参数</h2>
        <span class="count">这就是 AttentionEngine._evaluate() 的目标调用形态</span>
      </div>
      <dl>{rows}</dl>
      <div class="note">如果启用了 review signals，它们只存在于本次 dump 的内存对象里，不会写入用户数据。</div>
    </section>

    <section class="prompt">
      <div class="section-head">
        <h2>messages[0] role=system</h2>
        <span class="count">{len(system_prompt)} chars / {system_prompt.count(chr(10)) + 1} lines</span>
      </div>
      <pre>{_html_escape(system_prompt)}</pre>
    </section>

    <section class="prompt">
      <div class="section-head">
        <h2>messages[1] role=user</h2>
        <span class="count">{len(user_prompt)} chars / {user_prompt.count(chr(10)) + 1} lines</span>
      </div>
      <pre>{_html_escape(user_prompt)}</pre>
    </section>

    <section>
      <div class="section-head">
        <h2>上下文来源表</h2>
        <span class="count">每个 snapshot 模块实际读哪里</span>
      </div>
      <table>
        <thead>
          <tr><th>模块</th><th>代码入口</th><th>数据来源</th><th>备注</th></tr>
        </thead>
        <tbody>{source_table}</tbody>
      </table>
    </section>

    <section>
      <div class="section-head">
        <h2>完整调用 JSON 示例</h2>
        <span class="count">包含完整 messages 数组，不是摘要</span>
      </div>
      <pre>{_html_escape(messages_json)}</pre>
    </section>

    <section>
      <div class="section-head">
        <h2>Raw Snapshot JSON</h2>
        <span class="count">AttentionEngine._build_snapshot() 原始结构</span>
      </div>
      <pre>{_html_escape(raw_snapshot)}</pre>
    </section>
  </main>
</body>
</html>
""", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Dump the exact AttentionEngine system/user prompts without calling the LLM."
    )
    parser.add_argument("--user-id", help="User id under data/users/. Required unless --first-local-user is set.")
    parser.add_argument(
        "--first-local-user",
        action="store_true",
        help="Local-dev convenience only: auto-pick the first engaged/active local user. Do not use for account-specific review.",
    )
    parser.add_argument(
        "--trigger",
        default="review_dump",
        help="Snapshot trigger label to render. Default: review_dump.",
    )
    parser.add_argument(
        "--with-review-signals",
        action="store_true",
        help="Inject two non-persistent review signals so the prompt shows screenshot/chat signal formatting.",
    )
    parser.add_argument(
        "--output",
        help="Output path. Default: data/_admin/attention_prompt_review_<timestamp>.<format>",
    )
    parser.add_argument(
        "--format",
        choices=("md", "html"),
        default="md",
        help="Output format. Default: md.",
    )
    args = parser.parse_args()

    import auth
    from attention_engine import (
        AttentionEngine,
        _build_attention_system_prompt,
        build_attention_snapshot_prompt,
    )

    user_id = _choose_user_id(args.user_id, allow_first_local_user=args.first_local_user)
    user_data_dir = auth.get_user_data_dir(user_id)
    if not os.path.isdir(user_data_dir):
        raise SystemExit(f"User data dir does not exist: {user_data_dir}")
    auth.ensure_account_manifest(user_id)

    app = Flask("attention_prompt_dump")
    with app.app_context():
        g.user_id = user_id
        g.user_data_dir = user_data_dir
        g.is_admin = False

        engine = AttentionEngine(user_id=user_id, user_data_dir=user_data_dir)
        if args.with_review_signals:
            _add_review_signals(engine)

        snapshot = engine._build_snapshot(trigger=args.trigger)
        system_prompt = _build_attention_system_prompt()
        user_prompt = build_attention_snapshot_prompt(snapshot)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    meta = _metadata(
        user_id=user_id,
        user_data_dir=user_data_dir,
        trigger=args.trigger,
        with_review_signals=args.with_review_signals,
    )
    out_path = Path(args.output) if args.output else ROOT / "data" / "_admin" / f"attention_prompt_review_{ts}.{args.format}"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if args.format == "html":
        _write_html(out_path, meta=meta, system_prompt=system_prompt,
                    user_prompt=user_prompt, snapshot=snapshot)
    else:
        _write_markdown(out_path, meta=meta, system_prompt=system_prompt,
                        user_prompt=user_prompt, snapshot=snapshot)
    print(out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
