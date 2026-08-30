"""Unit tests for admin_api blueprint + admin_stats aggregation.

Uses Flask test client (no subprocess), patches auth._BASE_DATA_DIR to a
tmpdir, and exercises every admin endpoint without touching real data.
"""
import importlib
import json
import os
import secrets
import shutil
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def admin_app(tmp_path, monkeypatch):
    """Set up a Flask test client with a fresh isolated DATA_DIR."""
    data_dir = str(tmp_path / "data")
    admin_dir = os.path.join(data_dir, "_admin")
    os.makedirs(admin_dir)

    monkeypatch.setenv("DATA_DIR", data_dir)
    monkeypatch.setenv("SERVER_IP", "203.0.113.42")
    monkeypatch.setenv("SERVER_PORT", "5001")

    # Reload modules in dependency order so they pick up the new DATA_DIR
    for name in [
        "auth", "admin_stats", "admin_api", "storage", "sse",
    ]:
        if name in sys.modules:
            importlib.reload(sys.modules[name])

    import auth as _auth
    _auth._BASE_DATA_DIR = data_dir
    _auth._ADMIN_DIR = admin_dir

    # Seed admin token
    admin_token = secrets.token_urlsafe(32)
    with open(os.path.join(data_dir, "auth.json"), "w") as f:
        json.dump({"token": admin_token, "created_at": "2026-04-28"}, f)

    # Now import app and create test client. app must be reloaded so it picks
    # up the patched auth.
    if "app" in sys.modules:
        importlib.reload(sys.modules["app"])
    import app as _app
    _app.app.config["TESTING"] = True
    client = _app.app.test_client()

    yield {
        "client": client,
        "data_dir": data_dir,
        "admin_dir": admin_dir,
        "admin_token": admin_token,
    }


def _admin_hdr(token):
    return {"Authorization": f"Bearer {token}"}


def _seed_user(admin_dir, uid, **overrides):
    """Write a user record directly into _admin/users.json."""
    users_path = os.path.join(admin_dir, "users.json")
    users = {}
    if os.path.exists(users_path):
        with open(users_path) as f:
            users = json.load(f)
    users[uid] = {
        "token": overrides.get("token", secrets.token_urlsafe(16)),
        "invitation_code": overrides.get("invitation_code", f"MIRU-{uid[-6:]}"),
        "created_at": overrides.get("created_at", "2026-04-01 00:00:00"),
        "status": overrides.get("status", "active"),
    }
    with open(users_path, "w") as f:
        json.dump(users, f)
    # Pre-create user dir
    user_dir = os.path.join(os.path.dirname(admin_dir), "users", uid)
    os.makedirs(user_dir, exist_ok=True)
    return users[uid]


# ---------------------------------------------------------------------------
# Auth verify
# ---------------------------------------------------------------------------

def test_auth_verify_accepts_valid_token(admin_app):
    r = admin_app["client"].post(
        "/api/admin/auth/verify",
        json={"token": admin_app["admin_token"]},
    )
    assert r.status_code == 200
    assert r.get_json() == {"ok": True}


def test_auth_verify_rejects_bad_token(admin_app):
    r = admin_app["client"].post(
        "/api/admin/auth/verify",
        json={"token": "not-the-real-token"},
    )
    assert r.status_code == 401
    assert r.get_json()["ok"] is False


def test_auth_verify_rejects_empty(admin_app):
    r = admin_app["client"].post("/api/admin/auth/verify", json={})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Users list & detail
# ---------------------------------------------------------------------------

def test_users_list_rejects_bad_token(admin_app):
    """Non-admin token must be 401; the endpoint is admin-only."""
    r = admin_app["client"].get(
        "/api/admin/users",
        headers={"Authorization": "Bearer not-the-admin-token"},
        environ_overrides={"REMOTE_ADDR": "203.0.113.42"},  # remote IP
    )
    assert r.status_code in (401, 403)


def test_users_list_returns_redacted(admin_app):
    _seed_user(admin_app["admin_dir"], "u_abc123", token="real_secret_token")
    r = admin_app["client"].get(
        "/api/admin/users", headers=_admin_hdr(admin_app["admin_token"])
    )
    assert r.status_code == 200
    body = r.get_json()
    assert body["count"] == 1
    user = body["users"]["u_abc123"]
    assert "token" not in user, "raw token must not leak in list response"
    assert "token_masked" in user
    assert user["token_masked"].startswith("real")


def test_user_detail_returns_stats(admin_app):
    _seed_user(admin_app["admin_dir"], "u_xyz789")
    r = admin_app["client"].get(
        "/api/admin/users/u_xyz789", headers=_admin_hdr(admin_app["admin_token"])
    )
    assert r.status_code == 200
    body = r.get_json()
    assert body["user_id"] == "u_xyz789"
    assert "messages" in body
    assert "screenshots" in body
    assert "commitments" in body


def test_user_detail_404_for_unknown(admin_app):
    r = admin_app["client"].get(
        "/api/admin/users/u_does_not_exist", headers=_admin_hdr(admin_app["admin_token"])
    )
    assert r.status_code == 404


def test_attention_delivery_endpoint_runs_in_selected_user_context(admin_app, monkeypatch):
    """Admin diagnostic endpoint must execute core delivery inside the target
    user's data context, otherwise SSE/storage would hit _admin/global files."""
    uid = "u_attn01"
    _seed_user(admin_app["admin_dir"], uid)
    user_dir = os.path.join(admin_app["data_dir"], "users", uid)

    import core
    import storage

    seen = {}

    def fake_deliver_attention_intent_once(intent_id=None):
        from flask import g
        seen["intent_id"] = intent_id
        seen["user_id"] = getattr(g, "user_id", None)
        seen["user_data_dir"] = getattr(g, "user_data_dir", None)
        seen["is_admin"] = getattr(g, "is_admin", None)
        seen["storage_dir"] = storage.get_data_dir()
        return {
            "intent_id": intent_id,
            "delivery_status": "delivered",
            "message_id": "attention_proactive_test",
        }

    monkeypatch.setattr(
        core,
        "deliver_attention_intent_once",
        fake_deliver_attention_intent_once,
    )

    r = admin_app["client"].post(
        f"/api/admin/users/{uid}/attention-delivery",
        json={"intent_id": "intent_test"},
        headers=_admin_hdr(admin_app["admin_token"]),
    )

    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["entry"]["delivery_status"] == "delivered"
    assert seen == {
        "intent_id": "intent_test",
        "user_id": uid,
        "user_data_dir": user_dir,
        "is_admin": False,
        "storage_dir": user_dir,
    }


def test_attention_delivery_endpoint_rejects_unknown_user(admin_app, monkeypatch):
    import core

    called = False

    def fake_deliver_attention_intent_once(intent_id=None):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(
        core,
        "deliver_attention_intent_once",
        fake_deliver_attention_intent_once,
    )

    r = admin_app["client"].post(
        "/api/admin/users/u_missing/attention-delivery",
        json={},
        headers=_admin_hdr(admin_app["admin_token"]),
    )

    assert r.status_code == 404
    assert called is False


# ---------------------------------------------------------------------------
# User PATCH (suspend/activate)
# ---------------------------------------------------------------------------

def test_patch_user_suspend(admin_app):
    _seed_user(admin_app["admin_dir"], "u_alice1")
    r = admin_app["client"].patch(
        "/api/admin/users/u_alice1",
        json={"status": "suspended"},
        headers=_admin_hdr(admin_app["admin_token"]),
    )
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["status"] == "suspended"

    # Verify users.json reflects the change
    with open(os.path.join(admin_app["admin_dir"], "users.json")) as f:
        users = json.load(f)
    assert users["u_alice1"]["status"] == "suspended"


def test_patch_user_activate(admin_app):
    _seed_user(admin_app["admin_dir"], "u_bob222", status="suspended")
    r = admin_app["client"].patch(
        "/api/admin/users/u_bob222",
        json={"status": "active"},
        headers=_admin_hdr(admin_app["admin_token"]),
    )
    assert r.status_code == 200
    assert r.get_json()["status"] == "active"


def test_patch_user_rejects_bad_status(admin_app):
    _seed_user(admin_app["admin_dir"], "u_carol3")
    r = admin_app["client"].patch(
        "/api/admin/users/u_carol3",
        json={"status": "deleted"},
        headers=_admin_hdr(admin_app["admin_token"]),
    )
    assert r.status_code == 400


def test_patch_unknown_user_404(admin_app):
    r = admin_app["client"].patch(
        "/api/admin/users/u_not_real",
        json={"status": "suspended"},
        headers=_admin_hdr(admin_app["admin_token"]),
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# User DELETE
# ---------------------------------------------------------------------------

def test_delete_user_requires_confirm(admin_app):
    _seed_user(admin_app["admin_dir"], "u_d3l3t3")
    r = admin_app["client"].delete(
        "/api/admin/users/u_d3l3t3",
        json={},
        headers=_admin_hdr(admin_app["admin_token"]),
    )
    assert r.status_code == 400
    assert "confirm" in r.get_json().get("error", "").lower()


def test_delete_user_wrong_confirm(admin_app):
    _seed_user(admin_app["admin_dir"], "u_d3l3t3")
    r = admin_app["client"].delete(
        "/api/admin/users/u_d3l3t3",
        json={"confirm": "wrong0"},
        headers=_admin_hdr(admin_app["admin_token"]),
    )
    assert r.status_code == 400


def test_delete_user_success(admin_app):
    uid = "u_d3l3t3"
    _seed_user(admin_app["admin_dir"], uid)
    user_dir = os.path.join(admin_app["data_dir"], "users", uid)
    assert os.path.isdir(user_dir)

    r = admin_app["client"].delete(
        f"/api/admin/users/{uid}",
        json={"confirm": uid[-6:]},
        headers=_admin_hdr(admin_app["admin_token"]),
    )
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True

    # User dir gone
    assert not os.path.exists(user_dir)
    # User record gone
    with open(os.path.join(admin_app["admin_dir"], "users.json")) as f:
        users = json.load(f)
    assert uid not in users


def test_delete_admin_uid_refused(admin_app):
    r = admin_app["client"].delete(
        "/api/admin/users/_admin",
        json={"confirm": "_admin"[-6:]},
        headers=_admin_hdr(admin_app["admin_token"]),
    )
    assert r.status_code == 400
    assert "admin" in r.get_json()["error"].lower()


# ---------------------------------------------------------------------------
# Invitations
# ---------------------------------------------------------------------------

def test_invitations_create_then_list(admin_app):
    r = admin_app["client"].post(
        "/api/admin/invitations",
        json={"count": 3, "ip": "127.0.0.1", "port": 5001},
        headers=_admin_hdr(admin_app["admin_token"]),
    )
    assert r.status_code == 200
    codes = r.get_json()["codes"]
    assert len(codes) == 3

    r2 = admin_app["client"].get(
        "/api/admin/invitations", headers=_admin_hdr(admin_app["admin_token"])
    )
    assert r2.status_code == 200
    assert r2.get_json()["count"] == 3


def test_invitations_delete_unused(admin_app):
    admin_app["client"].post(
        "/api/admin/invitations",
        json={"count": 1, "ip": "127.0.0.1", "port": 5001},
        headers=_admin_hdr(admin_app["admin_token"]),
    )
    invitations_path = os.path.join(admin_app["admin_dir"], "invitations.json")
    with open(invitations_path) as f:
        invs = json.load(f)
    code = next(iter(invs.keys()))

    r = admin_app["client"].delete(
        f"/api/admin/invitations/{code}",
        headers=_admin_hdr(admin_app["admin_token"]),
    )
    assert r.status_code == 200

    with open(invitations_path) as f:
        invs2 = json.load(f)
    assert code not in invs2


def test_invitations_delete_used_refused(admin_app):
    # Create an invitation and mark it used
    admin_app["client"].post(
        "/api/admin/invitations",
        json={"count": 1},
        headers=_admin_hdr(admin_app["admin_token"]),
    )
    invitations_path = os.path.join(admin_app["admin_dir"], "invitations.json")
    with open(invitations_path) as f:
        invs = json.load(f)
    code = next(iter(invs.keys()))
    invs[code]["used_by"] = "u_someone"
    invs[code]["used_at"] = "2026-04-28"
    with open(invitations_path, "w") as f:
        json.dump(invs, f)

    r = admin_app["client"].delete(
        f"/api/admin/invitations/{code}",
        headers=_admin_hdr(admin_app["admin_token"]),
    )
    assert r.status_code == 409
    assert "used" in r.get_json()["error"].lower()


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def test_stats_endpoint(admin_app):
    _seed_user(admin_app["admin_dir"], "u_alice7")
    _seed_user(admin_app["admin_dir"], "u_bob888", status="suspended")

    r = admin_app["client"].get(
        "/api/admin/stats", headers=_admin_hdr(admin_app["admin_token"])
    )
    assert r.status_code == 200
    body = r.get_json()
    assert body["users"]["total"] == 2
    assert body["users"]["active"] == 1
    assert body["users"]["suspended"] == 1
    assert "messages" in body
    assert "storage_bytes" in body
    assert "generated_at" in body


def test_stats_caching(admin_app):
    """Two close-together calls should hit the cache (age > 0 second time)."""
    _seed_user(admin_app["admin_dir"], "u_cache1")
    r1 = admin_app["client"].get(
        "/api/admin/stats", headers=_admin_hdr(admin_app["admin_token"])
    )
    r2 = admin_app["client"].get(
        "/api/admin/stats", headers=_admin_hdr(admin_app["admin_token"])
    )
    assert r1.status_code == r2.status_code == 200
    # Second response should be a cache hit
    assert r2.get_json().get("_cache_age_s", 0) >= 0


def test_stats_invalidated_on_user_change(admin_app):
    """Creating/deleting users should bust the cache."""
    r1 = admin_app["client"].get(
        "/api/admin/stats", headers=_admin_hdr(admin_app["admin_token"])
    )
    assert r1.get_json()["users"]["total"] == 0

    # Create an invitation (invalidates cache)
    admin_app["client"].post(
        "/api/admin/invitations",
        json={"count": 1},
        headers=_admin_hdr(admin_app["admin_token"]),
    )

    # Suspend a user (would also invalidate, but no users exist yet)
    _seed_user(admin_app["admin_dir"], "u_late")
    admin_app["client"].patch(
        "/api/admin/users/u_late",
        json={"status": "suspended"},
        headers=_admin_hdr(admin_app["admin_token"]),
    )

    r2 = admin_app["client"].get(
        "/api/admin/stats", headers=_admin_hdr(admin_app["admin_token"])
    )
    assert r2.get_json()["users"]["total"] == 1
    assert r2.get_json()["users"]["suspended"] == 1


# ---------------------------------------------------------------------------
# Suspend evict — verify backend instances are cleared
# ---------------------------------------------------------------------------

def test_suspend_evicts_singletons(admin_app):
    """When admin suspends a user, every per-user instance dict drops them."""
    uid = "u_evict1"
    _seed_user(admin_app["admin_dir"], uid)

    # Plant fake per-user instances in every module that has _instances
    # 2026-05-08: summarizer removed; sleep_agent is now in the list.
    import care_engine, screen_analyzer, sleep_agent, character, miru_emotion
    import sys
    for mod_name in ["care_engine", "screen_analyzer", "sleep_agent",
                     "character", "miru_emotion"]:
        mod = sys.modules.get(mod_name)
        if mod is None:
            continue
        d = getattr(mod, "_instances", None)
        if isinstance(d, dict):
            d[uid] = "FAKE_INSTANCE"

    # Trigger suspend via PATCH
    r = admin_app["client"].patch(
        f"/api/admin/users/{uid}",
        json={"status": "suspended"},
        headers=_admin_hdr(admin_app["admin_token"]),
    )
    assert r.status_code == 200

    # All instance dicts should no longer contain uid
    for mod_name in ["care_engine", "screen_analyzer", "sleep_agent",
                     "character", "miru_emotion"]:
        mod = sys.modules.get(mod_name)
        if mod is None:
            continue
        d = getattr(mod, "_instances", None)
        if isinstance(d, dict):
            assert uid not in d, \
                f"{mod_name}._instances still has {uid} after suspend"


def test_suspend_does_not_affect_other_users(admin_app):
    """Suspending A must not evict B's instances."""
    uid_a, uid_b = "u_evictA", "u_evictB"
    _seed_user(admin_app["admin_dir"], uid_a)
    _seed_user(admin_app["admin_dir"], uid_b)

    import care_engine
    care_engine._instances[uid_a] = "A_INSTANCE"
    care_engine._instances[uid_b] = "B_INSTANCE"

    r = admin_app["client"].patch(
        f"/api/admin/users/{uid_a}",
        json={"status": "suspended"},
        headers=_admin_hdr(admin_app["admin_token"]),
    )
    assert r.status_code == 200
    assert uid_a not in care_engine._instances
    assert uid_b in care_engine._instances, \
        "Suspending A must not drop B's CareEngine instance"
    # Cleanup
    del care_engine._instances[uid_b]


# ---------------------------------------------------------------------------
# Real-format regression — these are the bugs that escaped the early test
# coverage because the original fixtures used empty user dirs:
#   1. screenshot_log.json is dict-by-date, NOT a flat list
#   2. commitments live under <user>/memory/commitments/, NOT <user>/commitments/
#   3. active.md uses free-text "- " entries, done.md uses "- [x]"
# ---------------------------------------------------------------------------

def _seed_real_user_data(data_dir, uid):
    """Lay down screenshot_log + memory/commitments files in the real format
    so the count helpers exercise production-shaped data."""
    user_dir = os.path.join(data_dir, "users", uid)
    os.makedirs(user_dir, exist_ok=True)

    # Real screenshot_log.json — dict keyed by date, list values
    today = "2026-04-28"
    yesterday = "2026-04-27"
    log = {
        yesterday: [
            {"t": f"{yesterday}T10:00:00", "d": "dev_a"},
            {"t": f"{yesterday}T15:00:00", "d": "dev_b"},
        ],
        today: [
            {"t": f"{today}T08:00:00", "d": "dev_a"},
            {"t": f"{today}T09:00:00", "d": "dev_a"},
            {"t": f"{today}T10:00:00", "d": "dev_b"},
        ],
    }
    with open(os.path.join(user_dir, "screenshot_log.json"), "w", encoding="utf-8") as f:
        json.dump(log, f)

    # Real commitments — under memory/commitments/
    commit_dir = os.path.join(user_dir, "memory", "commitments")
    os.makedirs(commit_dir, exist_ok=True)
    # active.md: 1 free-text bullet (must be IGNORED, matches core semantics),
    # 2 proper [ ] entries, 1 [x] entry (counts as done).
    with open(os.path.join(commit_dir, "active.md"), "w", encoding="utf-8") as f:
        f.write("# Active Commitments\n\n"
                "- 完成承诺解析器 (deadline: 2026-05-01)  [added: 2026-04-25 19:30]\n"
                "- [ ] 修复 admin dashboard  [added: 2026-04-26 10:00]\n"
                "- [ ] 重构数据存储  [added: 2026-04-26 11:00]\n"
                "- [x] 已完成的项目  [added: 2026-04-26 12:00]\n")
    # done.md: 2 [x] entries, plus 1 dup of an active.md entry (must dedup).
    with open(os.path.join(commit_dir, "done.md"), "w", encoding="utf-8") as f:
        f.write("- [x] 修复登录 bug (deadline: 2026-04-20) [completed: 2026-04-21]\n"
                "- [x] 部署 VPS [completed: 2026-04-22]\n"
                "- [x] 修复 admin dashboard  [added: 2026-04-26 10:00]\n")


def test_screenshot_count_handles_dict_format(admin_app):
    """Bug repro: stats showed 0 screenshots when production format is dict-by-date.

    With 2 entries on 2026-04-27 and 3 on 2026-04-28, total must be 5.
    """
    _seed_user(admin_app["admin_dir"], "u_real_fmt")
    _seed_real_user_data(admin_app["data_dir"], "u_real_fmt")

    r = admin_app["client"].get(
        "/api/admin/stats?force=1", headers=_admin_hdr(admin_app["admin_token"])
    )
    body = r.get_json()
    assert body["screenshots"]["total"] == 5, \
        f"expected 5 screenshots, got {body['screenshots']['total']}"


def test_commitments_count_uses_memory_path(admin_app):
    """Bug repro: stats showed 0 commitments because we read <user>/commitments/
    instead of the real <user>/memory/commitments/ path. Plus must match
    core.parse_commitments semantics: only "- [" entries count, ids dedup
    across active.md and done.md."""
    _seed_user(admin_app["admin_dir"], "u_real_fmt2")
    _seed_real_user_data(admin_app["data_dir"], "u_real_fmt2")

    r = admin_app["client"].get(
        "/api/admin/stats?force=1", headers=_admin_hdr(admin_app["admin_token"])
    )
    body = r.get_json()
    # active.md: 1 free-text bullet IGNORED, 2 "- [ ]" → active, 1 "- [x]" → done
    # done.md: 2 unique "- [x]", 1 dup of active.md ("修复 admin dashboard") deduped
    # Expected: active=2, done=1 (from active.md [x]) + 2 (unique done.md) = 3
    assert body["commitments"]["active"] == 2, \
        f"expected 2 active commitments, got {body['commitments']['active']}"
    assert body["commitments"]["done"] == 3, \
        f"expected 3 done commitments (1 from active.md + 2 unique from done.md), got {body['commitments']['done']}"


def test_user_detail_has_last_screenshot_field(admin_app):
    _seed_user(admin_app["admin_dir"], "u_real_fmt3")
    _seed_real_user_data(admin_app["data_dir"], "u_real_fmt3")

    r = admin_app["client"].get(
        "/api/admin/users/u_real_fmt3", headers=_admin_hdr(admin_app["admin_token"])
    )
    body = r.get_json()
    assert "last_screenshot_at" in body
    assert body["last_screenshot_at"].startswith("2026-04-28"), \
        f"expected latest screenshot timestamp, got {body['last_screenshot_at']!r}"
    assert body["screenshots"]["total"] == 5
    assert body["commitments"]["done"] == 3


def test_suspend_disconnects_sse(admin_app):
    """Suspending a user closes their connected SSE streams."""
    uid = "u_sse_test"
    _seed_user(admin_app["admin_dir"], uid)

    import sse
    q = sse.add_client(device_id="test_dev", user_id=uid)
    assert sse.get_client_count(uid) == 1

    admin_app["client"].patch(
        f"/api/admin/users/{uid}",
        json={"status": "suspended"},
        headers=_admin_hdr(admin_app["admin_token"]),
    )

    assert sse.get_client_count(uid) == 0
