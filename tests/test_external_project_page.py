from pathlib import Path


def test_external_project_page_serves_root_and_page_namespace(tmp_path, monkeypatch):
    import app as app_mod

    page_dir = tmp_path / "project_page" / "current"
    (page_dir / "_page" / "css").mkdir(parents=True)
    (page_dir / "index.html").write_text(
        "<!doctype html><title>External Miru</title>"
        '<link rel="stylesheet" href="/_page/css/main.css">'
        "<h1>External Project Page</h1>",
        encoding="utf-8",
    )
    (page_dir / "_page" / "css" / "main.css").write_text(
        "body{background:#fff7fb}",
        encoding="utf-8",
    )
    monkeypatch.setenv("MIRU_PROJECT_PAGE_DIR", str(page_dir))

    client = app_mod.app.test_client()

    root = client.get("/")
    assert root.status_code == 200
    assert b"External Project Page" in root.data
    assert root.headers["Cache-Control"] == "public, max-age=60"

    asset = client.get("/_page/css/main.css")
    assert asset.status_code == 200
    assert b"#fff7fb" in asset.data
    assert "immutable" in asset.headers["Cache-Control"]


def test_external_project_page_absent_falls_back_and_does_not_shadow_app_assets(tmp_path, monkeypatch):
    import app as app_mod

    monkeypatch.setenv("MIRU_PROJECT_PAGE_DIR", str(tmp_path / "missing"))
    client = app_mod.app.test_client()

    root = client.get("/")
    assert root.status_code == 200
    assert b"project page not deployed" not in root.data

    missing_page_asset = client.get("/_page/css/main.css")
    assert missing_page_asset.status_code == 404

    app_asset = client.get("/assets/live2d/Hiyori/Hiyori.model3.json")
    bundled_model = Path(__file__).resolve().parents[1] / "assets/live2d/Hiyori/Hiyori.model3.json"
    if bundled_model.exists():
        assert app_asset.status_code == 200
        assert b"Hiyori" in app_asset.data
    else:
        # Public source omits separately licensed Live2D sample data.
        assert app_asset.status_code == 404

    app_shell = client.get("/app")
    assert app_shell.status_code == 200
    assert b"Miru" in app_shell.data

    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.get_json()["ok"] is True
