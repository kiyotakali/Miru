"""Direct long-invitation login routing tests."""
from pathlib import Path

import auth


class _FakeResponse:
    status_code = 200

    def json(self):
        return {
            "token": "tok_test",
            "user_id": "u_test",
            "is_new": True,
        }


def test_client_mode_login_forwards_to_server_encoded_in_long_code(monkeypatch):
    import app as app_mod
    import requests

    server = auth.encode_server("203.0.113.42", 5001)
    code = f"MIRU-{server}-ABCDEF"
    calls = []
    persisted = []

    def fake_post(url, json, timeout):
        calls.append({"url": url, "json": json, "timeout": timeout})
        return _FakeResponse()

    def fake_persist(server_url, token, invitation_code, user_id, setup_complete=True):
        persisted.append({
            "server_url": server_url,
            "token": token,
            "invitation_code": invitation_code,
            "user_id": user_id,
            "setup_complete": setup_complete,
        })

    monkeypatch.setattr(app_mod, "_is_client_mode", True)
    monkeypatch.setattr(app_mod, "_client_mode_config", {})
    monkeypatch.setattr(app_mod, "_persist_client_config_and_start", fake_persist)
    monkeypatch.setattr(requests, "post", fake_post)

    client = app_mod.app.test_client()
    resp = client.post("/api/auth/login", json={"code": code})
    body = resp.get_json()

    assert resp.status_code == 200
    assert body["token"] == "tok_test"
    assert body["server_url"] == "http://203.0.113.42:5001"
    assert calls == [{
        "url": "http://203.0.113.42:5001/api/auth/login",
        "json": {"code": code},
        "timeout": 15,
    }]
    assert persisted == [{
        "server_url": "http://203.0.113.42:5001",
        "token": "tok_test",
        "invitation_code": code,
        "user_id": "u_test",
        "setup_complete": True,
    }]


def test_client_mode_login_can_hold_runtime_until_api_setup(monkeypatch):
    import app as app_mod
    import requests

    server = auth.encode_server("203.0.113.42", 5001)
    code = f"MIRU-{server}-ABCDEF"
    persisted = []

    def fake_post(url, json, timeout):
        return _FakeResponse()

    def fake_persist(server_url, token, invitation_code, user_id, setup_complete=True):
        persisted.append({
            "server_url": server_url,
            "token": token,
            "invitation_code": invitation_code,
            "user_id": user_id,
            "setup_complete": setup_complete,
        })

    monkeypatch.setattr(app_mod, "_is_client_mode", True)
    monkeypatch.setattr(app_mod, "_client_mode_config", {})
    monkeypatch.setattr(app_mod, "_persist_client_config_and_start", fake_persist)
    monkeypatch.setattr(requests, "post", fake_post)

    client = app_mod.app.test_client()
    resp = client.post("/api/auth/login", json={"code": code, "setup_complete": False})

    assert resp.status_code == 200
    assert persisted == [{
        "server_url": "http://203.0.113.42:5001",
        "token": "tok_test",
        "invitation_code": code,
        "user_id": "u_test",
        "setup_complete": False,
    }]


def test_client_mode_login_rejects_short_code_without_network(monkeypatch):
    import app as app_mod
    import requests

    calls = []

    def fake_post(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("short code must not be forwarded")

    monkeypatch.setattr(app_mod, "_is_client_mode", True)
    monkeypatch.setattr(app_mod, "_client_mode_config", {})
    monkeypatch.setattr(requests, "post", fake_post)

    client = app_mod.app.test_client()
    resp = client.post("/api/auth/login", json={"code": "MIRU-ABCDEF"})

    assert resp.status_code == 400
    assert "完整邀请码" in resp.get_json()["error"]
    assert calls == []


def test_client_mode_login_hides_connection_library_details(monkeypatch):
    import app as app_mod
    import requests

    server = auth.encode_server("203.0.113.42", 5001)
    code = f"MIRU-{server}-ABCDEF"
    monkeypatch.setattr(app_mod, "_is_client_mode", True)
    monkeypatch.setattr(app_mod, "_client_mode_config", {})

    cases = [
        (
            requests.Timeout("HTTPConnectionPool(host='203.0.113.42') timed out"),
            "连接服务器超时",
        ),
        (
            requests.ConnectionError("Max retries exceeded with url: /api/auth/login"),
            "无法连接邀请码对应的服务器",
        ),
        (requests.RequestException("raw transport detail"), "服务器暂时没有响应"),
    ]

    client = app_mod.app.test_client()
    for error, expected in cases:
        monkeypatch.setattr(
            requests,
            "post",
            lambda *args, _error=error, **kwargs: (_ for _ in ()).throw(_error),
        )
        response = client.post("/api/auth/login", json={"code": code})
        message = response.get_json()["error"]
        assert response.status_code == 502
        assert expected in message
        assert "HTTPConnectionPool" not in message
        assert "Max retries" not in message
        assert "raw transport detail" not in message


def test_server_mode_invalid_invitation_message_is_unchanged(monkeypatch):
    import app as app_mod

    monkeypatch.setattr(app_mod, "_is_client_mode", False)
    monkeypatch.setattr(auth, "login_with_code", lambda code: None)

    response = app_mod.app.test_client().post(
        "/api/auth/login",
        json={"code": "MIRU-ABCDEF"},
    )

    assert response.status_code == 401
    assert response.get_json()["error"] == "邀请码无效或已被停用"


def test_client_mode_local_settings_accept_cached_private_server_user(monkeypatch, tmp_path):
    """The DMG bridge has a VPS-issued token but no local users.json row."""
    import app as app_mod
    import server_config

    token = "tok_private_server"
    user_id = "u_remoteactive"
    data_dir = str(tmp_path / "data")

    monkeypatch.setenv("DATA_DIR", data_dir)
    monkeypatch.setattr(auth, "_BASE_DATA_DIR", data_dir)
    monkeypatch.setattr(app_mod, "_is_client_mode", True)
    monkeypatch.setattr(app_mod, "_client_mode_config", {
        "server_url": "http://203.0.113.42:5001",
        "auth_token": token,
        "user_id": user_id,
    })

    # The local bridge deliberately has no _admin/users.json record for the
    # private-server user; the cached client-mode token is the local authority.
    assert auth.get_user(user_id) is None
    assert auth.is_active_user_context(user_id, auth.get_user_data_dir(user_id))

    client = app_mod.app.test_client()
    resp = client.get(
        "/api/settings/app",
        headers={"Authorization": "Bearer " + token},
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert Path(body["config_file"]).parts[-3:] == (
        "users",
        user_id,
        "user_settings.json",
    )
    assert server_config.get()["port"] == 5001


def test_client_mode_seeds_missing_local_user_settings_without_overwriting(monkeypatch, tmp_path):
    import app as app_mod
    import user_settings
    from flask import g

    user_id = "u_remoteactive"
    data_dir = str(tmp_path / "data")

    monkeypatch.setenv("DATA_DIR", data_dir)
    monkeypatch.setattr(auth, "_BASE_DATA_DIR", data_dir)
    monkeypatch.setattr(app_mod, "_is_client_mode", True)
    monkeypatch.setattr(app_mod, "_client_mode_config", {
        "server_url": "http://203.0.113.42:5001",
        "auth_token": "tok_private_server",
        "user_id": user_id,
    })

    assert app_mod._seed_local_user_settings_from_remote({
        "screenshot_enabled": True,
        "screenshot_perm_guided": True,
        "auto_screenshot_interval": 30,
        "timezone": "Asia/Shanghai",
        "pet_hotkey": "ctrl+alt+m",
        "pet_collapse_delay": 9000,
    }, user_id) is True

    with app_mod.app.app_context():
        g.user_id = user_id
        g.user_data_dir = auth.get_user_data_dir(user_id)
        g.is_admin = False
        seeded = user_settings.get()
        assert seeded["screenshot_enabled"] is True
        assert seeded["auto_screenshot_interval"] == 30
        assert seeded["pet_hotkey"] == user_settings._default_pet_hotkey()
        assert seeded["pet_collapse_delay"] == 4000

        raw = user_settings._load()
        assert "pet_hotkey" not in raw
        assert "pet_collapse_delay" not in raw

        # Existing local choice wins on later startup sync.
        user_settings.update({"screenshot_enabled": False})

    assert app_mod._seed_local_user_settings_from_remote({
        "screenshot_enabled": True,
        "auto_screenshot_interval": 45,
    }, user_id) is False

    with app_mod.app.app_context():
        g.user_id = user_id
        g.user_data_dir = auth.get_user_data_dir(user_id)
        g.is_admin = False
        preserved = user_settings.get()
        assert preserved["screenshot_enabled"] is False
        assert preserved["auto_screenshot_interval"] == 30


def test_client_mode_does_not_seed_mac_pet_runtime_into_windows(monkeypatch, tmp_path):
    import app as app_mod
    import user_settings
    from flask import g

    user_id = "u_remote_windows"
    data_dir = str(tmp_path / "data")

    monkeypatch.setenv("DATA_DIR", data_dir)
    monkeypatch.setattr(auth, "_BASE_DATA_DIR", data_dir)
    monkeypatch.setattr(user_settings.platform, "system", lambda: "Windows")
    monkeypatch.setattr(app_mod, "_is_client_mode", True)
    monkeypatch.setattr(app_mod, "_client_mode_config", {
        "server_url": "http://203.0.113.42:5001",
        "auth_token": "tok_private_server",
        "user_id": user_id,
    })

    assert app_mod._seed_local_user_settings_from_remote({
        "screenshot_enabled": True,
        "auto_screenshot_interval": 45,
        "pet_hotkey": "cmd+option+m",
        "pet_collapse_delay": 12000,
    }, user_id) is True

    with app_mod.app.app_context():
        g.user_id = user_id
        g.user_data_dir = auth.get_user_data_dir(user_id)
        g.is_admin = False
        seeded = user_settings.get()
        assert seeded["screenshot_enabled"] is True
        assert seeded["auto_screenshot_interval"] == 45
        assert seeded["pet_hotkey"] == "ctrl+alt+m"
        assert seeded["pet_collapse_delay"] == 4000
