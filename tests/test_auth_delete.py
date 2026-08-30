"""Tests for auth.delete_user() + auth.export_user_data() (#191).

All tests are offline — use tmp DATA_DIR, no real network.
"""
import importlib
import json
import os
import shutil
from pathlib import Path


def _setup_auth(tmp_path):
    """Reload auth module pointing at a temp data dir."""
    data_dir = str(tmp_path / "data")
    os.environ["DATA_DIR"] = data_dir
    os.environ["SERVER_IP"] = "203.0.113.42"
    os.environ["SERVER_PORT"] = "5001"
    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(os.path.join(data_dir, "_admin"), exist_ok=True)
    os.makedirs(os.path.join(data_dir, "users"), exist_ok=True)
    import auth
    importlib.reload(auth)
    return auth, data_dir


def _full_code(auth, local_code, ip="203.0.113.42", port=5001):
    user_part = local_code.replace("MIRU-", "", 1)
    return f"MIRU-{auth.encode_server(ip, port)}-{user_part}"


def _seed_user(auth, data_dir, code="ABCDEF"):
    """Create an invitation + consume it → returns (user_id, token, local_code).

    `code` is the 6-char user-part; the stored key is "MIRU-<code>".
    """
    local_code = f"MIRU-{code}"
    inv_path = os.path.join(data_dir, "_admin", "invitations.json")
    with open(inv_path, "w") as f:
        json.dump({local_code: {
            "used_by": None,
            "used_at": None,
            "created_at": "2026-04-15T00:00:00",
        }}, f)
    result = auth.login_with_code(_full_code(auth, local_code))
    assert result is not None, "seed login should succeed"
    return result["user_id"], result["token"], local_code


def test_login_creates_account_manifest(tmp_path):
    auth, data_dir = _setup_auth(tmp_path)
    user_id, _, local_code = _seed_user(auth, data_dir, code="MANF5S")

    manifest_path = os.path.join(data_dir, "users", user_id, "account_manifest.json")
    assert os.path.isfile(manifest_path)
    with open(manifest_path) as f:
        manifest = json.load(f)
    assert manifest["user_id"] == user_id
    assert manifest["invitation_code"] == local_code


def test_login_never_reuses_orphan_user_dir(tmp_path, monkeypatch):
    auth, data_dir = _setup_auth(tmp_path)
    orphan_uid = "u_deadbeef0000"
    os.makedirs(os.path.join(data_dir, "users", orphan_uid), exist_ok=True)

    local_code = "MIRU-RPHAN2"
    with open(os.path.join(data_dir, "_admin", "invitations.json"), "w") as f:
        json.dump({local_code: {"used_by": None, "used_at": None}}, f)

    ids = iter(["deadbeef0000", "feedface0000"])
    monkeypatch.setattr(auth.secrets, "token_hex", lambda _n: next(ids))
    result = auth.login_with_code(_full_code(auth, local_code))

    assert result is not None
    assert result["user_id"] == "u_feedface0000"
    assert os.path.isdir(os.path.join(data_dir, "users", orphan_uid))


def _seed_user_with_data(auth, data_dir, code="XYZABC"):
    """Create user + populate their data dir with a few files."""
    user_id, token, local_code = _seed_user(auth, data_dir, code)
    user_dir = os.path.join(data_dir, "users", user_id)

    # Populate some fake per-user files
    with open(os.path.join(user_dir, "chat_history.json"), "w") as f:
        json.dump([{"role": "user", "text": "hi"}], f)
    with open(os.path.join(user_dir, "evidence_cards.json"), "w") as f:
        json.dump({"cards": []}, f)

    os.makedirs(os.path.join(user_dir, "memory"), exist_ok=True)
    with open(os.path.join(user_dir, "memory", "self.md"), "w") as f:
        f.write("# Self\nHi there.\n")

    os.makedirs(os.path.join(user_dir, "uploads"), exist_ok=True)
    with open(os.path.join(user_dir, "uploads", "photo.jpg"), "wb") as f:
        f.write(b"\xff\xd8\xff\xe0" + b"\x00" * 100)  # fake JPEG

    return user_id, token, local_code


# ----------------------------------------------------------------
# export_user_data
# ----------------------------------------------------------------

def test_export_returns_text_files_and_uploads(tmp_path):
    auth, data_dir = _setup_auth(tmp_path)
    user_id, _, _ = _seed_user_with_data(auth, data_dir)

    bundle = auth.export_user_data(user_id)
    assert bundle is not None
    assert bundle["user_id"] == user_id
    # Text files inlined
    assert "chat_history.json" in bundle["files"]
    assert "evidence_cards.json" in bundle["files"]
    assert "memory/self.md" in bundle["files"]
    assert "# Self" in bundle["files"]["memory/self.md"]
    # Binary upload listed but not inlined
    assert "uploads/photo.jpg" in bundle["uploads"]
    # Must not contain binary bytes
    for _name, content in bundle["files"].items():
        assert isinstance(content, str)


def test_export_missing_user_returns_none(tmp_path):
    auth, _data_dir = _setup_auth(tmp_path)
    assert auth.export_user_data("u_nonexistent") is None


def test_export_admin_returns_none(tmp_path):
    auth, _data_dir = _setup_auth(tmp_path)
    assert auth.export_user_data("_admin") is None


def test_export_empty_user_returns_none(tmp_path):
    auth, _data_dir = _setup_auth(tmp_path)
    assert auth.export_user_data("") is None


# ----------------------------------------------------------------
# delete_user — happy path
# ----------------------------------------------------------------

def test_delete_removes_user_and_files(tmp_path):
    auth, data_dir = _setup_auth(tmp_path)
    user_id, token, local_code = _seed_user_with_data(auth, data_dir)

    user_dir = os.path.join(data_dir, "users", user_id)
    assert os.path.isdir(user_dir), "precondition: user dir exists"

    result = auth.delete_user(user_id, reason="user_self_delete")
    assert result["ok"] is True, f"delete failed: {result.get('error')}"
    assert result["user_id"] == user_id
    assert result["invitation_code"] == local_code
    assert result["bytes_freed"] > 0

    # Filesystem: user dir gone
    assert not os.path.isdir(user_dir)

    # users.json: user removed
    with open(os.path.join(data_dir, "_admin", "users.json")) as f:
        users = json.load(f)
    assert user_id not in users


def test_stale_background_context_cannot_recreate_deleted_user_dir(tmp_path):
    auth, data_dir = _setup_auth(tmp_path)
    user_id, _token, _local_code = _seed_user_with_data(auth, data_dir)
    user_dir = os.path.join(data_dir, "users", user_id)

    result = auth.delete_user(user_id, reason="test_stale_background_context")
    assert result["ok"] is True
    assert not os.path.exists(user_dir)

    import storage
    importlib.reload(storage)
    from flask import Flask, g

    app = Flask(__name__)
    with app.app_context():
        g.user_id = user_id
        g.user_data_dir = user_dir
        g.is_admin = False
        storage.append_screenshot_log("late_device")
        gate_id = storage.append_screen_semantic_gate_log(
            observation="删除后的后台截图线程不应该重建用户目录",
            significance=4,
            should_continue=True,
            device_id="late_device",
        )

    assert gate_id == ""
    assert not os.path.exists(user_dir)

    # The storage guard is not enough by itself: several older modules used
    # to read g.user_data_dir directly.  A stale background thread touching
    # any of these must fail closed instead of recreating data/users/<uid>.
    for module_name, call in [
        ("memory", lambda m: m.ensure_dirs()),
        ("core_memory", lambda m: m.append("human", "\nlate write")),
        ("user_settings", lambda m: m.get()),
        ("self_profile", lambda m: m.update_profile({"canonical_name": "late"})),
        ("identity", lambda m: m.update({"name": "late"}, source="test")),
        ("device_manager", lambda m: m.register_device(
            "Late Mac", "desktop", "macos", "late_device"
        )),
    ]:
        module = importlib.import_module(module_name)
        importlib.reload(module)
        with app.app_context():
            g.user_id = user_id
            g.user_data_dir = user_dir
            g.is_admin = False
            try:
                call(module)
            except RuntimeError:
                pass
            else:
                raise AssertionError(f"{module_name} accepted stale deleted context")
        assert not os.path.exists(user_dir), f"{module_name} recreated deleted user dir"


def test_delete_revokes_invitation(tmp_path):
    auth, data_dir = _setup_auth(tmp_path)
    user_id, _token, local_code = _seed_user_with_data(auth, data_dir)

    auth.delete_user(user_id)

    with open(os.path.join(data_dir, "_admin", "invitations.json")) as f:
        invs = json.load(f)
    inv = invs[local_code]
    assert inv["used_by"] is None
    assert inv.get("revoked") is True
    assert "revoked_at" in inv

    # Re-login with same code must fail
    assert auth.login_with_code(_full_code(auth, local_code)) is None, \
        "revoked invitation must not allow re-login"


def test_delete_writes_audit_log(tmp_path):
    auth, data_dir = _setup_auth(tmp_path)
    user_id, _, local_code = _seed_user_with_data(auth, data_dir)

    auth.delete_user(user_id, reason="test_reason")

    audit_path = os.path.join(data_dir, "_admin", "deletions.json")
    assert os.path.isfile(audit_path)
    with open(audit_path) as f:
        log = json.load(f)
    assert isinstance(log, list)
    assert len(log) == 1
    entry = log[0]
    assert entry["user_id"] == user_id
    assert entry["invitation_code"] == local_code
    assert entry["reason"] == "test_reason"
    assert "deleted_at" in entry


def test_delete_audit_appends_across_multiple(tmp_path):
    auth, data_dir = _setup_auth(tmp_path)
    uid1, _, _ = _seed_user_with_data(auth, data_dir, code="CDE2A3")
    uid2, _, _ = _seed_user_with_data(auth, data_dir, code="CDE2A4")

    auth.delete_user(uid1)
    auth.delete_user(uid2)

    audit_path = os.path.join(data_dir, "_admin", "deletions.json")
    with open(audit_path) as f:
        log = json.load(f)
    assert len(log) == 2
    user_ids = [e["user_id"] for e in log]
    assert uid1 in user_ids
    assert uid2 in user_ids


# ----------------------------------------------------------------
# delete_user — safety guards
# ----------------------------------------------------------------

def test_delete_admin_rejected(tmp_path):
    auth, _data_dir = _setup_auth(tmp_path)
    result = auth.delete_user("_admin")
    assert result["ok"] is False
    assert "admin" in result["error"].lower()


def test_delete_empty_user_rejected(tmp_path):
    auth, _data_dir = _setup_auth(tmp_path)
    result = auth.delete_user("")
    assert result["ok"] is False


def test_delete_nonexistent_user(tmp_path):
    auth, _data_dir = _setup_auth(tmp_path)
    result = auth.delete_user("u_doesnotexist")
    assert result["ok"] is False
    assert "not found" in result["error"]


def test_delete_failure_is_not_silent_and_keeps_metadata(tmp_path, monkeypatch):
    auth, data_dir = _setup_auth(tmp_path)
    user_id, token, local_code = _seed_user_with_data(auth, data_dir, code="FA5D2M")
    user_dir = os.path.join(data_dir, "users", user_id)

    def fail_delete(_path):
        raise OSError("simulated rmtree failure")

    monkeypatch.setattr(auth, "_rmtree_user_dir_strict", fail_delete)
    result = auth.delete_user(user_id, reason="test_failure")

    assert result["ok"] is False
    assert "file cleanup failed" in result["error"]
    assert os.path.isdir(user_dir)

    with open(os.path.join(data_dir, "_admin", "users.json")) as f:
        users = json.load(f)
    assert users[user_id]["status"] == "delete_failed"
    assert users[user_id]["invitation_code"] == local_code
    assert auth.get_user_by_token(token) == (None, None)


# ----------------------------------------------------------------
# delete_user — token invalidation
# ----------------------------------------------------------------

def test_token_invalid_after_delete(tmp_path):
    auth, data_dir = _setup_auth(tmp_path)
    user_id, token, _ = _seed_user_with_data(auth, data_dir)

    # Before delete: token resolves to user
    uid_before, _ = auth.get_user_by_token(token)
    assert uid_before == user_id

    auth.delete_user(user_id)

    # After delete: token is invalid
    uid_after, user_after = auth.get_user_by_token(token)
    assert uid_after is None
    assert user_after is None


# ----------------------------------------------------------------
# delete_user — singleton cleanup
# ----------------------------------------------------------------

def test_delete_cleans_singleton_caches(tmp_path, monkeypatch):
    """Stub modules with _instances dicts — verify user entry is dropped."""
    auth, data_dir = _setup_auth(tmp_path)
    user_id, _, _ = _seed_user_with_data(auth, data_dir)

    # Create a fake module with an _instances dict containing the user
    import sys
    import types
    fake_mod = types.ModuleType("miru_emotion")
    fake_mod._instances = {user_id: object(), "other_user": object()}
    sys.modules["miru_emotion"] = fake_mod

    try:
        auth.delete_user(user_id)
        # The deleted user's entry should be gone; other user preserved
        assert user_id not in fake_mod._instances
        assert "other_user" in fake_mod._instances
    finally:
        sys.modules.pop("miru_emotion", None)


def test_delete_cleans_all_six_singleton_modules(tmp_path):
    """Verify auth.delete_user pops the user from every per-user instance dict.

    Regression guard for the 2026-05-07 bug where sleep_agent was missing
    from the cleanup list — its instance + Timer would leak after delete.
    """
    auth, data_dir = _setup_auth(tmp_path)
    user_id, _, _ = _seed_user_with_data(auth, data_dir, code="A226CM")

    # (module_name, attr_name) — must match auth._cleanup_user_singletons
    # 2026-05-08: character was wrongly listed under _instances; the real
    # attr is _configs. This test now verifies BOTH the dict name and the
    # cleanup actually pops it.
    expected_attrs = [
        ("miru_emotion",    "_instances"),
        ("care_engine",     "_instances"),
        ("screen_analyzer", "_instances"),
        ("sleep_agent",     "_instances"),
        ("character",       "_configs"),
        ("model_library",   "_instances"),
    ]

    import sys
    import types

    fake_mods = {}
    other_uid = "u_other_keep_me"
    for mod_name, attr in expected_attrs:
        m = types.ModuleType(mod_name)
        # Each mod gets the deleted user + an unrelated user; only the first
        # should disappear after delete_user runs.
        setattr(m, attr, {user_id: object(), other_uid: object()})
        sys.modules[mod_name] = m
        fake_mods[mod_name] = (m, attr)

    try:
        result = auth.delete_user(user_id)
        assert result["ok"] is True

        for mod_name, (m, attr) in fake_mods.items():
            d = getattr(m, attr)
            assert user_id not in d, (
                f"{mod_name}.{attr} still has deleted user — "
                "cleanup list is incomplete or attr name wrong"
            )
            assert other_uid in d, (
                f"{mod_name}.{attr} over-cleaned: dropped unrelated user"
            )
    finally:
        for mod_name, _ in expected_attrs:
            sys.modules.pop(mod_name, None)


def test_delete_cleanup_handles_missing_modules(tmp_path):
    """If a module is not loaded into sys.modules, cleanup must not crash."""
    auth, data_dir = _setup_auth(tmp_path)
    user_id, _, _ = _seed_user_with_data(auth, data_dir, code="M5SSMD")

    # Make sure modules are NOT in sys.modules. Cleanup should silently skip.
    import sys
    for mod_name in ("miru_emotion", "care_engine", "screen_analyzer",
                     "sleep_agent", "character", "model_library", "summarizer"):
        sys.modules.pop(mod_name, None)

    result = auth.delete_user(user_id)
    assert result["ok"] is True, "cleanup must not raise when modules absent"


def test_delete_cleanup_handles_module_without_instances_attr(tmp_path):
    """If a module exists but has no _instances attribute, cleanup must not crash."""
    auth, data_dir = _setup_auth(tmp_path)
    user_id, _, _ = _seed_user_with_data(auth, data_dir, code="N2ATTR")

    import sys
    import types
    bad = types.ModuleType("sleep_agent")
    # Intentionally do NOT set bad._instances
    sys.modules["sleep_agent"] = bad

    try:
        result = auth.delete_user(user_id)
        assert result["ok"] is True, "cleanup must not raise on missing attr"
    finally:
        sys.modules.pop("sleep_agent", None)


def test_delete_cleanup_real_sleep_agent_module(tmp_path):
    """End-to-end: import the real sleep_agent module, register a fake
    instance under user_id, run delete_user, verify it's popped.

    This exercises the real module path (vs. fake stubs above).
    """
    auth, data_dir = _setup_auth(tmp_path)
    user_id, _, _ = _seed_user_with_data(auth, data_dir, code="REA2SA")

    import sleep_agent
    # Stash any pre-existing entries to restore after, in case of test pollution
    snapshot = dict(sleep_agent._instances)

    # Inject a placeholder so we can verify removal without bringing up a
    # real SleepAgent (which would touch real Flask/storage).
    sleep_agent._instances[user_id] = object()
    sleep_agent._instances["u_other_real"] = object()

    try:
        result = auth.delete_user(user_id)
        assert result["ok"] is True
        assert user_id not in sleep_agent._instances, \
            "sleep_agent._instances still has the deleted user"
        assert "u_other_real" in sleep_agent._instances, \
            "cleanup over-reached and dropped unrelated user"
    finally:
        sleep_agent._instances.clear()
        sleep_agent._instances.update(snapshot)


# ----------------------------------------------------------------
# Integration with full login cycle
# ----------------------------------------------------------------

def test_full_cycle_login_delete_cannot_relogin(tmp_path):
    auth, data_dir = _setup_auth(tmp_path)

    # Seed + login
    uid, token, code = _seed_user_with_data(auth, data_dir, code="FU22F2")
    assert uid and token

    # Export first
    bundle = auth.export_user_data(uid)
    assert bundle is not None
    assert "chat_history.json" in bundle["files"]

    # Delete
    result = auth.delete_user(uid)
    assert result["ok"] is True

    # Re-login attempt with same code → None (revoked)
    assert auth.login_with_code(_full_code(auth, code)) is None
    # Token doesn't resolve
    assert auth.get_user_by_token(token) == (None, None)
    # Data dir gone
    assert not os.path.isdir(os.path.join(data_dir, "users", uid))
    # But audit still exists
    assert os.path.isfile(os.path.join(data_dir, "_admin", "deletions.json"))


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
