"""Static guards for the 2026-05-09 P0+P1 security fixes.

These were 6 real vulnerabilities verified end-to-end against the live VPS
at the time of audit. The fixes are very small (one-line guards in most
cases), which makes accidental regressions very easy. This file pins each
fix in place by grepping for the exact code pattern that has to remain.

If any of these tests fail, you've likely re-opened a known-exploited hole.

Coverage:
  Bug #1 — /api/device/qr returns the CURRENT user's token, not admin token
  Bug #2 — /api/admin/* requires the admin token (no localhost bypass via nginx)
  Bug #3 — /api/ai/config POST + /api/ai/ping POST gated to admin
  Bug #4 — _cleanup_user_singletons stops CareEngine + cancels SleepAgent timer
  Bug #5 — auth.delete_user disconnects the user's SSE streams
  Bug #6 — model write endpoints (upload/download/rename/delete/active/...) gated
"""
import os


def _read(path: str) -> str:
    full = os.path.join(os.path.dirname(__file__), "..", path)
    with open(full, "r", encoding="utf-8") as f:
        return f.read()


# ===========================================================================

def test_bug2_admin_route_no_local_bypass():
    """auth.check_request must NOT auto-promote local requests to admin.

    Pre-fix, a `if is_local_request(req): _set_admin_context()` branch lived
    inside the `/api/admin/*` block. Combined with nginx not forwarding
    X-Real-IP (every public request looked like 127.0.0.1 to Flask), this
    made the entire admin API publicly readable WITHOUT any token.
    """
    auth = _read("auth.py")
    passed, total = 0, 0

    # Locate the admin block. We accept BOTH `path.startswith("/api/admin/")`
    # and the `if path == "/api/admin/auth/verify":` line as anchors.
    admin_idx = auth.find('path.startswith("/api/admin/")')
    total += 1
    if admin_idx > 0:
        print("  [PASS] /api/admin/ block located")
        passed += 1
    else:
        print("  [FAIL] /api/admin/ block missing")
        return passed, total

    # The next ~700 chars of the admin block must NOT contain
    # `is_local_request` — that was the bypass. We also explicitly check
    # the literal that called _set_admin_context after is_local_request.
    block = auth[admin_idx:admin_idx + 2000]
    total += 1
    bypass_pat = "if is_local_request(req):\n            _set_admin_context()"
    if bypass_pat not in block:
        print("  [PASS] localhost bypass removed from admin block")
        passed += 1
    else:
        print("  [FAIL] localhost bypass still present (P0 #2 regression)")

    # And the explicit fix-marker comment is in the source so future readers
    # see WHY the bypass is gone.
    total += 1
    if "P0 fix" in block and "remote_addr=127.0.0.1" in block:
        print("  [PASS] fix-marker comment present (preserves intent)")
        passed += 1
    else:
        print("  [FAIL] fix-marker comment missing — bypass might come back")

    print(f"\n  Bug2: {passed}/{total} passed")
    assert passed == total, "Bug #2 regression"


# ===========================================================================

def test_bug3_ai_config_admin_only():
    """/api/ai/config POST and /api/ai/ping POST must require admin context.

    These write to data/_admin/ai_config.json — the GLOBAL host/api_key/model
    used by every user's chat. A malicious user could repoint the host to
    proxy all platform traffic.
    """
    app_py = _read("app.py")
    passed, total = 0, 0

    # Helper exists
    total += 1
    if "def _require_admin():" in app_py:
        print("  [PASS] _require_admin() helper defined")
        passed += 1
    else:
        print("  [FAIL] _require_admin() helper missing")

    # POST /api/ai/config — extract the function body and check guard is the
    # first statement after the route decorator.
    set_idx = app_py.find('@app.route("/api/ai/config", methods=["POST"])')
    total += 1
    if set_idx > 0 and "_require_admin()" in app_py[set_idx:set_idx + 600]:
        print("  [PASS] /api/ai/config POST gated by _require_admin()")
        passed += 1
    else:
        print("  [FAIL] /api/ai/config POST is NOT gated (P0 #3 regression)")

    # POST /api/ai/ping
    ping_idx = app_py.find('@app.route("/api/ai/ping", methods=["POST"])')
    total += 1
    if ping_idx > 0 and "_require_admin()" in app_py[ping_idx:ping_idx + 400]:
        print("  [PASS] /api/ai/ping POST gated by _require_admin()")
        passed += 1
    else:
        print("  [FAIL] /api/ai/ping POST is NOT gated")

    print(f"\n  Bug3: {passed}/{total} passed")
    assert passed == total, "Bug #3 regression"


# ===========================================================================

def test_bug1_device_qr_returns_user_token():
    """/api/device/qr must encode the CURRENT user's token, NOT admin token.

    Pre-fix, it called auth.get_or_create_token() — an alias for
    get_admin_token(). Any logged-in user could read the response and obtain
    admin privileges.
    """
    app_py = _read("app.py")
    passed, total = 0, 0

    qr_idx = app_py.find('@app.route("/api/device/qr"')
    total += 1
    if qr_idx > 0:
        print("  [PASS] /api/device/qr route located")
        passed += 1
    else:
        print("  [FAIL] /api/device/qr route missing")
        return passed, total

    body = app_py[qr_idx:qr_idx + 2500]

    # Strip the function docstring (which legitimately mentions the old call
    # for context) before checking whether the actual code still uses it.
    code_only = body
    # Remove triple-quoted strings (greedy match on first """... ... """)
    import re
    code_only = re.sub(r'"""[\s\S]*?"""', '', code_only)
    total += 1
    if "_auth.get_or_create_token()" not in code_only and "get_or_create_token()" not in code_only:
        print("  [PASS] no longer calls auth.get_or_create_token() (admin alias)")
        passed += 1
    else:
        print("  [FAIL] still calls get_or_create_token() — leaks admin token")

    total += 1
    if "_auth.get_user(uid)" in body or "_auth.get_user(user_id)" in body:
        print("  [PASS] looks up current user's token via auth.get_user()")
        passed += 1
    else:
        print("  [FAIL] does not look up per-user token")

    # auth.get_user() helper was added
    auth = _read("auth.py")
    total += 1
    if "def get_user(user_id: str)" in auth:
        print("  [PASS] auth.get_user(user_id) helper exists")
        passed += 1
    else:
        print("  [FAIL] auth.get_user() helper missing")

    print(f"\n  Bug1: {passed}/{total} passed")
    assert passed == total, "Bug #1 regression"


# ===========================================================================

def test_bug6_model_write_endpoints_admin_only():
    """All model write endpoints (POST/PATCH/DELETE) must require admin.

    Models are global shared assets (one Live2D bundle for all users); the UI
    tab is hidden but the API was world-writable. A malicious user could
    fill global disk with uploaded zips, change every user's active persona,
    or delete models.
    """
    app_py = _read("app.py")
    passed, total = 0, 0

    # Each tuple: (route declaration, expected window after declaration to find guard)
    write_endpoints = [
        ('@app.route("/api/models/active", methods=["POST"])', 600),
        ('@app.route("/api/models/download", methods=["POST"])', 400),
        ('@app.route("/api/models/<model_id>", methods=["PATCH"])', 400),
        ('@app.route("/api/models/<model_id>", methods=["DELETE"])', 300),
        ('@app.route("/api/models/upload", methods=["POST"])', 500),
        ('@app.route("/api/models/<model_id>/layout", methods=["POST"])', 500),
        ('@app.route("/api/models/<model_id>/soul", methods=["POST"])', 500),
        ('@app.route("/api/models/<model_id>/fields", methods=["POST"])', 400),
        ('@app.route("/api/models/<model_id>/avatar", methods=["POST"])', 600),
        ('@app.route("/api/models/<model_id>/auto-avatar", methods=["POST"])', 500),
    ]

    for decl, window in write_endpoints:
        idx = app_py.find(decl)
        total += 1
        if idx <= 0:
            print(f"  [SKIP] route not found: {decl[:60]}…")
            continue
        # Find function body start
        body = app_py[idx:idx + window]
        if "_require_admin()" in body:
            print(f"  [PASS] gated: {decl[40:80]}…")
            passed += 1
        else:
            print(f"  [FAIL] NOT gated: {decl[40:80]}…")

    print(f"\n  Bug6: {passed}/{total} passed")
    assert passed == total, "Bug #6 regression"


# ===========================================================================

def test_bug4_cleanup_stops_threads_and_timers():
    """auth._cleanup_user_singletons must stop CareEngine + cancel SleepAgent
    timer BEFORE pop'ing them from the _instances dict.

    Pre-fix, it only pop'd. The orphaned thread's stop_event stayed False,
    so it kept ticking every 60s and crashing on the deleted user_data_dir.
    """
    auth = _read("auth.py")
    passed, total = 0, 0

    cleanup_idx = auth.find("def _cleanup_user_singletons(")
    total += 1
    if cleanup_idx > 0:
        print("  [PASS] _cleanup_user_singletons function located")
        passed += 1
    else:
        print("  [FAIL] _cleanup_user_singletons missing")
        return passed, total

    # Look at function body. Window grew from 3000 to 4000 chars on
    # 2026-05-11 when ScreenSleepAgent.stop_for_user was added between
    # SleepAgent timer cancel and the pop loop. The function naturally
    # grows as new per-user singletons are registered; bump the window
    # when adding more cleanup hooks.
    body = auth[cleanup_idx:cleanup_idx + 4000]

    total += 1
    if "inst.stop()" in body and 'getattr(ce_mod, "_instances"' in body:
        print("  [PASS] CareEngine.stop() called before pop")
        passed += 1
    else:
        print("  [FAIL] CareEngine.stop() missing or not on _instances dict")

    total += 1
    if "inst._cancel_timer()" in body and 'getattr(sa_mod, "_instances"' in body:
        print("  [PASS] SleepAgent._cancel_timer() called before pop")
        passed += 1
    else:
        print("  [FAIL] SleepAgent._cancel_timer() missing")

    # 2026-05-16: ScreenSleepAgent removed. Screenshots now go through a
    # per-screenshot async fork in screen_analyzer (no per-user instance,
    # so nothing to stop/cancel). _cleanup_user_singletons no longer
    # touches screen_sleep_agent.

    # Order check: stop/cancel must come BEFORE the pop loop
    pop_idx = body.find("d.pop(user_id, None)")
    stop_idx = body.find("inst.stop()")
    cancel_idx = body.find("inst._cancel_timer()")
    total += 1
    if 0 < stop_idx < pop_idx and 0 < cancel_idx < pop_idx:
        print("  [PASS] stop/cancel all happen BEFORE the dict pop loop")
        passed += 1
    else:
        print("  [FAIL] stop/cancel ordering wrong (must run before pop)")

    print(f"\n  Bug4: {passed}/{total} passed")
    assert passed == total, "Bug #4 regression"


# ===========================================================================

def test_bug5_delete_user_disconnects_sse():
    """auth.delete_user must call sse.disconnect_user(user_id).

    suspend_user already does this; admin_api.delete_user already does this;
    only self-delete (POST /api/auth/account DELETE) used to leak — other
    devices kept streaming until heartbeat timeout.
    """
    auth = _read("auth.py")
    passed, total = 0, 0

    del_idx = auth.find("def delete_user(user_id: str")
    total += 1
    if del_idx > 0:
        print("  [PASS] delete_user function located")
        passed += 1
    else:
        print("  [FAIL] delete_user missing")
        return passed, total

    # Find the next `def ` after delete_user — that's the end of the function
    next_def = auth.find("\ndef ", del_idx + 1)
    body = auth[del_idx:next_def] if next_def > 0 else auth[del_idx:]

    total += 1
    if "sse.disconnect_user(user_id)" in body:
        print("  [PASS] delete_user calls sse.disconnect_user")
        passed += 1
    else:
        print("  [FAIL] delete_user does NOT disconnect SSE")

    print(f"\n  Bug5: {passed}/{total} passed")
    assert passed == total, "Bug #5 regression"


# ===========================================================================

def main():
    test_bug2_admin_route_no_local_bypass()
    test_bug3_ai_config_admin_only()
    test_bug1_device_qr_returns_user_token()
    test_bug6_model_write_endpoints_admin_only()
    test_bug4_cleanup_stops_threads_and_timers()
    test_bug5_delete_user_disconnects_sse()
    print("\n" + "=" * 60)
    print("All P0+P1 security guards in place.")
    print("=" * 60)


if __name__ == "__main__":
    main()
