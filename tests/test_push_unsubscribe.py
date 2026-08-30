"""Tests for /api/push/unsubscribe — needed for Android APK to clean up
stale web push subscriptions (MiruConnectionService native SSE replaces it).
"""
import importlib
import os

from flask import g


def _fake_check_request(req=None):
    """Bypass auth middleware in tests."""
    import auth
    g.user_id = "_admin"
    g.user_data_dir = os.path.join(auth._BASE_DATA_DIR, "_admin")
    g.is_admin = True
    return None


def _setup(tmp_path, monkeypatch):
    data_dir = str(tmp_path / "data")
    os.environ["DATA_DIR"] = data_dir
    os.makedirs(os.path.join(data_dir, "_admin"), exist_ok=True)
    import storage
    importlib.reload(storage)
    import app as _app
    importlib.reload(_app)
    import auth
    monkeypatch.setattr(auth, "check_request", _fake_check_request)
    return _app, storage


def _seed_subs(app_mod, storage, *endpoints):
    """Seed subscriptions into the _admin user's data dir (where the endpoint
    will read from when auth bypass sets g.user_data_dir = _admin path)."""
    with app_mod.app.app_context():
        from flask import g
        import auth
        g.user_id = "_admin"
        g.user_data_dir = os.path.join(auth._BASE_DATA_DIR, "_admin")
        g.is_admin = True
        os.makedirs(g.user_data_dir, exist_ok=True)
        path = storage.push_subscriptions_path()
        subs = [{"endpoint": e, "keys": {"auth": "x", "p256dh": "y"}} for e in endpoints]
        storage.write_json(path, subs)
        return path


def _read_subs(app_mod, storage):
    with app_mod.app.app_context():
        from flask import g
        import auth
        g.user_id = "_admin"
        g.user_data_dir = os.path.join(auth._BASE_DATA_DIR, "_admin")
        g.is_admin = True
        return storage.read_json(storage.push_subscriptions_path()) or []


def test_unsubscribe_removes_matching_endpoint(tmp_path, monkeypatch):
    app_mod, storage = _setup(tmp_path, monkeypatch)
    _seed_subs(app_mod, storage, "https://push.example/abc", "https://push.example/def")

    client = app_mod.app.test_client()
    resp = client.post("/api/push/unsubscribe",
                       json={"endpoint": "https://push.example/abc"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["removed"] == 1

    remaining = _read_subs(app_mod, storage)
    assert len(remaining) == 1
    assert remaining[0]["endpoint"] == "https://push.example/def"


def test_unsubscribe_nonexistent_is_noop(tmp_path, monkeypatch):
    app_mod, storage = _setup(tmp_path, monkeypatch)
    _seed_subs(app_mod, storage, "https://push.example/keep")

    client = app_mod.app.test_client()
    resp = client.post("/api/push/unsubscribe",
                       json={"endpoint": "https://push.example/missing"})
    assert resp.status_code == 200
    assert resp.get_json()["removed"] == 0

    remaining = _read_subs(app_mod, storage)
    assert len(remaining) == 1


def test_unsubscribe_missing_endpoint_returns_400(tmp_path, monkeypatch):
    app_mod, _ = _setup(tmp_path, monkeypatch)
    client = app_mod.app.test_client()
    resp = client.post("/api/push/unsubscribe", json={})
    assert resp.status_code == 400
