import json
import threading

import auth


def _patch_data_dir(monkeypatch, tmp_path):
    data_dir = str(tmp_path / "data")
    monkeypatch.setenv("DATA_DIR", data_dir)
    monkeypatch.setattr(auth, "_BASE_DATA_DIR", data_dir)
    monkeypatch.setattr(auth, "_ADMIN_DIR", str(tmp_path / "data" / "_admin"))
    return data_dir


def _patch_client_runtime(monkeypatch, app_mod, cfg, *, generation=0,
                          active_local_user_id=""):
    cancel_event = threading.Event()
    monkeypatch.setattr(app_mod, "_client_mode_config", dict(cfg))
    monkeypatch.setattr(app_mod, "_client_runtime_generation", generation)
    monkeypatch.setattr(app_mod, "_client_runtime_cancel_event", cancel_event)
    monkeypatch.setattr(app_mod, "_client_local_runtime_user_id", active_local_user_id)
    return cancel_event


def test_local_single_device_start_creates_account_and_launcher_config(monkeypatch, tmp_path):
    import app as app_mod

    data_dir = _patch_data_dir(monkeypatch, tmp_path)
    saved = []
    triggered = []

    monkeypatch.setattr(app_mod, "_is_client_mode", True)
    _patch_client_runtime(monkeypatch, app_mod, {})
    monkeypatch.setattr(app_mod, "_stop_client_screen_sensor", lambda: None)
    monkeypatch.setattr(app_mod, "_save_launcher_config", lambda cfg: saved.append(dict(cfg)))
    monkeypatch.setattr(app_mod, "_deferred_client_setup_trigger", lambda url, token: triggered.append((url, token)))

    client = app_mod.app.test_client()
    resp = client.post(
        "/api/client/local/start",
        json={},
        environ_base={"REMOTE_ADDR": "127.0.0.1", "SERVER_PORT": "5001"},
    )
    body = resp.get_json()

    assert resp.status_code == 200
    assert body["ok"] is True
    assert body["mode"] == "local"
    assert body["local_only"] is True
    assert body["invitation_code"] == ""
    assert body["server_url"] == "http://127.0.0.1:5001"
    assert body["token"]
    assert body["user_id"].startswith("u_")

    users = json.loads((tmp_path / "data" / "_admin" / "users.json").read_text(encoding="utf-8"))
    user = users[body["user_id"]]
    assert user["account_type"] == auth.LOCAL_SINGLE_DEVICE_ACCOUNT_TYPE
    assert user["invitation_code"] == ""
    assert user["full_invitation_code"] == ""

    manifest = json.loads(
        (tmp_path / "data" / "users" / body["user_id"] / "account_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["user_id"] == body["user_id"]
    assert manifest["account_type"] == auth.LOCAL_SINGLE_DEVICE_ACCOUNT_TYPE

    assert saved == [{
        "mode": "local",
        "local_only": True,
        "invitation_code": "",
        "server_url": "http://127.0.0.1:5001",
        "auth_token": body["token"],
        "user_id": body["user_id"],
        "device_name": saved[0]["device_name"],
        "setup_complete": False,
    }]
    assert triggered == []

    second = client.post(
        "/api/client/local/start",
        json={},
        environ_base={"REMOTE_ADDR": "127.0.0.1", "SERVER_PORT": "5001"},
    ).get_json()
    assert second["user_id"] == body["user_id"]
    assert second["token"] == body["token"]
    assert second["is_new"] is False
    assert saved[-1]["setup_complete"] is True
    assert triggered == [("http://127.0.0.1:5001", body["token"])]


def test_legacy_launcher_config_with_token_counts_as_setup_complete():
    import app as app_mod

    assert app_mod._setup_complete_from_launcher_config({"auth_token": "tok"}) is True
    assert app_mod._setup_complete_from_launcher_config({
        "auth_token": "tok",
        "setup_complete": False,
    }) is False
    assert app_mod._setup_complete_from_launcher_config({}) is False


def test_remote_first_user_can_create_first_local_account(monkeypatch, tmp_path):
    import app as app_mod

    _patch_data_dir(monkeypatch, tmp_path)
    remote_cfg = {
        "server_url": "http://server.example:5001",
        "auth_token": "tok_remote",
        "user_id": "u_remote_only_on_vps",
        "mode": "remote",
        "local_only": False,
        "setup_complete": True,
    }
    monkeypatch.setattr(app_mod, "_is_client_mode", True)
    remote_event = _patch_client_runtime(monkeypatch, app_mod, remote_cfg, generation=3)
    sensor_stops = []
    cleanups = []
    triggered = []
    cfg_path = tmp_path / "config.json"
    monkeypatch.setattr(app_mod, "_launcher_config_path", lambda: cfg_path)
    monkeypatch.setattr(app_mod, "_stop_client_screen_sensor", lambda: sensor_stops.append(True))
    monkeypatch.setattr(auth, "_cleanup_user_singletons", lambda uid: cleanups.append(uid))
    monkeypatch.setattr(
        app_mod,
        "_deferred_client_setup_trigger",
        lambda url, token: triggered.append((url, token)),
    )

    response = app_mod.app.test_client().post(
        "/api/client/local/start",
        json={},
        environ_base={"REMOTE_ADDR": "127.0.0.1", "SERVER_PORT": "5001"},
    )
    body = response.get_json()

    assert response.status_code == 200
    assert body["ok"] is True
    assert body["is_new"] is True
    assert body["user_id"] != remote_cfg["user_id"]
    assert remote_event.is_set()
    assert sensor_stops == [True]
    assert cleanups == []
    assert triggered == []  # first local account still waits for API setup
    assert app_mod._client_mode_config["mode"] == "local"
    assert app_mod._client_mode_config["setup_complete"] is False
    assert auth.get_user(body["user_id"])["account_type"] == auth.LOCAL_SINGLE_DEVICE_ACCOUNT_TYPE
    saved = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert saved["user_id"] == body["user_id"]
    assert saved["setup_complete"] is False


def test_local_single_device_start_reports_config_persist_failure(monkeypatch, tmp_path):
    import app as app_mod

    _patch_data_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(app_mod, "_is_client_mode", True)
    monkeypatch.setattr(app_mod, "_client_mode_config", {})

    def fail_persist(*args, **kwargs):
        raise OSError("readonly config")

    monkeypatch.setattr(app_mod, "_persist_client_config_and_start", fail_persist)

    client = app_mod.app.test_client()
    resp = client.post(
        "/api/client/local/start",
        json={},
        environ_base={"REMOTE_ADDR": "127.0.0.1", "SERVER_PORT": "5001"},
    )
    body = resp.get_json()

    assert resp.status_code == 500
    assert body["ok"] is False
    assert body["mode"] == "local"
    assert body["local_only"] is True
    assert "配置保存失败" in body["error"]
    assert body["user_id"].startswith("u_")


def test_local_single_device_invitation_endpoint_is_explicitly_local(monkeypatch, tmp_path):
    import app as app_mod

    _patch_data_dir(monkeypatch, tmp_path)
    local = auth.get_or_create_local_single_device_user()
    monkeypatch.setattr(app_mod, "_is_client_mode", True)
    monkeypatch.setattr(app_mod, "_client_mode_config", {
        "server_url": "http://127.0.0.1:5001",
        "auth_token": local["token"],
        "user_id": local["user_id"],
        "mode": "local",
        "local_only": True,
    })

    client = app_mod.app.test_client()
    resp = client.get(
        "/api/auth/invitation",
        headers={"Authorization": "Bearer " + local["token"]},
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )
    body = resp.get_json()

    assert resp.status_code == 200
    assert body["ok"] is True
    assert body["mode"] == "local"
    assert body["local_only"] is True
    assert body["invitation_code"] == ""
    assert body["server_url"] == ""


def test_local_single_device_owner_ai_config_writes_local_config(monkeypatch, tmp_path):
    import ai_config
    import app as app_mod

    _patch_data_dir(monkeypatch, tmp_path)
    ai_config.invalidate_cache()
    local = auth.get_or_create_local_single_device_user()
    monkeypatch.setattr(app_mod, "_is_client_mode", True)
    monkeypatch.setattr(app_mod, "_client_mode_config", {
        "server_url": "http://127.0.0.1:5001",
        "auth_token": local["token"],
        "user_id": local["user_id"],
        "mode": "local",
        "local_only": True,
    })

    client = app_mod.app.test_client()
    headers = {"Authorization": "Bearer " + local["token"], "Content-Type": "application/json"}
    res = client.get(
        "/api/owner/ai-config",
        headers=headers,
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )
    assert res.status_code == 200
    assert res.get_json()["owner"]["owner_user_id"] == local["user_id"]

    secret = "sk-local-secret-123456"
    patch = client.patch(
        "/api/owner/ai-config/chat",
        headers=headers,
        json={
            "host": "https://api.deepseek.com/v1",
            "model": "deepseek-chat",
            "api_key": secret,
            "max_tokens": 4096,
        },
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )
    raw = patch.get_data(as_text=True)
    assert patch.status_code == 200
    assert secret not in raw
    body = patch.get_json()
    assert body["config"]["tiers"]["chat"]["has_api_key"] is True
    assert body["config"]["tiers"]["chat"]["api_key_masked"] == "sk-l***3456"

    saved = json.loads((tmp_path / "data" / "_admin" / "ai_config.json").read_text(encoding="utf-8"))
    assert saved["tiers"]["chat"]["host"] == "api.deepseek.com/v1"
    assert saved["tiers"]["chat"]["model"] == "deepseek-chat"
    assert saved["tiers"]["chat"]["api_key"] == secret


def test_local_single_device_chat_does_not_forward_to_remote(monkeypatch, tmp_path):
    import app as app_mod

    _patch_data_dir(monkeypatch, tmp_path)
    local = auth.get_or_create_local_single_device_user()
    monkeypatch.setattr(app_mod, "_is_client_mode", True)
    monkeypatch.setattr(app_mod, "_client_mode_config", {
        "server_url": "http://127.0.0.1:5001",
        "auth_token": local["token"],
        "user_id": local["user_id"],
        "mode": "local",
        "local_only": True,
    })

    def fail_forward(*args, **kwargs):
        raise AssertionError("local single-device chat must not forward to a remote server")

    calls = []

    def fake_receive(text, image=None, device_id=""):
        calls.append({"text": text, "image": image, "device_id": device_id})
        return {"ok": True, "queued": True}

    monkeypatch.setattr(app_mod, "_forward_chat_to_vps", fail_forward)
    monkeypatch.setattr(app_mod.core, "receive_chat_message", fake_receive)

    client = app_mod.app.test_client()
    resp = client.post(
        "/api/chat",
        json={"text": "本地模式测试"},
        headers={"Authorization": "Bearer " + local["token"], "X-Device-Id": "mac_local"},
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True, "queued": True}
    assert calls == [{"text": "本地模式测试", "image": None, "device_id": "mac_local"}]


def test_local_single_device_starts_local_backend_services_once(monkeypatch, tmp_path):
    import app as app_mod
    import core_memory
    import curator
    import memory
    import screen_analyzer

    _patch_data_dir(monkeypatch, tmp_path)
    local = auth.get_or_create_local_single_device_user()
    calls = []

    class FakeAnalyzer:
        def set_vlm_interval(self, interval):
            calls.append(("vlm_interval", interval))

    monkeypatch.setattr(app_mod, "_is_client_mode", True)
    monkeypatch.setattr(app_mod, "_client_mode_config", {
        "server_url": "http://127.0.0.1:5001",
        "auth_token": local["token"],
        "user_id": local["user_id"],
        "mode": "local",
        "local_only": True,
        "setup_complete": True,
    })
    monkeypatch.setattr(app_mod, "_client_local_runtime_user_id", "")
    monkeypatch.setattr(app_mod, "_client_local_backend_services_started", False)
    monkeypatch.setattr(app_mod, "_start_reminder_loop", lambda: calls.append(("reminder", None)))
    monkeypatch.setattr(app_mod, "_start_attention_engine_spawner", lambda interval=300: calls.append(("attention", interval)))
    monkeypatch.setattr(memory, "ensure_dirs", lambda: calls.append(("memory", None)))
    monkeypatch.setattr(core_memory, "initialize_from_existing", lambda: {"actions": ["seeded"]})
    monkeypatch.setattr(screen_analyzer, "get_analyzer", lambda: FakeAnalyzer())
    monkeypatch.setattr(
        curator,
        "start_loop_for_user",
        lambda uid, data_dir: calls.append(("curator", uid, data_dir)),
    )

    started = app_mod._start_local_single_device_backend_services_once(port=5001)
    duplicate = app_mod._start_local_single_device_backend_services_once(port=5001)

    assert started is True
    assert duplicate is False
    assert calls.count(("reminder", None)) == 1
    assert calls.count(("attention", 300)) == 1
    assert ("memory", None) in calls
    assert ("vlm_interval", 30) in calls
    assert calls.count((
        "curator",
        local["user_id"],
        auth.get_user_data_dir(local["user_id"]),
    )) == 2
    assert app_mod._client_local_runtime_user_id == local["user_id"]


def test_remote_client_mode_does_not_start_local_backend_services(monkeypatch, tmp_path):
    import app as app_mod
    import curator

    _patch_data_dir(monkeypatch, tmp_path)
    local = auth.get_or_create_local_single_device_user()
    calls = []
    monkeypatch.setattr(app_mod, "_is_client_mode", True)
    monkeypatch.setattr(app_mod, "_client_local_backend_services_started", False)
    monkeypatch.setattr(app_mod, "_client_mode_config", {
        "server_url": "http://203.0.113.42:5001",
        "auth_token": local["token"],
        "user_id": local["user_id"],
        "mode": "remote",
        "local_only": False,
        "setup_complete": True,
    })
    monkeypatch.setattr(app_mod, "_client_local_runtime_user_id", "")
    monkeypatch.setattr(app_mod, "_start_reminder_loop", lambda: calls.append("reminder"))
    monkeypatch.setattr(app_mod, "_start_attention_engine_spawner", lambda interval=300: calls.append("attention"))
    monkeypatch.setattr(curator, "start_loop_for_user", lambda *args: calls.append("curator"))

    assert app_mod._start_local_single_device_backend_services_once(port=5001) is False
    assert calls == []


def test_pending_local_setup_does_not_start_curator(monkeypatch, tmp_path):
    import app as app_mod
    import curator

    _patch_data_dir(monkeypatch, tmp_path)
    local = auth.get_or_create_local_single_device_user()
    calls = []
    monkeypatch.setattr(app_mod, "_is_client_mode", True)
    monkeypatch.setattr(app_mod, "_client_local_backend_services_started", False)
    monkeypatch.setattr(app_mod, "_client_mode_config", {
        "server_url": "http://127.0.0.1:5001",
        "auth_token": local["token"],
        "user_id": local["user_id"],
        "mode": "local",
        "local_only": True,
        "setup_complete": False,
    })
    monkeypatch.setattr(app_mod, "_client_local_runtime_user_id", "")
    monkeypatch.setattr(curator, "start_loop_for_user", lambda *args: calls.append(args))

    assert app_mod._start_local_single_device_backend_services_once(port=5001) is False
    assert calls == []
    assert app_mod._client_local_runtime_user_id == ""


def test_client_setup_complete_marks_config_and_starts_runtime(monkeypatch, tmp_path):
    import app as app_mod

    _patch_data_dir(monkeypatch, tmp_path)
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(
        json.dumps({
            "mode": "local",
            "local_only": True,
            "invitation_code": "",
            "server_url": "http://127.0.0.1:5001",
            "auth_token": "tok_local",
            "user_id": "u_local",
            "device_name": "MacBook",
            "setup_complete": False,
        }),
        encoding="utf-8",
    )
    triggered = []
    monkeypatch.setattr(app_mod, "_is_client_mode", True)
    monkeypatch.setattr(app_mod, "_launcher_config_path", lambda: cfg_path)
    monkeypatch.setattr(app_mod, "_deferred_client_setup_trigger", lambda url, token: triggered.append((url, token)))
    _patch_client_runtime(monkeypatch, app_mod, {
        "server_url": "http://127.0.0.1:5001",
        "auth_token": "tok_local",
        "user_id": "u_local",
        "mode": "local",
        "local_only": True,
        "setup_complete": False,
    })
    monkeypatch.setattr(app_mod, "_stop_client_screen_sensor", lambda: None)

    client = app_mod.app.test_client()
    resp = client.post(
        "/api/client/setup/complete",
        json={},
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )
    body = resp.get_json()

    assert resp.status_code == 200
    assert body["ok"] is True
    assert body["setup_complete"] is True
    assert triggered == [("http://127.0.0.1:5001", "tok_local")]
    saved = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert saved["setup_complete"] is True
    assert app_mod._client_mode_config["setup_complete"] is True


def test_client_mode_logout_only_allows_current_remote_page(monkeypatch, tmp_path):
    import app as app_mod

    _patch_data_dir(monkeypatch, tmp_path)
    cleared = []
    sensor_stops = []
    singleton_cleanups = []
    monkeypatch.setattr(app_mod, "_is_client_mode", True)
    _patch_client_runtime(monkeypatch, app_mod, {
        "server_url": "http://203.0.113.10:5001",
        "auth_token": "tok_remote",
        "user_id": "u_remote",
        "mode": "remote",
        "local_only": False,
        "setup_complete": True,
    })
    monkeypatch.setattr(app_mod, "_launcher_config_path", lambda: tmp_path / "config.json")
    monkeypatch.setattr(app_mod, "_pet_should_hide_event", type("E", (), {"set": lambda self: cleared.append(True)})())
    monkeypatch.setattr(app_mod, "_stop_client_screen_sensor", lambda: sensor_stops.append(True))
    monkeypatch.setattr(auth, "_cleanup_user_singletons", lambda uid: singleton_cleanups.append(uid))
    (tmp_path / "config.json").write_text(
        json.dumps({
            "server_url": "http://203.0.113.10:5001",
            "auth_token": "tok_remote",
            "user_id": "u_remote",
        }),
        encoding="utf-8",
    )

    client = app_mod.app.test_client()
    evil = client.post(
        "/api/auth/logout",
        headers={"Origin": "http://evil.example:5001"},
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert evil.status_code == 403
    assert "Access-Control-Allow-Origin" not in evil.headers
    saved = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert saved["auth_token"] == "tok_remote"
    assert saved["user_id"] == "u_remote"
    assert cleared == []
    assert sensor_stops == []

    resp = client.post(
        "/api/auth/logout",
        headers={"Origin": "http://203.0.113.10:5001"},
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert resp.status_code == 200
    assert resp.headers["Access-Control-Allow-Origin"] == "http://203.0.113.10:5001"
    saved = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert saved["auth_token"] == ""
    assert saved["user_id"] == ""
    assert cleared == [True]
    assert sensor_stops == [True]
    assert singleton_cleanups == []


def test_background_user_selection_preserves_server_mode(monkeypatch):
    import app as app_mod

    engaged = ["u_server_a", "u_server_b"]
    calls = []
    monkeypatch.setattr(app_mod, "_is_client_mode", False)
    monkeypatch.setattr(
        auth,
        "get_engaged_user_ids",
        lambda days=7: calls.append(days) or list(engaged),
    )
    monkeypatch.setattr(app_mod, "_client_local_runtime_user_id", "u_stale_local")

    assert app_mod._get_background_user_ids(days=3) == engaged
    assert calls == [3]


def test_background_user_selection_uses_fresh_active_local_account(monkeypatch, tmp_path):
    import app as app_mod

    _patch_data_dir(monkeypatch, tmp_path)
    local = auth.get_or_create_local_single_device_user()
    cfg = {
        "server_url": "http://127.0.0.1:5001",
        "auth_token": local["token"],
        "user_id": local["user_id"],
        "mode": "local",
        "local_only": True,
        "setup_complete": True,
    }
    monkeypatch.setattr(app_mod, "_is_client_mode", True)
    _patch_client_runtime(
        monkeypatch,
        app_mod,
        cfg,
        active_local_user_id=local["user_id"],
    )
    monkeypatch.setattr(
        auth,
        "get_engaged_user_ids",
        lambda days=7: (_ for _ in ()).throw(
            AssertionError("local runtime selection must not depend on engagement")
        ),
    )

    assert app_mod._get_background_user_ids(days=7) == [local["user_id"]]

    app_mod._client_local_runtime_user_id = ""
    assert app_mod._get_background_user_ids(days=7) == []

    app_mod._client_local_runtime_user_id = "u_mismatched_runtime"
    assert app_mod._get_background_user_ids(days=7) == []

    app_mod._client_local_runtime_user_id = local["user_id"]

    app_mod._client_mode_config = {
        **cfg,
        "mode": "remote",
        "local_only": False,
        "server_url": "http://203.0.113.42:5001",
    }
    assert app_mod._get_background_user_ids(days=7) == []

    app_mod._client_mode_config = {**cfg, "setup_complete": False}
    assert app_mod._get_background_user_ids(days=7) == []


def test_fresh_local_curator_survives_spawner_style_sweep(monkeypatch, tmp_path):
    import app as app_mod
    import curator

    _patch_data_dir(monkeypatch, tmp_path)
    local = auth.get_or_create_local_single_device_user()
    uid = local["user_id"]
    cfg = {
        "server_url": "http://127.0.0.1:5001",
        "auth_token": local["token"],
        "user_id": uid,
        "mode": "local",
        "local_only": True,
        "setup_complete": True,
    }
    monkeypatch.setattr(app_mod, "_is_client_mode", True)
    _patch_client_runtime(
        monkeypatch,
        app_mod,
        cfg,
        active_local_user_id=uid,
    )
    monkeypatch.setattr(auth, "get_engaged_user_ids", lambda days=7: [])

    curator.start_loop_for_user(uid, auth.get_user_data_dir(uid))
    thread = curator._curator_user_threads[uid]
    try:
        selected = set(app_mod._get_background_user_ids(days=7))
        if uid not in selected:
            curator.stop_loop_for_user(uid)

        assert selected == {uid}
        assert curator._curator_user_threads.get(uid) is thread
        assert thread.is_alive()
    finally:
        curator.stop_loop_for_user(uid)


def test_direct_remote_local_switch_retires_only_mac_owned_runtime(monkeypatch, tmp_path):
    import app as app_mod
    import curator

    _patch_data_dir(monkeypatch, tmp_path)
    local = auth.get_or_create_local_single_device_user()
    remote_cfg = {
        "server_url": "http://203.0.113.42:5001",
        "auth_token": "tok_remote",
        "user_id": "u_remote_on_vps",
        "mode": "remote",
        "local_only": False,
        "setup_complete": True,
    }
    monkeypatch.setattr(app_mod, "_is_client_mode", True)
    remote_event = _patch_client_runtime(monkeypatch, app_mod, remote_cfg, generation=10)
    monkeypatch.setattr(app_mod, "_client_local_backend_services_started", True)
    sensor_stops = []
    cleanups = []
    curator_starts = []
    monkeypatch.setattr(app_mod, "_stop_client_screen_sensor", lambda: sensor_stops.append(True))
    monkeypatch.setattr(auth, "_cleanup_user_singletons", lambda uid: cleanups.append(uid))
    monkeypatch.setattr(
        curator,
        "start_loop_for_user",
        lambda uid, data_dir: curator_starts.append((uid, data_dir)),
    )

    local_cfg = {
        "server_url": "http://127.0.0.1:5001",
        "auth_token": local["token"],
        "user_id": local["user_id"],
        "mode": "local",
        "local_only": True,
        "setup_complete": True,
    }
    local_generation, local_event = app_mod._replace_client_runtime_config(local_cfg)

    assert local_generation == 11
    assert remote_event.is_set()
    assert sensor_stops == [True]
    assert cleanups == []  # the VPS user/runtime is never cleaned from this Mac
    assert app_mod._start_local_single_device_backend_services_once(
        expected_generation=local_generation,
        cancel_event=local_event,
    ) is False  # process-global loops were already running
    assert curator_starts == [(
        local["user_id"],
        auth.get_user_data_dir(local["user_id"]),
    )]
    assert app_mod._client_local_runtime_user_id == local["user_id"]

    next_remote_cfg = {**remote_cfg, "auth_token": "tok_remote_next"}
    remote_generation, next_event = app_mod._replace_client_runtime_config(next_remote_cfg)

    assert remote_generation == 12
    assert local_event.is_set()
    assert not next_event.is_set()
    assert sensor_stops == [True, True]
    assert cleanups == [local["user_id"]]
    assert app_mod._client_local_runtime_user_id == ""
    assert auth.get_user_data_dir(local["user_id"])  # switch keeps local data


def test_local_curator_start_is_serialized_with_account_replacement(monkeypatch, tmp_path):
    import app as app_mod
    import curator

    _patch_data_dir(monkeypatch, tmp_path)
    local = auth.get_or_create_local_single_device_user()
    local_cfg = {
        "server_url": "http://127.0.0.1:5001",
        "auth_token": local["token"],
        "user_id": local["user_id"],
        "mode": "local",
        "local_only": True,
        "setup_complete": True,
    }
    remote_cfg = {
        "server_url": "http://server.example:5001",
        "auth_token": "tok_remote",
        "user_id": "u_remote",
        "mode": "remote",
        "local_only": False,
        "setup_complete": True,
    }
    monkeypatch.setattr(app_mod, "_is_client_mode", True)
    local_event = _patch_client_runtime(monkeypatch, app_mod, local_cfg, generation=40)
    monkeypatch.setattr(app_mod, "_client_local_backend_services_started", True)
    start_entered = threading.Event()
    allow_start_to_finish = threading.Event()
    switch_done = threading.Event()
    order = []
    errors = []

    def fake_curator_start(uid, data_dir):
        order.append("curator_start")
        start_entered.set()
        if not allow_start_to_finish.wait(timeout=2):
            raise AssertionError("test did not release Curator start")
        order.append("curator_started")

    def run_local_start():
        try:
            app_mod._start_local_single_device_backend_services_once(
                expected_generation=40,
                cancel_event=local_event,
            )
        except Exception as exc:
            errors.append(exc)

    def run_switch():
        try:
            app_mod._replace_client_runtime_config(remote_cfg)
            switch_done.set()
        except Exception as exc:
            errors.append(exc)

    monkeypatch.setattr(curator, "start_loop_for_user", fake_curator_start)
    monkeypatch.setattr(app_mod, "_stop_client_screen_sensor", lambda: order.append("sensor_stop"))
    monkeypatch.setattr(
        auth,
        "_cleanup_user_singletons",
        lambda uid: order.append("local_cleanup"),
    )

    starter = threading.Thread(target=run_local_start)
    starter.start()
    assert start_entered.wait(timeout=1)
    switcher = threading.Thread(target=run_switch)
    switcher.start()

    assert not switch_done.wait(timeout=0.05)
    allow_start_to_finish.set()
    starter.join(timeout=2)
    switcher.join(timeout=2)

    assert errors == []
    assert switch_done.is_set()
    assert order.index("curator_started") < order.index("local_cleanup")
    assert local_event.is_set()
    assert app_mod._client_local_runtime_user_id == ""


def test_sensor_binding_is_serialized_with_account_replacement(monkeypatch):
    import app as app_mod
    import sensor

    old_cfg = {
        "server_url": "http://old.example:5001",
        "auth_token": "tok_old",
        "user_id": "u_old",
        "mode": "remote",
        "local_only": False,
        "setup_complete": True,
    }
    new_cfg = {
        "server_url": "http://new.example:5001",
        "auth_token": "tok_new",
        "user_id": "u_new",
        "mode": "remote",
        "local_only": False,
        "setup_complete": True,
    }
    monkeypatch.setattr(app_mod, "_is_client_mode", True)
    old_event = _patch_client_runtime(monkeypatch, app_mod, old_cfg, generation=50)
    start_entered = threading.Event()
    allow_start_to_finish = threading.Event()
    switch_done = threading.Event()
    order = []
    errors = []

    class FakeSensor:
        def start(self):
            order.append("sensor_start")
            start_entered.set()
            if not allow_start_to_finish.wait(timeout=2):
                raise AssertionError("test did not release sensor start")
            order.append("sensor_started")

    monkeypatch.setattr(sensor, "get_sensor", lambda **kwargs: FakeSensor())
    monkeypatch.setattr(app_mod, "_stop_client_screen_sensor", lambda: order.append("sensor_stop"))

    def run_sensor_start():
        try:
            app_mod._start_client_screen_sensor(
                old_cfg["server_url"],
                "mac_device",
                old_cfg["auth_token"],
                50,
                old_event,
            )
        except Exception as exc:
            errors.append(exc)

    def run_switch():
        try:
            app_mod._replace_client_runtime_config(new_cfg)
            switch_done.set()
        except Exception as exc:
            errors.append(exc)

    starter = threading.Thread(target=run_sensor_start)
    starter.start()
    assert start_entered.wait(timeout=1)
    switcher = threading.Thread(target=run_switch)
    switcher.start()

    assert not switch_done.wait(timeout=0.05)
    allow_start_to_finish.set()
    starter.join(timeout=2)
    switcher.join(timeout=2)

    assert errors == []
    assert switch_done.is_set()
    assert order == ["sensor_start", "sensor_started", "sensor_stop"]
    assert old_event.is_set()
    assert app_mod._client_mode_config == new_cfg


def test_local_logout_stops_singletons_but_keeps_account_data(monkeypatch, tmp_path):
    import app as app_mod

    _patch_data_dir(monkeypatch, tmp_path)
    local = auth.get_or_create_local_single_device_user()
    cfg = {
        "server_url": "http://127.0.0.1:5001",
        "auth_token": local["token"],
        "user_id": local["user_id"],
        "mode": "local",
        "local_only": True,
        "setup_complete": True,
    }
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setattr(app_mod, "_is_client_mode", True)
    old_event = _patch_client_runtime(
        monkeypatch,
        app_mod,
        cfg,
        generation=4,
        active_local_user_id=local["user_id"],
    )
    sensor_stops = []
    cleanups = []
    pet_hides = []
    monkeypatch.setattr(app_mod, "_launcher_config_path", lambda: cfg_path)
    monkeypatch.setattr(app_mod, "_stop_client_screen_sensor", lambda: sensor_stops.append(True))
    monkeypatch.setattr(auth, "_cleanup_user_singletons", lambda uid: cleanups.append(uid))
    monkeypatch.setattr(
        app_mod,
        "_pet_should_hide_event",
        type("E", (), {"set": lambda self: pet_hides.append(True)})(),
    )

    assert app_mod._force_relogin("test local logout", expected_generation=4) is True
    assert old_event.is_set()
    assert app_mod._client_runtime_generation == 5
    assert app_mod._client_local_runtime_user_id == ""
    assert sensor_stops == [True]
    assert cleanups == [local["user_id"]]
    assert pet_hides == [True]
    assert auth.get_user(local["user_id"])["status"] == "active"
    assert (tmp_path / "data" / "users" / local["user_id"]).is_dir()
    saved = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert saved["auth_token"] == ""
    assert saved["user_id"] == ""


def test_stale_session_cannot_force_relogin_current_account(monkeypatch):
    import app as app_mod

    old_cfg = {
        "server_url": "http://old.example:5001",
        "auth_token": "tok_old",
        "user_id": "u_old",
        "mode": "remote",
        "local_only": False,
        "setup_complete": True,
    }
    new_cfg = {
        "server_url": "http://new.example:5001",
        "auth_token": "tok_new",
        "user_id": "u_new",
        "mode": "remote",
        "local_only": False,
        "setup_complete": True,
    }
    monkeypatch.setattr(app_mod, "_is_client_mode", True)
    old_event = _patch_client_runtime(monkeypatch, app_mod, old_cfg, generation=20)
    monkeypatch.setattr(app_mod, "_stop_client_screen_sensor", lambda: None)

    generation, current_event = app_mod._replace_client_runtime_config(new_cfg)

    assert generation == 21
    assert old_event.is_set()
    assert app_mod._force_relogin("stale heartbeat", expected_generation=20) is False
    assert app_mod._client_mode_config == new_cfg
    assert not current_event.is_set()


def test_client_heartbeat_is_scoped_to_runtime_generation(monkeypatch):
    import app as app_mod
    import requests

    cfg = {
        "server_url": "http://server.example:5001",
        "auth_token": "tok_current",
        "user_id": "u_current",
        "mode": "remote",
        "local_only": False,
        "setup_complete": True,
    }
    monkeypatch.setattr(app_mod, "_is_client_mode", True)
    cancel_event = _patch_client_runtime(monkeypatch, app_mod, cfg, generation=30)
    posts = []
    relogins = []

    class Response:
        status_code = 401

    def fake_post(url, **kwargs):
        posts.append((url, kwargs))
        return Response()

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(
        app_mod,
        "_force_relogin",
        lambda reason, expected_generation=None: relogins.append(
            (reason, expected_generation)
        ) or True,
    )

    app_mod._client_heartbeat_loop(
        cfg["server_url"],
        cfg["auth_token"],
        "mac_device",
        30,
        cancel_event,
        interval=0,
    )

    assert len(posts) == 3
    assert relogins == [("heartbeat 401 x3", 30)]

    posts.clear()
    app_mod._client_heartbeat_loop(
        cfg["server_url"],
        cfg["auth_token"],
        "mac_device",
        29,
        threading.Event(),
        interval=0,
    )
    assert posts == []


def test_local_client_heartbeat_restores_a_deleted_desktop_record(monkeypatch):
    import app as app_mod
    import requests

    cfg = {
        "server_url": "http://127.0.0.1:5001",
        "auth_token": "tok_local",
        "user_id": "u_local",
        "mode": "local",
        "local_only": True,
        "setup_complete": True,
    }

    class OneIterationEvent:
        def is_set(self):
            return False

        def wait(self, timeout=None):
            return True

    class Response:
        def __init__(self, body):
            self.status_code = 200
            self._body = body

        def json(self):
            return self._body

    cancel_event = OneIterationEvent()
    monkeypatch.setattr(app_mod, "_is_client_mode", True)
    monkeypatch.setattr(app_mod, "_client_mode_config", dict(cfg))
    monkeypatch.setattr(app_mod, "_client_runtime_generation", 31)
    monkeypatch.setattr(app_mod, "_client_runtime_cancel_event", cancel_event)
    posts = []

    def fake_post(url, **kwargs):
        posts.append((url, kwargs["json"]))
        if url.endswith("/api/device/heartbeat"):
            return Response({"ok": False})
        return Response({"ok": True})

    monkeypatch.setattr(requests, "post", fake_post)
    registration = {
        "name": "Windows PC",
        "type": "desktop",
        "platform": "win32",
        "device_id": "local_windows",
    }

    app_mod._client_heartbeat_loop(
        cfg["server_url"],
        cfg["auth_token"],
        "local_windows",
        31,
        cancel_event,
        interval=0,
        restore_missing_device=True,
        device_registration=registration,
    )

    assert posts == [
        ("http://127.0.0.1:5001/api/device/heartbeat", {"device_id": "local_windows"}),
        ("http://127.0.0.1:5001/api/device/register", registration),
    ]


def test_remote_client_heartbeat_does_not_restore_missing_devices(monkeypatch):
    import app as app_mod
    import requests

    cfg = {
        "server_url": "http://server.example:5001",
        "auth_token": "tok_remote",
        "user_id": "u_remote",
        "mode": "remote",
        "local_only": False,
        "setup_complete": True,
    }

    class OneIterationEvent:
        def is_set(self):
            return False

        def wait(self, timeout=None):
            return True

    class Response:
        status_code = 200

        def json(self):
            return {"ok": False}

    cancel_event = OneIterationEvent()
    monkeypatch.setattr(app_mod, "_is_client_mode", True)
    monkeypatch.setattr(app_mod, "_client_mode_config", dict(cfg))
    monkeypatch.setattr(app_mod, "_client_runtime_generation", 32)
    monkeypatch.setattr(app_mod, "_client_runtime_cancel_event", cancel_event)
    posts = []
    monkeypatch.setattr(
        requests,
        "post",
        lambda url, **kwargs: posts.append(url) or Response(),
    )

    app_mod._client_heartbeat_loop(
        cfg["server_url"],
        cfg["auth_token"],
        "remote_windows",
        32,
        cancel_event,
        interval=0,
        restore_missing_device=False,
    )

    assert posts == ["http://server.example:5001/api/device/heartbeat"]


def test_remote_client_mode_chat_still_forwards(monkeypatch, tmp_path):
    import app as app_mod

    _patch_data_dir(monkeypatch, tmp_path)
    local = auth.get_or_create_local_single_device_user()
    monkeypatch.setattr(app_mod, "_is_client_mode", True)
    monkeypatch.setattr(app_mod, "_client_mode_config", {
        "server_url": "http://203.0.113.42:5001",
        "auth_token": local["token"],
        "user_id": local["user_id"],
        "mode": "remote",
        "local_only": False,
    })

    forwarded = []

    def fake_forward(text, image_file):
        forwarded.append({"text": text, "image_file": image_file})
        return app_mod.jsonify({"ok": True, "forwarded": True})

    def fail_core(*args, **kwargs):
        raise AssertionError("remote client-mode chat must forward to the private server")

    monkeypatch.setattr(app_mod, "_forward_chat_to_vps", fake_forward)
    monkeypatch.setattr(app_mod.core, "receive_chat_message", fail_core)

    client = app_mod.app.test_client()
    resp = client.post(
        "/api/chat",
        json={"text": "远端模式测试"},
        headers={"Authorization": "Bearer " + local["token"]},
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True, "forwarded": True}
    assert forwarded == [{"text": "远端模式测试", "image_file": None}]


def test_legacy_client_mode_chat_still_forwards(monkeypatch, tmp_path):
    import app as app_mod

    _patch_data_dir(monkeypatch, tmp_path)
    local = auth.get_or_create_local_single_device_user()
    monkeypatch.setattr(app_mod, "_is_client_mode", True)
    monkeypatch.setattr(app_mod, "_client_mode_config", {
        "server_url": "http://203.0.113.42:5001",
        "auth_token": local["token"],
        "user_id": local["user_id"],
    })

    forwarded = []

    def fake_forward(text, image_file):
        forwarded.append({"text": text, "image_file": image_file})
        return app_mod.jsonify({"ok": True, "forwarded": True})

    def fail_core(*args, **kwargs):
        raise AssertionError("legacy client-mode chat must forward to the private server")

    monkeypatch.setattr(app_mod, "_forward_chat_to_vps", fake_forward)
    monkeypatch.setattr(app_mod.core, "receive_chat_message", fail_core)

    client = app_mod.app.test_client()
    resp = client.post(
        "/api/chat",
        json={"text": "legacy 模式测试"},
        headers={"Authorization": "Bearer " + local["token"]},
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True, "forwarded": True}
    assert forwarded == [{"text": "legacy 模式测试", "image_file": None}]
