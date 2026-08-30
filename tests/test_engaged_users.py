"""Test auth.get_engaged_user_ids — only active users with recent chat count."""
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def _seed_user(base_dir, uid, last_user_msg_time=None, status="active"):
    """Create user account + optional chat history file."""
    admin_dir = os.path.join(base_dir, "_admin")
    os.makedirs(admin_dir, exist_ok=True)
    users_path = os.path.join(admin_dir, "users.json")
    users = {}
    if os.path.exists(users_path):
        with open(users_path) as f:
            users = json.load(f)
    users[uid] = {"status": status, "created_at": "2026-01-01 00:00:00"}
    with open(users_path, "w") as f:
        json.dump(users, f)

    user_dir = os.path.join(base_dir, "users", uid)
    os.makedirs(user_dir, exist_ok=True)
    if last_user_msg_time is not None:
        chat_path = os.path.join(user_dir, "chat_history.json")
        with open(chat_path, "w") as f:
            json.dump([
                {"role": "assistant", "text": "老的回复", "time": "2026-01-01 00:00:00"},
                {"role": "user", "text": "hi", "time": last_user_msg_time},
                {"role": "assistant", "text": "新的回复", "time": last_user_msg_time},
            ], f)


def test_get_engaged_user_ids():
    """A user who sent a message within 7 days is engaged; older / never / suspended is not."""
    tmp = tempfile.mkdtemp(prefix="miru_engaged_test_")
    import auth
    # Patch module-level paths directly (don't rely on env vars — other tests
    # may have already imported auth, capturing _BASE_DATA_DIR at that time).
    old_base = auth._BASE_DATA_DIR
    old_admin = auth._ADMIN_DIR
    auth._BASE_DATA_DIR = tmp
    auth._ADMIN_DIR = os.path.join(tmp, "_admin")
    try:

        now = datetime.now()
        recent = (now - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
        old = (now - timedelta(days=14)).strftime("%Y-%m-%d %H:%M:%S")
        edge = (now - timedelta(days=6, hours=23)).strftime("%Y-%m-%d %H:%M:%S")

        _seed_user(tmp, "u_recent", last_user_msg_time=recent)
        _seed_user(tmp, "u_old", last_user_msg_time=old)
        _seed_user(tmp, "u_never", last_user_msg_time=None)
        _seed_user(tmp, "u_edge", last_user_msg_time=edge)
        _seed_user(tmp, "u_suspended", last_user_msg_time=recent, status="suspended")

        engaged = auth.get_engaged_user_ids(days=7)
        engaged_set = set(engaged)
        assert "u_recent" in engaged_set, "recent user should be engaged"
        assert "u_edge" in engaged_set, "user just inside 7-day window should be engaged"
        assert "u_old" not in engaged_set, "user 14 days old should NOT be engaged"
        assert "u_never" not in engaged_set, "user with no chat history should NOT be engaged"
        assert "u_suspended" not in engaged_set, "suspended user should NOT be engaged"

        # Active but not engaged list sanity check
        active = set(auth.get_active_user_ids())
        assert "u_recent" in active and "u_old" in active and "u_never" in active
        assert "u_suspended" not in active
        print(f"  [PASS] engaged={sorted(engaged)} active={sorted(active)}")
    finally:
        # Restore auth module state so we don't pollute later tests.
        auth._BASE_DATA_DIR = old_base
        auth._ADMIN_DIR = old_admin
        shutil.rmtree(tmp, ignore_errors=True)


def _seed_screenshot_only_user(base_dir, uid, last_screenshot_time):
    """Create a user with NO chat history but a recent screenshot_log entry —
    simulates a user whose desktop pet is running quietly while they work,
    no typing involved."""
    admin_dir = os.path.join(base_dir, "_admin")
    os.makedirs(admin_dir, exist_ok=True)
    users_path = os.path.join(admin_dir, "users.json")
    users = {}
    if os.path.exists(users_path):
        with open(users_path) as f:
            users = json.load(f)
    users[uid] = {"status": "active", "created_at": "2026-01-01 00:00:00"}
    with open(users_path, "w") as f:
        json.dump(users, f)

    user_dir = os.path.join(base_dir, "users", uid)
    os.makedirs(user_dir, exist_ok=True)
    log_path = os.path.join(user_dir, "screenshot_log.json")
    with open(log_path, "w") as f:
        json.dump([
            {"device_id": "local", "captured_at": "2026-01-01 00:00:00"},
            {"device_id": "local", "captured_at": last_screenshot_time},
        ], f)


def test_engaged_dual_signal_screenshot():
    """User who only screenshots (no chat) within 7d is still engaged."""
    tmp = tempfile.mkdtemp(prefix="miru_engaged_dual_")
    import auth
    old_base = auth._BASE_DATA_DIR
    old_admin = auth._ADMIN_DIR
    auth._BASE_DATA_DIR = tmp
    auth._ADMIN_DIR = os.path.join(tmp, "_admin")
    try:
        now = datetime.now()
        recent = (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        old = (now - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")

        _seed_screenshot_only_user(tmp, "u_silent_observer", recent)
        _seed_screenshot_only_user(tmp, "u_stale_observer", old)
        # User with no signals at all
        _seed_user(tmp, "u_nothing", last_user_msg_time=None)

        engaged = set(auth.get_engaged_user_ids(days=7))
        assert "u_silent_observer" in engaged, \
            "user with recent screenshot but no chat should still be engaged"
        assert "u_stale_observer" not in engaged, \
            "user with stale screenshot should NOT be engaged"
        assert "u_nothing" not in engaged
    finally:
        auth._BASE_DATA_DIR = old_base
        auth._ADMIN_DIR = old_admin
        shutil.rmtree(tmp, ignore_errors=True)


def test_engaged_either_signal():
    """Either chat or screenshot within window → engaged."""
    tmp = tempfile.mkdtemp(prefix="miru_engaged_either_")
    import auth
    old_base = auth._BASE_DATA_DIR
    old_admin = auth._ADMIN_DIR
    auth._BASE_DATA_DIR = tmp
    auth._ADMIN_DIR = os.path.join(tmp, "_admin")
    try:
        now = datetime.now()
        recent = (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        old = (now - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")

        # User has stale chat but recent screenshot
        _seed_user(tmp, "u_mixed", last_user_msg_time=old)
        user_dir = os.path.join(tmp, "users", "u_mixed")
        with open(os.path.join(user_dir, "screenshot_log.json"), "w") as f:
            json.dump([{"device_id": "local", "captured_at": recent}], f)

        engaged = set(auth.get_engaged_user_ids(days=7))
        assert "u_mixed" in engaged, \
            "user with stale chat + recent screenshot should be engaged"
    finally:
        auth._BASE_DATA_DIR = old_base
        auth._ADMIN_DIR = old_admin
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_get_engaged_user_ids()
    test_engaged_dual_signal_screenshot()
    test_engaged_either_signal()
    print("All engaged user tests passed.")
