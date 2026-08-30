from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_dmg_bundle_includes_brand_assets():
    spec = (ROOT / "miru.spec").read_text(encoding="utf-8")
    windows_spec = (ROOT / "miru_windows.spec").read_text(encoding="utf-8")

    assert "('icon-192.png', '.')" in spec
    assert "('icon-512.png', '.')" in spec
    assert "('manifest.json', '.')" in spec
    assert "('assets', 'assets')" in spec
    assert '("assets", "assets")' in windows_spec


def test_brand_and_favicon_routes_serve_bundled_files():
    import app as app_mod

    client = app_mod.app.test_client()
    logo = client.get("/assets/brand/miru-logo.svg")
    favicon = client.get("/favicon.ico")

    assert logo.status_code == 200
    assert logo.mimetype == "image/svg+xml"
    assert b"<svg" in logo.data[:500]
    assert favicon.status_code == 200
    assert favicon.mimetype == "image/png"
    assert favicon.data.startswith(b"\x89PNG\r\n\x1a\n")


def test_brand_asset_route_blocks_path_traversal():
    import app as app_mod

    response = app_mod.app.test_client().get(
        "/assets/brand/%2e%2e/%2e%2e/app.py",
    )
    assert response.status_code == 404


def test_client_mode_rewrites_brand_assets_to_local_bundle():
    launcher = (ROOT / "miru_launcher.py").read_text(encoding="utf-8")

    assert "__MIRU_LOCAL_BRAND_ASSET_BASE__='http://127.0.0.1:5001'" in launcher
    assert "'/icon-192.png':assetBase+'/icon-192.png'" in launcher
    assert "'/icon-512.png':assetBase+'/icon-512.png'" in launcher
    assert "'/manifest.json':assetBase+'/manifest.json'" in launcher
    assert "MutationObserver" in launcher
