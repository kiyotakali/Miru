from flask import g

import app as app_module
import auth
import curator


def _authenticated_user(user_id, user_data_dir):
    def _check_request(_request=None):
        g.user_id = user_id
        g.user_data_dir = str(user_data_dir)
        g.is_admin = False
        return None

    return _check_request


def test_server_request_restarts_curator_after_background_eviction(monkeypatch, tmp_path):
    """An engaged server user must recover Curator on their next request."""
    starts = []
    user_dir = tmp_path / "users" / "u_fresh_server"
    user_dir.mkdir(parents=True)

    monkeypatch.setattr(app_module, "_is_client_mode", False)
    monkeypatch.setattr(
        auth,
        "check_request",
        _authenticated_user("u_fresh_server", user_dir),
    )
    monkeypatch.setattr(
        curator,
        "start_loop_for_user",
        lambda user_id, data_dir: starts.append((user_id, data_dir)),
    )

    response = app_module.app.test_client().get("/api/health")

    assert response.status_code == 200
    assert starts == [("u_fresh_server", str(user_dir))]


def test_remote_client_request_never_starts_a_local_curator(monkeypatch, tmp_path):
    starts = []
    user_dir = tmp_path / "users" / "u_remote_server"
    user_dir.mkdir(parents=True)

    monkeypatch.setattr(app_module, "_is_client_mode", True)
    monkeypatch.setattr(
        auth,
        "check_request",
        _authenticated_user("u_remote_server", user_dir),
    )
    monkeypatch.setattr(
        curator,
        "start_loop_for_user",
        lambda user_id, data_dir: starts.append((user_id, data_dir)),
    )

    response = app_module.app.test_client().get("/api/health")

    assert response.status_code == 200
    assert starts == []
