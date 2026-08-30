import importlib
import json
import os
import secrets
import sys

import pytest


@pytest.fixture
def owner_app(tmp_path, monkeypatch):
    data_dir = str(tmp_path / "data")
    admin_dir = os.path.join(data_dir, "_admin")
    os.makedirs(admin_dir, exist_ok=True)

    monkeypatch.setenv("DATA_DIR", data_dir)
    monkeypatch.setenv("SERVER_IP", "203.0.113.42")
    monkeypatch.setenv("SERVER_PORT", "5001")
    for key in (
        "AI_VISION_HOST", "AI_VISION_KEY", "AI_VISION_MODEL",
        "AI_CHAT_HOST", "AI_CHAT_KEY", "AI_CHAT_MODEL",
        "AI_MEMORY_HOST", "AI_MEMORY_KEY", "AI_MEMORY_MODEL",
    ):
        monkeypatch.delenv(key, raising=False)

    for name in ["auth", "ai_config", "owner_config", "admin_api", "storage", "app"]:
        if name in sys.modules:
            importlib.reload(sys.modules[name])

    import auth
    auth._BASE_DATA_DIR = data_dir
    auth._ADMIN_DIR = admin_dir

    admin_token = "admin_" + secrets.token_urlsafe(16)
    with open(os.path.join(data_dir, "auth.json"), "w", encoding="utf-8") as f:
        json.dump({"token": admin_token}, f)

    import ai_config
    ai_config.invalidate_cache()

    import app as app_mod
    app_mod.app.config["TESTING"] = True

    yield {
        "client": app_mod.app.test_client(),
        "data_dir": data_dir,
        "admin_dir": admin_dir,
        "admin_token": admin_token,
        "app_mod": app_mod,
    }


def _seed_user(
    admin_dir,
    uid,
    token=None,
    status="active",
    invitation_code=None,
    full_invitation_code=None,
):
    token = token or secrets.token_urlsafe(24)
    users_path = os.path.join(admin_dir, "users.json")
    users = {}
    if os.path.exists(users_path):
        with open(users_path, encoding="utf-8") as f:
            users = json.load(f)
    users[uid] = {
        "token": token,
        "invitation_code": invitation_code or f"MIRU-{uid[-6:].upper()}",
        "created_at": "2026-06-11T00:00:00",
        "status": status,
    }
    if full_invitation_code:
        users[uid]["full_invitation_code"] = full_invitation_code
    with open(users_path, "w", encoding="utf-8") as f:
        json.dump(users, f)
    user_dir = os.path.join(os.path.dirname(admin_dir), "users", uid)
    os.makedirs(os.path.join(user_dir, "uploads"), exist_ok=True)
    return token


def _hdr(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def test_owner_ai_config_get_claims_first_active_user(owner_app):
    token = _seed_user(owner_app["admin_dir"], "u_owner01")

    res = owner_app["client"].get("/api/owner/ai-config", headers=_hdr(token))

    assert res.status_code == 200
    body = res.get_json()
    assert body["ok"] is True
    assert body["owner"]["owner_user_id"] == "u_owner01"
    assert set(body["config"]["tiers"]) == {"vision", "chat", "memory"}

    owner_path = os.path.join(owner_app["admin_dir"], "owner.json")
    with open(owner_path, encoding="utf-8") as f:
        saved = json.load(f)
    assert saved["owner_user_id"] == "u_owner01"


def test_user_can_view_saved_full_invitation_code(owner_app):
    import auth as auth_mod

    full_code = f"MIRU-{auth_mod.encode_server('203.0.113.42', 5001)}-ABCDEF"
    token = _seed_user(
        owner_app["admin_dir"],
        "u_invite01",
        invitation_code="MIRU-ABCDEF",
        full_invitation_code=full_code,
    )

    res = owner_app["client"].get("/api/auth/invitation", headers=_hdr(token))

    assert res.status_code == 200
    body = res.get_json()
    assert body["ok"] is True
    assert body["invitation_code"] == full_code
    assert body["local_invitation_code"] == "MIRU-ABCDEF"
    assert body["server_url"] == "http://203.0.113.42:5001"

    manifest_path = os.path.join(
        owner_app["data_dir"],
        "users",
        "u_invite01",
        "account_manifest.json",
    )
    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)
    assert manifest["full_invitation_code"] == full_code


def test_owner_ai_config_rejects_unauth_admin_and_second_user(owner_app):
    tok_a = _seed_user(owner_app["admin_dir"], "u_owner_a")
    tok_b = _seed_user(owner_app["admin_dir"], "u_owner_b")

    no_auth = owner_app["client"].get("/api/owner/ai-config")
    assert no_auth.status_code == 401

    admin = owner_app["client"].get(
        "/api/owner/ai-config",
        headers=_hdr(owner_app["admin_token"]),
    )
    assert admin.status_code == 401

    assert owner_app["client"].get("/api/owner/ai-config", headers=_hdr(tok_a)).status_code == 200
    denied = owner_app["client"].get("/api/owner/ai-config", headers=_hdr(tok_b))
    assert denied.status_code == 403
    assert "owner" in denied.get_json()["error"]


def test_owner_ai_config_update_preserves_masks_and_clears_key(owner_app):
    token = _seed_user(owner_app["admin_dir"], "u_owner02")
    secret = "sk-owner-secret-123456"

    res = owner_app["client"].patch(
        "/api/owner/ai-config/chat",
        headers=_hdr(token),
        json={
            "host": "https://api.deepseek.com/v1",
            "model": "deepseek-chat",
            "api_key": secret,
            "max_tokens": 12345,
        },
    )

    assert res.status_code == 200
    raw = res.get_data(as_text=True)
    assert secret not in raw
    body = res.get_json()
    chat_public = body["config"]["tiers"]["chat"]
    assert chat_public["host"] == "api.deepseek.com/v1"
    assert chat_public["model"] == "deepseek-chat"
    assert chat_public["api_key_masked"] == "sk-o***3456"
    assert chat_public["has_api_key"] is True
    assert "api_key" not in chat_public

    import ai_config
    cfg = ai_config.get_tier_config("chat")
    assert cfg["api_key"] == secret
    assert cfg["max_tokens"] == 12345

    # Empty api_key is a no-op; it must not overwrite the saved secret.
    res2 = owner_app["client"].patch(
        "/api/owner/ai-config/chat",
        headers=_hdr(token),
        json={"host": "api.deepseek.com", "model": "deepseek-reasoner", "api_key": ""},
    )
    assert res2.status_code == 200
    assert ai_config.get_tier_config("chat")["api_key"] == secret
    assert ai_config.get_tier_config("chat")["model"] == "deepseek-reasoner"

    res3 = owner_app["client"].patch(
        "/api/owner/ai-config/chat",
        headers=_hdr(token),
        json={"clear_api_key": True},
    )
    assert res3.status_code == 200
    assert ai_config.get_tier_config("chat")["api_key"] == ""
    assert res3.get_json()["config"]["tiers"]["chat"]["has_api_key"] is False


def test_owner_ai_config_invalid_tier_and_stale_owner_reclaim(owner_app):
    tok_a = _seed_user(owner_app["admin_dir"], "u_owner03")
    with open(os.path.join(owner_app["admin_dir"], "owner.json"), "w", encoding="utf-8") as f:
        json.dump({"owner_user_id": "u_deleted_owner"}, f)

    res = owner_app["client"].get("/api/owner/ai-config", headers=_hdr(tok_a))
    assert res.status_code == 200
    assert res.get_json()["owner"]["owner_user_id"] == "u_owner03"

    bad = owner_app["client"].patch(
        "/api/owner/ai-config/not_a_tier",
        headers=_hdr(tok_a),
        json={"host": "h"},
    )
    assert bad.status_code == 400
    assert "unknown tier" in bad.get_json()["error"]


def test_owner_ai_config_test_endpoint_is_owner_guarded_and_sanitized(owner_app, monkeypatch):
    token = _seed_user(owner_app["admin_dir"], "u_owner04")
    owner_app["client"].get("/api/owner/ai-config", headers=_hdr(token))

    calls = []

    def fake_ping(tier, timeout_seconds=15):
        calls.append((tier, timeout_seconds))
        return {
            "ok": False,
            "tier": tier,
            "host": "api.example.com",
            "model": "model-x",
            "http_status": 401,
            "error_type": "AuthenticationError",
            "error": "bad key",
        }

    monkeypatch.setattr(owner_app["app_mod"].ai_config, "ping_tier", fake_ping)

    res = owner_app["client"].post("/api/owner/ai-config/memory/test", headers=_hdr(token))

    assert res.status_code == 502
    assert calls == [("memory", 20)]
    body = res.get_json()
    assert body["http_status"] == 401
    assert body["host"] == "api.example.com"
    assert body["model"] == "model-x"
