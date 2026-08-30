from pathlib import Path

import app


ROOT = Path(__file__).resolve().parents[1]


def test_android_lifecycle_shim_is_served_and_loaded_by_both_shells():
    client = app.app.test_client()
    response = client.get("/assets/js/android-lifecycle-shim.js")

    assert response.status_code == 200
    assert b"Capacitor.triggerEvent" in response.data
    for template in ("index.html", "login.html"):
        html = (ROOT / "templates" / template).read_text(encoding="utf-8")
        assert '<script src="/assets/js/android-lifecycle-shim.js"></script>' in html


def test_android_first_run_brand_respects_status_bar_safe_area():
    html = (ROOT / "templates" / "login.html").read_text(encoding="utf-8")

    assert "document.documentElement.classList.add('android-client')" in html
    assert (
        "--android-safe-top: max(env(safe-area-inset-top, 0px), 34px);" in html
    )
    assert "html.android-client body" in html
    assert "padding-top: calc(18px + var(--android-safe-top));" in html


def test_android_manifest_uses_long_lived_special_use_service_and_no_backup():
    manifest = (
        ROOT / "miru-mobile" / "android" / "app" / "src" / "main" / "AndroidManifest.xml"
    ).read_text(encoding="utf-8")

    assert 'android:allowBackup="false"' in manifest
    assert 'android:foregroundServiceType="specialUse"' in manifest
    assert "FOREGROUND_SERVICE_SPECIAL_USE" in manifest
    assert 'android:foregroundServiceType="dataSync"' not in manifest


def test_android_device_id_prefers_native_install_identity():
    html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
    assert "window.MiruAndroid.getInstallDeviceId()" in html
    assert "saveCredentialsForProfile" in (
        ROOT / "templates" / "login.html"
    ).read_text(encoding="utf-8")


def test_android_loopback_control_routes_require_install_secret(monkeypatch):
    monkeypatch.setenv("MIRU_ANDROID", "1")
    monkeypatch.setenv("MIRU_ANDROID_CLIENT_SECRET", "test-install-secret")
    monkeypatch.setattr(app, "_is_client_mode", True)
    client = app.app.test_client()

    denied = client.get("/api/client-config")
    allowed = client.get(
        "/api/client-config",
        headers={"X-Miru-Client-Secret": "test-install-secret"},
    )

    assert denied.status_code == 403
    assert allowed.status_code == 200


def test_android_loopback_data_routes_do_not_accept_implicit_user(monkeypatch):
    monkeypatch.setenv("MIRU_ANDROID", "1")
    monkeypatch.setattr(app, "_is_client_mode", True)
    monkeypatch.setattr(
        app,
        "_client_mode_config",
        {"user_id": "local-user", "auth_token": "local-token"},
    )
    client = app.app.test_client()

    denied = client.get("/api/chat/history")
    allowed = client.get(
        "/api/chat/history",
        headers={"Authorization": "Bearer local-token"},
    )

    assert denied.status_code == 401
    assert allowed.status_code == 200


def test_android_api_setup_completion_sends_logged_in_user_token():
    html = (ROOT / "templates" / "login.html").read_text(encoding="utf-8")
    start = html.index("async function completeClientSetup()")
    end = html.index("async function enterMiruFromApiSetup", start)
    completion_block = html[start:end]

    assert "headers['Authorization'] = 'Bearer ' + apiSetupState.token" in completion_block
    assert "headers: androidClientHeaders(headers)" in completion_block


def test_android_activity_recreation_refreshes_live_runtime_launch_url():
    source = (
        ROOT
        / "miru-mobile"
        / "android"
        / "app"
        / "src"
        / "main"
        / "java"
        / "com"
        / "miru"
        / "companion"
        / "MiruPythonRuntime.java"
    ).read_text(encoding="utf-8")

    ready_fast_path = source.index("if (ready && !launchUrl.isEmpty())")
    first_callback = source.index("callback.onReady", ready_fast_path)
    wait_path = source.index("private static void waitForStart")
    second_callback = source.index("callback.onReady", wait_path)

    assert "callback.onReady(getLaunchUrl());" in source[first_callback:first_callback + 80]
    assert "callback.onReady(getLaunchUrl());" in source[second_callback:second_callback + 80]
