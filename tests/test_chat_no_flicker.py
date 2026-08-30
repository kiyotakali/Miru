"""Static guard for the no-flicker chat view UX (2026-05-09).

The `showCompanionChat()` function used to ALWAYS do a full DOM rebuild on
view re-entry, which made every tab-switch flash the entire 200-message
history. After this fix the function picks between a full reload (safety
net) and a silent incremental refresh (default — keeps DOM, no flicker)
based on whether: (a) it's the first entry, (b) SSE had a disconnect since
last view, or (c) >30min has passed since the last full reload.

This test enforces that the relevant variables, conditions, and call-site
remain wired up. If any of these checks fail you've likely regressed back
to the always-rebuild behavior.
"""
import os


def test_chat_view_no_flicker_machinery():
    print("=" * 60)
    print("TEST: showCompanionChat no-flicker machinery")
    print("=" * 60)
    passed, total = 0, 0

    path = os.path.join(os.path.dirname(__file__), "..", "templates", "index.html")
    with open(path, "r", encoding="utf-8") as f:
        html = f.read()

    # 1. State variables exist
    total += 1
    if "_sseHadDisconnect" in html and "_lastFullChatReloadTs" in html:
        print("  [PASS] disconnect flag + last-reload timestamp declared")
        passed += 1
    else:
        print("  [FAIL] state variables missing")

    # 2. Staleness window constant present and ≥ 5 min (sanity)
    total += 1
    if "_CHAT_FULL_RELOAD_INTERVAL = 30 * 60 * 1000" in html:
        print("  [PASS] staleness window = 30 min")
        passed += 1
    else:
        print("  [FAIL] _CHAT_FULL_RELOAD_INTERVAL changed or missing")

    # 3. SSE onerror sets the disconnect flag (gated by previously-connected)
    total += 1
    if "if (_sseConnected) _sseHadDisconnect = true" in html:
        print("  [PASS] SSE onerror gates _sseHadDisconnect on prior connection")
        passed += 1
    else:
        print("  [FAIL] _sseHadDisconnect not set in SSE onerror (gated)")

    # 4. Full-reload path stamps _lastFullChatReloadTs
    total += 1
    if "_lastFullChatReloadTs = Date.now();" in html:
        print("  [PASS] full reload stamps _lastFullChatReloadTs")
        passed += 1
    else:
        print("  [FAIL] timestamp not stamped after full reload")

    # 5. showCompanionChat decision logic uses all three triggers
    total += 1
    needs_check = ("var needsFullReload = !companionChatLoaded" in html
                   and "_sseHadDisconnect" in html
                   and "_CHAT_FULL_RELOAD_INTERVAL" in html)
    if needs_check:
        print("  [PASS] showCompanionChat checks (firstEntry || disconnect || stale)")
        passed += 1
    else:
        print("  [FAIL] needsFullReload composition missing one or more triggers")

    # 6. Silent branch exists and resets disconnect flag after the gate
    total += 1
    if ("loadCompanionChatHistory({ silent: true });" in html
            and "_sseHadDisconnect = false;" in html):
        print("  [PASS] silent branch + disconnect flag reset after entry")
        passed += 1
    else:
        print("  [FAIL] silent branch or flag reset missing")

    # 7. Regression guard: the OLD "Always full reload" comment must not exist
    total += 1
    if "Always full reload + scroll to bottom when opening/reopening" not in html:
        print("  [PASS] old 'Always full reload' design comment removed")
        passed += 1
    else:
        print("  [FAIL] old design comment still in place — flicker likely back")

    # 8. Bootstrap path remains (full reload on app startup with prefetched data)
    total += 1
    if "loadCompanionChatHistory({ _bootstrapData: bootstrapChatData })" in html:
        print("  [PASS] bootstrap full-reload path preserved")
        passed += 1
    else:
        print("  [FAIL] bootstrap path missing — first launch broken")

    print(f"\n  ChatNoFlicker: {passed}/{total} passed")
    assert passed == total, f"chat no-flicker: {passed}/{total}"


if __name__ == "__main__":
    test_chat_view_no_flicker_machinery()
