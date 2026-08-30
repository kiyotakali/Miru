"""Test Curator lifecycle: start / stop / suspend integration (2026-05-08).

Mirrors CareEngine's per-user instance lifecycle. Verifies:
  1. start_loop_for_user starts a daemon + populates both registries
  2. start_loop_for_user is idempotent (second call no-op)
  3. stop_loop_for_user really terminates the daemon (not just pops dict)
  4. stop_loop_for_user is idempotent on unknown user
  5. start after stop creates a fresh thread
  6. auth._cleanup_user_singletons stops curator (suspend_user / delete_user path)
  7. spawner-style sweep stops curators for users not in engaged set
  8. Stop on a thread that's currently sleeping wakes it up promptly (≤2s)
"""
import os
import sys
import time
import shutil
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def _setup_user_dir():
    return tempfile.mkdtemp(prefix="curator_lifecycle_test_")


def _drain_curator_state():
    """Reset curator's per-user registries between tests so leftover daemons
    from a previous test don't leak into the next.
    """
    import curator
    for uid in list(curator._curator_user_threads.keys()):
        curator.stop_loop_for_user(uid)


def test_start_creates_thread_and_event():
    print("=" * 60)
    print("TEST: start_loop_for_user populates thread + event")
    print("=" * 60)
    import curator
    _drain_curator_state()
    tmp = _setup_user_dir()
    passed, total = 0, 0

    try:
        curator.start_loop_for_user("alice", tmp)

        total += 1
        if "alice" in curator._curator_user_threads:
            print("  [PASS] thread registered")
            passed += 1
        else:
            print("  [FAIL] thread missing")

        total += 1
        if "alice" in curator._curator_user_stop_events:
            print("  [PASS] stop_event registered")
            passed += 1
        else:
            print("  [FAIL] stop_event missing")

        total += 1
        if curator._curator_user_threads["alice"].is_alive():
            print("  [PASS] thread is alive")
            passed += 1
        else:
            print("  [FAIL] thread already dead")
    finally:
        _drain_curator_state()
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n  StartCreates: {passed}/{total} passed")
    return passed, total


def test_start_idempotent():
    print("\n" + "=" * 60)
    print("TEST: start_loop_for_user is idempotent")
    print("=" * 60)
    import curator
    _drain_curator_state()
    tmp = _setup_user_dir()
    passed, total = 0, 0

    try:
        curator.start_loop_for_user("bob", tmp)
        first = curator._curator_user_threads["bob"]
        first_event = curator._curator_user_stop_events["bob"]

        curator.start_loop_for_user("bob", tmp)
        second = curator._curator_user_threads["bob"]
        second_event = curator._curator_user_stop_events["bob"]

        total += 1
        if first is second:
            print("  [PASS] same thread object after second start")
            passed += 1
        else:
            print("  [FAIL] new thread spawned (race or leak)")

        total += 1
        if first_event is second_event:
            print("  [PASS] same stop_event")
            passed += 1
        else:
            print("  [FAIL] event replaced")
    finally:
        _drain_curator_state()
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n  StartIdempotent: {passed}/{total} passed")
    return passed, total


def test_stop_actually_stops_thread():
    print("\n" + "=" * 60)
    print("TEST: stop_loop_for_user terminates the daemon thread")
    print("=" * 60)
    import curator
    _drain_curator_state()
    tmp = _setup_user_dir()
    passed, total = 0, 0

    # Force fast tick so the test doesn't hang for a minute waiting on the
    # default 60s tick. The stop_event.wait(timeout=...) will pick up the
    # new value because the loop reads CURATOR_LOOP_TICK_SECONDS each
    # iteration via the global.
    orig_tick = curator.CURATOR_LOOP_TICK_SECONDS
    curator.CURATOR_LOOP_TICK_SECONDS = 1

    try:
        curator.start_loop_for_user("carol", tmp)
        thread = curator._curator_user_threads["carol"]
        time.sleep(0.5)  # let the loop enter wait()

        curator.stop_loop_for_user("carol")

        total += 1
        if "carol" not in curator._curator_user_threads:
            print("  [PASS] removed from thread dict")
            passed += 1
        else:
            print("  [FAIL] still in thread dict")

        total += 1
        if "carol" not in curator._curator_user_stop_events:
            print("  [PASS] removed from stop_event dict")
            passed += 1
        else:
            print("  [FAIL] still in stop_event dict")

        # stop_loop_for_user joins with timeout=2.0 internally; thread
        # should be dead by the time stop() returns.
        total += 1
        if not thread.is_alive():
            print("  [PASS] thread is no longer alive")
            passed += 1
        else:
            print("  [FAIL] thread still alive after stop")
    finally:
        curator.CURATOR_LOOP_TICK_SECONDS = orig_tick
        _drain_curator_state()
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n  StopActuallyStops: {passed}/{total} passed")
    return passed, total


def test_stop_unknown_user_is_noop():
    print("\n" + "=" * 60)
    print("TEST: stop_loop_for_user on unknown user is no-op")
    print("=" * 60)
    import curator
    _drain_curator_state()
    passed, total = 0, 0

    try:
        # Should not raise
        total += 1
        try:
            curator.stop_loop_for_user("does_not_exist")
            print("  [PASS] no exception on unknown user")
            passed += 1
        except Exception as e:
            print(f"  [FAIL] raised: {e}")
    finally:
        _drain_curator_state()

    print(f"\n  StopUnknown: {passed}/{total} passed")
    return passed, total


def test_restart_after_stop():
    print("\n" + "=" * 60)
    print("TEST: start_loop_for_user works again after stop")
    print("=" * 60)
    import curator
    _drain_curator_state()
    tmp = _setup_user_dir()
    passed, total = 0, 0

    orig_tick = curator.CURATOR_LOOP_TICK_SECONDS
    curator.CURATOR_LOOP_TICK_SECONDS = 1

    try:
        curator.start_loop_for_user("dave", tmp)
        first = curator._curator_user_threads["dave"]
        time.sleep(0.5)
        curator.stop_loop_for_user("dave")

        # Start again — should give a NEW thread
        curator.start_loop_for_user("dave", tmp)
        second = curator._curator_user_threads["dave"]

        total += 1
        if second is not None and second.is_alive():
            print("  [PASS] fresh thread after restart")
            passed += 1
        else:
            print("  [FAIL] no thread")

        total += 1
        if second is not first:
            print("  [PASS] new thread object (not the dead one)")
            passed += 1
        else:
            print("  [FAIL] reused dead thread reference")
    finally:
        curator.CURATOR_LOOP_TICK_SECONDS = orig_tick
        _drain_curator_state()
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n  RestartAfterStop: {passed}/{total} passed")
    return passed, total


def test_cleanup_user_singletons_stops_curator():
    print("\n" + "=" * 60)
    print("TEST: auth._cleanup_user_singletons stops curator")
    print("=" * 60)
    import curator
    import auth
    _drain_curator_state()
    tmp = _setup_user_dir()
    passed, total = 0, 0

    orig_tick = curator.CURATOR_LOOP_TICK_SECONDS
    curator.CURATOR_LOOP_TICK_SECONDS = 1

    try:
        curator.start_loop_for_user("eve", tmp)
        thread = curator._curator_user_threads["eve"]
        time.sleep(0.5)

        # Calling _cleanup_user_singletons directly (private API but stable
        # — both suspend_user and delete_user route through it)
        auth._cleanup_user_singletons("eve")

        total += 1
        if "eve" not in curator._curator_user_threads:
            print("  [PASS] thread removed from registry")
            passed += 1
        else:
            print("  [FAIL] still registered")

        total += 1
        if not thread.is_alive():
            print("  [PASS] daemon thread terminated")
            passed += 1
        else:
            print("  [FAIL] thread still alive after cleanup")
    finally:
        curator.CURATOR_LOOP_TICK_SECONDS = orig_tick
        _drain_curator_state()
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n  CleanupHook: {passed}/{total} passed")
    return passed, total


def test_spawner_evict_stops_inactive_users():
    print("\n" + "=" * 60)
    print("TEST: spawner-style sweep stops curators for non-engaged users")
    print("=" * 60)
    import curator
    _drain_curator_state()
    tmp_alice = _setup_user_dir()
    tmp_bob = _setup_user_dir()
    passed, total = 0, 0

    orig_tick = curator.CURATOR_LOOP_TICK_SECONDS
    curator.CURATOR_LOOP_TICK_SECONDS = 1

    try:
        curator.start_loop_for_user("alice", tmp_alice)
        curator.start_loop_for_user("bob", tmp_bob)
        time.sleep(0.5)

        # Simulate the sweep that lives in _start_care_engine_spawner:
        # alice is engaged, bob fell out. Sweep should stop bob, leave
        # alice untouched.
        engaged = {"alice"}
        curator_active = set(curator._curator_user_threads.keys())
        for uid in curator_active - engaged:
            curator.stop_loop_for_user(uid)

        total += 1
        if "alice" in curator._curator_user_threads:
            print("  [PASS] alice (engaged) untouched")
            passed += 1
        else:
            print("  [FAIL] alice incorrectly stopped")

        total += 1
        if "bob" not in curator._curator_user_threads:
            print("  [PASS] bob (non-engaged) evicted")
            passed += 1
        else:
            print("  [FAIL] bob still active")

        # Also confirm the bob thread actually terminated, not just got popped
        time.sleep(0.3)
        bob_threads = [t for t in threading.enumerate()
                       if t.name == "curator_loop_bob"]
        total += 1
        live_bobs = [t for t in bob_threads if t.is_alive()]
        if not live_bobs:
            print("  [PASS] no live curator_loop_bob threads remain")
            passed += 1
        else:
            print(f"  [FAIL] {len(live_bobs)} live bob threads remain")
    finally:
        curator.CURATOR_LOOP_TICK_SECONDS = orig_tick
        _drain_curator_state()
        shutil.rmtree(tmp_alice, ignore_errors=True)
        shutil.rmtree(tmp_bob, ignore_errors=True)

    print(f"\n  SpawnerEvict: {passed}/{total} passed")
    return passed, total


def test_stop_during_active_tick_is_prompt():
    """Even when the daemon is in the middle of wait(), stop should bring
    it down within ~tick_seconds, not orphan it for a long time."""
    print("\n" + "=" * 60)
    print("TEST: stop_loop_for_user wakes a sleeping daemon promptly")
    print("=" * 60)
    import curator
    _drain_curator_state()
    tmp = _setup_user_dir()
    passed, total = 0, 0

    # 30s tick — without the Event-based wait, stop would have to wait up to
    # 30s for the next iteration. With it, stop should return in <2s.
    orig_tick = curator.CURATOR_LOOP_TICK_SECONDS
    curator.CURATOR_LOOP_TICK_SECONDS = 30

    try:
        curator.start_loop_for_user("frank", tmp)
        thread = curator._curator_user_threads["frank"]
        time.sleep(0.3)  # ensure we're inside wait()

        t0 = time.monotonic()
        curator.stop_loop_for_user("frank")
        elapsed = time.monotonic() - t0

        total += 1
        if not thread.is_alive():
            print(f"  [PASS] thread dead after stop (elapsed {elapsed:.2f}s)")
            passed += 1
        else:
            print(f"  [FAIL] thread still alive (elapsed {elapsed:.2f}s)")

        total += 1
        if elapsed < 3.0:
            print(f"  [PASS] stop returned within {elapsed:.2f}s (<3s budget)")
            passed += 1
        else:
            print(f"  [FAIL] stop took {elapsed:.2f}s — Event wait not interrupted?")
    finally:
        curator.CURATOR_LOOP_TICK_SECONDS = orig_tick
        _drain_curator_state()
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n  StopPrompt: {passed}/{total} passed")
    return passed, total


def main():
    results = []
    results.append(test_start_creates_thread_and_event())
    results.append(test_start_idempotent())
    results.append(test_stop_actually_stops_thread())
    results.append(test_stop_unknown_user_is_noop())
    results.append(test_restart_after_stop())
    results.append(test_cleanup_user_singletons_stops_curator())
    results.append(test_spawner_evict_stops_inactive_users())
    results.append(test_stop_during_active_tick_is_prompt())

    p = sum(r[0] for r in results)
    t = sum(r[1] for r in results)
    print("\n" + "=" * 60)
    print(f"TOTAL: {p}/{t} passed")
    print("=" * 60)
    if p < t:
        sys.exit(1)


if __name__ == "__main__":
    main()
