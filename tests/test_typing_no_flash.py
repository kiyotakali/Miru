"""Static guard for the cross-cycle typing-indicator flash fix (2026-05-09).

Bug symptom: When the user sent a follow-up message during an in-progress
LLM generation, the main chat window's "Miru 正在输入..." indicator would
flash off→on briefly. The dock pet was unaffected because it has an
optimistic typing lock.

Root cause: _csm_do_full_reply's finally block UNCONDITIONALLY broadcast
typing_stop and cleared _csm_typing, even though _csm_try_deliver right
after would see pending non-empty and start a new cycle (which sets
_csm_typing=True ~10ms later). Two SSE events out: typing_stop, then
typing_start — the main window's header override toggled visibly.

Fix: move the clear + broadcast into _csm_try_deliver, on the
pending-empty branch only. The outer except in _csm_do_full_reply still
emits an emergency typing_stop for the crash case.
"""
import os


def _read_core() -> str:
    full = os.path.join(os.path.dirname(__file__), "..", "core.py")
    with open(full, "r", encoding="utf-8") as f:
        return f.read()


def _slice_function(src: str, fn_name: str) -> str:
    """Return source of a top-level function. Plain string scan, no regex."""
    needle = f"\ndef {fn_name}("
    start = src.find(needle)
    if start < 0:
        return ""
    start += 1  # skip leading newline
    # Find next top-level "def " — naive scan for newline + "def "
    nxt = src.find("\ndef ", start + 1)
    return src[start:nxt] if nxt > 0 else src[start:]


def _slice_pending_branch(deliver_src: str) -> str:
    """Slice from `if pending:` down to the next non-pending-branch line.
    Returns empty string if not found.
    """
    idx = deliver_src.find("if pending:")
    if idx < 0:
        return ""
    # End at the next line that starts with "        # Pending" or
    # "        try:" (indent level 8 = the if/else level after the if pending block).
    # Use the next standalone `try:` at 8-space indent, OR the comment marker.
    end_markers = [
        "\n        # Pending is empty",
        "\n        # Pending empty",
        "\n        try:\n            storage.append_chat_message",
    ]
    end = len(deliver_src)
    for marker in end_markers:
        m = deliver_src.find(marker, idx)
        if m >= 0 and m < end:
            end = m
    return deliver_src[idx:end]


def test_typing_no_cross_cycle_flash():
    print("=" * 60)
    print("TEST: typing indicator no flash across batched generation cycles")
    print("=" * 60)
    src = _read_core()
    full_reply = _slice_function(src, "_csm_do_full_reply")
    deliver = _slice_function(src, "_csm_try_deliver")
    passed, total = 0, 0

    total += 1
    if full_reply and deliver:
        print("  [PASS] both target functions located")
        passed += 1
    else:
        print("  [FAIL] could not locate _csm_do_full_reply or _csm_try_deliver")
        assert False

    # 1. Inner finally in _csm_do_full_reply must be a no-op (no typing
    # mutation). The fix replaces the body with `pass` + comment.
    # Heuristic: find each `finally:` in _csm_do_full_reply and check the
    # NEXT 800 chars (the block body).
    bad_finally = []
    pos = 0
    finally_count = 0
    while True:
        idx = full_reply.find("\n        finally:\n", pos)
        if idx < 0:
            break
        finally_count += 1
        body = full_reply[idx:idx + 800]
        # Stop the body at the next non-indented line (next def / outer except).
        # This is good-enough; we only check for forbidden tokens.
        if "_csm_typing[user_id] = False" in body[:600]:
            # Could legitimately appear in outer-except crash safety net which
            # is NOT a finally. Constrain by checking it's between this finally
            # and the next dedent (a line not starting with whitespace).
            # Find the next blank line or comment at 0 indent.
            sub_end = body.find("\n        # --- Deliver")  # outer marker
            if sub_end < 0:
                sub_end = 600
            sub_body = body[:sub_end]
            if "_csm_typing[user_id] = False" in sub_body:
                bad_finally.append("clears _csm_typing")
            if 'broadcast("typing_stop"' in sub_body:
                bad_finally.append("broadcasts typing_stop")
        pos = idx + 1

    total += 1
    if finally_count > 0 and not bad_finally:
        print(f"  [PASS] inner finally(s) in _csm_do_full_reply are no-op (no typing mutation)")
        passed += 1
    else:
        print(f"  [FAIL] finally count={finally_count}, forbidden tokens: {bad_finally}")

    # 2. _csm_try_deliver MUST clear _csm_typing somewhere on the deliver path.
    total += 1
    if "_csm_typing[user_id] = False" in deliver:
        print("  [PASS] _csm_try_deliver clears _csm_typing on deliver")
        passed += 1
    else:
        print("  [FAIL] _csm_try_deliver does NOT clear _csm_typing")

    # 3. _csm_try_deliver MUST broadcast typing_stop on the deliver path.
    total += 1
    if 'broadcast("typing_stop"' in deliver:
        print("  [PASS] _csm_try_deliver broadcasts typing_stop on deliver")
        passed += 1
    else:
        print("  [FAIL] _csm_try_deliver does NOT broadcast typing_stop")

    # 4. The "pending non-empty" branch must NOT clear _csm_typing or broadcast typing_stop.
    pending_branch = _slice_pending_branch(deliver)
    total += 1
    if pending_branch:
        clears = "_csm_typing[user_id] = False" in pending_branch
        broadcasts = 'broadcast("typing_stop"' in pending_branch
        if not clears and not broadcasts:
            print("  [PASS] cross-cycle (pending non-empty) branch keeps typing True (no flash)")
            passed += 1
        else:
            print(f"  [FAIL] cross-cycle branch unexpectedly mutates: clears={clears} broadcasts={broadcasts}")
    else:
        print("  [FAIL] could not locate 'if pending:' branch in _csm_try_deliver")

    # 5. Outer except crash-safety net must still broadcast typing_stop.
    total += 1
    crit_idx = full_reply.find('print(f"[Chat] CRITICAL')
    stop_idx = full_reply.rfind('broadcast("typing_stop"')  # last one (in outer except)
    if crit_idx > 0 and stop_idx > crit_idx:
        print("  [PASS] outer except still broadcasts emergency typing_stop on crash")
        passed += 1
    else:
        print(f"  [FAIL] crash-safety typing_stop missing (crit={crit_idx}, stop={stop_idx})")

    # 6. In _csm_try_deliver: chat_message broadcast precedes typing_stop.
    total += 1
    chat_pos = deliver.find('broadcast("chat_message"')
    stop_pos = deliver.find('broadcast("typing_stop"')
    if 0 < chat_pos < stop_pos:
        print("  [PASS] _csm_try_deliver broadcasts chat_message BEFORE typing_stop")
        passed += 1
    else:
        print(f"  [FAIL] broadcast order wrong: chat_message at {chat_pos}, typing_stop at {stop_pos}")

    print(f"\n  TypingNoFlash: {passed}/{total} passed")
    assert passed == total, f"typing-flash regression: {passed}/{total}"


if __name__ == "__main__":
    test_typing_no_cross_cycle_flash()
