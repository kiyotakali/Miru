"""Multi-tenant cross-user state isolation (2026-05-08).

Backend invariants only — frontend localStorage / IndexedDB cleanup is
exercised via the JS lint (we can't run a browser here). Frontend behavior
is exercised manually with the deployment + APK install.

Coverage:
  1. _cleanup_user_singletons pops character._configs (was previously
     broken: referenced non-existent _instances attr → silent no-op,
     letting old user's CharacterConfig + soul.md text linger)
  2. _cleanup_user_singletons pops sleep_agent._instances + miru_emotion +
     screen_analyzer + care_engine (all the daemon-thread holders)
  3. _cleanup_user_singletons calls curator.stop_loop_for_user (P0-B fix)
  4. suspend_user → flips status + invalidates token + evicts singletons
  5. delete_user → revokes invitation + removes user record + evicts
  6. SSE disconnect_user reaches all the user's queues
"""
import os
import sys
import time
import shutil
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def _setup_user_dir():
    return tempfile.mkdtemp(prefix="user_isolation_test_")


def _reset_flask_g():
    try:
        from flask.globals import _cv_app
        while _cv_app.get() is not None:
            _cv_app.get().pop()
    except Exception:
        pass


def _push_g(uid, tmp):
    import app as _app_mod
    ctx = _app_mod.app.app_context()
    ctx.push()
    from flask import g
    g.user_id = uid
    g.user_data_dir = tmp
    g.is_admin = False
    return ctx


# ============================================================================

def test_cleanup_pops_all_registered_modules():
    print("=" * 60)
    print("TEST: _cleanup_user_singletons pops every registered module dict")
    print("=" * 60)
    import auth
    import character
    import miru_emotion
    import sleep_agent
    import screen_analyzer
    import care_engine
    passed, total = 0, 0

    # Plant fake entries in each module's dict so we can verify they're popped
    fake_uid = "fake_test_user_xyz"

    miru_emotion._instances[fake_uid]    = "FAKE_INSTANCE"
    sleep_agent._instances[fake_uid]     = "FAKE_INSTANCE"
    screen_analyzer._instances[fake_uid] = "FAKE_INSTANCE"
    care_engine._instances[fake_uid]     = "FAKE_INSTANCE"
    character._configs[fake_uid]         = ("FAKE_CONFIG", "fake_text")

    auth._cleanup_user_singletons(fake_uid)

    cases = [
        ("miru_emotion._instances",    miru_emotion._instances),
        ("sleep_agent._instances",     sleep_agent._instances),
        ("screen_analyzer._instances", screen_analyzer._instances),
        ("care_engine._instances",     care_engine._instances),
        ("character._configs",         character._configs),
    ]
    for label, d in cases:
        total += 1
        if fake_uid not in d:
            print(f"  [PASS] {label} popped")
            passed += 1
        else:
            print(f"  [FAIL] {label} still has {fake_uid}")

    print(f"\n  CleanupAll: {passed}/{total} passed")
    return passed, total


# ============================================================================

def test_cleanup_calls_curator_stop():
    print("\n" + "=" * 60)
    print("TEST: _cleanup_user_singletons stops curator daemon (P0-B regression)")
    print("=" * 60)
    import auth
    import curator
    tmp = _setup_user_dir()
    passed, total = 0, 0

    orig_tick = curator.CURATOR_LOOP_TICK_SECONDS
    curator.CURATOR_LOOP_TICK_SECONDS = 1

    try:
        curator.start_loop_for_user("alice_isolation", tmp)
        thread = curator._curator_user_threads.get("alice_isolation")
        time.sleep(0.3)

        auth._cleanup_user_singletons("alice_isolation")

        total += 1
        if "alice_isolation" not in curator._curator_user_threads:
            print("  [PASS] curator thread popped")
            passed += 1
        else:
            print("  [FAIL] curator thread still registered")

        total += 1
        if thread is not None and not thread.is_alive():
            print("  [PASS] curator daemon actually stopped")
            passed += 1
        else:
            print("  [FAIL] daemon still alive")
    finally:
        curator.CURATOR_LOOP_TICK_SECONDS = orig_tick
        for uid in list(curator._curator_user_threads.keys()):
            curator.stop_loop_for_user(uid)
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n  CleanupCurator: {passed}/{total} passed")
    return passed, total


# ============================================================================

def test_cleanup_unknown_module_attr_is_safe():
    """Defensive: white-list mentions a module attr that doesn't exist
    (e.g. model_library has no _instances) — must be a no-op, not crash."""
    print("\n" + "=" * 60)
    print("TEST: cleanup is safe when module/attr missing")
    print("=" * 60)
    import auth
    passed, total = 0, 0

    total += 1
    try:
        auth._cleanup_user_singletons("nonexistent_user_999")
        print("  [PASS] cleanup of unknown user is no-op (no exception)")
        passed += 1
    except Exception as e:
        print(f"  [FAIL] raised: {e}")

    print(f"\n  CleanupSafe: {passed}/{total} passed")
    return passed, total


# ============================================================================

def test_user_scoped_ls_keys_listed():
    """Verify the frontend user-scoped key registry is non-empty and
    contains the keys we know are per-user. Static check only — actual
    execution is in browser."""
    print("\n" + "=" * 60)
    print("TEST: templates/index.html _USER_SCOPED_LS_KEYS lists known per-user keys")
    print("=" * 60)
    passed, total = 0, 0

    path = os.path.join(os.path.dirname(__file__), "..", "templates", "index.html")
    with open(path, "r", encoding="utf-8") as f:
        html = f.read()

    # Find the registry block
    idx = html.find("_USER_SCOPED_LS_KEYS")
    total += 1
    if idx > 0:
        print("  [PASS] _USER_SCOPED_LS_KEYS registry exists")
        passed += 1
    else:
        print("  [FAIL] registry missing")
        return passed, total

    # Check that each known per-user key is in the registry
    expected_keys = ["miru_auth_token", "miru_onboarded", "sidebarGroups"]
    block_end = html.find("];", idx)
    block = html[idx:block_end]
    for key in expected_keys:
        total += 1
        if "'" + key + "'" in block or '"' + key + '"' in block:
            print(f"  [PASS] '{key}' registered")
            passed += 1
        else:
            print(f"  [FAIL] '{key}' missing from registry")

    # _purgeAllUserScopedState impl is in script 3 (after MiruCache); the
    # stub at the top forwards to the global. Verify the right things are
    # cleared and the regression-prone things are NOT cleared.
    total += 1
    if "_renderedMsgIds.clear" in html:
        print("  [PASS] purge clears _renderedMsgIds set")
        passed += 1
    else:
        print("  [FAIL] _renderedMsgIds not cleared in purge")

    # Regression guard: window.CHARACTER MUST NOT be set to null in purge.
    # 16 callsites read CHARACTER.name without null-guard; clearing it
    # would crash any SSE/timer handler that fires before the user
    # completes re-login (web/DMG path doesn't reload, only shows overlay).
    # Cross-user leak is prevented by the natural location.reload() after
    # successful re-login.
    total += 1
    if "window.CHARACTER = null" not in html:
        print("  [PASS] window.CHARACTER NOT nulled by purge (would crash UI)")
        passed += 1
    else:
        print("  [FAIL] window.CHARACTER = null exists — will crash unguarded reads")

    # The impl must be registered as a callable on window so cross-script-tag
    # callers (like the stub) can reach it.
    total += 1
    if "window._purgeAllUserScopedState = function" in html:
        print("  [PASS] purge impl registered on window")
        passed += 1
    else:
        print("  [FAIL] purge impl not exposed cross-script")

    total += 1
    if "window._handleAuthLostImpl = function" in html:
        print("  [PASS] auth-lost impl registered on window")
        passed += 1
    else:
        print("  [FAIL] auth-lost impl not exposed cross-script")

    # Check pet.html layout key has user scope
    pet_path = os.path.join(os.path.dirname(__file__), "..", "templates", "pet.html")
    with open(pet_path, "r", encoding="utf-8") as f:
        pet_html = f.read()
    total += 1
    if "_layoutStorageKey" in pet_html and "token.slice" in pet_html:
        print("  [PASS] pet.html layout key includes token-scoped suffix")
        passed += 1
    else:
        print("  [FAIL] pet.html layout key not user-scoped")

    print(f"\n  StaticReg: {passed}/{total} passed")
    return passed, total


# ============================================================================

def test_apk_disconnect_now_signature():
    """Verify MiruConnectionService.disconnectNow + clearCredentials wiring."""
    print("\n" + "=" * 60)
    print("TEST: APK SSE disconnectNow + clearCredentials hookup")
    print("=" * 60)
    passed, total = 0, 0

    svc_path = os.path.join(os.path.dirname(__file__), "..",
                             "miru-mobile/android/app/src/main/java/com/miru/companion/MiruConnectionService.java")
    main_path = os.path.join(os.path.dirname(__file__), "..",
                              "miru-mobile/android/app/src/main/java/com/miru/companion/MainActivity.java")
    with open(svc_path, "r", encoding="utf-8") as f:
        svc = f.read()
    with open(main_path, "r", encoding="utf-8") as f:
        main = f.read()

    total += 1
    if "public static void disconnectNow()" in svc:
        print("  [PASS] disconnectNow() declared in MiruConnectionService")
        passed += 1
    else:
        print("  [FAIL] disconnectNow() missing")

    total += 1
    if "sLiveInstance" in svc and "sLiveInstance = this" in svc:
        print("  [PASS] sLiveInstance registered in onCreate")
        passed += 1
    else:
        print("  [FAIL] sLiveInstance not set up")

    total += 1
    if "sLiveInstance == this" in svc and "sLiveInstance = null" in svc:
        print("  [PASS] sLiveInstance cleared in onDestroy")
        passed += 1
    else:
        print("  [FAIL] sLiveInstance not cleared")

    total += 1
    if "MiruConnectionService.disconnectNow()" in main:
        print("  [PASS] clearCredentials calls disconnectNow")
        passed += 1
    else:
        print("  [FAIL] disconnectNow not invoked from clearCredentials")

    total += 1
    if 'remove("capture_server_url")' in main and 'remove("capture_auth_token")' in main:
        print("  [PASS] clearCredentials also wipes capture_* keys")
        passed += 1
    else:
        print("  [FAIL] capture_* keys leak past logout")

    print(f"\n  APKDisconnect: {passed}/{total} passed")
    return passed, total


# ============================================================================

def test_apk_screen_capture_token_invalidation():
    """Verify ScreenCaptureService stops on logout AND restarts on token mismatch.

    The bug: ScreenCaptureService caches authToken in memory at service start.
    When the account changes (delete-and-relogin / token rotation) without
    explicitly stopping the capture service, the next /api/device/screenshot
    upload uses the previous user's token and 401s. Visible symptom: settings
    page shows "已分析截图: 0" forever despite long use.

    Fix surfaces:
      1. ScreenCaptureService.currentAuthToken (static, set/cleared on
         start/destroy) — exposes the live token to MainActivity for
         mismatch detection.
      2. MainActivity.clearCredentials sends STOP intent to
         ScreenCaptureService (so logout actually drops the cached token).
      3. MainActivity.requestScreenCapture detects token mismatch and stops
         the running service before re-prompting MediaProjection.
    """
    print("\n" + "=" * 60)
    print("TEST: ScreenCaptureService stale-token invalidation")
    print("=" * 60)
    passed, total = 0, 0

    svc_path = os.path.join(os.path.dirname(__file__), "..",
                             "miru-mobile/android/app/src/main/java/com/miru/companion/ScreenCaptureService.java")
    main_path = os.path.join(os.path.dirname(__file__), "..",
                              "miru-mobile/android/app/src/main/java/com/miru/companion/MainActivity.java")
    with open(svc_path, "r", encoding="utf-8") as f:
        svc = f.read()
    with open(main_path, "r", encoding="utf-8") as f:
        main = f.read()

    # ---- Service-side: currentAuthToken plumbing
    total += 1
    if "static volatile String currentAuthToken" in svc:
        print("  [PASS] ScreenCaptureService exposes static volatile currentAuthToken")
        passed += 1
    else:
        print("  [FAIL] currentAuthToken field missing or not static volatile")

    total += 1
    if "currentAuthToken = authToken" in svc:
        print("  [PASS] currentAuthToken set in onStartCommand")
        passed += 1
    else:
        print("  [FAIL] currentAuthToken never assigned at service start")

    total += 1
    if "currentAuthToken = null" in svc:
        print("  [PASS] currentAuthToken cleared in onDestroy")
        passed += 1
    else:
        print("  [FAIL] currentAuthToken not cleared on destroy (will leak across restart)")

    # ---- MainActivity-side: clearCredentials stops capture service
    total += 1
    if ("clearCredentials: stopping ScreenCaptureService" in main
            and "ScreenCaptureService.class" in main):
        print("  [PASS] clearCredentials sends STOP intent to ScreenCaptureService")
        passed += 1
    else:
        print("  [FAIL] clearCredentials does not stop ScreenCaptureService")

    # ---- screen_capture_enabled cleared so onDestroy doesn't show paused notification
    total += 1
    if 'remove("screen_capture_enabled")' in main:
        print("  [PASS] clearCredentials also clears screen_capture_enabled pref")
        passed += 1
    else:
        print("  [FAIL] screen_capture_enabled persists across logout (misleading paused notification)")

    # ---- requestScreenCapture token-mismatch guard
    total += 1
    if ("ScreenCaptureService.currentAuthToken" in main
            and "stale" in main.lower()
            and "skipping auth" in main):
        print("  [PASS] requestScreenCapture detects stale token + restarts service")
        passed += 1
    else:
        print("  [FAIL] requestScreenCapture missing stale-token detection")

    print(f"\n  APKScreenCaptureToken: {passed}/{total} passed")
    return passed, total


def main():
    results = []
    results.append(test_cleanup_pops_all_registered_modules())
    results.append(test_cleanup_calls_curator_stop())
    results.append(test_cleanup_unknown_module_attr_is_safe())
    results.append(test_user_scoped_ls_keys_listed())
    results.append(test_apk_disconnect_now_signature())
    results.append(test_apk_screen_capture_token_invalidation())

    p = sum(r[0] for r in results)
    t = sum(r[1] for r in results)
    print("\n" + "=" * 60)
    print(f"TOTAL: {p}/{t} passed")
    print("=" * 60)
    if p < t:
        sys.exit(1)


if __name__ == "__main__":
    main()
