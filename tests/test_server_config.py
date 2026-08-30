"""Tests for server_config.py — global settings (port, flask_debug, VAPID, default_timezone).

No Flask context needed. Uses DATA_DIR env var to isolate per test.
"""

import importlib
import json
import os


def _reload_server_config(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("PORT", raising=False)
    monkeypatch.delenv("FLASK_DEBUG", raising=False)
    monkeypatch.delenv("VAPID_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("VAPID_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("TIMEZONE", raising=False)
    import server_config
    importlib.reload(server_config)
    return server_config


def test_persist_and_mask(monkeypatch, tmp_path):
    sc = _reload_server_config(monkeypatch, tmp_path)

    result = sc.update({
        "port": 6123,
        "flask_debug": False,
        "vapid_public_key": "public-xyz",
        "vapid_private_key": "private-abcdefgh1234",
        "default_timezone": "Asia/Shanghai",
    })
    assert result["ok"] is True

    cfg = sc.get()
    assert cfg["port"] == 6123
    assert cfg["flask_debug"] is False
    assert cfg["vapid_public_key"] == "public-xyz"
    assert cfg["vapid_private_key"] == "private-abcdefgh1234"
    assert cfg["default_timezone"] == "Asia/Shanghai"


def test_saved_overrides_env(monkeypatch, tmp_path):
    monkeypatch.setenv("PORT", "7001")
    monkeypatch.setenv("FLASK_DEBUG", "1")
    monkeypatch.setenv("VAPID_PUBLIC_KEY", "env-public")
    monkeypatch.setenv("VAPID_PRIVATE_KEY", "env-private")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import server_config
    importlib.reload(server_config)

    result = server_config.update({
        "port": 7333,
        "flask_debug": False,
        "vapid_public_key": "saved-public",
        "vapid_private_key": "saved-private",
    })
    assert result["ok"] is True

    cfg = server_config.get()
    assert cfg["port"] == 7333
    assert cfg["flask_debug"] is False
    assert cfg["vapid_public_key"] == "saved-public"
    assert cfg["vapid_private_key"] == "saved-private"


def test_invalid_timezone_rejected(monkeypatch, tmp_path):
    sc = _reload_server_config(monkeypatch, tmp_path)
    result = sc.update({"default_timezone": "Mars/Olympus"})
    assert result["ok"] is False
    assert "Invalid timezone" in result["error"]


def test_invalid_port_rejected(monkeypatch, tmp_path):
    sc = _reload_server_config(monkeypatch, tmp_path)
    for bad in [0, 70000, "abc"]:
        r = sc.update({"port": bad})
        assert r["ok"] is False


def test_clear_vapid_triggers_regeneration(monkeypatch, tmp_path):
    """Clearing VAPID public key causes get() to auto-generate a fresh keypair."""
    sc = _reload_server_config(monkeypatch, tmp_path)
    sc.update({"vapid_public_key": "abc", "vapid_private_key": "def"})
    # update() returns config through get(), which triggers regeneration since
    # the saved key was a dummy 'abc' (but non-empty); clear explicitly:
    sc.update({"clear_vapid_public_key": True})
    cfg = sc.get()
    # After clear + get(), a real keypair should be regenerated (non-empty, non-dummy)
    assert cfg["vapid_public_key"]
    assert cfg["vapid_public_key"] != "abc"
    assert cfg["vapid_private_key"]


def test_migrate_splits_and_backs_up(monkeypatch, tmp_path):
    """Legacy mixed app_settings.json → server_config.json + per-user user_settings.json."""
    sc = _reload_server_config(monkeypatch, tmp_path)

    # Root legacy file (server+user mixed)
    with open(os.path.join(tmp_path, "app_settings.json"), "w") as f:
        json.dump({
            "port": 5001,
            "flask_debug": True,
            "vapid_public_key": "root-pub",
            "vapid_private_key": "root-pri",
            "auto_screenshot_interval": 120,  # leaked user field on root
        }, f)

    # Two per-user legacy files — one with timezone, one with a newer VAPID
    users_dir = os.path.join(tmp_path, "users")
    os.makedirs(os.path.join(users_dir, "u_aaa"), exist_ok=True)
    os.makedirs(os.path.join(users_dir, "u_bbb"), exist_ok=True)
    with open(os.path.join(users_dir, "u_aaa", "app_settings.json"), "w") as f:
        json.dump({
            "vapid_public_key": "user-aaa-pub",
            "vapid_private_key": "user-aaa-pri",
            "pet_hotkey": "cmd+e",
            "pet_collapse_delay": 8000,
        }, f)
    with open(os.path.join(users_dir, "u_bbb", "app_settings.json"), "w") as f:
        json.dump({
            "timezone": "Asia/Shanghai",
            "auto_screenshot_interval": 45,
            "pet_chat_position": 70,
        }, f)

    summary = sc.migrate_legacy_settings()
    assert summary["server_migrated"] is True

    # server_config.json should exist with port+flask_debug, user VAPID preferred
    server_path = os.path.join(tmp_path, "_admin", "server_config.json")
    assert os.path.exists(server_path)
    with open(server_path) as f:
        server = json.load(f)
    assert server["port"] == 5001
    assert server["flask_debug"] is True
    assert server["vapid_public_key"] == "user-aaa-pub"  # user-dir wins over root
    # default_timezone picked up from u_bbb (Fix: independent of VAPID break)
    assert server["default_timezone"] == "Asia/Shanghai"

    # Per-user user_settings.json created with only user fields
    aaa_path = os.path.join(users_dir, "u_aaa", "user_settings.json")
    bbb_path = os.path.join(users_dir, "u_bbb", "user_settings.json")
    assert os.path.exists(aaa_path)
    assert os.path.exists(bbb_path)
    with open(aaa_path) as f:
        aaa = json.load(f)
    with open(bbb_path) as f:
        bbb = json.load(f)
    assert aaa["pet_hotkey"] == "cmd+e"
    assert aaa["pet_collapse_delay"] == 8000
    assert "vapid_public_key" not in aaa  # server fields filtered out
    assert bbb["timezone"] == "Asia/Shanghai"
    assert bbb["auto_screenshot_interval"] == 45

    # Old files renamed to .bak
    assert os.path.exists(os.path.join(tmp_path, "app_settings.json.bak"))
    assert os.path.exists(os.path.join(users_dir, "u_aaa", "app_settings.json.bak"))

    # Idempotent — second call is a no-op
    summary2 = sc.migrate_legacy_settings()
    assert summary2["server_migrated"] is False
    assert summary2["users_migrated"] == []


def test_migrate_timezone_picked_up_when_first_user_has_no_tz(monkeypatch, tmp_path):
    """Regression: the earlier implementation broke out of the scan loop as soon
    as it found a user-dir VAPID, so a user later in alphabetical order whose only
    contribution is `timezone` would never be read."""
    sc = _reload_server_config(monkeypatch, tmp_path)

    users_dir = os.path.join(tmp_path, "users")
    os.makedirs(os.path.join(users_dir, "u_aaa"), exist_ok=True)  # has VAPID, no tz
    os.makedirs(os.path.join(users_dir, "u_zzz"), exist_ok=True)  # has tz, no VAPID
    with open(os.path.join(users_dir, "u_aaa", "app_settings.json"), "w") as f:
        json.dump({
            "vapid_public_key": "aaa-pub",
            "vapid_private_key": "aaa-pri",
        }, f)
    with open(os.path.join(users_dir, "u_zzz", "app_settings.json"), "w") as f:
        json.dump({"timezone": "Asia/Shanghai"}, f)

    sc.migrate_legacy_settings()

    server_path = os.path.join(tmp_path, "_admin", "server_config.json")
    with open(server_path) as f:
        server = json.load(f)
    assert server["vapid_public_key"] == "aaa-pub"
    assert server["default_timezone"] == "Asia/Shanghai"  # would have been "" before fix


def test_migrate_skips_if_server_config_exists(monkeypatch, tmp_path):
    sc = _reload_server_config(monkeypatch, tmp_path)

    # Pre-existing server_config.json
    admin_dir = os.path.join(tmp_path, "_admin")
    os.makedirs(admin_dir, exist_ok=True)
    with open(os.path.join(admin_dir, "server_config.json"), "w") as f:
        json.dump({"port": 9000, "vapid_public_key": "preexisting"}, f)

    # Legacy file with different values — should be ignored
    with open(os.path.join(tmp_path, "app_settings.json"), "w") as f:
        json.dump({"port": 5001, "vapid_public_key": "legacy"}, f)

    summary = sc.migrate_legacy_settings()
    assert summary["server_migrated"] is False

    with open(os.path.join(admin_dir, "server_config.json")) as f:
        server = json.load(f)
    assert server["port"] == 9000
    assert server["vapid_public_key"] == "preexisting"
