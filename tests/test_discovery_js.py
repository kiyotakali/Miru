"""APK login-page invitation parsing tests.

The private-server release uses the long invitation code as the only public
login credential. Android must decode the server segment locally and connect
directly to that server; discovery / bundled fallback must not be involved in
new login.
"""
import json
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

import auth


INDEX = Path(__file__).resolve().parent.parent / "miru-mobile" / "www" / "index.html"


def _node_available() -> bool:
    return shutil.which("node") is not None


def _real_invitation_js() -> str:
    src = INDEX.read_text(encoding="utf-8")
    m = re.search(
        r"// Base31 alphabet[\s\S]*?var\s+_connectTimeout\s*=\s*null;",
        src,
    )
    assert m, "could not extract invitation parsing JS from APK index.html"
    return m.group(0)


def _run_parse(code: str) -> dict | None:
    harness = textwrap.dedent(
        f"""
        {_real_invitation_js()}
        const parsed = parseInvitationCode({json.dumps(code)});
        console.log(JSON.stringify(parsed));
        """
    )
    proc = subprocess.run(
        ["node", "-e", harness],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if proc.returncode != 0:
        pytest.fail(
            "node failed:\nSTDOUT:\n" + proc.stdout
            + "\nSTDERR:\n" + proc.stderr
            + "\n--- harness ---\n" + harness
        )
    return json.loads(proc.stdout.strip())


@pytest.mark.skipif(not _node_available(), reason="node not available")
def test_apk_long_invitation_decodes_server_url():
    server = auth.encode_server("203.0.113.42", 5001)
    code = f"MIRU-{server}-ABCDEF"
    parsed = _run_parse(code)
    assert parsed == {
        "serverUrl": "http://203.0.113.42:5001",
        "userCode": "ABCDEF",
        "fullCode": code,
    }


@pytest.mark.skipif(not _node_available(), reason="node not available")
def test_apk_accepts_pasted_body_but_not_short_code():
    server = auth.encode_server("1.2.3.4", 80)
    parsed = _run_parse(f"{server}ABCDEF")
    assert parsed["serverUrl"] == "http://1.2.3.4:80"
    assert parsed["fullCode"] == f"MIRU-{server}-ABCDEF"

    assert _run_parse("MIRU-ABCDEF") is None
    assert _run_parse("ABCDEF") is None


def test_apk_login_page_no_discovery_fallback_for_new_login():
    src = INDEX.read_text(encoding="utf-8")
    assert "BUNDLED_FALLBACK_URL" not in src
    assert "DISCOVERY_URL" not in src
    assert "DEFAULT_SERVER_URL" not in src
    assert "_resolveDefaultServerUrl" not in src


def _desktop_login_helpers() -> str:
    src = (Path(__file__).resolve().parent.parent / "templates" / "index.html").read_text(
        encoding="utf-8"
    )
    start = src.index("function _canonicalFullInvitationCode")
    end = src.index("async function _doLogin", start)
    return src[start:end]


def _run_desktop_canonical(code: str) -> str:
    harness = textwrap.dedent(
        f"""
        {_desktop_login_helpers()}
        const result = _canonicalFullInvitationCode({json.dumps(code)});
        console.log(JSON.stringify(result));
        """
    )
    proc = subprocess.run(
        ["node", "-e", harness],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if proc.returncode != 0:
        pytest.fail(
            "node failed:\nSTDOUT:\n" + proc.stdout
            + "\nSTDERR:\n" + proc.stderr
            + "\n--- harness ---\n" + harness
        )
    return json.loads(proc.stdout.strip())


def _run_desktop_redirect(
    server_url: str,
    token: str,
    *,
    client_mode: bool,
    platform: str | None = "windows",
) -> str:
    harness = textwrap.dedent(
        f"""
        global.window = {{
          location: {{ origin: 'http://127.0.0.1:5001' }},
          __MIRU_CLIENT_MODE__: {json.dumps(client_mode)},
          __MIRU_DESKTOP_PLATFORM__: {json.dumps(platform)}
        }};
        {_desktop_login_helpers()}
        const result = _appLoginRedirectUrl(
          {json.dumps(server_url)},
          {json.dumps(token)}
        );
        console.log(JSON.stringify(result));
        """
    )
    proc = subprocess.run(
        ["node", "-e", harness],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if proc.returncode != 0:
        pytest.fail(
            "node failed:\nSTDOUT:\n" + proc.stdout
            + "\nSTDERR:\n" + proc.stderr
            + "\n--- harness ---\n" + harness
        )
    return json.loads(proc.stdout.strip())


def _run_desktop_target(
    target: str,
    *,
    client_mode: bool,
    platform: str | None = "windows",
) -> str:
    harness = textwrap.dedent(
        f"""
        global.window = {{
          location: {{ origin: 'http://127.0.0.1:5001' }},
          __MIRU_CLIENT_MODE__: {json.dumps(client_mode)},
          __MIRU_DESKTOP_PLATFORM__: {json.dumps(platform)}
        }};
        {_desktop_login_helpers()}
        console.log(JSON.stringify(_desktopClientUrl({json.dumps(target)})));
        """
    )
    proc = subprocess.run(
        ["node", "-e", harness],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if proc.returncode != 0:
        pytest.fail(
            "node failed:\nSTDOUT:\n" + proc.stdout
            + "\nSTDERR:\n" + proc.stderr
            + "\n--- harness ---\n" + harness
        )
    return json.loads(proc.stdout.strip())


@pytest.mark.skipif(not _node_available(), reason="node not available")
def test_desktop_login_canonicalizes_pasted_body():
    server = auth.encode_server("203.0.113.42", 5001)
    body = f"{server}ABCDEF"
    assert _run_desktop_canonical(body) == f"MIRU-{server}-ABCDEF"
    assert _run_desktop_canonical(f"MIRU-{server}-ABCDEF") == f"MIRU-{server}-ABCDEF"
    assert _run_desktop_canonical("MIRU-ABCDEF") == ""


@pytest.mark.skipif(not _node_available(), reason="node not available")
def test_desktop_login_redirect_preserves_windows_bridge_markers():
    redirect = _run_desktop_redirect(
        "https://miru.example.test/", "token with spaces", client_mode=True
    )
    assert redirect == (
        "https://miru.example.test/app?token=token+with+spaces"
        "&desktop=1&desktop_platform=windows"
    )


@pytest.mark.skipif(not _node_available(), reason="node not available")
def test_browser_login_redirect_does_not_gain_desktop_markers():
    redirect = _run_desktop_redirect(
        "https://miru.example.test/", "browser-token", client_mode=False
    )
    assert redirect == "https://miru.example.test/app?token=browser-token"


@pytest.mark.skipif(not _node_available(), reason="node not available")
def test_mac_login_redirect_preserves_macos_bridge_marker():
    redirect = _run_desktop_redirect(
        "https://miru.example.test/",
        "mac-token",
        client_mode=True,
        platform="macos",
    )
    assert redirect == (
        "https://miru.example.test/app?token=mac-token"
        "&desktop=1&desktop_platform=macos"
    )


@pytest.mark.skipif(not _node_available(), reason="node not available")
def test_desktop_login_redirect_without_platform_defaults_to_macos():
    redirect = _run_desktop_redirect(
        "https://miru.example.test/",
        "fallback-token",
        client_mode=True,
        platform=None,
    )
    assert redirect.endswith("&desktop=1&desktop_platform=macos")


@pytest.mark.skipif(not _node_available(), reason="node not available")
def test_switch_account_target_preserves_windows_bridge_markers():
    assert _run_desktop_target(
        "http://127.0.0.1:5001/login", client_mode=True
    ) == (
        "http://127.0.0.1:5001/login?desktop=1"
        "&desktop_platform=windows"
    )


def test_switch_account_uses_desktop_tagged_login_target():
    src = (Path(__file__).resolve().parent.parent / "templates" / "index.html").read_text(
        encoding="utf-8"
    )
    start = src.index("async function settingsSwitchAccount")
    end = src.index("function _fmtScreenshotTime", start)
    block = src[start:end]
    assert "desktopLoginTarget = _desktopClientUrl(localBase + '/login')" in block
    assert "window.location.replace(desktopLoginTarget)" in block
    assert "window.location.href = _desktopClientUrl" not in block
    assert "}, 3000);" in block


def test_mac_launcher_sets_platform_before_page_scripts_run():
    launcher = (Path(__file__).resolve().parent.parent / "miru_launcher.py").read_text(
        encoding="utf-8"
    )
    assert "window.__MIRU_DESKTOP_PLATFORM__ = 'macos';" in launcher
