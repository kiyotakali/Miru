"""Sleep-time Agent v3 — background memory consolidation between conversations.

Inspired by Letta's Sleep-time Compute: a dedicated agent that runs in the
background, looking at recent conversation turns and turning them into:
  · slot writes (Slot Writer → Pass 4 AppendEditor per matched slot)
  · persona / human block updates (Persona Writer, accumulated every N batches)
  · commitment-completion detections

The chat agent focuses on conversation quality (real-time, with chat_history
already in its prompt — so it never depends on the sleep agent's freshness).
The sleep agent focuses on memory quality, runs purely in the background, and
intentionally has a debounce delay so it sees full conversation context, not
single-message fragments.

Pipeline (v3, 2026-05-13):
    enqueue(role, text)
      → debounce 3 min  (or 12-msg cap)
      → snapshot batch
      → Slot Writer (every batch)
            output: slot_writes (kind=match/new), completed_commitments
            each matched slot_write → route_with_slot_write → Pass 4 AppendEditor
      → persona_writer_state.record_batch (accumulate)
      → if should_run (every N=4 batches) → Persona Writer
            output: human_update, persona_update
"""

from __future__ import annotations

import threading
from datetime import datetime

import core_memory
import memory


# Debounce config
DEBOUNCE_SECONDS = 180   # 3 minutes after last message
MAX_QUEUE_SIZE = 12       # force flush at this many messages
MAX_RETRIES = 3           # max times a failed batch is re-queued


class SleepAgent:
    """Queues chat messages, then batch-processes them into memory updates.

    Each instance is bound to ONE user — captured from the Flask request context
    at construction time. The background flush thread re-establishes that user's
    context so memory / core_memory reads hit the right user's data dir.
    """

    def __init__(self, user_id: str | None = None, user_data_dir: str | None = None):
        # Capture user binding from the Flask context at construction time.
        if user_id is None:
            try:
                from flask import g
                user_id = getattr(g, "user_id", "_admin")
            except (RuntimeError, ImportError):
                user_id = "_admin"
        self._user_id = user_id
        if user_data_dir is None and user_id != "_admin":
            try:
                import auth as _auth
                user_data_dir = _auth.get_user_data_dir(user_id)
            except Exception:
                user_data_dir = None
        self._user_data_dir = user_data_dir

        self._queue: list[dict] = []
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._running = False

    def _push_user_context(self):
        """Push Flask app context with this user's g bindings."""
        if not self._user_data_dir:
            return None
        try:
            import app as _app_mod
            ctx = _app_mod.app.app_context()
            ctx.push()
            from flask import g
            g.user_id = self._user_id
            g.user_data_dir = self._user_data_dir
            g.is_admin = (self._user_id == "_admin")
            return ctx
        except Exception:
            return None

    def enqueue(self, role: str, text: str, timestamp: str | None = None,
                image: str | None = None):
        """Add a chat message to the processing queue.

        Args:
            role: 'user' or 'assistant'
            text: Message content (may be empty if there's an image)
            timestamp: ISO timestamp (default: now)
            image: optional uploaded image filename (resolved via
                   storage._uploads_dir() at flush time). If both text and
                   image are empty the message is dropped.
        """
        # Allow image-only messages (text may be empty if user just sent a pic)
        text = (text or "").strip()
        if not text and not image:
            return

        ts = timestamp or datetime.now().isoformat()[:19]
        entry = {"role": role, "text": text, "time": ts}
        if image:
            entry["image"] = image
        with self._lock:
            self._queue.append(entry)
            queue_len = len(self._queue)

        self._reset_timer()

        if queue_len >= MAX_QUEUE_SIZE:
            self._cancel_timer()
            self._flush_async()

    def _reset_timer(self):
        self._cancel_timer()
        self._timer = threading.Timer(DEBOUNCE_SECONDS, self._flush_async)
        self._timer.daemon = True
        self._timer.start()

    def _cancel_timer(self):
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _flush_async(self):
        t = threading.Thread(target=self._do_flush, daemon=True)
        t.start()

    def _do_flush(self):
        with self._lock:
            if not self._queue:
                return
            if self._running:
                return
            self._running = True
            batch = list(self._queue)
            self._queue.clear()

        # Restore this user's Flask context so memory/core_memory writes go to
        # the correct user's data dir.
        ctx = self._push_user_context()
        try:
            self._process_batch(batch)
        except Exception as e:
            print(f"[SleepAgent] Error for {self._user_id}: {e}")
            # Re-queue with retry counter; drop if max retries exceeded
            retries = batch[0].get("_retries", 0) + 1 if batch else 1
            if retries <= MAX_RETRIES:
                for msg in batch:
                    msg["_retries"] = retries
                with self._lock:
                    self._queue = batch + self._queue
                print(f"[SleepAgent] Re-queued batch (retry {retries}/{MAX_RETRIES})")
            else:
                print(f"[SleepAgent] Dropping batch after {MAX_RETRIES} retries")
        finally:
            if ctx is not None:
                try:
                    ctx.pop()
                except Exception:
                    pass
            with self._lock:
                self._running = False

    def _process_batch(self, messages: list[dict]):
        """v3 batch handler:
          Step 1: Slot Writer (every batch) → slot_writes → Pass 4 AppendEditor
          Step 2: persona_writer_state.record_batch (累计)
          Step 3: if should_run → Persona Writer (async)
        """
        if not messages:
            return

        memory.ensure_dirs()

        try:
            import identity
            import memory_router
            import persona_writer_state as pws
            from memory_prompts_v3 import call_slot_writer
        except Exception as e:
            print(f"[SleepAgent] import failed: {e}; batch dropped")
            return

        # Gather Slot Writer inputs
        user_name = (identity.get_user_name() or "").strip() or "用户"
        identity_gt = ""
        try:
            identity_gt = identity.compose_ground_truth_block()
        except Exception:
            pass

        # Slot index: 4 domains, top 30 by last_active
        slot_index = {}
        try:
            for domain in ("project", "person", "topic", "self"):
                slots = memory_router.load_active_slots(domain) or []
                slots.sort(key=lambda s: (s.get("last_active") or ""), reverse=True)
                slot_index[domain] = [
                    {
                        "id": s.get("id"),
                        "title": s.get("title"),
                        "summary": s.get("summary"),
                        "aliases": s.get("aliases", []),
                        "last_active": s.get("last_active"),
                    }
                    for s in slots[:30]
                ]
        except Exception as e:
            print(f"[SleepAgent] slot_index load failed: {e}")
            slot_index = {"project": [], "person": [], "topic": [], "self": []}

        active_commitments = []
        try:
            active_md = memory.read_file("commitments/active.md") or ""
            for line in active_md.split("\n"):
                stripped = line.strip()
                if not stripped.startswith("- [ ]"):
                    continue
                body = stripped[6:].strip() if len(stripped) > 6 else stripped
                for marker in [" (deadline:", "  [added:", " --"]:
                    if marker in body:
                        body = body.split(marker)[0]
                if body:
                    active_commitments.append(body.strip())
        except Exception:
            pass

        current_time = datetime.now().isoformat(timespec="seconds")
        print(f"[SleepAgent] Processing {len(messages)} messages...")

        # ─── Step 1: Slot Writer ───
        slot_result = call_slot_writer(
            user_name=user_name,
            identity_ground_truth=identity_gt,
            slot_index=slot_index,
            active_commitments=active_commitments,
            messages=messages,
            current_time=current_time,
        )

        slot_writes_dispatched = []
        completed_titles = []
        if slot_result is None:
            print("[SleepAgent] Slot Writer retry exhausted; "
                  "batch dropped (per design: no fallback)")
        else:
            completed_titles = slot_result.completed_commitments or []
            # 2026-05-16: chat 路径不再给 Pass 4 喂对话原文. Slot Writer 已经把
            # 本 batch 压成 content_to_integrate, Pass 4 信它即可 — 不需要回头
            # 看原文判断模糊/反话, 那是 Slot Writer 的职责.
            source_context = f"(chat batch flushed at {current_time})"

            for sw in slot_result.slot_writes:
                try:
                    r = memory_router.route_with_slot_write(sw, source_context)
                    if r.get("ok"):
                        slot_writes_dispatched.append({
                            "domain": sw.domain,
                            "slot_id": r.get("slot_id"),
                            "kind": sw.kind,
                            "summary": (sw.new_slot_meta.summary
                                         if sw.new_slot_meta else ""),
                        })
                        # SSE broadcast
                        try:
                            import sse
                            sse.broadcast("memory_changed", {
                                "domain": sw.domain,
                                "slot_id": r.get("slot_id"),
                                "action": r.get("action"),
                            }, user_id=self._user_id)
                        except Exception:
                            pass
                    else:
                        print(f"[SleepAgent] slot_write failed: "
                              f"{r.get('reason')}")
                except Exception as e:
                    print(f"[SleepAgent] slot_write exception: {e}")

        # ─── Auto-complete commitments ───
        completed_count = 0
        if completed_titles:
            try:
                from tools.complete_commitment import CompleteCommitmentTool
                tool = CompleteCommitmentTool()
                for title in completed_titles:
                    if not title or not isinstance(title, str):
                        continue
                    r = tool.execute({"title": title})
                    if r.get("status") == "ok":
                        completed_count += 1
                        print(f"[SleepAgent] Auto-completed: {title}")
                if completed_count > 0:
                    try:
                        from core import _broadcast_commitment_sync
                        _broadcast_commitment_sync()
                    except Exception:
                        pass
            except Exception as e:
                print(f"[SleepAgent] commitment tool failed: {e}")

        # ─── Step 2: record batch in persona_writer_state ───
        try:
            pws.record_batch(messages, slot_writes_dispatched)
        except Exception as e:
            print(f"[SleepAgent] persona_writer_state.record_batch failed: {e}")

        # ─── Step 3: maybe trigger Persona Writer ───
        if pws.should_run():
            self._trigger_persona_writer_async()

        print(
            f"[SleepAgent] Done: {len(slot_writes_dispatched)} slot_writes, "
            f"{completed_count} commitments completed"
        )

    def _trigger_persona_writer_async(self):
        """Fire Persona Writer in a background thread.

        Captures user context so the worker thread can re-push Flask g.
        Failure (validation / retry exhausted) clears pending state too
        (option X — don't let bad data retry forever).
        """
        user_id = self._user_id
        user_data_dir = self._user_data_dir

        def _run():
            ctx = None
            if user_data_dir:
                try:
                    import app as _app_mod
                    ctx = _app_mod.app.app_context()
                    ctx.push()
                    from flask import g as _g
                    _g.user_id = user_id
                    _g.user_data_dir = user_data_dir
                    _g.is_admin = (user_id == "_admin")
                except Exception as e:
                    print(f"[PersonaWriter] ctx push failed: {e}")

            try:
                import identity
                import persona_writer_state as pws
                from memory_prompts_v3 import call_persona_writer

                meta = pws.load_meta()
                user_name = (identity.get_user_name() or "").strip() or "用户"
                identity_gt = ""
                try:
                    identity_gt = identity.compose_ground_truth_block()
                except Exception:
                    pass

                # Read current human / persona blocks
                try:
                    blocks = core_memory.get_all_blocks()
                    current_human = blocks.get("human", "") or ""
                    current_persona = blocks.get("persona", "") or ""
                except Exception:
                    current_human = ""
                    current_persona = ""

                dialog_buffer = pws.concat_dialogs_for_llm(meta)
                proactive_outcomes = pws.concat_proactive_outcomes_for_llm(meta)
                new_slot_summaries = meta.get("pending_new_slots", [])
                current_time = datetime.now().isoformat(timespec="seconds")

                print(f"[PersonaWriter] running on "
                      f"{meta.get('batches_since_last_run', 0)} batches, "
                      f"{len(new_slot_summaries)} new slots, "
                      f"{len(meta.get('pending_proactive_outcomes', []) or [])} proactive events")

                result = call_persona_writer(
                    user_name=user_name,
                    identity_ground_truth=identity_gt,
                    current_human=current_human,
                    current_persona=current_persona,
                    new_slot_summaries=new_slot_summaries,
                    dialog_buffer=dialog_buffer,
                    proactive_outcomes=proactive_outcomes,
                    current_time=current_time,
                )

                if result is None:
                    print("[PersonaWriter] retry exhausted; clearing pending "
                          "state (per design: no fallback)")
                    pws.reset_after_run(success=False)
                    return

                # Apply updates
                def _apply_block_update(label, update):
                    if not update:
                        return
                    try:
                        if update.action == "append" and update.content:
                            r = core_memory.append(label, update.content)
                            if r.get("ok"):
                                print(f"[PersonaWriter] {label} appended "
                                      f"+{len(update.content)} chars")
                            else:
                                print(f"[PersonaWriter] {label} append failed: "
                                      f"{r.get('error')}")
                        elif update.action == "replace":
                            old = (update.old_text or "").strip()
                            if old:
                                r = core_memory.replace(label, old,
                                                         update.content)
                                if r.get("ok"):
                                    print(f"[PersonaWriter] {label} replaced ok")
                                else:
                                    print(f"[PersonaWriter] {label} replace "
                                          f"failed: {r.get('error')}")
                    except Exception as e:
                        print(f"[PersonaWriter] {label} apply failed: {e}")

                _apply_block_update("human", result.human_update)
                _apply_block_update("persona", result.persona_update)

                pws.reset_after_run(success=True)
                print(
                    f"[PersonaWriter] Done: "
                    f"human={'updated' if result.human_update else 'unchanged'}, "
                    f"persona={'updated' if result.persona_update else 'unchanged'}"
                )

            except Exception as e:
                print(f"[PersonaWriter] exception: {e}")
                try:
                    import persona_writer_state as pws
                    pws.reset_after_run(success=False)
                except Exception:
                    pass
            finally:
                if ctx is not None:
                    try:
                        ctx.pop()
                    except Exception:
                        pass

        threading.Thread(target=_run, daemon=True,
                          name="persona_writer").start()

    def _apply_index_updates(self, updates: list[dict]):
        lines = memory.read_index().split("\n")
        for upd in updates:
            section = upd.get("section", "")
            new_line = upd.get("line", "")
            if not section or not new_line:
                continue
            if new_line in lines:
                continue
            section_header = f"## {section}"
            inserted = False
            for i, line in enumerate(lines):
                if line.strip() == section_header:
                    insert_pos = i + 1
                    while insert_pos < len(lines) and lines[insert_pos].strip() == "":
                        insert_pos += 1
                    lines.insert(insert_pos, new_line)
                    inserted = True
                    break
            if not inserted:
                lines.append(f"\n## {section}")
                lines.append(new_line)
        memory.update_index("\n".join(lines))

    def flush_sync(self):
        self._cancel_timer()
        self._do_flush()

    def pending_count(self) -> int:
        with self._lock:
            return len(self._queue)


# Singleton
_instances: dict[str, SleepAgent] = {}


def _current_user_id() -> str:
    try:
        from flask import g
        return getattr(g, "user_id", "_admin")
    except (RuntimeError, ImportError):
        return "_admin"


def get_sleep_agent() -> SleepAgent:
    uid = _current_user_id()
    if uid not in _instances:
        data_dir = None
        if uid != "_admin":
            try:
                import auth as _auth
                data_dir = _auth.get_user_data_dir(uid)
            except Exception:
                data_dir = None
        _instances[uid] = SleepAgent(user_id=uid, user_data_dir=data_dir)
    return _instances[uid]
