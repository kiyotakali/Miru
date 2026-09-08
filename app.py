import os
import json
import sys
import shutil
import tempfile
import threading
import time
import hmac
from datetime import datetime

# --- Auto-install missing Python packages BEFORE any third-party import ---
if __name__ == "__main__":
    _missing = []
    for _mod, _pkg in [("flask", "flask"), ("flask_compress", "flask-compress"),
                        ("PIL", "Pillow"), ("pywebpush", "pywebpush"),
                        ("anthropic", "anthropic"), ("openai", "openai"),
                        ("requests", "requests"), ("socksio", "httpx[socks]"),
                        ("paramiko", "paramiko")]:
        try:
            __import__(_mod)
        except ImportError:
            _missing.append(_pkg)
    if _missing:
        print(f"[preflight] Missing Python packages: {', '.join(_missing)}")
        print("[preflight] Auto-installing from requirements.txt...")
        import subprocess, shutil
        from pathlib import Path
        _req = str(Path(__file__).resolve().parent / "requirements.txt")
        if shutil.which("uv"):
            subprocess.run(["uv", "pip", "install", "-r", _req, "-q"], check=False)
        else:
            subprocess.run([sys.executable, "-m", "pip", "install", "-r", _req, "-q"], check=False)
        print("[preflight] ✓ Python dependencies installed")
    del _missing

from flask import Flask, Response, g, jsonify, render_template, request, send_from_directory
from flask_compress import Compress
from werkzeug.utils import secure_filename
import storage
import core
import companion
import ai_config
import server_config
import user_settings
import self_profile
from character import get_config, reload_config, SOUL_PATH
app = Flask(__name__)
Compress(app)

# Admin API blueprint — kept structurally separate from the user pipeline.
# All /api/admin/* routes live in admin_api.py and never import from app.py.
from admin_api import admin_bp as _admin_bp
app.register_blueprint(_admin_bp)


def _http_origin_base(value: str) -> str:
    """Normalize an http(s) URL/origin to scheme://host[:port]."""
    if not value:
        return ""
    try:
        from urllib.parse import urlparse
        parsed = urlparse(str(value).strip())
    except Exception:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


def _is_allowed_client_logout_origin(origin: str) -> bool:
    """Allow client-mode logout only from the current app origin.

    Remote private-server pages need to ask the local launcher to clear its
    cached token when the user switches accounts. Arbitrary webpages should
    not be able to force that logout side effect.
    """
    if not _is_client_mode:
        return False
    origin_base = _http_origin_base(origin)
    if not origin_base:
        return False

    cfg = _client_mode_config or {}
    allowed = {
        _http_origin_base(cfg.get("server_url", "")),
        _http_origin_base(request.host_url),
        "http://127.0.0.1:5001",
        "http://localhost:5001",
    }
    allowed.discard("")
    return origin_base in allowed


@app.before_request
def _before_request():
    """Auth middleware: local requests pass, remote need token."""
    if os.environ.get("MIRU_ANDROID") == "1" and request.method != "OPTIONS":
        protected = (
            request.path.startswith("/api/client"),
            request.path in {"/api/auth/login", "/api/auth/logout"},
        )
        if any(protected):
            expected = os.environ.get("MIRU_ANDROID_CLIENT_SECRET", "")
            provided = request.headers.get("X-Miru-Client-Secret", "")
            if not expected or not hmac.compare_digest(provided, expected):
                return jsonify({"error": "Android client authorization required"}), 403
    import auth
    result = auth.check_request(request)
    # First-touch hook: start the per-user Curator background loop on the
    # server (not on DMG client shells). Idempotent — re-calling for the
    # same user is a no-op.
    if result is None and not _is_client_mode:
        try:
            uid = getattr(g, "user_id", None)
            udir = getattr(g, "user_data_dir", None)
            if uid and udir and uid != "_admin":
                import curator as _curator
                _curator.start_loop_for_user(uid, udir)
        except Exception as _e:
            # Curator startup must never break a request
            print(f"[curator] start_loop_for_user failed: {_e}")
    return result


@app.after_request
def _after_request(response):
    """CORS for desktop pet + LAN devices + disable API caching."""
    origin = request.headers.get('Origin', '')
    allowed_prefixes = [
        'http://localhost', 'http://127.0.0.1', 'tauri://localhost',
        'https://localhost',  # Capacitor 5+ WebView origin (Android APK)
        'capacitor://localhost',  # Capacitor iOS origin
        'http://192.168.', 'http://10.', 'http://172.',  # LAN ranges
    ]
    # Also allow DOMAIN / tunnel URL origin (HTTPS)
    domain = os.environ.get("DOMAIN", "").strip().rstrip("/")
    tunnel_url = os.environ.get("TUNNEL_URL", "").strip().rstrip("/")
    if domain:
        allowed_prefixes.append(f"https://{domain}")
    if tunnel_url:
        allowed_prefixes.append(tunnel_url)
    client_logout_origin = (
        request.path == "/api/auth/logout"
        and _is_allowed_client_logout_origin(origin)
    )
    if client_logout_origin or any(origin == p or origin.startswith(p) for p in allowed_prefixes):
        response.headers['Access-Control-Allow-Origin'] = origin
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, If-None-Match, X-Device-Id, X-Miru-Client-Secret'
        # PUT is used by /api/commitments/<cid> (complete/edit). Leaving it
        # off caused browsers to CORS-block the preflight, so the actual PUT
        # never sent — frontend fell through to its error path and refetched
        # stale data, producing the "completed DDL reverts 1s later" bug.
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, PATCH, DELETE, OPTIONS'
        response.headers['Access-Control-Expose-Headers'] = 'ETag'
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response

_BUILD_VERSION = None
def _get_version():
    global _BUILD_VERSION
    if _BUILD_VERSION is None:
        _BUILD_VERSION = datetime.now().strftime("%Y%m%d%H%M%S")
    return _BUILD_VERSION


# ===== Auth & Admin Endpoints =====

def _require_admin():
    """Guard for admin-only endpoints. Returns (response, 403) tuple if the
    request lacks admin context, else None. Use as:

        @app.route("/api/foo", methods=["POST"])
        def api_foo():
            guard = _require_admin()
            if guard: return guard
            ...

    auth.check_request sets g.is_admin=True only when the request presents
    a valid admin token (after the 2026-05-09 P0 fix that removed the
    silent localhost-bypass that turned any nginx-proxied request into admin).
    """
    if not getattr(g, "is_admin", False):
        return jsonify({"error": "admin only"}), 403
    return None


def _require_instance_owner():
    """Guard for owner-level instance settings.

    These routes are intentionally *not* admin routes. A Miru private-server
    instance has a normal user who owns its local ``data/_admin`` model config,
    while the broad /api/admin surface remains unavailable to user tokens.
    """
    uid = getattr(g, "user_id", None)
    if getattr(g, "is_admin", False) or not uid or uid == "_admin":
        return {"ok": False, "error": "user token required", "status_code": 401}
    import owner_config
    return owner_config.ensure_instance_owner(uid)


def _client_login_connection_error(exc: Exception) -> str:
    """Return a user-facing login error without leaking requests internals."""
    import requests as _req

    if isinstance(exc, _req.Timeout):
        return "连接服务器超时，请检查服务器是否运行，以及端口、防火墙或安全组"
    if isinstance(exc, _req.ConnectionError):
        return "无法连接邀请码对应的服务器，请检查服务器地址、端口、防火墙或安全组"
    return "服务器暂时没有响应，请稍后重试"


@app.route("/api/auth/login", methods=["POST"])
def api_auth_login():
    """Login with invitation code. Returns token on success.

    In client mode (DMG bundle), this forwards to VPS, saves the returned
    token to config.json, populates _client_mode_config, and kicks off the
    deferred client setup (sensor / device register). In server mode it
    validates locally against invitations.json.
    """
    body = request.get_json(silent=True) or {}
    code = body.get("code", "").strip()
    if not code:
        return jsonify({"error": "请输入邀请码"}), 400

    if _is_client_mode:
        # Full invitation codes carry the private server address. In client
        # mode the local Flask process is only a native bridge; it must forward
        # login to the server encoded in the code, not to discovery/default VPS.
        import auth as _auth_mod
        server_url = _auth_mod.server_url_from_invitation_code(code)
        if not server_url:
            return jsonify({"error": "请输入完整邀请码"}), 400

        import requests as _req
        try:
            resp = _req.post(
                f"{server_url}/api/auth/login",
                json={"code": code},
                timeout=15,
            )
        except Exception as e:
            return jsonify({"error": _client_login_connection_error(e)}), 502
        if resp.status_code != 200:
            try:
                err = resp.json().get("error", "邀请码无效")
            except Exception:
                err = "邀请码无效"
            return jsonify({"error": err}), resp.status_code
        data = resp.json()
        token = data.get("token")
        if not token:
            return jsonify({"error": "服务器未返回 token"}), 502
        setup_complete = body.get("setup_complete")
        if setup_complete is None:
            setup_complete = True
        # Persist + wire up client mode
        try:
            _persist_client_config_and_start(server_url, token, code,
                                             data.get("user_id", ""),
                                             setup_complete=bool(setup_complete))
        except Exception as e:
            print(f"[Login] Warning: post-login setup failed: {e}")
        # Tell login.html where to redirect — WebView navigates directly to
        # VPS so all API calls (chat/memory/etc.) go to the source of truth.
        # Local Flask only stays for sensor/pet/client-config endpoints.
        return jsonify({**data, "server_url": server_url})

    # Server mode: local validation
    import auth
    result = auth.login_with_code(code)
    if not result:
        return jsonify({"error": "邀请码无效或已被停用"}), 401
    return jsonify(result)


@app.route("/api/auth/me", methods=["GET"])
def api_auth_me():
    """Return current user info."""
    return jsonify({
        "user_id": getattr(g, "user_id", "_admin"),
        "is_admin": getattr(g, "is_admin", False),
    })


@app.route("/api/auth/invitation", methods=["GET"])
def api_auth_invitation():
    """Return the saved long invitation code for the logged-in user."""
    user_id = getattr(g, "user_id", None)
    if not user_id or user_id == "_admin" or getattr(g, "is_admin", False):
        return jsonify({"ok": False, "error": "需要先登录"}), 401
    import auth
    info = auth.get_user_invitation_info(user_id)
    if not info:
        return jsonify({
            "ok": False,
            "error": "当前账号还没有保存完整邀请码，请用原邀请码重新登录一次后再查看",
        }), 404
    return jsonify({"ok": True, **info})


# Admin endpoints (GET/POST/PATCH/DELETE for invitations + users + stats)
# moved to admin_api.py blueprint. The legacy POST suspend/activate routes
# below stay as thin shims so tools/invite_cli.py keeps working unchanged.

@app.route("/api/admin/users/<user_id>/suspend", methods=["POST"])
def api_admin_suspend_user_legacy(user_id):
    """Legacy CLI endpoint — kept for tools/invite_cli.py backward-compat.

    The new admin Web UI uses PATCH /api/admin/users/<uid> with
    body {"status": "suspended"} instead.
    """
    if not getattr(g, "is_admin", False):
        return jsonify({"error": "Admin only"}), 403
    import auth, admin_stats
    ok = auth.suspend_user(user_id)  # also evicts singletons + SSE
    if ok:
        admin_stats.invalidate_cache()
    return jsonify({"ok": ok})


@app.route("/api/admin/users/<user_id>/activate", methods=["POST"])
def api_admin_activate_user_legacy(user_id):
    """Legacy CLI endpoint — kept for tools/invite_cli.py backward-compat."""
    if not getattr(g, "is_admin", False):
        return jsonify({"error": "Admin only"}), 403
    import auth, admin_stats
    ok = auth.activate_user(user_id)
    if ok:
        admin_stats.invalidate_cache()
    return jsonify({"ok": ok})


@app.route("/api/auth/logout", methods=["POST"])
def api_auth_logout():
    """Logout: clear cached credentials, kill pet, bounce to /login."""
    origin = request.headers.get("Origin", "")
    if _is_client_mode and origin and not _is_allowed_client_logout_origin(origin):
        return jsonify({"error": "logout origin not allowed"}), 403
    _force_relogin("user requested logout")
    return jsonify({"ok": True})


@app.route("/api/auth/export", methods=["GET"])
def api_auth_export():
    """Export the current user's data as a single JSON bundle.

    Available to every authenticated user (self-export). Returns all text
    files (.json/.md) inline; binary uploads are listed by filename only
    (client can fetch them separately via /data/uploads/... if needed).
    """
    user_id = getattr(g, "user_id", None)
    if not user_id or getattr(g, "is_admin", False):
        return jsonify({"error": "login required"}), 401
    import auth
    bundle = auth.export_user_data(user_id)
    if bundle is None:
        return jsonify({"error": "user data not found"}), 404
    body = json.dumps(bundle, ensure_ascii=False, indent=2)
    resp = Response(body, content_type="application/json")
    # Suggest a download filename for browser "Save as"
    resp.headers["Content-Disposition"] = (
        f'attachment; filename="miru-export-{user_id}.json"'
    )
    return resp


@app.route("/api/auth/account", methods=["DELETE"])
def api_auth_delete_account():
    """User-initiated account deletion. Irreversible.

    Frontend must confirm twice before calling this. Server performs:
      - Revoke invitation code (no more logins with the same code)
      - Delete data/users/<user_id>/ recursively (uploads, memory/, personas, etc.)
      - Audit entry in data/_admin/deletions.json (no PII, just IDs + timestamps)
      - Clean up any in-memory per-user singletons

    Body (optional): { "confirm": "DELETE", "reason": "..." }
    Responds: { "ok": true, "bytes_freed": N } on success.
    """
    user_id = getattr(g, "user_id", None)
    if not user_id or getattr(g, "is_admin", False):
        return jsonify({"error": "login required"}), 401
    body = request.get_json(silent=True) or {}
    # Double confirmation guard — the magic string must match
    if (body.get("confirm") or "").strip() != "DELETE":
        return jsonify({
            "error": "confirmation required",
            "hint": "POST body must include {\"confirm\": \"DELETE\"}"
        }), 400
    reason = (body.get("reason") or "user_self_delete")[:200]

    import auth
    result = auth.delete_user(user_id, reason=reason)
    if not result.get("ok"):
        return jsonify(result), 500

    # Also kick any open sessions — other devices should be bounced to /login
    try:
        _force_relogin(f"account deleted: {user_id}")
    except Exception:
        pass

    return jsonify({
        "ok": True,
        "bytes_freed": result.get("bytes_freed", 0),
        "invitation_code": result.get("invitation_code"),
    })


@app.route("/privacy")
def privacy_page():
    """Public privacy policy page (no auth required).

    Linked from the landing page and from the in-app settings panel
    (before users click '删除账号').
    """
    resp = app.make_response(render_template("privacy.html"))
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


import hashlib

def _etag_json(data):
    """Return a Response with ETag. If If-None-Match matches, return 304."""
    body = json.dumps(data, ensure_ascii=False, default=str)
    etag = '"' + hashlib.md5(body.encode()).hexdigest() + '"'
    if request.headers.get("If-None-Match") == etag:
        return Response(status=304)
    resp = Response(body, content_type="application/json")
    resp.headers["ETag"] = etag
    return resp


_VALID_MEMORY_SCOPE_INPUTS = {"user", "airi", "all", "*", "", "default", "main"}


def _validate_memory_scope_raw(raw, default="user"):
    """Abort 400 if *raw* is a clearly invalid memory_scope value."""
    if str(raw or "").strip().lower() not in _VALID_MEMORY_SCOPE_INPUTS and raw != default:
        from flask import abort
        abort(400, description="Invalid memory_scope: %s" % raw)


def _get_memory_scope(default="user", allow_all=False):
    raw = request.args.get("memory_scope", default)
    _validate_memory_scope_raw(raw, default)
    return storage._normalize_memory_scope(raw, allow_all=allow_all)


@app.route("/api/health")
def api_health():
    """Liveness probe for tests + docker compose health checks.

    Public, no auth. Returns immediately without touching user data.
    """
    return {"ok": True, "ts": time.time()}


def _project_page_current_dir() -> str:
    """Return the optional external project-page directory.

    The public release repository can deploy a static homepage here without
    touching the Miru app bundle, user data, Docker lifecycle, or /assets app
    namespace. When the directory is absent we keep serving the bundled
    templates/landing.html page exactly as before.
    """
    configured = os.environ.get("MIRU_PROJECT_PAGE_DIR", "").strip()
    if configured:
        return os.path.realpath(configured)
    return os.path.realpath(os.path.join(os.path.dirname(__file__), "project_page", "current"))


def _external_project_page_available() -> bool:
    page_dir = _project_page_current_dir()
    return os.path.isfile(os.path.join(page_dir, "index.html"))


@app.route("/admin")
def admin_page():
    """Render the standalone admin SPA shell.

    The HTML is fully self-contained: it does not import any user-SPA
    bundle and uses its own localStorage key (miru_admin_token), so the
    user app and admin pipeline can never accidentally cross-contaminate.
    Auth happens client-side via POST /api/admin/auth/verify after the
    operator pastes their admin token.
    """
    resp = app.make_response(render_template("admin.html"))
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/")
def landing():
    """Project landing page — public marketing/download page.

    The actual web app moved to /app. Existing users with bookmark to /
    will see this page; the app is reachable at /app for fallback.
    """
    if _external_project_page_available():
        resp = send_from_directory(_project_page_current_dir(), "index.html")
        resp.headers["Cache-Control"] = "public, max-age=60"
        return resp
    resp = app.make_response(render_template("landing.html", build_version=_get_version()))
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp


@app.route("/_page/<path:filename>")
def project_page_asset(filename):
    """Serve assets for the external public project page.

    This intentionally uses /_page/* instead of /assets/* so the public site
    cannot shadow app-critical routes such as /assets/live2d/*.
    """
    if not _external_project_page_available():
        return jsonify({"error": "project page not deployed"}), 404
    resp = send_from_directory(os.path.join(_project_page_current_dir(), "_page"), filename)
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


@app.route("/app")
def app_page():
    """Web app entry — same UI as installed clients (DMG/APK).

    Kept as a fallback / debugging entry. Not advertised on the landing page.
    APK and DMG point their WebView here.

    Special query ?nuke=1 — emergency cache clear. Returns Clear-Site-Data
    header so the browser wipes all caches, SWs, cookies for this origin.
    """
    nuke = request.args.get("nuke") == "1"
    if nuke:
        # Minimal HTML that just triggers site-data wipe then redirects home.
        html = (
            "<!doctype html><meta charset=utf-8>"
            "<title>Clearing cache…</title>"
            "<style>body{font-family:sans-serif;text-align:center;padding:60px;color:#666}</style>"
            "<p>正在清除本地缓存…<br>稍等片刻自动跳转。</p>"
            "<script>setTimeout(function(){location.href='/app';},1500)</script>"
        )
        resp = app.make_response(html)
        resp.headers["Clear-Site-Data"] = '"cache", "storage", "executionContexts"'
        resp.headers["Cache-Control"] = "no-store"
        return resp

    resp = app.make_response(render_template("index.html", build_version=_get_version()))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


@app.route("/login")
def login_page():
    """Invitation code entry — Japanese minimalist design.

    DMG WebView opens this on first launch when no auth token is stored.
    On successful login, JS redirects to /app?token=<token>.
    """
    resp = app.make_response(render_template("login.html"))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


@app.route("/pet")
def pet_page():
    """Lightweight desktop pet page (vanilla JS + Live2D)."""
    resp = app.make_response(render_template("pet.html"))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


@app.route("/downloads/<path:filename>")
def downloads(filename):
    """Serve installer files (DMG, APK) from VPS data/downloads/.

    Public — anyone can download. Files are placed manually via
    deploy/upload_release.sh or directly into /opt/miru/data/downloads/.
    """
    downloads_dir = os.path.join(os.path.dirname(__file__), "data", "downloads")
    if not os.path.isdir(downloads_dir):
        return jsonify({"error": "downloads directory not configured"}), 404
    return send_from_directory(downloads_dir, filename, as_attachment=True)


# ===== Client Mode Config Endpoint =====

# Module-level client config (set in __main__ when --client is used)
_client_mode_config = None  # dict or None, set in __main__ for --client mode
_is_client_mode = False  # True when launched via miru_launcher or --client

# Event flags for launcher ↔ Flask communication (main thread checks these in NSRunLoop)
_open_webui_requested = threading.Event()
_launch_pet_requested = threading.Event()
# Set ONLY after we've confirmed the user (cached token validates, or fresh
# /api/auth/login succeeds). Launcher waits on this before spawning the pet —
# without it the pet could appear before the invitation-code screen, leaking
# screen-recording prompts to a not-yet-logged-in user.
_pet_ready_event = threading.Event()
# Set when token is invalidated server-side (401/403 from VPS). Launcher
# kills the pet and the WebView reloads /login.
_pet_should_hide_event = threading.Event()

# Deferred setup trigger — set by run_client_mode(), called after login
_deferred_client_setup_trigger = None
_client_local_backend_services_lock = threading.Lock()
_client_local_backend_services_started = False
_client_runtime_lock = threading.Lock()
_client_runtime_generation = 0
_client_runtime_cancel_event = threading.Event()
_client_local_runtime_user_id = ""


def _teardown_client_runtime(cfg: dict) -> None:
    """Stop runtime owned by the previous Mac client session.

    Remote sessions only own the Mac sensor/heartbeat; their server runtime is
    a different process and is deliberately untouched. Local-only sessions
    additionally own per-user backend singletons inside this Mac process.
    """
    if not _is_client_mode:
        return

    _stop_client_screen_sensor()
    old_uid = str(cfg.get("user_id") or "").strip()
    old_was_local = cfg.get("mode") == "local" or cfg.get("local_only") is True
    if not (old_was_local and old_uid):
        return
    try:
        import auth as _auth
        user = _auth.get_user(old_uid) or {}
        if (user.get("status") == "active"
                and user.get("account_type") == _auth.LOCAL_SINGLE_DEVICE_ACCOUNT_TYPE):
            _auth._cleanup_user_singletons(old_uid)
    except Exception as exc:
        print(f"[Client] Local runtime cleanup failed (non-fatal): {exc}")


def _replace_client_runtime_config(cfg: dict) -> tuple[int, threading.Event]:
    """Install a new client session and cancel work from the previous one."""
    global _client_mode_config, _client_runtime_generation
    global _client_runtime_cancel_event, _client_local_runtime_user_id
    with _client_runtime_lock:
        old = dict(_client_mode_config or {})
        _client_runtime_cancel_event.set()
        _client_runtime_generation += 1
        _client_runtime_cancel_event = threading.Event()
        _client_local_runtime_user_id = ""
        _client_mode_config = dict(cfg)
        generation = _client_runtime_generation
        cancel_event = _client_runtime_cancel_event

    # Finish retiring the old Mac runtime before the caller starts deferred
    # setup for the replacement session. This also covers direct local/remote
    # switches that do not pass through /api/auth/logout first.
    _teardown_client_runtime(old)
    return generation, cancel_event


def _client_runtime_snapshot() -> tuple[int, threading.Event]:
    with _client_runtime_lock:
        return _client_runtime_generation, _client_runtime_cancel_event


def _client_session_is_current(generation: int,
                               cancel_event: threading.Event | None = None,
                               server_url: str = "",
                               token: str = "") -> bool:
    """Whether deferred client work still belongs to the selected account."""
    if not _is_client_mode:
        return False
    with _client_runtime_lock:
        if generation != _client_runtime_generation:
            return False
        if cancel_event is not None and cancel_event is not _client_runtime_cancel_event:
            return False
        if _client_runtime_cancel_event.is_set():
            return False
        cfg = _client_mode_config or {}
        if not bool(cfg.get("setup_complete")):
            return False
        if server_url and str(cfg.get("server_url") or "").rstrip("/") != server_url.rstrip("/"):
            return False
        if token and str(cfg.get("auth_token") or "") != token:
            return False
        return True


def _verified_local_runtime_user_id(cfg: dict | None = None) -> str:
    """Return the configured local-only uid only when it is a real active account."""
    if not _is_client_mode:
        return ""
    cfg = cfg or _client_mode_config or {}
    if not bool(cfg.get("setup_complete")):
        return ""
    if not (cfg.get("mode") == "local" or cfg.get("local_only") is True):
        return ""
    uid = str(cfg.get("user_id") or "").strip()
    if not uid:
        return ""
    try:
        import auth as _auth
        user = _auth.get_user(uid) or {}
        if (user.get("status") == "active"
                and user.get("account_type") == _auth.LOCAL_SINGLE_DEVICE_ACCOUNT_TYPE):
            return uid
    except Exception:
        pass
    return ""


def _stop_client_screen_sensor() -> None:
    """Stop an existing Mac sensor without creating a new singleton."""
    try:
        sensor_mod = sys.modules.get("sensor")
        sensor = getattr(sensor_mod, "_instance", None) if sensor_mod else None
        if sensor is not None:
            sensor.stop()
    except Exception as exc:
        print(f"[Client] ScreenSensor stop failed (non-fatal): {exc}")


def _start_client_screen_sensor(backend_url: str, device_id: str,
                                auth_token: str, generation: int,
                                cancel_event: threading.Event) -> bool:
    """Atomically bind/start the sensor for the selected client session.

    Holding the client-runtime lock across singleton reconfiguration prevents
    an old deferred setup thread from restoring its token after a newer
    account has already replaced it. Sensor startup itself is non-blocking.
    """
    with _client_runtime_lock:
        cfg = _client_mode_config or {}
        if (generation != _client_runtime_generation
                or cancel_event is not _client_runtime_cancel_event
                or cancel_event.is_set()
                or not bool(cfg.get("setup_complete"))
                or str(cfg.get("server_url") or "").rstrip("/") != backend_url.rstrip("/")
                or str(cfg.get("auth_token") or "") != auth_token):
            return False
        from sensor import get_sensor
        get_sensor(
            backend_url=backend_url,
            device_id=device_id,
            auth_token=auth_token,
        ).start()
        return True


def _launcher_config_path():
    """Return path to the launcher's config.json (client mode only)."""
    override = os.environ.get("MIRU_CLIENT_CONFIG_PATH", "").strip()
    if override:
        from pathlib import Path
        return Path(override).expanduser().resolve()
    from desktop_paths import launcher_config_path
    return launcher_config_path()


def _save_launcher_config(cfg: dict):
    """Persist client-mode config to the platform-specific desktop data dir."""
    p = _launcher_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def _setup_complete_from_launcher_config(cfg: dict) -> bool:
    """Interpret setup_complete with backward compatibility for old DMGs.

    New first-run accounts explicitly save setup_complete=false while the user
    is on the API Key page. Older installed configs did not have this field at
    all; if such a config already has a token, treat it as a returning account
    so upgrades do not bounce existing users back into setup.
    """
    if "setup_complete" in cfg:
        return bool(cfg.get("setup_complete"))
    return bool(cfg.get("auth_token"))


def _is_local_single_device_mode() -> bool:
    """Whether this client-mode bridge is running a local-only Miru account."""
    cfg = _client_mode_config or {}
    return cfg.get("mode") == "local" or cfg.get("local_only") is True


def _start_local_single_device_backend_services_once(
        port: int = 5001,
        expected_generation: int | None = None,
        cancel_event: threading.Event | None = None) -> bool:
    """Start server-side background loops for DMG local-only mode.

    In remote/private-server mode, the DMG is only a thin client and the real
    server runs nightly maintenance.  In local-only mode the Mac process is
    the real server, so it must also own reminder/nightly maintenance;
    otherwise append-only slot bodies never get their daily compaction pass.
    """
    global _client_local_backend_services_started, _client_local_runtime_user_id
    if not (_is_client_mode and _is_local_single_device_mode()):
        return False

    cfg = _client_mode_config or {}
    uid = _verified_local_runtime_user_id(cfg)
    if not uid:
        print("[ClientLocalBackend] Skip startup: local user/setup is not verified yet")
        return False
    if expected_generation is not None and not _client_session_is_current(
            expected_generation, cancel_event, cfg.get("server_url", ""), cfg.get("auth_token", "")):
        print("[ClientLocalBackend] Skip startup: client session is stale")
        return False

    try:
        import auth as _auth
        user_data_dir = _auth.get_user_data_dir(uid)
        with _client_runtime_lock:
            if expected_generation is not None:
                if expected_generation != _client_runtime_generation:
                    return False
                if cancel_event is not None and cancel_event is not _client_runtime_cancel_event:
                    return False
            # Curator activation and the active-user marker share the session
            # lock. A concurrent account replacement therefore either waits
            # and then stops this loop, or wins first and prevents the start.
            _client_local_runtime_user_id = uid
            try:
                import curator as _curator
                _curator.start_loop_for_user(uid, user_data_dir)
            except Exception as e:
                print(f"[ClientLocalBackend] Curator startup failed (non-fatal): {e}")
    except Exception as e:
        print(f"[ClientLocalBackend] User activation failed: {e}")
        return False

    with _client_local_backend_services_lock:
        if _client_local_backend_services_started:
            return False

        try:
            with app.app_context():
                g.user_id = uid
                g.user_data_dir = user_data_dir
                g.is_admin = False
                storage._ensure_dirs()

                try:
                    import memory as _mem
                    _mem.ensure_dirs()
                except Exception as e:
                    print(f"[ClientLocalBackend] memory ensure failed (non-fatal): {e}")

                try:
                    import core_memory as _cm
                    init = _cm.initialize_from_existing()
                    if init.get("actions"):
                        print(f"[ClientLocalBackend] Core memory initialized: {init['actions']}")
                except Exception as e:
                    print(f"[ClientLocalBackend] core memory init failed (non-fatal): {e}")

                try:
                    import server_config as _sc
                    from screen_analyzer import get_analyzer
                    runtime = _sc.get()
                    get_analyzer().set_vlm_interval(runtime.get("auto_screenshot_interval", 30))
                except Exception as e:
                    print(f"[ClientLocalBackend] screen analyzer setup failed (non-fatal): {e}")

            _start_reminder_loop()

            try:
                _start_attention_engine_spawner(interval=300)
            except Exception as e:
                print(f"[ClientLocalBackend] AttentionEngine spawner failed (non-fatal): {e}")

            _client_local_backend_services_started = True
            print(f"[ClientLocalBackend] Started local maintenance services for {uid} on port {port}")
            return True
        except Exception as e:
            print(f"[ClientLocalBackend] Startup failed: {e}")
            return False


def _persist_client_config_and_start(server_url: str, token: str,
                                     invitation_code: str = "",
                                     user_id: str = "",
                                     mode: str = "remote",
                                     local_only: bool = False,
                                     setup_complete: bool = True):
    """After /api/auth/login succeeds in client mode:
    1. Save config.json
    2. Populate _client_mode_config
    3. Trigger deferred client setup only after first-run setup is complete
    """
    import platform as _plat
    setup_complete = bool(setup_complete)
    cfg = {
        "mode": mode,
        "local_only": bool(local_only),
        "invitation_code": "" if local_only else invitation_code,
        "server_url": server_url.rstrip("/"),
        "auth_token": token,
        "user_id": user_id,
        "device_name": _plat.node() or (
            "Windows PC" if sys.platform == "win32" else "MacBook"
        ),
        "setup_complete": setup_complete,
    }
    _save_launcher_config(cfg)

    runtime_cfg = {
        "server_url": cfg["server_url"],
        "auth_token": token,
        "user_id": user_id,
        "mode": mode,
        "local_only": bool(local_only),
        "setup_complete": setup_complete,
    }
    _replace_client_runtime_config(runtime_cfg)

    # Kick off deferred setup only once the first-run API setup page is done.
    fn = globals().get("_deferred_client_setup_trigger")
    if setup_complete and callable(fn):
        try:
            fn(cfg["server_url"], token)
        except Exception as e:
            print(f"[Client] Deferred setup trigger failed: {e}")


def _complete_client_setup_and_start() -> dict:
    """Mark first-run setup complete, persist it, and activate local runtime."""
    cfg_path = _launcher_config_path()
    if not cfg_path.exists():
        raise RuntimeError("没有找到本机登录配置，请重新开始")
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError("本机登录配置读取失败，请重新开始") from exc
    server_url = str(cfg.get("server_url") or "").rstrip("/")
    token = str(cfg.get("auth_token") or "")
    if not server_url or not token:
        raise RuntimeError("登录状态不完整，请重新开始")

    cfg["server_url"] = server_url
    cfg["setup_complete"] = True
    _save_launcher_config(cfg)

    runtime_cfg = {
        "server_url": server_url,
        "auth_token": token,
        "user_id": cfg.get("user_id", ""),
        "mode": cfg.get("mode", ""),
        "local_only": bool(cfg.get("local_only")),
        "setup_complete": True,
    }
    _replace_client_runtime_config(runtime_cfg)

    fn = globals().get("_deferred_client_setup_trigger")
    if callable(fn):
        try:
            fn(server_url, token)
        except Exception as e:
            print(f"[Client] Deferred setup trigger failed: {e}")
    return dict(runtime_cfg)


def _seed_local_user_settings_from_remote(remote_settings: dict, user_id: str) -> bool:
    """Seed desktop-local capture settings from the private server once.

    The desktop sensor reads local per-device settings so it can keep working
    even when the WebView is on a remote origin. After a clean install/cache
    wipe the local file is absent, but the private server may already know that
    the user enabled screenshots. Copy only missing capture/timezone fields so
    an existing local choice is never overwritten by startup sync.

    Pet hotkeys and collapse delay are deliberately excluded: their defaults
    are platform-specific and they must not leak from the first connected
    Windows desktop into a Mac (or vice versa).
    """
    if not user_id or not isinstance(remote_settings, dict):
        return False
    try:
        import auth as _auth
        seedable = {
            "timezone",
            "screenshot_enabled",
            "screenshot_perm_guided",
            "auto_screenshot_interval",
        }
        allowed = getattr(user_settings, "USER_FIELDS", set()) & seedable
        with app.app_context():
            g.user_id = user_id
            g.user_data_dir = _auth.get_user_data_dir(user_id)
            g.is_admin = False
            existing = user_settings._load()
            payload = {
                key: remote_settings[key]
                for key in allowed
                if key in remote_settings and key not in existing
            }
            if not payload:
                return False
            result = user_settings.update(payload)
            return bool(result.get("ok"))
    except Exception as exc:
        print(f"[Client] local settings seed failed: {exc}")
        return False

def _force_relogin(reason: str = "", expected_generation: int | None = None) -> bool:
    """Invalidate cached credentials, signal launcher to kill the pet and
    bounce the WebView back to /login. Called when VPS returns 401/403 —
    typically because the admin suspended the user or rotated their token.
    Idempotent: safe to call repeatedly."""
    global _client_mode_config, _client_runtime_generation
    global _client_runtime_cancel_event, _client_local_runtime_user_id

    with _client_runtime_lock:
        if expected_generation is not None and expected_generation != _client_runtime_generation:
            print(f"[Client] Ignore stale force re-login: {reason or 'token invalid'}")
            return False
        old = dict(_client_mode_config or {})
        if _is_client_mode:
            _client_runtime_cancel_event.set()
            _client_runtime_generation += 1
            _client_runtime_cancel_event = threading.Event()
            _client_local_runtime_user_id = ""
        _client_mode_config = {
            "server_url": old.get("server_url", ""),
            "auth_token": "",
            "user_id": "",
            "mode": old.get("mode", ""),
            "local_only": bool(old.get("local_only")),
            "setup_complete": False,
        }

    print(f"[Client] Force re-login triggered: {reason or 'token invalid'}")

    # This only retires runtime owned by the Mac client. It never sends a
    # request to the private server, so VPS Curator and phone sessions continue.
    _teardown_client_runtime(old)

    # Wipe the persisted token so a relaunch goes back to /login.
    try:
        cfg_path = _launcher_config_path()
        if cfg_path.exists():
            try:
                cur = json.loads(cfg_path.read_text(encoding="utf-8"))
            except Exception:
                cur = {}
            cur["auth_token"] = ""
            cur["user_id"] = ""
            cfg_path.write_text(json.dumps(cur, indent=2, ensure_ascii=False),
                                encoding="utf-8")
    except Exception as _e:
        print(f"[Client] Could not clear config.json: {_e}")
    # Tell the launcher to kill the pet + reload WebView → /login.
    _pet_should_hide_event.set()
    return True


def _client_desktop_device_payload(device_id: str) -> dict:
    import platform as _plat

    return {
        "name": _plat.node() or ("Windows PC" if sys.platform == "win32" else "MacBook"),
        "type": "desktop",
        "platform": sys.platform,
        "device_id": device_id,
    }


def _register_client_desktop_device(server_url: str, token: str, payload: dict,
                                    generation: int,
                                    cancel_event: threading.Event) -> bool:
    """Register the native desktop only while its client session is current."""
    import requests as _req

    if not _client_session_is_current(
            generation, cancel_event, server_url=server_url, token=token):
        return False
    response = _req.post(
        f"{server_url}/api/device/register",
        json=dict(payload),
        headers={"Authorization": f"Bearer {token}"},
        timeout=5,
    )
    if not _client_session_is_current(
            generation, cancel_event, server_url=server_url, token=token):
        return False
    if response.status_code >= 400:
        return False
    try:
        return response.json().get("ok") is not False
    except Exception:
        return True


def _client_heartbeat_loop(server_url: str, token: str, device_id: str,
                           generation: int, cancel_event: threading.Event,
                           interval: float = 60,
                           restore_missing_device: bool = False,
                           device_registration: dict | None = None) -> None:
    """Heartbeat only while its originating client session is current."""
    import requests as _req

    auth_failures = 0
    while _client_session_is_current(
            generation, cancel_event, server_url=server_url, token=token):
        try:
            response = _req.post(
                f"{server_url}/api/device/heartbeat",
                json={"device_id": device_id},
                headers={"Authorization": f"Bearer {token}"},
                timeout=5,
            )
            if not _client_session_is_current(
                    generation, cancel_event, server_url=server_url, token=token):
                return
            if response.status_code in (401, 403):
                auth_failures += 1
                if auth_failures >= 3:
                    _force_relogin(
                        f"heartbeat {response.status_code} x3",
                        expected_generation=generation,
                    )
                    return
            else:
                auth_failures = 0
                if restore_missing_device and response.status_code == 200:
                    try:
                        missing = response.json().get("ok") is False
                    except Exception:
                        missing = False
                    if missing and _client_session_is_current(
                            generation, cancel_event,
                            server_url=server_url, token=token):
                        payload = device_registration or _client_desktop_device_payload(device_id)
                        try:
                            restored = _register_client_desktop_device(
                                server_url, token, payload, generation, cancel_event,
                            )
                            if restored:
                                print("[Client] Restored deleted local desktop device record")
                        except Exception as exc:
                            print(f"[Client] Device restore failed (non-fatal): {exc}")
        except Exception:
            pass
        if cancel_event.wait(timeout=interval):
            return


@app.route("/api/client-config")
def api_client_config():
    """Return client mode configuration.
    Frontend uses this to redirect API calls to VPS."""
    if _client_mode_config:
        return jsonify({
            "client_mode": True,
            "server_url": _client_mode_config.get("server_url", ""),
            "auth_token": _client_mode_config.get("auth_token", ""),
            "mode": _client_mode_config.get("mode", ""),
            "local_only": bool(_client_mode_config.get("local_only")),
            "setup_complete": bool(_client_mode_config.get("setup_complete")),
        })
    return jsonify({"client_mode": False})


@app.route("/api/client/setup/complete", methods=["POST"])
def api_client_setup_complete():
    """Finish first-run setup and activate the local Mac runtime."""
    guard = _require_client_provision_context()
    if guard:
        return guard
    try:
        cfg = _complete_client_setup_and_start()
    except RuntimeError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, **cfg})


@app.route("/api/client/local/start", methods=["POST"])
def api_client_local_start():
    """Create or resume the local-only single-device account from first-run UI."""
    guard = _require_client_provision_context()
    if guard:
        return guard

    import auth as _auth_mod
    result = _auth_mod.get_or_create_local_single_device_user()
    host = request.host or ""
    port = ""
    if ":" in host:
        port = host.rsplit(":", 1)[-1]
    else:
        env_port = request.environ.get("SERVER_PORT") or ""
        if env_port and env_port not in {"80", "443"}:
            port = env_port
    port = port or "5001"
    server_url = f"http://127.0.0.1:{port}"
    try:
        setup_complete = not bool(result.get("is_new"))
        _persist_client_config_and_start(
            server_url,
            result["token"],
            invitation_code="",
            user_id=result["user_id"],
            mode="local",
            local_only=True,
            setup_complete=setup_complete,
        )
    except Exception as e:
        print(f"[Local Mode] post-start setup failed: {e}")
        return jsonify({
            "ok": False,
            "error": "本地账号已创建，但本机配置保存失败。请重启 Miru 后再试。",
            "mode": "local",
            "local_only": True,
            "user_id": result.get("user_id", ""),
        }), 500
    return jsonify({
        "ok": True,
        **result,
        "server_url": server_url,
        "invitation_code": "",
    })


def _require_client_provision_context():
    """Allow provisioning only from the local DMG bridge before login."""
    if not _is_client_mode:
        return jsonify({"error": "not found"}), 404
    try:
        import auth as _auth_mod
        if not _auth_mod.is_local_request(request):
            return jsonify({"error": "local client only"}), 403
    except Exception:
        return jsonify({"error": "local client only"}), 403
    return None


@app.route("/api/client/provision/self-server/defaults", methods=["GET"])
def api_client_provision_self_server_defaults():
    guard = _require_client_provision_context()
    if guard:
        return guard
    import client_provisioning as _cp
    return jsonify({
        "ok": True,
        "defaults": {
            "ssh_user": "",
            "ssh_port": 22,
            "host_home": _cp.DEFAULT_HOST_HOME,
            "port_start": _cp.DEFAULT_PORT_START,
            "port_end": _cp.DEFAULT_PORT_END,
            "image": _cp.DEFAULT_IMAGE,
        },
    })


@app.route("/api/client/provision/self-server/create", methods=["POST"])
def api_client_provision_self_server_create():
    guard = _require_client_provision_context()
    if guard:
        return guard
    import client_provisioning as _cp
    is_multipart = (request.mimetype or "").lower().startswith("multipart/")
    if (
        is_multipart
        and request.content_length
        and request.content_length
        > _cp.MAX_IMAGE_TAR_BYTES + _cp.MAX_SSH_PRIVATE_KEY_BYTES + 4 * 1024 * 1024
    ):
        return jsonify({"ok": False, "error": "Miru 服务包过大，请确认下载的是正确的 Miru 服务包"}), 400
    saved_image_tar = ""
    saved_ssh_private_key = ""
    try:
        if is_multipart:
            upload = request.files.get("image_tar")
            ssh_key_upload = request.files.get("ssh_private_key")
            payload = request.form.to_dict()
        else:
            upload = None
            ssh_key_upload = None
            payload = request.get_json(silent=True) or {}
        if upload and upload.filename:
            raw_name = os.path.basename(str(upload.filename).replace("\\", "/"))
            suffix = _cp.image_tar_suffix(raw_name)
            if not suffix:
                raise _cp.ProvisioningError("Miru 镜像包必须是 .tar、.tar.gz 或 .tgz 文件")
            safe_name = secure_filename(raw_name)
            if not safe_name or not _cp.is_allowed_image_tar_name(safe_name):
                safe_name = f"miru-server-image{suffix}"
            tmp_dir = tempfile.mkdtemp(prefix="miru-image-")
            saved_image_tar = os.path.join(tmp_dir, safe_name)
            _copy_stream_limited(
                upload.stream,
                saved_image_tar,
                _cp.MAX_IMAGE_TAR_BYTES,
                "Miru 服务包过大，请确认下载的是正确的 Miru 服务包",
                _cp.ProvisioningError,
            )
            payload["image_tar_path"] = saved_image_tar
            payload["image_tar_name"] = safe_name
            payload["image_tar_size"] = os.path.getsize(saved_image_tar)
        if ssh_key_upload and ssh_key_upload.filename:
            tmp_dir = tempfile.mkdtemp(prefix="miru-ssh-key-")
            saved_ssh_private_key = os.path.join(tmp_dir, "ssh-private-key")
            _copy_stream_limited(
                ssh_key_upload.stream,
                saved_ssh_private_key,
                _cp.MAX_SSH_PRIVATE_KEY_BYTES,
                "SSH 私钥文件过大，请确认选择的是正确的私钥",
                _cp.ProvisioningError,
            )
            os.chmod(saved_ssh_private_key, 0o600)
            payload["ssh_private_key_path"] = saved_ssh_private_key
            payload["ssh_private_key_name"] = "SSH 私钥"
            payload["ssh_private_key_size"] = os.path.getsize(saved_ssh_private_key)
        job = _cp.create_self_server_job(payload)
        if saved_image_tar and job.request.image_tar_path != saved_image_tar:
            _cp._cleanup_local_image_tar(saved_image_tar)
            saved_image_tar = ""
        if saved_ssh_private_key and job.request.ssh_private_key_path != saved_ssh_private_key:
            _cp._cleanup_local_ssh_private_key(saved_ssh_private_key)
            saved_ssh_private_key = ""
    except _cp.ProvisioningError as exc:
        if saved_image_tar:
            _cp._cleanup_local_image_tar(saved_image_tar)
        if saved_ssh_private_key:
            _cp._cleanup_local_ssh_private_key(saved_ssh_private_key)
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception:
        if saved_image_tar:
            _cp._cleanup_local_image_tar(saved_image_tar)
        if saved_ssh_private_key:
            _cp._cleanup_local_ssh_private_key(saved_ssh_private_key)
        raise
    return jsonify({"ok": True, "job": job.public_dict()}), 202


def _copy_stream_limited(stream, destination: str, max_bytes: int, error_message: str, error_cls=RuntimeError) -> None:
    total = 0
    with open(destination, "wb") as out:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise error_cls(error_message)
            out.write(chunk)


@app.route("/api/client/provision/self-server/status/<job_id>", methods=["GET"])
def api_client_provision_self_server_status(job_id):
    guard = _require_client_provision_context()
    if guard:
        return guard
    import client_provisioning as _cp
    job = _cp.get_job(job_id)
    if not job:
        return jsonify({"ok": False, "error": "job not found"}), 404
    return jsonify({"ok": True, "job": job.public_dict()})


@app.route("/api/client/provision/self-server/public-health/<job_id>", methods=["POST"])
def api_client_provision_self_server_public_health(job_id):
    guard = _require_client_provision_context()
    if guard:
        return guard
    import client_provisioning as _cp
    job = _cp.get_job(job_id)
    if not job:
        return jsonify({"ok": False, "error": "job not found"}), 404
    if job.status != "succeeded" or not job.server_url:
        return jsonify({"ok": False, "error": "Miru 还没有完成创建，请稍后再检查。"}), 409
    try:
        result = _cp.check_self_server_public_health(job.server_url)
    except _cp.ProvisioningError as exc:
        return jsonify({
            "ok": False,
            "server_url": job.server_url,
            "error": str(exc),
        }), 409
    return jsonify({"ok": True, **result})



@app.route("/api/version")
def api_version():
    return jsonify({"version": _get_version()})


_DEFAULT_HUMAN_PLACEHOLDER = "（还不了解用户，等待通过对话了解）"


def _human_block_has_real_onboarding_content(human_block: str | None) -> bool:
    """Return true only for real legacy human facts, not the seed placeholder."""
    text = "\n".join(
        line.strip()
        for line in str(human_block or "").splitlines()
        if line.strip()
    )
    if not text:
        return False
    return text != _DEFAULT_HUMAN_PLACEHOLDER


def _needs_onboarding() -> bool:
    """Return whether the current user still needs the first-meet flow."""
    if storage.is_onboarding_completed():
        return False

    # Legacy compatibility: older accounts may have real human facts but no
    # onboarding_meta.json. Do not treat the default "unknown user" seed as
    # completion; fresh accounts would otherwise skip the first-meet flow.
    import core_memory
    human_block = core_memory.get_block("human") or ""
    if _human_block_has_real_onboarding_content(human_block):
        return False
    return True


@app.route("/api/bootstrap")
def api_bootstrap():
    """Aggregate first-screen data into a single response.

    Merges: character, relationship, chat_history, emotion_today,
    onboarding_status, and version — replacing 5+ individual GETs.
    """
    import core_memory

    # Character
    cfg = get_config()
    character_data = {
        "name": cfg.name,
        "user_address": cfg.user_address,
        "avatar_url": "/assets/character-avatar.png",
    }

    # Relationship
    meta = storage.ensure_first_meet_date()
    first_meet = meta.get("first_meet_date", "")
    days = 0
    if first_meet:
        from datetime import datetime as _dt
        try:
            days = (_dt.now() - _dt.strptime(first_meet, "%Y-%m-%d")).days
        except ValueError:
            pass
    relationship_data = {
        "first_meet_date": first_meet,
        "days_together": days,
        "character_name": cfg.name,
    }

    # Chat history
    limit = int(request.args.get("chat_limit", 200))
    history = storage.get_chat_history(limit=max(1, min(limit, 500)))
    for msg in history:
        if msg.get("type") == "reminder" and msg.get("image"):
            img = msg["image"]
            base = img.rsplit(".", 1)[0] if "." in img else img
            for ext in [".png", ".jpg", ".webp"]:
                if os.path.exists(os.path.join(storage._uploads_dir(), base + ext)):
                    msg["image"] = base + ext
                    break

    # Emotion today (wrap in {entries: []} to match frontend expectation)
    emotion_today = {"entries": storage.get_today_emotion_log()}

    return jsonify({
        "character": character_data,
        "relationship": relationship_data,
        "chat_history": history,
        "emotion_today": emotion_today,
        "onboarding": {"needs_onboarding": _needs_onboarding()},
        "version": _get_version(),
        "auth": {
            "user_id": getattr(g, "user_id", "_admin"),
            "is_admin": getattr(g, "is_admin", False),
        },
    })


@app.route("/api/app/latest")
def api_app_latest():
    """Return latest APK version info for update checking."""
    meta_path = os.path.join(storage.DATA_DIR, "releases", "latest.json")
    if os.path.exists(meta_path):
        return send_from_directory(os.path.dirname(meta_path), "latest.json")
    return jsonify({"version_code": 0, "version_name": "unknown", "download_url": ""})


@app.route("/api/app/download")
def api_app_download():
    """Serve the latest APK file."""
    meta_path = os.path.join(storage.DATA_DIR, "releases", "latest.json")
    if os.path.exists(meta_path):
        import json as _json
        meta = _json.loads(open(meta_path).read())
        apk_name = meta.get("apk_file", "miru-latest.apk")
        releases_dir = os.path.join(storage.DATA_DIR, "releases")
        if os.path.exists(os.path.join(releases_dir, apk_name)):
            return send_from_directory(releases_dir, apk_name, as_attachment=True)
    return jsonify({"error": "No APK available"}), 404


@app.route("/data/uploads/<path:filename>")
def serve_upload(filename):
    return send_from_directory(storage._uploads_dir(), filename)


# ===== Emotion Endpoints (kept) =====

@app.route("/api/emotion/today", methods=["GET"])
def api_emotion_today():
    """Return today's emotion log entries."""
    return jsonify(storage.get_today_emotion_log())


@app.route("/api/emotion/<date_str>", methods=["GET"])
def api_emotion_by_date(date_str):
    """Return emotion log for a specific date (YYYY-MM-DD)."""
    return _etag_json(storage.get_emotion_log_by_date(date_str))


@app.route("/api/emotion/month/<month_str>", methods=["GET"])
def api_emotion_month(month_str):
    """Return per-day emotion summary for a month (YYYY-MM).

    Response: {"2026-04-01": {"count": 5, "dominant_mood": "happy", "avg_valence": 0.42}, ...}
    """
    return _etag_json(storage.get_emotion_log_month(month_str))


# ===== Miru Emotion Endpoints =====

@app.route("/api/miru-emotion/current", methods=["GET"])
def api_miru_emotion_current():
    """Return Miru's current emotional state + relationship meter.

    #193-D: now also returns ``first_meet_date`` + ``days_together`` so the
    frontend can render the unified "关系仪表盘" in the Miru Emotion panel
    without a second request to /api/relationship.
    """
    try:
        import miru_emotion
        emo = miru_emotion.get_instance()
        state = emo.get_state()
        # Ensure first_meet_date is bootstrapped (migrates legacy file if present)
        first_meet = emo.ensure_first_meet_date()
        days_together = emo.get_days_together()
        relationship = emo.get_relationship()
        stage = emo.get_relationship_stage()
        cfg = get_config()
        return _etag_json({
            "current": state,
            "relationship": relationship,
            "stage": stage,
            "first_meet_date": first_meet,
            "days_together": days_together,
            "character_name": cfg.name,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/miru-emotion/history", methods=["GET"])
def api_miru_emotion_history():
    """Return Miru's emotion history for visualization.

    Query params:
        limit: max entries (default 50)
    """
    try:
        import miru_emotion
        limit = request.args.get("limit", 50, type=int)
        emo = miru_emotion.get_instance()
        history = emo.get_history(limit=min(limit, 200))
        return _etag_json(history)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ===== Read-only endpoints =====


@app.route("/api/journal", methods=["GET"])
def api_journal_list():
    """List journal entries with new schema (date descending).

    Also opportunistically backfills up to a few missing/malformed JSON
    journals per call (max 3) so the user eventually sees every day.
    """
    import journal as _j
    _j.scan_and_backfill(days=30, max_per_call=3)
    return _etag_json(_j.list_journals())


@app.route("/api/journal/<date>", methods=["GET"])
def api_journal_detail(date):
    """Read a single journal by date. Returns the v2 JSON schema if available,
    otherwise lazily generates it (backfill on demand)."""
    import journal as _j
    j = _j.get_journal(date)
    if not j:
        # Lazy-generate from whatever we have (chat / observations)
        j = _j.generate_daily_journal(date, force=False)
    if j:
        return _etag_json(j)
    return jsonify({"error": "未找到该日期的日记"}), 404


@app.route("/api/journal/<date>/regenerate", methods=["POST"])
def api_journal_regenerate(date):
    """Force regenerate a single day's journal (user-triggered refresh)."""
    import journal as _j
    j = _j.generate_daily_journal(date, force=True)
    if not j:
        return jsonify({"error": "生成失败"}), 500
    return jsonify(j)


@app.route("/api/journal/backfill", methods=["POST"])
def api_journal_backfill():
    """One-time bulk regenerate last N days (default 30). User-triggered.

    Heavier than list-scan; use sparingly. Returns {"regenerated": [...]}
    """
    import journal as _j
    body = request.get_json(silent=True) or {}
    days = int(body.get("days", 30))
    days = max(1, min(days, 90))
    return jsonify(_j.regenerate_all(days=days))


@app.route("/api/timeline", methods=["GET"])
def api_timeline():
    memory_scope = _get_memory_scope(default="user", allow_all=True)
    timeline = storage.read_json(storage.timeline_path())

    result = []
    existing_ids = set()
    for entry in timeline:
        item = dict(entry)
        img = entry.get("image", "")
        if img:
            base = img.rsplit(".", 1)[0] if "." in img else img
            for ext in [".png", ".jpg", ".webp"]:
                if os.path.exists(os.path.join(storage._uploads_dir(), base + ext)):
                    item["image"] = base + ext
                    break
        result.append(item)
        existing_ids.add(entry["id"])

    # Backwards compat: merge orphan reminders not yet in timeline
    all_reminders = storage.read_json(storage.reminders_path())
    for r in all_reminders:
        r_key = r.get("key", "")
        created = r.get("created_at", "")
        r_id = r_key + "_" + created.replace(" ", "_").replace(":", "")
        if r_id in existing_ids or r_key in existing_ids:
            continue
        img = r.get("image_filename", "")
        if img:
            base = img.rsplit(".", 1)[0] if "." in img else img
            for ext in [".png", ".jpg", ".webp"]:
                if os.path.exists(os.path.join(storage._uploads_dir(), base + ext)):
                    img = base + ext
                    break
        result.append({
            "id": r_id,
            "reminder_key": r_key,
            "type": "reminder",
            "text": r.get("text", ""),
            "image": img,
            "time": created,
            "status": "confirmed",
        })
        existing_ids.add(r_id)

    result.sort(key=lambda x: x.get("time", ""))
    return jsonify(result)


# ===== Airi Chat Channel =====

@app.route("/api/chat", methods=["POST"])
def api_chat():
    """Receive a chat message asynchronously. Reply appears via polling.

    Accepts JSON: {"text": "...", "sync": true}
    Or FormData: text + optional image file
    """
    # Detect content type
    if request.content_type and "multipart/form-data" in request.content_type:
        text = (request.form.get("text") or "").strip()
        sync = request.form.get("sync", "").lower() in ("true", "1")
        image_file = request.files.get("image")
    else:
        body = request.get_json(silent=True) or {}
        text = body.get("text", "").strip()
        sync = bool(body.get("sync"))
        image_file = None

    if not text and not image_file:
        return jsonify({"error": "请输入消息"}), 400

    # In remote client mode (DMG logged into a private server), forward chat to
    # the private server. In local single-device mode, the local Flask process
    # is the source of truth and must run core.* directly.
    if _is_client_mode and not _is_local_single_device_mode() and not sync:
        return _forward_chat_to_vps(text, image_file)

    # Handle image upload
    image_filename = None
    if image_file and image_file.filename and core.allowed_file(image_file.filename):
        ts = datetime.now().strftime("chat_%Y%m%d_%H%M%S")
        ext = image_file.filename.rsplit(".", 1)[1].lower()
        image_filename = f"{ts}.{ext}"
        image_path = os.path.join(storage._uploads_dir(), image_filename)
        image_file.save(image_path)
        image_filename = storage.compress_image(image_path)

    # Sync mode for companion API backward compatibility
    if sync:
        result = core.chat_with_companion(text)
        return jsonify(result)

    # Async mode: buffer message and return immediately
    device_id = request.headers.get("X-Device-Id") or request.args.get("device_id") or ""
    result = core.receive_chat_message(text, image=image_filename, device_id=device_id)
    return jsonify(result)


def _forward_chat_to_vps(text: str, image_file=None):
    """Forward /api/chat to VPS in client mode. VPS is source of truth for
    LLM (chat agent + sleep agent + memory router) since DMG users don't
    have API keys — only admin configures them via admin UI.
    """
    import requests as _req
    cfg = _client_mode_config or {}
    server_url = (cfg.get("server_url") or "").rstrip("/")
    token = cfg.get("auth_token", "")
    if not server_url or not token:
        return jsonify({"error": "未连接服务器,请重新登录"}), 502

    headers = {"Authorization": f"Bearer {token}"}
    device_id = request.headers.get("X-Device-Id") or request.args.get("device_id") or ""
    if device_id:
        headers["X-Device-Id"] = device_id

    try:
        if image_file and image_file.filename:
            files = {"image": (image_file.filename, image_file.stream, image_file.mimetype)}
            data = {"text": text}
            resp = _req.post(f"{server_url}/api/chat", headers=headers,
                            files=files, data=data, timeout=30)
        else:
            resp = _req.post(f"{server_url}/api/chat", headers=headers,
                            json={"text": text}, timeout=30)
    except Exception as e:
        return jsonify({"error": f"无法连接服务器 ({e})"}), 502

    try:
        return jsonify(resp.json()), resp.status_code
    except Exception:
        return jsonify({"error": "服务器响应异常"}), resp.status_code


@app.route("/api/chat/typing")
def api_chat_typing():
    """Return whether Airi is currently generating a reply."""
    return jsonify({"typing": core.get_typing_status()})



@app.route("/api/cleanup-legacy", methods=["POST"])
def api_cleanup_legacy():
    """Clean up old data files for new agent architecture."""
    result = core.cleanup_legacy_data()
    return jsonify(result)


@app.route("/api/chat/history", methods=["GET"])
def api_chat_history():
    """Return recent chat history (last 200 messages).

    Optional query params:
      - limit: max messages (default 200, max 500)
      - since: ISO timestamp — return only messages after this time
    """
    limit = int(request.args.get("limit", 200))
    since = request.args.get("since", "")
    history = storage.get_chat_history(limit=max(1, min(limit, 500)))
    # Filter by time if 'since' provided (for offline message catch-up)
    if since:
        history = [m for m in history if m.get("time", "") > since]
    # Fix reminder image extensions (stored as .png but may have been saved as .jpg/.webp)
    for msg in history:
        if msg.get("type") == "reminder" and msg.get("image"):
            img = msg["image"]
            base = img.rsplit(".", 1)[0] if "." in img else img
            for ext in [".png", ".jpg", ".webp"]:
                if os.path.exists(os.path.join(storage._uploads_dir(), base + ext)):
                    msg["image"] = base + ext
                    break
    return jsonify(history)


@app.route("/api/core-memory", methods=["GET"])
def api_core_memory():
    """Return the always-in-context human/persona blocks for review.

    These are not slot files, but they are injected into the main agent and
    AttentionEngine every turn. Exposing them read-only in the UI makes the
    learning loop auditable without mixing them into archival slot memory.
    """
    import core_memory

    blocks = core_memory.get_all_blocks() or {}
    labels = {
        "human": "我理解的你",
        "persona": "我和你的关系",
    }
    result = {}
    for key in ("human", "persona"):
        content = (blocks.get(key) or "").strip()
        result[key] = {
            "label": key,
            "title": labels[key],
            "content": content,
            "chars": len(content),
        }
    return jsonify({"ok": True, "blocks": result})


# ===== Notification Endpoints =====

@app.route("/api/notifications/pending", methods=["GET"])
def api_notifications_pending():
    """Return unread notifications for a device (for offline push)."""
    device_id = request.args.get("device_id", "unknown")
    notifs = storage.get_pending_notifications(device_id, mark_read=True)
    # Return only text/type/time (strip read_by for privacy)
    return jsonify([{
        "id": n["id"], "text": n["text"], "type": n["type"], "time": n["time"]
    } for n in notifs])


# ===== Reminder Endpoints =====

@app.route("/api/reminders/check", methods=["GET"])
def api_reminders_check():
    # Schedule-based reminders removed — all proactive messages go through agent now.
    # This endpoint is kept for backward compatibility with polling clients.
    return jsonify({"reminders": []})


# ===== SSE (Server-Sent Events) =====

@app.route("/api/events")
def api_events():
    """SSE endpoint — real-time push of chat messages, typing, proactive messages.

    Accepts ?token= for auth (EventSource cannot set headers)
    and ?device_id= for client identification.
    """
    import sse as _sse
    device_id = request.args.get("device_id", "unknown")
    user_id = getattr(g, "user_id", "_admin")
    q = _sse.add_client(device_id, user_id=user_id)
    resp = Response(_sse.stream_generator(q), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp


# ===== Device Management =====

@app.route("/api/device/register", methods=["POST"])
def api_device_register():
    """Register a device. Auth handled by check_request middleware (Bearer header)."""
    import device_manager

    data = request.get_json(silent=True) or {}
    name = data.get("name", "Unknown Device")
    device_type = data.get("type", "desktop")
    device_platform = data.get("platform", "web")
    device_id = data.get("device_id")

    device = device_manager.register_device(name, device_type, device_platform, device_id)
    return jsonify({"ok": True, "device": device})


@app.route("/api/device/heartbeat", methods=["POST"])
def api_device_heartbeat():
    """Update device last_seen timestamp."""
    import device_manager

    data = request.get_json(silent=True) or {}
    device_id = data.get("device_id", "")
    if not device_id:
        return jsonify({"error": "device_id required"}), 400
    ok = device_manager.heartbeat(device_id)
    return jsonify({"ok": ok})


@app.route("/api/device/list", methods=["GET"])
def api_device_list():
    """Return registered devices with online status. Hide stale devices (>24h)."""
    import device_manager
    devices = device_manager.get_devices()
    now_ts = time.time()
    local_id = device_manager.get_local_device_id()
    result = []
    for d in devices:
        # Skip desktop browsers — they aren't real devices
        # Real desktops have platform like darwin/linux/win32; browsers have MacIntel/Win32/web etc.
        if d.get("type") == "desktop" and d.get("platform") not in ("darwin", "linux", "win32", "macos", "windows"):
            continue
        try:
            last_seen = datetime.fromisoformat(d.get("last_seen", ""))
            age_seconds = now_ts - last_seen.timestamp()
            # The server's own device is always online (it's serving this request)
            if d.get("device_id") == local_id:
                d["is_online"] = True
            else:
                d["is_online"] = age_seconds < 120
            # Hide devices inactive for over 24 hours (except the server itself)
            if age_seconds > 86400 and d.get("device_id") != local_id:
                continue
        except (ValueError, TypeError):
            d["is_online"] = False
            continue
        result.append(d)
    # Online first, then by last_seen descending
    result.sort(key=lambda d: (not d.get("is_online"), d.get("last_seen", "")))
    return jsonify(result)


@app.route("/api/pet-runtime-broadcast", methods=["POST"])
def api_pet_runtime_broadcast():
    """Push pet-side runtime settings (hotkey, collapse delay) over SSE.

    These values are intentionally NOT persisted to user_settings.json:
    they're per-device and live in the desktop client's localStorage.
    The SSE broadcast lets pet.html (running in a separate Tauri webview
    that doesn't share localStorage with the main UI) hot-reload the
    global hotkey and auto-collapse timer without restart.

    Body: {pet_hotkey?: str, pet_collapse_delay?: int}
    Effect: broadcasts 'pet_runtime_changed' to all SSE clients of the
    current user. Same user_id = same physical machine in our deployment,
    so this is effectively device-local in practice.
    """
    import sse
    try:
        from flask import g
        uid = getattr(g, "user_id", None)
    except Exception:
        uid = None
    if not uid:
        return jsonify({"error": "no user context"}), 401

    body = request.get_json(silent=True) or {}
    payload = {}
    if "pet_hotkey" in body and body["pet_hotkey"]:
        payload["pet_hotkey"] = str(body["pet_hotkey"]).strip().lower()
    if "pet_collapse_delay" in body and body["pet_collapse_delay"] is not None:
        try:
            d = int(body["pet_collapse_delay"])
            payload["pet_collapse_delay"] = max(min(d, 60000), 1000)
        except (ValueError, TypeError):
            pass

    if not payload:
        return jsonify({"error": "no recognized fields"}), 400

    sse.broadcast("pet_runtime_changed", payload, user_id=uid)
    return jsonify({"ok": True, "broadcast": payload})


@app.route("/api/device/<device_id>", methods=["DELETE"])
def api_device_delete(device_id):
    """User-initiated removal of a device record.

    Use case: a Vivo APK whose LocalStorage rotated → leaves a stale
    "Vivo V2415A" record. Auto-dedupe handles same-name duplicates,
    but the user may also want to remove a device they're not using
    anymore (e.g. an old phone they sold).

    The DELETE only affects the registry. The native desktop client in local
    single-device mode restores its own active record after a missing-device
    heartbeat; other devices register again when their app starts. Stale ones
    remain deleted.
    """
    import device_manager
    if not device_id:
        return jsonify({"error": "device_id required"}), 400
    ok = device_manager.remove_device(device_id)
    if not ok:
        return jsonify({"error": "device not found"}), 404
    return jsonify({"ok": True, "device_id": device_id})


@app.route("/api/device/qr", methods=["GET"])
def api_device_qr():
    """Generate QR code for phone connection — encodes the CURRENT USER's
    token so the phone logs in as the same account on a second device.

    2026-05-09 [P0 fix]: previously returned auth.get_or_create_token()
    which is the ADMIN token (legacy alias from single-user days). Any
    logged-in user could scan the QR / read the response and obtain
    admin privileges, then access /api/admin/users etc. Now scoped to
    the current user.
    """
    import auth as _auth
    import device_manager

    uid = getattr(g, "user_id", None)
    if not uid or uid == "_admin":
        return jsonify({"error": "user login required"}), 401
    user = _auth.get_user(uid)
    if not user:
        return jsonify({"error": "user not found"}), 404
    token = user.get("token", "")
    if not token:
        return jsonify({"error": "user has no token"}), 500

    # Build the URL the phone will connect to.
    # Priority: DOMAIN env > TUNNEL_URL env > request Host header > LAN IP fallback
    domain = os.environ.get("DOMAIN", "").strip().rstrip("/")
    tunnel_url = os.environ.get("TUNNEL_URL", "").strip().rstrip("/")
    if domain:
        url = f"https://{domain}/?token={token}"
    elif tunnel_url:
        url = f"{tunnel_url}/?token={token}"
    else:
        # Use the Host header from the current request — this is the address
        # the user is already using to reach the server (works for both LAN and VPS)
        host = request.host  # e.g. "192.168.1.5:5001"
        scheme = request.scheme
        url = f"{scheme}://{host}/?token={token}"

    # Generate QR code as data URL
    try:
        import qrcode
        import io
        import base64
        qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_L,
                           box_size=10, border=3)
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        qr_b64 = base64.b64encode(buf.getvalue()).decode()
        qr_data_url = f"data:image/png;base64,{qr_b64}"
    except ImportError:
        # qrcode library not installed — return URL only
        qr_data_url = None

    # Check if APK is available
    apk_path = os.path.join(os.path.dirname(__file__), "miru-mobile", "Miru-v0.1.0-debug.apk")
    has_apk = os.path.exists(apk_path)

    return jsonify({
        "url": url,
        "token": token,
        "lan_ip": request.host.split(":")[0],
        "qr_data_url": qr_data_url,
        "mode": "tunnel" if tunnel_url else "lan",
        "has_apk": has_apk,
        "apk_url": "/download/miru.apk" if has_apk else None,
    })


@app.route("/download/miru.apk")
def download_apk():
    """Serve Miru Android APK for download."""
    apk_dir = os.path.join(os.path.dirname(__file__), "miru-mobile")
    return send_from_directory(apk_dir, "Miru-v0.1.0-debug.apk",
                               as_attachment=True, download_name="Miru.apk")


@app.route("/api/device/screenshot", methods=["POST"])
def api_device_screenshot():
    """Receive a screenshot from a device sensor, forward to ScreenAnalyzer."""
    import device_manager
    from screen_analyzer import get_analyzer

    device_id = request.form.get("device_id", "unknown")
    captured_at = request.form.get("captured_at")  # ISO timestamp from client

    # Accept multipart file upload
    if "image" not in request.files:
        return jsonify({"error": "No image file"}), 400
    jpeg_bytes = request.files["image"].read()
    if not jpeg_bytes:
        return jsonify({"error": "Empty image"}), 400

    # Record device activity (heartbeat)
    device_manager.record_device_activity(device_id)

    # Forward to analyzer (pass captured_at for accurate log timestamps)
    result = get_analyzer().analyze(jpeg_bytes, device_id=device_id,
                                    captured_at=captured_at)
    if result:
        # AttentionEngine receives sig>=2 screenshot signals inside
        # screen_analyzer.analyze() and owns both user-affect and Miru-inner
        # updates.  The old per-screenshot emotion workers are intentionally
        # not dispatched here anymore.
        return jsonify({"ok": True, "observation": result.get("observation", "")[:100]})
    return jsonify({"ok": True, "skipped": True})


@app.route("/api/debug/sensor_status", methods=["GET"])
def api_debug_sensor_status():
    """Diagnostic endpoint: dump the local ScreenSensor runtime state so we
    can tell from the outside whether capture is running, paused, or
    permanently backed-off due to missing Screen Recording permission.
    """
    try:
        import sensor as _sensor
        inst = _sensor._instance
    except Exception as e:
        return jsonify({"ok": False, "error": f"sensor import failed: {e}"}), 500
    if inst is None:
        return jsonify({"ok": True, "running": False, "note": "ScreenSensor not instantiated (likely server-side VPS deployment — no local sensor)"})
    last_active = inst.get_last_screen_active_time()
    disabled_until = getattr(inst, "_capture_disabled_until", None)
    last_capture_at = getattr(inst, "_last_capture_at", None)
    return jsonify({
        "ok": True,
        "running": getattr(inst, "_running", False),
        "user_enabled": getattr(inst, "_user_enabled", False),
        "capture_disabled": getattr(inst, "_capture_disabled", False),
        "capture_disabled_until": disabled_until.isoformat()[:19] if disabled_until else None,
        "consecutive_fail_count": getattr(inst, "_capture_fail_count", 0),
        "last_screen_active_time": last_active.isoformat()[:19] if last_active else None,
        "last_capture_at": last_capture_at.isoformat()[:19] if last_capture_at else None,
        "last_capture_display_index": getattr(inst, "_last_capture_display_index", None),
        "last_capture_display_name": getattr(inst, "_last_capture_display_name", ""),
        "last_capture_size": getattr(inst, "_last_capture_size", None),
        "last_capture_error": getattr(inst, "_last_capture_error", ""),
        "device_id": getattr(inst, "_device_id", None),
        "backend_url": getattr(inst, "_backend_url", None),
    })


def _json_default_for_debug(value):
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    return str(value)


def _render_attention_prompt_debug_html(payload: dict) -> str:
    import html as _html

    meta = payload.get("meta") or {}
    system_prompt = payload.get("system_prompt", "")
    user_prompt = payload.get("user_prompt", "")
    snapshot = payload.get("snapshot") or {}
    messages_json = json.dumps(payload.get("llm_call") or {}, ensure_ascii=False, indent=2)
    raw_snapshot = json.dumps(snapshot, ensure_ascii=False, indent=2, default=_json_default_for_debug)

    def esc(value):
        return _html.escape(str(value), quote=True)

    rows = "\n".join(
        f"<dt>{esc(k)}</dt><dd>{esc(v)}</dd>"
        for k, v in [
            ("user_id", meta.get("user_id", "")),
            ("user_data_dir", meta.get("user_data_dir", "")),
            ("generated_at", meta.get("generated_at", "")),
            ("trigger", meta.get("trigger", "")),
            ("with_review_signals", meta.get("with_review_signals", False)),
            ("route", "/api/debug/attention-prompt"),
        ]
    )
    sources = [
        ("System soul/personality", "character.get_config().raw_text", "soul.md / active persona soul", "完整注入，不压缩"),
        ("Shared behavior core", "_load_agent_behavior_core_section()", "agent_behavior.md ## Core", "存在时附加"),
        ("Activity", "sleep_inference.infer_activity_state()", "screenshot_log/chat/device activity", "当前活跃/idle/作息窗口"),
        ("Conversation window", "storage.get_chat_history(limit=240) -> _build_conversation_window", "chat_history.json", "近 7 天 / 最多 80 条，标注 reply/proactive/care 与用户回应"),
        ("Proactive cadence", "_build_proactive_cadence(history)", "chat_history.json", "近 7 天主动次数、24h 次数、未回应数量、上次主动是否被回应"),
        ("Current segments", "storage.load_attention_state().current_segments", "attention_state.json", "三段连续状态：我没说出口的想法 / 我对用户状态的感觉 / 我自己的心情"),
        ("Speak intent queue", "storage.load_attention_intent_queue()", "attention_intent_queue.json", "待 delivery gate 判断的 speak_intent"),
        ("Message distances", "_message_distances(history)", "chat_history.json", "距用户/assistant/主动消息多久"),
        ("New signals", "AttentionEngine._signals deque", "内存中的 screenshot/chat/state signals", "最近 1 小时，最多 40 条"),
        ("Inner segments", "storage.get_recent_attention_log(limit=30)", "attention_log.json", "最近的第一人称状态段，不是 tick 采样"),
        ("User affect segments", "storage.get_today_emotion_log()", "emotion_log.json", "当天用户情绪状态段与 3h bucket"),
        ("Self emotion", "miru_emotion.get_instance().get_state()", "miru_emotion.json", "我自己的当前心情"),
        ("Human/persona blocks", "core_memory.get_all_blocks()", "core_memory.json", "仅 account_manifest.json 绑定当前 user_id 后注入"),
        ("Identity facts", "identity.compose_ground_truth_block(header=False)", "memory/self/identity/main.md 或 identity.json fallback", "权威身份事实"),
        ("Commitments", "_format_relevant_commitments(now)", "memory/commitments/active.md", "只注入未完成、当前相关、有 deadline 的 DDL；很旧逾期项会跳过"),
    ]
    source_rows = "\n".join(
        "<tr>"
        f"<td>{esc(name)}</td><td>{esc(call)}</td><td>{esc(src)}</td><td>{esc(note)}</td>"
        "</tr>"
        for name, call, src, note in sources
    )
    style = """
    :root { color-scheme: light; --bg:#f6f7f9; --panel:#fff; --ink:#171a1f; --muted:#667085; --line:#d8dee8; --code:#0f172a; --code-ink:#e5e7eb; }
    * { box-sizing: border-box; }
    body { margin:0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; background:var(--bg); color:var(--ink); line-height:1.55; }
    header { position:sticky; top:0; z-index:2; border-bottom:1px solid var(--line); background:rgba(246,247,249,.96); padding:18px 28px; }
    h1 { margin:0 0 6px; font-size:24px; letter-spacing:0; }
    .sub { color:var(--muted); font-size:13px; }
    main { max-width:1280px; margin:0 auto; padding:22px 28px 56px; }
    section { background:var(--panel); border:1px solid var(--line); border-radius:8px; margin:18px 0; overflow:hidden; }
    .head { display:flex; align-items:baseline; justify-content:space-between; gap:16px; padding:14px 16px; border-bottom:1px solid var(--line); }
    h2 { margin:0; font-size:17px; letter-spacing:0; }
    .count { color:var(--muted); font-size:12px; white-space:nowrap; }
    dl { display:grid; grid-template-columns:180px 1fr; gap:8px 14px; margin:0; padding:16px; }
    dt { color:var(--muted); } dd { margin:0; min-width:0; overflow-wrap:anywhere; }
    pre { margin:0; padding:18px; overflow:auto; white-space:pre-wrap; word-break:break-word; font:12.5px/1.55 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; background:var(--code); color:var(--code-ink); }
    table { width:100%; border-collapse:collapse; font-size:13px; } th,td { text-align:left; vertical-align:top; border-bottom:1px solid var(--line); padding:10px 12px; overflow-wrap:anywhere; } th { color:var(--muted); background:#fafbfc; }
    @media (max-width:720px) { header, main { padding-left:14px; padding-right:14px; } dl { grid-template-columns:1fr; } .head { display:block; } .count { display:block; margin-top:4px; } }
    """
    return f"""<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>AttentionEngine Prompt Debug</title><style>{style}</style></head>
<body>
  <header><h1>AttentionEngine Prompt Debug</h1><div class="sub">当前认证账号 · 完整未压缩 · 不调用 LLM</div></header>
  <main>
    <section><div class="head"><h2>调用参数</h2><span class="count">当前登录用户上下文</span></div><dl>{rows}</dl></section>
    <section><div class="head"><h2>messages[0] role=system</h2><span class="count">{len(system_prompt)} chars / {system_prompt.count(chr(10)) + 1} lines</span></div><pre>{esc(system_prompt)}</pre></section>
    <section><div class="head"><h2>messages[1] role=user</h2><span class="count">{len(user_prompt)} chars / {user_prompt.count(chr(10)) + 1} lines</span></div><pre>{esc(user_prompt)}</pre></section>
    <section><div class="head"><h2>上下文来源表</h2><span class="count">每个 snapshot 模块实际读哪里</span></div><table><thead><tr><th>模块</th><th>代码入口</th><th>数据来源</th><th>备注</th></tr></thead><tbody>{source_rows}</tbody></table></section>
    <section><div class="head"><h2>完整调用 JSON 示例</h2><span class="count">包含完整 messages 数组</span></div><pre>{esc(messages_json)}</pre></section>
    <section><div class="head"><h2>Raw Snapshot JSON</h2><span class="count">AttentionEngine._build_snapshot() 原始结构</span></div><pre>{esc(raw_snapshot)}</pre></section>
  </main>
</body>
</html>"""


@app.route("/api/debug/attention-prompt", methods=["GET"])
@app.route("/api/debug/attention_prompt", methods=["GET"])
def api_debug_attention_prompt():
    """Render the exact AttentionEngine prompt for the authenticated user."""
    uid = getattr(g, "user_id", None)
    user_data_dir = getattr(g, "user_data_dir", None)
    if not uid or uid == "_admin" or not user_data_dir:
        return jsonify({"error": "user account required"}), 401

    trigger = (request.args.get("trigger") or "debug_review").strip()[:80]
    with_review_signals = request.args.get("with_review_signals") in {"1", "true", "yes"}

    try:
        import auth as _auth
        _auth.ensure_account_manifest(uid)
        from attention_engine import (
            AttentionEngine,
            _build_attention_system_prompt,
            build_attention_snapshot_prompt,
        )

        engine = AttentionEngine(user_id=uid, user_data_dir=user_data_dir)
        if with_review_signals:
            engine.record_signal("screenshot", {
                "device_id": "debug-route",
                "device_name": "Prompt debug route",
                "observation": "【debug signal】用户正在查看当前账号的 AttentionEngine 完整 prompt。",
                "significance": 4,
            })
            engine.record_signal("chat_in", {
                "text": "【debug signal】请展示当前账号实际会传给 AttentionEngine 的完整上下文。",
                "device_id": "debug-route",
            })

        snapshot = engine._build_snapshot(trigger=trigger)
        system_prompt = _build_attention_system_prompt()
        user_prompt = build_attention_snapshot_prompt(snapshot)
        llm_call = {
            "helper": "prompt._call_llm_json",
            "tier": "memory",
            "reasoning": False,
            "temperature": 0.45,
            "max_tokens": 12000,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        payload = {
            "meta": {
                "user_id": uid,
                "user_data_dir": user_data_dir,
                "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "trigger": trigger,
                "with_review_signals": with_review_signals,
            },
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "snapshot": snapshot,
            "llm_call": llm_call,
        }
    except Exception as e:
        return jsonify({"error": f"attention prompt build failed: {e}"}), 500

    if (request.args.get("format") or "html").lower() == "json":
        return app.response_class(
            json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default_for_debug),
            mimetype="application/json",
        )
    return Response(_render_attention_prompt_debug_html(payload), mimetype="text/html")


@app.route("/api/screenshot/stats", methods=["GET"])
def api_screenshot_stats():
    """Per-device analyzed screenshot stats from ScreenSemanticGate pass log."""
    raw = storage.get_screenshot_stats()
    # Enrich with device display names
    try:
        import device_manager
        devices = {d["device_id"]: d for d in device_manager.get_devices()}
    except Exception:
        devices = {}
    result = {}
    for did, s in raw.items():
        name = devices.get(did, {}).get("name", did)
        result[did] = {
            "name": name,
            "total": s["total"],
            "today": s["today"],
            "last_time": s["last_time"],
        }
    return jsonify({"ok": True, "stats": result})


# ===== Onboarding Endpoints =====

@app.route("/api/onboarding/status", methods=["GET"])
def api_onboarding_status():
    """Check if the per-user first-meet flow is still needed."""
    return jsonify({"needs_onboarding": _needs_onboarding()})


@app.route("/api/onboarding/submit", methods=["POST"])
def api_onboarding_submit():
    """Receive onboarding answers and initialize core_memory + self_profile."""
    answers = request.get_json(silent=True) or {}
    try:
        result = core.initialize_from_questionnaire(answers)
        return jsonify({"ok": True, "result": result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ===== Push Notification Endpoints =====

@app.route("/api/push/vapid-key", methods=["GET"])
def api_push_vapid_key():
    """Return the VAPID public key for push subscription."""
    import server_config
    pub_key = server_config.get().get("vapid_public_key", "")
    return jsonify({"publicKey": pub_key})


@app.route("/api/push/subscribe", methods=["POST"])
def api_push_subscribe():
    """Store a push subscription from the client."""
    sub = request.get_json(silent=True)
    if not sub or not sub.get("endpoint"):
        return jsonify({"error": "Invalid subscription"}), 400
    subs = storage.read_json(storage.push_subscriptions_path()) or []
    # Dedup by endpoint
    existing = {s.get("endpoint") for s in subs}
    if sub["endpoint"] not in existing:
        subs.append(sub)
        storage.write_json(storage.push_subscriptions_path(), subs)
    return jsonify({"ok": True})


@app.route("/api/push/unsubscribe", methods=["POST"])
def api_push_unsubscribe():
    """Remove a push subscription. Used by Android APK to clean up stale
    subscriptions — MiruConnectionService native SSE replaces web push there."""
    body = request.get_json(silent=True) or {}
    endpoint = body.get("endpoint", "")
    if not endpoint:
        return jsonify({"error": "Missing endpoint"}), 400
    subs = storage.read_json(storage.push_subscriptions_path()) or []
    new_subs = [s for s in subs if s.get("endpoint") != endpoint]
    if len(new_subs) != len(subs):
        storage.write_json(storage.push_subscriptions_path(), new_subs)
    return jsonify({"ok": True, "removed": len(subs) - len(new_subs)})


@app.route("/api/reminders/today", methods=["GET"])
def api_reminders_today():
    today_str = datetime.now().strftime("%Y-%m-%d")
    records = storage.get_reminders_for_date(today_str)
    reminders = []
    for r in records:
        img_filename = r.get("image_filename", "")
        image_url = ""
        if img_filename:
            base = img_filename.rsplit(".", 1)[0] if "." in img_filename else img_filename
            for ext in [".png", ".jpg", ".webp"]:
                if os.path.exists(os.path.join(storage._uploads_dir(), base + ext)):
                    image_url = f"/data/uploads/{base}{ext}"
                    break
            if not image_url:
                image_url = f"/data/uploads/{img_filename}"
        reminders.append({
            "id": r.get("key", ""),
            "text": r.get("text", ""),
            "image_url": image_url,
            "created_at": r.get("created_at", ""),
        })
    return jsonify({"reminders": reminders})


@app.route("/api/reminders/image-ready/<filename>")
def api_reminder_image_ready(filename):
    safe_name = secure_filename(filename)
    base = safe_name.rsplit(".", 1)[0] if "." in safe_name else safe_name
    for ext in [".png", ".jpg", ".webp"]:
        path = os.path.join(storage._uploads_dir(), base + ext)
        if os.path.exists(path):
            return jsonify({"ready": True, "url": f"/data/uploads/{base}{ext}"})
    return jsonify({"ready": False})


# ===== Character API =====

@app.route("/api/character")
def api_character():
    """Return current character identity for frontend dynamic rendering."""
    cfg = get_config()
    return jsonify({
        "name": cfg.name,
        "user_address": cfg.user_address,
        "avatar_url": "/assets/character-avatar.png",
    })


@app.route("/api/character/fields", methods=["GET"])
def api_character_fields_get():
    """Return all parsed fields of the active character's soul.md."""
    cfg = get_config()
    return jsonify({
        "name": cfg.name,
        "user_address": cfg.user_address,
        "personality": cfg.personality,
        "speech_patterns": cfg.speech_patterns,
        "appearance": cfg.appearance,
        "backstory": cfg.backstory,
        "interests": cfg.interests,
        "prompt_hints": dict(cfg.prompt_hints),
    })


@app.route("/api/character/fields", methods=["POST"])
def api_character_fields_set():
    """Update specific character fields and write back to soul.md."""
    import character
    body = request.get_json(silent=True) or {}
    if not body:
        return jsonify({"error": "empty body"}), 400

    # Start from current config values
    cfg = get_config()
    name = body.get("name", cfg.name)
    user_address = body.get("user_address", cfg.user_address)
    personality = body.get("personality", cfg.personality)
    speech_patterns = body.get("speech_patterns", cfg.speech_patterns)
    appearance = body.get("appearance", cfg.appearance)
    backstory = body.get("backstory", cfg.backstory)
    interests = body.get("interests", cfg.interests)

    # Merge prompt_hints: keep existing, override with provided
    hints = dict(cfg.prompt_hints)
    if "prompt_hints" in body and isinstance(body["prompt_hints"], dict):
        hints.update(body["prompt_hints"])

    # Reconstruct soul.md content
    lines = []
    lines.append("# Identity")
    lines.append("")
    lines.append(f"- **Name**: {name}")
    lines.append(f"- **User Address**: {user_address}")
    lines.append("")
    lines.append("# Personality")
    lines.append("")
    lines.append(personality)
    lines.append("")
    lines.append("# Speech Patterns")
    lines.append("")
    lines.append(speech_patterns)
    lines.append("")
    lines.append("# Appearance")
    lines.append("")
    lines.append(appearance)
    lines.append("")
    lines.append("# Backstory")
    lines.append("")
    lines.append(backstory)
    lines.append("")
    lines.append("# Interests")
    lines.append("")
    lines.append(interests)
    lines.append("")
    lines.append("# Prompt Hints")
    lines.append("")
    for k, v in hints.items():
        lines.append(f"- {k}: {v}")
    lines.append("")

    content = "\n".join(lines)

    # Write to the active soul.md path
    soul_path = character._resolve_soul_path()
    os.makedirs(os.path.dirname(soul_path), exist_ok=True)
    with open(soul_path, "w", encoding="utf-8") as f:
        f.write(content)

    # Reload config so all modules pick up changes
    reload_config()
    return jsonify({"ok": True})


@app.route("/api/models/list", methods=["GET"])
def api_models_list_detailed():
    """Return all models with character metadata and active status."""
    import model_library
    lib = model_library._load_library()
    active_id = lib.get("active_model_id")
    result = []
    for m in lib.get("models", []):
        mid = m.get("id", "")
        # Try to read character name from persona soul.md
        char_name = m.get("name", "")
        soul_path = model_library.get_persona_soul_path(mid)
        if os.path.exists(soul_path):
            try:
                from character import CharacterConfig
                tmp_cfg = CharacterConfig()
                with open(soul_path, "r", encoding="utf-8") as f:
                    tmp_cfg._parse(f.read())
                char_name = tmp_cfg.name
            except Exception:
                pass
        # Build avatar URL
        avatar_path = f"/api/models/{mid}/avatar"
        result.append({
            "id": mid,
            "name": char_name,
            "model_name": m.get("name", ""),
            "avatar_path": avatar_path,
            "is_active": mid == active_id,
        })
    return jsonify(result)


@app.route("/api/relationship")
def api_relationship():
    """Return relationship metadata (days together, first meet date)."""
    meta = storage.ensure_first_meet_date()
    first_meet = meta.get("first_meet_date", "")
    days = 0
    if first_meet:
        from datetime import datetime as _dt
        try:
            days = (_dt.now() - _dt.strptime(first_meet, "%Y-%m-%d")).days
        except ValueError:
            pass
    cfg = get_config()
    return jsonify({
        "first_meet_date": first_meet,
        "days_together": days,
        "character_name": cfg.name,
    })


@app.route("/api/companion/bootstrap", methods=["GET"])
def api_companion_bootstrap():
    """Aggregate character runtime data for richer companion frontends."""
    chat_limit = companion.coerce_limit(request.args.get("chat_limit", 20), default=20, maximum=100)
    return jsonify(companion.build_bootstrap(chat_limit=chat_limit))


@app.route("/api/companion/manifest", methods=["GET"])
def api_companion_manifest():
    return jsonify(companion.build_manifest(version=_get_version()))


@app.route("/api/companion/runtime", methods=["GET"])
def api_companion_runtime():
    chat_limit = companion.coerce_limit(request.args.get("chat_limit", 20), default=20, maximum=100)
    plan_limit = companion.coerce_limit(request.args.get("plan_limit", 7), default=7, maximum=30)
    return jsonify(companion.build_runtime_snapshot(chat_limit=chat_limit, plan_limit=plan_limit, version=_get_version()))


@app.route("/api/companion/character", methods=["GET"])
def api_companion_character():
    return jsonify(companion.build_character_payload())


@app.route("/api/companion/schedule", methods=["GET"])
def api_companion_schedule():
    items = companion.build_schedule_payload()
    return jsonify({"items": items, "count": len(items)})


@app.route("/api/companion/commitments", methods=["GET"])
def api_companion_commitments():
    items = companion.build_commitments_payload()
    return jsonify({"items": items, "count": len(items)})


# ===== Commitments CRUD (DDL Tracker) =====

@app.route("/api/commitments", methods=["GET"])
def api_commitments_list():
    include_done = request.args.get("include_done", "true").lower() == "true"
    items = core.parse_commitments(include_done=include_done)
    return _etag_json({"items": items, "count": len(items)})


@app.route("/api/commitments", methods=["POST"])
def api_commitments_create():
    body = request.get_json(silent=True) or {}
    title = body.get("title", "").strip()
    if not title:
        return jsonify({"error": "missing title"}), 400
    deadline = body.get("deadline", "").strip()
    detail = body.get("detail", "").strip()
    result = core.add_commitment_manual(title, deadline, detail)
    return jsonify(result)


@app.route("/api/commitments/<cid>", methods=["PUT"])
def api_commitments_update(cid):
    body = request.get_json(silent=True) or {}
    action = body.get("action", "edit")
    if action == "complete":
        result = core.complete_commitment_by_id(cid)
    else:
        result = core.edit_commitment_by_id(
            cid,
            title=body.get("title"),
            deadline=body.get("deadline"),
        )
    if result["status"] == "not_found":
        return jsonify(result), 404
    return jsonify(result)


@app.route("/api/commitments/<cid>", methods=["DELETE"])
def api_commitments_delete(cid):
    result = core.delete_commitment_by_id(cid)
    if result["status"] == "not_found":
        return jsonify(result), 404
    return jsonify(result)


@app.route("/api/commitments/stats", methods=["GET"])
def api_commitments_stats():
    return jsonify(core.get_commitment_stats())


@app.route("/api/companion/chat", methods=["POST"])
def api_companion_chat():
    body = request.get_json(silent=True) or {}
    text = body.get("text", "").strip()
    if not text:
        return jsonify({"error": "请输入消息"}), 400

    history_limit = companion.coerce_limit(body.get("history_limit", 20), default=20, maximum=100)
    return jsonify(companion.send_chat(text, history_limit=history_limit))


@app.route("/api/airi/v1/models", methods=["GET"])
def api_airi_models():
    return jsonify(companion.list_openai_models())


@app.route("/api/airi/v1/chat/completions", methods=["POST"])
def api_airi_chat_completions():
    body = request.get_json(silent=True) or {}
    messages = body.get("messages", [])
    model = body.get("model", "contextlife-companion")
    stream = bool(body.get("stream", False))
    history_limit = companion.coerce_limit(body.get("history_limit", 20), default=20, maximum=100)

    try:
        completion = companion.build_openai_chat_completion(messages, model=model, history_limit=history_limit)
    except ValueError as err:
        return jsonify({"error": {"message": str(err), "type": "invalid_request_error"}}), 400

    if not stream:
        return jsonify(completion)

    def generate():
        for chunk in companion.stream_openai_chat_completion_payload(completion):
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return Response(generate(), mimetype="text/event-stream")


@app.route("/api/companion/chat/history", methods=["GET"])
def api_companion_chat_history():
    limit = companion.coerce_limit(request.args.get("limit", 50), default=50, maximum=200)
    items = companion.build_chat_history(limit=limit)
    return jsonify({"items": items, "count": len(items)})


@app.route("/api/companion/reminders", methods=["GET"])
def api_companion_reminders():
    date_str = (request.args.get("date") or "").strip() or None
    items = companion.build_today_reminders(date_str=date_str)
    return jsonify({"items": items, "count": len(items)})


# ===== Character Avatar Route =====

@app.route("/assets/character-avatar.png")
@app.route("/assets/character-avatar.jpeg")
def serve_character_avatar():
    import model_library
    avatar_path = model_library.get_active_avatar_path()
    return send_from_directory(os.path.dirname(avatar_path), os.path.basename(avatar_path))




# ===== PWA Routes =====

def _bundled_resource_root() -> str:
    return getattr(sys, "_MEIPASS", os.path.dirname(__file__))


@app.route("/assets/brand/<path:filename>")
def serve_brand_asset(filename):
    return send_from_directory(
        os.path.join(_bundled_resource_root(), "assets", "brand"),
        filename,
    )


@app.route("/assets/js/android-lifecycle-shim.js")
def serve_android_lifecycle_shim():
    return send_from_directory(
        os.path.join(_bundled_resource_root(), "assets", "js"),
        "android-lifecycle-shim.js",
        mimetype="application/javascript",
    )


@app.route("/favicon.ico")
def serve_favicon():
    return send_from_directory(
        _bundled_resource_root(),
        "icon-192.png",
        mimetype="image/png",
    )

@app.route("/manifest.json")
def serve_manifest():
    return send_from_directory(_bundled_resource_root(), "manifest.json")


@app.route("/service-worker.js")
def serve_sw():
    sw_path = os.path.join(os.path.dirname(__file__), "service-worker.js")
    with open(sw_path, encoding="utf-8") as f:
        content = f.read().replace("__BUILD_VERSION__", _get_version())
    resp = app.make_response(content)
    resp.headers["Content-Type"] = "application/javascript"
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


@app.route("/api/debug/client_log", methods=["POST"])
def api_debug_client_log():
    """Frontend-to-backend debug logger. Appends to /tmp/miru_client.log so
    we can diagnose issues like 'window reloads unexpectedly' without needing
    DevTools access in the Tauri WebView. Safe no-op if disk write fails."""
    body = request.get_json(silent=True) or {}
    tag = str(body.get("tag", ""))[:40]
    msg = str(body.get("msg", ""))[:2000]
    try:
        import time as _time
        ts = _time.strftime("%H:%M:%S")
        with open("/tmp/miru_client.log", "a", encoding="utf-8") as f:
            f.write(f"[{ts}] [{tag}] {msg}\n")
    except Exception:
        pass
    return jsonify({"ok": True})


@app.route("/api/admin/force_client_refresh", methods=["POST"])
def api_admin_force_client_refresh():
    """Broadcast a `force_refresh` SSE event to a user so their open clients
    clear SW caches and hard-reload. Admin-only (auth middleware enforces).
    Body: {"user_id": "..."} (optional; defaults to current g.user_id)
    """
    body = request.get_json(silent=True) or {}
    target_uid = body.get("user_id") or getattr(g, "user_id", None)
    if not target_uid:
        return jsonify({"error": "no user_id"}), 400
    import sse as _sse
    _sse.broadcast("force_refresh", {"reason": "admin_trigger"}, user_id=target_uid)
    return jsonify({"ok": True, "user_id": target_uid})


@app.route("/icon-192.png")
def serve_icon_192():
    return send_from_directory(_bundled_resource_root(), "icon-192.png")


@app.route("/icon-512.png")
def serve_icon_512():
    return send_from_directory(_bundled_resource_root(), "icon-512.png")


# ===== Memory API (filesystem-based) =====

@app.route("/api/memory/tree", methods=["GET"])
def api_memory_tree():
    """Return memory directory tree with rich metadata."""
    import memory
    rich = request.args.get("rich", "1")
    if rich == "1":
        return _etag_json({"tree": memory.list_tree_rich()})
    return _etag_json({"tree": memory.list_tree()})


@app.route("/api/memory/index", methods=["GET"])
def api_memory_index():
    """Return memory index.md content."""
    import memory
    return _etag_json({"content": memory.read_index()})


@app.route("/api/memory/read", methods=["GET"])
def api_memory_read():
    """Read a specific memory file by path."""
    import memory
    path = request.args.get("path", "").strip()
    if not path:
        return jsonify({"error": "path required"}), 400
    content = memory.read_file(path)
    if content is None:
        return jsonify({"error": "not found"}), 404
    return _etag_json({"path": path, "content": content})


@app.route("/api/memory/write", methods=["POST"])
def api_memory_write():
    """Write to a memory file."""
    import memory
    body = request.get_json(silent=True) or {}
    path = body.get("path", "").strip()
    content = body.get("content", "")
    mode = body.get("mode", "write")
    if not path or not content:
        return jsonify({"error": "path and content required"}), 400
    if mode == "append":
        ok = memory.append_to_file(path, content)
    else:
        ok = memory.write_file(path, content)
    if not ok:
        return jsonify({"error": "write failed"}), 400
    return jsonify({"status": "ok", "path": path})


@app.route("/api/memory/search", methods=["GET"])
def api_memory_search():
    """Search memory files by keyword.

    Memory v2 enhancement: also includes slot metadata matches (title /
    summary / aliases) so slot-aware search just works.
    """
    import memory
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"error": "q required"}), 400
    max_results = int(request.args.get("max", 10))
    results = memory.search(query, max_results=max_results)

    # Slot metadata matches
    try:
        import memory_router
        ql = query.lower()
        slot_matches = []
        for domain in memory_router.DOMAINS:
            for s in memory_router.load_all_slots(domain):
                if s.get("status") == "archived":
                    # archived slot 仍可搜, 但标记
                    pass
                fields = [
                    s.get("title", "").lower(),
                    s.get("summary", "").lower(),
                    " ".join(s.get("aliases", [])).lower(),
                    s.get("id", "").lower(),
                ]
                hit = any(ql in f for f in fields)
                if hit:
                    slot_matches.append({
                        "kind": "slot",
                        "domain": domain,
                        "slot_id": s.get("id"),
                        "title": s.get("title"),
                        "summary": s.get("summary", ""),
                        "icon": s.get("icon", "📄"),
                        "status": s.get("status", "active"),
                        "main_file": s.get("main_file"),
                    })
        # Tag content results as kind="content"
        for r in results:
            r["kind"] = "content"
        # Combine: slot matches first
        combined = slot_matches + results
        return jsonify({"results": combined,
                        "total": len(combined),
                        "slot_matches": len(slot_matches),
                        "content_matches": len(results)})
    except Exception:
        return jsonify({"results": results, "total": len(results)})


# ===== Memory v2 — Slot endpoints =====

@app.route("/api/memory/slots", methods=["GET"])
def api_memory_slots():
    """Return all slots grouped by domain + status.

    Format: {project: {active: [...], paused: [...], archived: [...]}, ...}
    """
    import memory
    return _etag_json({"slots": memory.list_slots_grouped()})


@app.route("/api/memory/slot/<domain>/<slot_id>", methods=["GET"])
def api_memory_slot_get(domain, slot_id):
    """Return single slot full detail (metadata + main.md + related DDLs)."""
    import memory_router
    if domain not in memory_router.DOMAINS:
        return jsonify({"error": "invalid domain"}), 400
    slot = memory_router.get_slot(domain, slot_id)
    if slot is None:
        return jsonify({"error": "slot not found"}), 404

    import memory
    main_md = memory.read_file(slot.get("main_file") or
                                memory_router._slot_main_file_rel(domain, slot_id)) or ""

    # Related DDLs from commitments/active.md (best-effort match by slot_id or title)
    related_ddls = []
    try:
        commit_md = memory.read_file("commitments/active.md") or ""
        # crude match: any line containing slot_id or slot title
        title = slot.get("title", "")
        for line in commit_md.split("\n"):
            ln = line.strip()
            if not ln.startswith("- "):
                continue
            if slot_id in ln or (title and title in ln):
                related_ddls.append(ln)
    except Exception:
        pass

    return jsonify({
        "slot": slot,
        "main_md": main_md,
        "related_ddls": related_ddls,
    })


@app.route("/api/memory/slot/<domain>/<slot_id>", methods=["PATCH"])
def api_memory_slot_patch(domain, slot_id):
    """Update slot metadata (title/icon/status/pinned/summary/aliases)."""
    import memory_router
    if domain not in memory_router.DOMAINS:
        return jsonify({"error": "invalid domain"}), 400
    body = request.get_json(silent=True) or {}
    updated = memory_router.update_slot_metadata(domain, slot_id, body)
    if updated is None:
        return jsonify({"error": "slot not found or update failed"}), 404
    return jsonify({"ok": True, "slot": updated})


@app.route("/api/memory/slot/<domain>/<slot_id>", methods=["DELETE"])
def api_memory_slot_delete(domain, slot_id):
    """Hard-delete a slot (record + main.md file)."""
    import memory_router
    if domain not in memory_router.DOMAINS:
        return jsonify({"error": "invalid domain"}), 400
    ok = memory_router.hard_delete_slot(domain, slot_id)
    if not ok:
        return jsonify({"error": "slot not found"}), 404
    return jsonify({"ok": True})


@app.route("/api/memory/slot/<domain>/<slot_id>/archive", methods=["POST"])
def api_memory_slot_archive(domain, slot_id):
    """Archive a slot (sets status='archived'). Reversible via PATCH."""
    import memory_router
    if domain not in memory_router.DOMAINS:
        return jsonify({"error": "invalid domain"}), 400
    updated = memory_router.archive_slot(domain, slot_id)
    if updated is None:
        return jsonify({"error": "slot not found"}), 404
    return jsonify({"ok": True, "slot": updated})


@app.route("/api/memory/slot/<domain>/<slot_id>/pin", methods=["POST"])
def api_memory_slot_pin(domain, slot_id):
    """Pin/unpin a slot. Body: {pinned: bool}."""
    import memory_router
    if domain not in memory_router.DOMAINS:
        return jsonify({"error": "invalid domain"}), 400
    body = request.get_json(silent=True) or {}
    pinned = bool(body.get("pinned", True))
    updated = memory_router.pin_slot(domain, slot_id, pinned)
    if updated is None:
        return jsonify({"error": "slot not found"}), 404
    return jsonify({"ok": True, "slot": updated})


@app.route("/api/memory/slot/<domain>/merge", methods=["POST"])
def api_memory_slot_merge(domain):
    """Merge source slot into target slot (LLM-integrates content).

    Body: {source_id: str, target_id: str}
    """
    import memory_router
    if domain not in memory_router.DOMAINS:
        return jsonify({"error": "invalid domain"}), 400
    body = request.get_json(silent=True) or {}
    source_id = (body.get("source_id") or "").strip()
    target_id = (body.get("target_id") or "").strip()
    if not source_id or not target_id:
        return jsonify({"error": "source_id and target_id required"}), 400
    if source_id == target_id:
        return jsonify({"error": "source and target must differ"}), 400
    merged = memory_router.merge_slots(domain, source_id, target_id)
    if merged is None:
        return jsonify({"error": "merge failed (LLM error or slot missing)"}), 500
    return jsonify({"ok": True, "slot": merged})


@app.route("/api/memory/slots/audit", methods=["POST"])
def api_memory_slot_audit():
    """Manually trigger daily slot audit (active→paused→archived transitions).

    Normally runs as a daily cron, but admin/dev can trigger on demand.
    """
    import memory_router
    summary = memory_router.daily_slot_audit()
    return jsonify({"ok": True, "summary": summary})


# ===== AI Config Endpoints =====

@app.route("/api/ai/config", methods=["GET"])
def api_ai_get_config():
    return jsonify(ai_config.get_public_config())


@app.route("/api/ai/config", methods=["POST"])
def api_ai_set_config():
    # 2026-05-09 [P0 fix]: was world-writable. ai_config.json is global state
    # (host/api_key/model used by every user); a malicious user could repoint
    # the chat tier host to evil.attacker.com and proxy all platform traffic.
    guard = _require_admin()
    if guard: return guard
    body = request.get_json(silent=True) or {}
    result = ai_config.update_public_config(body)
    return jsonify(result), 200 if result.get("ok") else 400


@app.route("/api/ai/ping", methods=["POST"])
def api_ai_ping():
    # admin-only sibling of /api/ai/config — they share the same global state.
    guard = _require_admin()
    if guard: return guard
    result = ai_config.ping_provider()
    return jsonify(result), 200 if result.get("ok") else 400


# ===== Owner AI Config Endpoints =====

@app.route("/api/owner/ai-config", methods=["GET"])
def api_owner_ai_config_get():
    guard = _require_instance_owner()
    if not guard.get("ok"):
        return jsonify({"error": guard.get("error", "owner required")}), guard.get("status_code", 403)

    import owner_config
    return jsonify({
        "ok": True,
        "owner": owner_config.get_public_owner_state(),
        "config": ai_config.get_public_config(),
    })


@app.route("/api/owner/ai-config/<tier>", methods=["PATCH"])
def api_owner_ai_config_update(tier):
    guard = _require_instance_owner()
    if not guard.get("ok"):
        return jsonify({"error": guard.get("error", "owner required")}), guard.get("status_code", 403)

    body = request.get_json(silent=True) or {}
    result = ai_config.update_tier_config(tier, body)
    if not result.get("ok"):
        return jsonify(result), 400
    return jsonify(result)


@app.route("/api/owner/ai-config/<tier>/test", methods=["POST"])
def api_owner_ai_config_test(tier):
    guard = _require_instance_owner()
    if not guard.get("ok"):
        return jsonify({"error": guard.get("error", "owner required")}), guard.get("status_code", 403)

    result = ai_config.ping_tier(tier, timeout_seconds=20)
    return jsonify(result), 200 if result.get("ok") else 502


# ===== Self Profile Endpoints =====

@app.route("/api/self-profile", methods=["GET"])
def api_self_profile_get():
    return jsonify(self_profile.get_profile())


@app.route("/api/self-profile", methods=["POST"])
def api_self_profile_set():
    body = request.get_json(silent=True) or {}
    result = self_profile.update_profile(body)
    return jsonify(result), 200 if result.get("ok") else 400


# ===== App Settings Endpoints =====
#
# Two layers:
#   /api/admin/server-config  — global (port, VAPID, default_timezone), admin only
#   /api/user-settings        — per-user (timezone, pet UI, screenshot interval)
#
# Legacy /api/settings/app kept as a compatibility alias that merges both layers
# and routes writes by field. Scheduled for removal once all frontends migrate.


def _mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return value[:4] + "***" + value[-4:]


def _public_server_config() -> dict:
    cfg = server_config.get()
    return {
        "ok": True,
        "port": cfg["port"],
        "flask_debug": cfg["flask_debug"],
        "vapid_public_key": cfg["vapid_public_key"],
        "has_vapid_private_key": bool(cfg["vapid_private_key"]),
        "vapid_private_key_masked": _mask_secret(cfg["vapid_private_key"]),
        "default_timezone": cfg["default_timezone"],
    }


@app.route("/api/admin/server-config", methods=["GET"])
def api_server_config_get():
    return jsonify(_public_server_config())


@app.route("/api/admin/server-config", methods=["POST"])
def api_server_config_set():
    body = request.get_json(silent=True) or {}
    result = server_config.update(body)
    if result.get("ok"):
        return jsonify({"ok": True, "config": _public_server_config()})
    return jsonify(result), 400


@app.route("/api/user-settings", methods=["GET"])
def api_user_settings_get():
    return jsonify({"ok": True, "settings": user_settings.get()})


@app.route("/api/user-settings", methods=["POST"])
def api_user_settings_set():
    body = request.get_json(silent=True) or {}
    result = user_settings.update(body)
    if result.get("ok") and "auto_screenshot_interval" in body:
        # Update BOTH the analyzer VLM throttle AND the local capture sensor
        # so interval changes take effect immediately end-to-end.
        new_interval = user_settings.screenshot_interval()
        try:
            from screen_analyzer import get_analyzer
            get_analyzer().set_vlm_interval(new_interval)
        except Exception:
            pass
        try:
            from sensor import _instance as _sensor_inst
            if _sensor_inst is not None:
                _sensor_inst.set_interval(new_interval)
        except Exception:
            pass
    if result.get("ok") and "screenshot_enabled" in body:
        # Sync local ScreenSensor on/off flag
        try:
            from sensor import _instance as _sensor_inst
            if _sensor_inst is not None:
                _sensor_inst.set_enabled(bool(body["screenshot_enabled"]))
        except Exception:
            pass
    # Broadcast settings change so other UIs (pet toolbar + main chat header +
    # settings panel) can refresh their toggle state without manual reload.
    if result.get("ok"):
        try:
            import sse
            uid = getattr(g, "user_id", None)
            sse.broadcast("user_settings_sync",
                          {"settings": user_settings.get()},
                          user_id=uid)
        except Exception:
            pass
    return jsonify(result), 200 if result.get("ok") else 400


_LEGACY_SERVER_FIELDS = {"port", "flask_debug",
                         "vapid_public_key", "vapid_private_key",
                         "clear_vapid_public_key", "clear_vapid_private_key",
                         "default_timezone"}


@app.route("/api/settings/app", methods=["GET"])
def api_app_get_settings():
    """DEPRECATED: use /api/admin/server-config + /api/user-settings.

    Returns a merged view for backwards compatibility with old clients.
    """
    merged = _public_server_config()
    merged.update(user_settings.get())
    merged["config_file"] = user_settings._config_path() or ""
    return jsonify(merged)


@app.route("/api/settings/app", methods=["POST"])
def api_app_set_settings():
    """DEPRECATED: routes writes to server_config or user_settings by field."""
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"ok": False, "error": "invalid payload"}), 400

    server_payload = {k: body[k] for k in body if k in _LEGACY_SERVER_FIELDS}
    user_payload = {k: body[k] for k in body if k not in _LEGACY_SERVER_FIELDS}

    if server_payload:
        r = server_config.update(server_payload)
        if not r.get("ok"):
            return jsonify(r), 400
    if user_payload:
        r = user_settings.update(user_payload)
        if not r.get("ok"):
            return jsonify(r), 400

    if "auto_screenshot_interval" in user_payload:
        new_interval = user_settings.screenshot_interval()
        try:
            from screen_analyzer import get_analyzer
            get_analyzer().set_vlm_interval(new_interval)
        except Exception:
            pass
        try:
            from sensor import _instance as _sensor_inst
            if _sensor_inst is not None:
                _sensor_inst.set_interval(new_interval)
        except Exception:
            pass
    if "screenshot_enabled" in user_payload:
        try:
            from sensor import _instance as _sensor_inst
            if _sensor_inst is not None:
                _sensor_inst.set_enabled(bool(user_payload["screenshot_enabled"]))
        except Exception:
            pass

    merged = _public_server_config()
    merged.update(user_settings.get())
    return jsonify({"ok": True, "settings": merged})


# ===== Soul.md Settings Endpoints =====

@app.route("/api/settings/soul", methods=["GET"])
def api_soul_get():
    """Return raw soul.md content for the active model's persona."""
    import model_library
    soul_path = model_library.get_active_soul_path()
    try:
        with open(soul_path, "r", encoding="utf-8") as f:
            content = f.read()
    except FileNotFoundError:
        content = ""
    return jsonify({"content": content})


@app.route("/api/settings/soul", methods=["POST"])
def api_soul_set():
    """Write soul.md content for the active model's persona and reload config."""
    import model_library
    body = request.get_json(silent=True) or {}
    content = body.get("content")
    if content is None:
        return jsonify({"error": "missing content"}), 400
    active = model_library.get_active_model()
    if active:
        model_library.save_persona_soul(active["id"], content)
    else:
        with open(SOUL_PATH, "w", encoding="utf-8") as f:
            f.write(content)
    cfg = reload_config()
    return jsonify({"ok": True, "name": cfg.name, "user_address": cfg.user_address})


# ===== Model Library Endpoints =====

@app.route("/api/models", methods=["GET"])
def api_models_list():
    import model_library
    models = model_library.list_models()
    result = []
    for m in models:
        d = dict(m)
        d["is_default_avatar"] = model_library.is_default_avatar(m.get("id", ""))
        result.append(d)
    return jsonify(result)


@app.route("/api/models/active", methods=["GET"])
def api_models_active_get():
    import model_library
    model = model_library.get_active_model()
    if not model:
        return jsonify({"active": None})
    return jsonify(model)


@app.route("/api/models/active", methods=["POST"])
def api_models_active_set():
    # 2026-05-09 [P0 fix]: model write ops are admin-only. Models are global
    # shared assets (one Live2D bundle for all users); the UI tab is hidden
    # (display:none) but the API was world-writable. See also Bug #6 audit.
    guard = _require_admin()
    if guard: return guard
    import model_library
    body = request.get_json(silent=True) or {}
    model_id = body.get("model_id", "")
    if not model_id:
        return jsonify({"error": "missing model_id"}), 400
    try:
        model = model_library.set_active_model(model_id)
        reload_config()  # Switch persona along with model
        try:
            import sse
            # Admin global event — affects every user's chat persona
            sse.broadcast_all("model_changed", {"model_id": model_id})
        except Exception:
            pass
        return jsonify({"ok": True, "model": model})
    except ValueError as e:
        return jsonify({"error": str(e)}), 404


@app.route("/api/models/download", methods=["POST"])
def api_models_download():
    guard = _require_admin()
    if guard: return guard
    import model_library
    body = request.get_json(silent=True) or {}
    url = body.get("url", "").strip()
    name = body.get("name", "").strip()
    if not url or not name:
        return jsonify({"error": "missing url or name"}), 400
    try:
        model = model_library.download_model(url, name)
        return jsonify({"ok": True, "model": model})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/models/<model_id>", methods=["PATCH"])
def api_models_rename(model_id):
    guard = _require_admin()
    if guard: return guard
    import model_library
    body = request.get_json(silent=True) or {}
    new_name = body.get("name", "").strip()
    if not new_name:
        return jsonify({"error": "missing name"}), 400
    try:
        model = model_library.rename_model(model_id, new_name)
        return jsonify({"ok": True, "model": model})
    except ValueError as e:
        return jsonify({"error": str(e)}), 404


@app.route("/api/models/<model_id>", methods=["DELETE"])
def api_models_delete(model_id):
    guard = _require_admin()
    if guard: return guard
    import model_library
    ok = model_library.remove_model(model_id)
    if not ok:
        return jsonify({"error": "model not found"}), 404
    return jsonify({"ok": True})


@app.route("/api/models/upload", methods=["POST"])
def api_models_upload():
    guard = _require_admin()
    if guard: return guard
    import model_library
    if "file" not in request.files:
        return jsonify({"error": "no file uploaded"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "empty filename"}), 400
    # Display name preserves original filename (including Chinese); disk name is sanitized
    name = os.path.splitext(f.filename)[0]
    safe_filename = secure_filename(f.filename)
    if not safe_filename.endswith(".zip"):
        safe_filename += ".zip"
    models_dir = os.path.join(storage.DATA_DIR, "models")
    os.makedirs(models_dir, exist_ok=True)
    dest_path = os.path.join(models_dir, safe_filename)
    f.save(dest_path)
    relative_path = os.path.join("models", safe_filename)
    model = model_library.add_model(name=name, local_path=relative_path)
    return jsonify({"ok": True, "model": model})


@app.route("/api/models/<model_id>/layout", methods=["GET"])
def api_model_layout_get(model_id):
    import model_library
    layout = model_library.get_model_layout(model_id)
    return jsonify({"model_id": model_id, "layout": layout})


_layout_version = {"v": 0, "model_id": None, "ts": 0}

@app.route("/api/models/<model_id>/layout", methods=["POST"])
def api_model_layout_set(model_id):
    guard = _require_admin()
    if guard: return guard
    import model_library, time
    body = request.get_json(silent=True) or {}
    layout = body.get("layout", body)
    try:
        model = model_library.save_model_layout(model_id, layout)
        _layout_version["v"] += 1
        _layout_version["model_id"] = model_id
        _layout_version["ts"] = time.time()
        return jsonify({"ok": True, "model": model})
    except ValueError as e:
        return jsonify({"error": str(e)}), 404


@app.route("/api/models/layout-version", methods=["GET"])
def api_layout_version():
    return jsonify(_layout_version)


@app.route("/data/models/<path:filename>")
def serve_model_file(filename):
    models_dir = os.path.join(storage.get_data_dir(), "models")
    return send_from_directory(models_dir, filename)



@app.route("/assets/js/live2d/<path:filename>")
def serve_live2d_js(filename):
    """Serve Live2D SDK JS files for the settings page model previews."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(__file__))
    js_dir = os.path.join(base, "assets", "js", "live2d")
    return send_from_directory(js_dir, filename)


@app.route("/assets/live2d/<path:filename>")
def serve_live2d_model(filename):
    """Serve the bundled Live2D model (Hiyori) packaged into the app.
    Resolves from PyInstaller's _MEIPASS when frozen, source tree otherwise.
    Same canonical asset directory is mounted into the Android APK via
    Gradle sourceSets — single source of truth for both clients."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(__file__))
    live2d_dir = os.path.join(base, "assets", "live2d")
    return send_from_directory(live2d_dir, filename)


# ===== Per-Model Persona Endpoints =====

@app.route("/api/models/<model_id>/soul", methods=["GET"])
def api_model_soul_get(model_id):
    """Return raw soul.md content for a specific model."""
    import model_library
    content = model_library.read_persona_soul(model_id)
    return jsonify({"content": content, "model_id": model_id})


@app.route("/api/models/<model_id>/soul", methods=["POST"])
def api_model_soul_set(model_id):
    """Write soul.md content for a specific model."""
    guard = _require_admin()
    if guard: return guard
    import model_library
    body = request.get_json(silent=True) or {}
    content = body.get("content")
    if content is None:
        return jsonify({"error": "missing content"}), 400
    model_library.save_persona_soul(model_id, content)
    # If this is the active model, reload config
    active = model_library.get_active_model()
    if active and active.get("id") == model_id:
        reload_config()
    return jsonify({"ok": True, "model_id": model_id})


@app.route("/api/models/<model_id>/fields", methods=["GET"])
def api_model_fields_get(model_id):
    """Return parsed soul.md fields for a specific model."""
    import model_library
    soul_path = model_library.get_persona_soul_path(model_id)
    if not os.path.exists(soul_path):
        return jsonify({"error": "soul.md not found"}), 404
    from character import CharacterConfig
    cfg = CharacterConfig()
    with open(soul_path, "r", encoding="utf-8") as f:
        cfg._parse(f.read())
    # Also include layout info
    layout = model_library.get_model_layout(model_id)
    return jsonify({
        "name": cfg.name,
        "user_address": cfg.user_address,
        "personality": cfg.personality,
        "speech_patterns": cfg.speech_patterns,
        "appearance": cfg.appearance,
        "backstory": cfg.backstory,
        "interests": cfg.interests,
        "prompt_hints": dict(cfg.prompt_hints),
        "layout": layout,
    })


@app.route("/api/models/<model_id>/fields", methods=["POST"])
def api_model_fields_set(model_id):
    """Update soul.md fields for a specific model."""
    guard = _require_admin()
    if guard: return guard
    import model_library
    body = request.get_json(silent=True) or {}
    if not body:
        return jsonify({"error": "empty body"}), 400

    # Read current values as fallback
    soul_path = model_library.get_persona_soul_path(model_id)
    from character import CharacterConfig
    cfg = CharacterConfig()
    if os.path.exists(soul_path):
        with open(soul_path, "r", encoding="utf-8") as f:
            cfg._parse(f.read())

    name = body.get("name", cfg.name)
    user_address = body.get("user_address", cfg.user_address)
    personality = body.get("personality", cfg.personality)
    speech_patterns = body.get("speech_patterns", cfg.speech_patterns)
    appearance = body.get("appearance", cfg.appearance)
    backstory = body.get("backstory", cfg.backstory)
    interests = body.get("interests", cfg.interests)

    hints = dict(cfg.prompt_hints)
    if "prompt_hints" in body and isinstance(body["prompt_hints"], dict):
        hints.update(body["prompt_hints"])

    lines = [
        "# Identity", "",
        f"- **Name**: {name}",
        f"- **User Address**: {user_address}", "",
        "# Personality", "", personality, "",
        "# Speech Patterns", "", speech_patterns, "",
        "# Appearance", "", appearance, "",
        "# Backstory", "", backstory, "",
        "# Interests", "", interests, "",
        "# Prompt Hints", "",
    ]
    for k, v in hints.items():
        lines.append(f"- {k}: {v}")
    lines.append("")

    content = "\n".join(lines)
    model_library.save_persona_soul(model_id, content)

    active = model_library.get_active_model()
    if active and active.get("id") == model_id:
        reload_config()
    return jsonify({"ok": True})


@app.route("/api/models/<model_id>/avatar", methods=["POST"])
def api_model_avatar_upload(model_id):
    """Upload an avatar image for a specific model."""
    guard = _require_admin()
    if guard: return guard
    import model_library
    if "file" not in request.files:
        return jsonify({"error": "no file uploaded"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "empty filename"}), 400
    ext = os.path.splitext(f.filename)[1].lstrip(".").lower()
    if ext not in ("jpeg", "jpg", "png", "webp"):
        return jsonify({"error": "unsupported image format"}), 400
    if ext == "jpg":
        ext = "jpeg"
    data = f.read()
    model_library.save_persona_avatar(model_id, data, ext)
    return jsonify({"ok": True, "model_id": model_id})


@app.route("/api/models/<model_id>/auto-avatar", methods=["POST"])
def api_model_auto_avatar(model_id):
    """Accept a rendered Live2D screenshot as avatar."""
    guard = _require_admin()
    if guard: return guard
    import model_library
    if "file" not in request.files:
        return jsonify({"error": "no file uploaded"}), 400
    f = request.files["file"]
    data = f.read()
    if not data:
        return jsonify({"error": "empty file"}), 400
    model_library.save_persona_avatar(model_id, data, "png")
    return jsonify({"ok": True, "model_id": model_id})


@app.route("/api/models/<model_id>/avatar", methods=["GET"])
def api_model_avatar_get(model_id):
    """Serve the avatar image for a specific model."""
    import model_library
    avatar_path = model_library.get_persona_avatar_path(model_id)
    return send_from_directory(os.path.dirname(avatar_path), os.path.basename(avatar_path))


@app.route("/api/system/clear-all-records", methods=["POST"])
def api_system_clear_all_records():
    body = request.get_json(silent=True) or {}
    result = core.clear_all_records(body.get("confirm_text", ""))
    return jsonify(result), 200 if result.get("ok") else 400


# ===== Pet ↔ Main-UI visibility coordination =====

import time as _time_mod

_ui_heartbeat_ts = {}  # {user_id: epoch seconds} per-user heartbeat


@app.route("/api/pet/ui-active", methods=["GET"])
def api_pet_ui_active_get():
    """Return whether the main UI is currently active (heartbeat within last 3s)."""
    uid = getattr(g, "user_id", "_admin")
    ts = _ui_heartbeat_ts.get(uid, 0.0)
    active = (_time_mod.time() - ts) < 3
    return jsonify({"active": active, "ts": ts})


@app.route("/api/pet/ui-active", methods=["POST"])
def api_pet_ui_active_post():
    """Main UI sends this heartbeat every ~3s while the page is open."""
    uid = getattr(g, "user_id", "_admin")
    data = request.get_data()
    if data == b"reset":
        _ui_heartbeat_ts[uid] = 0.0
    else:
        _ui_heartbeat_ts[uid] = _time_mod.time()
    return jsonify({"ok": True})


@app.route("/api/shutdown", methods=["POST"])
def api_shutdown():
    """Gracefully shut down the entire ContextLife server and all child processes."""
    import signal
    import threading

    def _do_shutdown():
        import time as _t
        _t.sleep(0.5)  # let the response reach the client
        linux_desktop = _is_linux_desktop()

        # 1) Kill child processes (pet, etc.).
        # In Flask debug mode, _launch_full_stack runs in the *master*
        # process but this handler runs in the *worker*, so
        # _pet_children_list is typically None here.  Two strategies:
        #   (a) Read child PIDs from the file written by _launch_full_stack.
        #   (b) On Windows, kill the master with /T to nuke the whole tree.
        _kill_pet()

        # (a) Kill child PIDs saved to disk
        _pid_file = _pet_pid_dir() / ".child_pids"
        if _pid_file.exists() and not linux_desktop:
            try:
                for _line in _pid_file.read_text().splitlines():
                    _cpid = int(_line.strip())
                    try:
                        if os.name == "nt":
                            import subprocess as _sp
                            _sp.call(
                                ["taskkill", "/PID", str(_cpid), "/F", "/T"],
                                stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
                            )
                        else:
                            os.kill(_cpid, signal.SIGTERM)
                    except (ProcessLookupError, PermissionError, OSError):
                        pass
            except Exception:
                pass
            try:
                _pid_file.unlink(missing_ok=True)
            except Exception:
                pass

        # Also kill from _pet_children_list if available (non-debug mode)
        if _pet_children_list:
            for proc in _pet_children_list:
                if proc.poll() is None:
                    if os.name == "nt":
                        import subprocess as _sp
                        _sp.call(
                            ["taskkill", "/PID", str(proc.pid), "/F", "/T"],
                            stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
                        )
                    else:
                        proc.terminate()
            _deadline = _t.time() + 3
            while _t.time() < _deadline:
                if all(p.poll() is not None for p in _pet_children_list):
                    break
                _t.sleep(0.2)
            for proc in _pet_children_list:
                if proc.poll() is None:
                    proc.kill()

        # The Linux desktop owns Flask in a thread, not a reloader child.
        # Its parent is the user's shell/session and must never be signalled.
        if linux_desktop:
            os._exit(0)

        # 2) Kill the Flask master process (reloader parent).
        #    On Windows, /T kills the entire tree rooted at the master,
        #    catching any children the PID file may have missed.
        ppid = os.getppid()
        try:
            if os.name == "nt":
                import subprocess as _sp
                _sp.call(
                    ["taskkill", "/PID", str(ppid), "/F", "/T"],
                    stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
                )
            else:
                os.kill(ppid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        _t.sleep(0.2)
        os._exit(0)

    threading.Thread(target=_do_shutdown, daemon=True).start()
    return jsonify({"ok": True, "message": "ContextLife is shutting down..."})


# ===== Startup =====


def _get_background_user_ids(days: int = 7) -> list[str]:
    """Select backend users without letting a DMG service stale accounts.

    Normal server mode deliberately preserves the original all-engaged-users
    behavior. Client mode may run server-side loops only for the currently
    verified local-single-device account; remote/login/setup states run none.
    """
    import auth as _auth

    if not _is_client_mode:
        return _auth.get_engaged_user_ids(days=days)

    with _client_runtime_lock:
        uid = _client_local_runtime_user_id
        cfg = dict(_client_mode_config or {})
    if not uid or uid != _verified_local_runtime_user_id(cfg):
        return []
    # In local single-device mode the verified runtime marker already means
    # this is the one account currently owned by the Mac process. Requiring a
    # recent chat/screenshot here makes a fresh account disappear from the
    # first spawner scan, which stops Curator before its first 60-second tick.
    return [uid]


def _run_for_each_engaged_user(fn, label="background"):
    """Run fn() once per ENGAGED user (active + chatted in the last 7 days).

    Proactive/attention features (morning/nightly greetings, AttentionEngine) only apply to
    users who have actually started using the system, not every account that
    exists.
    """
    import auth as _auth
    for uid in _get_background_user_ids(days=7):
        try:
            with app.app_context():
                g.user_id = uid
                g.user_data_dir = _auth.get_user_data_dir(uid)
                g.is_admin = False
                fn()
        except Exception as e:
            print(f"[{label}] Error for {uid}: {e}")


_journal_generated_today: dict[str, str] = {}  # {user_id: "YYYY-MM-DD"}
_journal_backfill_done: set[str] = set()  # {user_id} — one-time bulk upgrade flag per process
_slot_compactor_backfill_attempts: dict[str, float] = {}  # {user_id:date: last_attempt_ts}

def _check_journal_auto():
    """Nightly daily-journal generation + silent legacy backfill.

    Two sub-tasks:
      1. 23:45-00:14 window: generate today's (or just-finished yesterday's) journal
      2. Any time: if this user has legacy MD-only journals, silently convert
         a few per tick until all are upgraded (invisible to the user)
    """
    import user_settings as _us
    now = _us.user_now()
    uid = getattr(g, "user_id", "_admin")
    from datetime import timedelta as _td

    # Sub-task 1: nightly generate
    in_window = (now.hour == 23 and now.minute >= 45) or (now.hour == 0 and now.minute < 15)
    if in_window:
        if now.hour == 0:
            target = (now - _td(days=1)).strftime("%Y-%m-%d")
        else:
            target = now.strftime("%Y-%m-%d")
        if _journal_generated_today.get(uid) != target:
            try:
                import journal as _j
                result = _j.generate_daily_journal(target, force=True)
                if result:
                    _journal_generated_today[uid] = target
                    print(f"[Journal] Auto-generated {target} for {uid}")
            except Exception as e:
                print(f"[Journal] Auto-generate failed for {uid}: {e}")

    # Sub-task 1.5: missed nightly SlotDailyCompactor catch-up.
    # Journal has scan_and_backfill(), but append-only slot bodies also need a
    # recovery path when the desktop app was asleep/closed during 23:25-23:35.
    # If yesterday or earlier still has pending append entries, compact up to
    # today so same-slot records from multiple days can be merged in one pass.
    try:
        import memory_router as _mr
        yesterday = (now - _td(days=1)).strftime("%Y-%m-%d")
        today = now.strftime("%Y-%m-%d")
        if _mr.has_uncompacted_slot_appends(yesterday):
            attempt_key = f"{uid}:{today}"
            last_attempt = _slot_compactor_backfill_attempts.get(attempt_key, 0)
            should_attempt = (time.time() - last_attempt) >= 3600
            if should_attempt:
                _slot_compactor_backfill_attempts[attempt_key] = time.time()
                compact_result = _mr.run_slot_daily_compactor(date_str=today, max_slots=8)
                if compact_result.get("compacted") or compact_result.get("failed") or compact_result.get("skipped"):
                    print(f"[SlotDailyCompactor] Catch-up for {uid}: {compact_result}")
                if any(
                    item.get("reason") == "max_slots_reached"
                    for item in compact_result.get("skipped", [])
                    if isinstance(item, dict)
                ):
                    # Let the next reminder tick continue the backlog.
                    _slot_compactor_backfill_attempts.pop(attempt_key, None)
    except Exception as e:
        print(f"[SlotDailyCompactor] Catch-up failed for {uid}: {e}")

    # Sub-task 2: silent legacy backfill — generate a handful per loop tick until
    # the user's legacy MDs are all converted. Runs forever but cheap when done.
    try:
        import journal as _j
        info = _j.scan_and_backfill(days=30, max_per_call=2)
        if info.get("regenerated"):
            print(f"[Journal] Silent backfill for {uid}: {info['regenerated']}")
    except Exception as e:
        print(f"[Journal] Backfill failed for {uid}: {e}")


def _start_reminder_loop(interval=60):
    """Start a server-side background thread that runs proactive message checks.

    Two channels: morning message (08:00), nightly message (23:30).
    AttentionEngine runs in its own per-user threads (see _start_attention_engine_spawner).
    Iterates over engaged users only.
    """
    def _loop():
        while True:
            try:
                _run_for_each_engaged_user(core.check_morning_message, "Morning")
                _run_for_each_engaged_user(core.check_nightly_message, "Nightly")
                _run_for_each_engaged_user(_check_journal_auto, "Journal")
            except Exception:
                pass
            time.sleep(interval)

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    print(f"[Reminder] Background loop started (interval={interval}s)")
    return t


def _start_attention_engine_spawner(interval=300):
    """Start a background thread that lazily spawns AttentionEngine per user.

    Every `interval` seconds it scans the server's engaged users or the DMG's
    one active local runtime user. For each user without a running instance it
    creates and starts one bound to that user. AttentionEngine writes inner
    state and speak_intent, then the delivery gate may call the proactive main
    agent and send one chat message if the hard rules allow it.

    Existing instances are NOT restarted or torn down, so a user who stops
    chatting keeps their engine running (the engine's presence-detection
    logic will correctly stay quiet while they're away).
    """
    from attention_engine import get_attention_engine, get_all_instances, remove_attention_engine

    def _loop():
        import auth as _auth
        # First scan after a short delay to let Flask finish booting
        time.sleep(10)
        while True:
            try:
                engaged = set(_get_background_user_ids(days=7))
                instances = get_all_instances()

                # 1) Spawn engines for newly-engaged users
                for uid in engaged:
                    if uid in instances and instances[uid]._running:
                        continue  # already spawned
                    try:
                        with app.app_context():
                            g.user_id = uid
                            g.user_data_dir = _auth.get_user_data_dir(uid)
                            g.is_admin = False
                            inst = get_attention_engine()
                            inst.start()
                    except Exception as e:
                        print(f"[AttentionEngine] Failed to spawn for {uid}: {e}")

                # 2) Stop + evict engines for users who fell out of the 7-day
                #    window.  remove_attention_engine() also disables auto-start
                #    on stale references, so a racing request cannot resurrect
                #    a registry-free loop.
                for uid in [u for u in instances.keys() if u not in engaged]:
                    try:
                        remove_attention_engine(uid)
                        print(f"[AttentionEngine] Evicted {uid} (no chat/screenshot in 7d)")
                    except Exception as e:
                        print(f"[AttentionEngine] Evict failed for {uid}: {e}")

                # 3) Stop curator daemons for the same fall-out users. Curator
                #    runs lazily (only when slot writes accumulate) so it's
                #    cheap to leave alive, but we still want symmetry with
                #    AttentionEngine eviction so the per-user thread footprint
                #    stays bounded.
                try:
                    import curator as _curator
                    curator_active = set(_curator._curator_user_threads.keys())
                    for uid in curator_active - engaged:
                        try:
                            _curator.stop_loop_for_user(uid)
                            print(f"[Curator] Evicted {uid} (no chat/screenshot in 7d)")
                        except Exception as e:
                            print(f"[Curator] Evict failed for {uid}: {e}")
                except Exception as e:
                    print(f"[Curator] Spawner sweep failed (non-fatal): {e}")
            except Exception as e:
                print(f"[AttentionEngine] Spawner error: {e}")
            time.sleep(interval)

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    print(f"[AttentionEngine] Spawner started (scan every {interval}s, engaged window=7d)")
    return t


# Backward-compatible name used by older tests/docs.  It now starts
# AttentionEngine, not the old proactive-sending CareEngine.
def _start_care_engine_spawner(interval=300):
    return _start_attention_engine_spawner(interval=interval)





# Module-level ref so /api/pet/restart can kill + respawn the pet process
_pet_process = None
_pet_env = None
_pet_children_list = None


def _is_linux_desktop(env=None):
    env = os.environ if env is None else env
    return sys.platform.startswith("linux") and env.get("MIRU_DESKTOP_PLATFORM") == "linux"


def _pet_pid_dir():
    """Stable directory for .pet.pid — not _MEIPASS in PyInstaller."""
    from pathlib import Path
    if getattr(sys, "frozen", False):
        from desktop_paths import app_support_dir
        d = app_support_dir()
        d.mkdir(parents=True, exist_ok=True)
        return d
    return Path(__file__).resolve().parent


def _kill_pet():
    """Kill the currently running pet process (if any) and clean up PID files."""
    global _pet_process
    pid_dir = _pet_pid_dir()
    pid_file = pid_dir / ".pet.pid"
    child_pid_file = pid_dir / ".child_pids"
    pet_lock_file = pid_dir / "data" / ".pet.lock"
    linux_desktop = _is_linux_desktop()

    def _terminate_pid(pid: int) -> None:
        if os.name == "nt":
            from windows.platform import taskkill_process_tree
            taskkill_process_tree(pid)
        else:
            os.kill(pid, 15)  # SIGTERM (Unix)

    def _force_kill_pid(pid: int) -> None:
        """SIGKILL fallback — guaranteed to terminate on Unix."""
        try:
            if os.name == "nt":
                from windows.platform import taskkill_process_tree
                taskkill_process_tree(pid)
            else:
                os.kill(pid, 9)  # SIGKILL
        except (ProcessLookupError, PermissionError, OSError):
            pass

    if _pet_process is not None and _pet_process.poll() is None:
        try:
            _terminate_pid(_pet_process.pid)
            if os.name != "nt":
                _pet_process.wait(timeout=5)
        except Exception:
            # SIGTERM didn't work within 5s → SIGKILL
            _force_kill_pid(_pet_process.pid)
            try:
                _pet_process.wait(timeout=2)
            except Exception:
                pass
        _pet_process = None
    elif pid_file.exists() and not linux_desktop:
        try:
            old_pid = int(pid_file.read_text().strip())
            if os.name == "nt":
                from windows.platform import is_pet_process_alive
                if not is_pet_process_alive(old_pid):
                    raise ProcessLookupError(old_pid)
            _terminate_pid(old_pid)
            # Brief wait then force-kill if still alive
            import time as _t
            _t.sleep(1)
            try:
                os.kill(old_pid, 0)  # existence check
                _force_kill_pid(old_pid)
            except (ProcessLookupError, PermissionError, OSError):
                pass  # already gone
        except Exception:
            pass

    # Linux uses flock plus an owning-parent watcher, never persisted PIDs.
    if linux_desktop:
        return

    # Clean up PID/lock files so the next app launch always starts a fresh
    # visible pet instead of inheriting a hidden/stale singleton state.
    for f in (pid_file, child_pid_file, pet_lock_file):
        try:
            f.unlink(missing_ok=True)
        except Exception:
            pass


def _find_tauri_binary():
    """Locate the Tauri release binary. Returns Path or None."""
    import sys
    from pathlib import Path
    ROOT = Path(__file__).resolve().parent
    # PyInstaller bundle: miru-pet next to the main executable
    if getattr(sys, "frozen", False):
        bundled_name = "miru-pet.exe" if os.name == "nt" else "miru-pet"
        bundled = Path(sys.executable).parent / bundled_name
        if bundled.exists():
            return bundled
    # Release binary
    binary = ROOT / "src-tauri" / "target" / "release" / "app"
    if os.name == "nt":
        binary = binary.with_suffix(".exe")
    if binary.exists():
        return binary
    # Debug binary fallback
    debug = ROOT / "src-tauri" / "target" / "debug" / "app"
    if os.name == "nt":
        debug = debug.with_suffix(".exe")
    if debug.exists():
        return debug
    return None


def _launch_pet_tauri(pet_url, pet_hotkey, env, cwd):
    """Launch the native pet: Qt on Linux, Tauri on Windows/macOS."""
    import subprocess
    pet_env = (env or os.environ).copy()
    if _is_linux_desktop(pet_env):
        from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
        parts = urlsplit(pet_url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query["desktop_platform"] = "linux"
        pet_url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
        from pathlib import Path
        command = [sys.executable, "-s", str(Path(__file__).resolve().parent / "linux_pet.py")]
        pet_env["AIRI_PET_PARENT_PID"] = str(os.getpid())
    else:
        tauri_bin = _find_tauri_binary()
        if not tauri_bin:
            return None
        command = [str(tauri_bin)]
    pet_env["AIRI_PET_URL"] = pet_url
    pet_env["AIRI_PET_HOTKEY"] = pet_hotkey
    print(f"[pet] Starting native pet (hotkey: {pet_hotkey})")
    return subprocess.Popen(command, cwd=cwd, env=pet_env)


def _respawn_pet():
    """Restart the desktop pet (non-blocking, runs in a thread)."""
    global _pet_process
    import threading
    _kill_pet()

    def _do():
        global _pet_process
        import platform
        from pathlib import Path

        ROOT = Path(__file__).resolve().parent
        PET_URL = "http://127.0.0.1:5001/pet"
        env = _pet_env or os.environ.copy()
        # user_settings.get() returns a platform-appropriate default when no
        # Flask user context is available (pet spawner runs outside a request).
        pet_hotkey = user_settings.get().get("pet_hotkey",
            "cmd+option+m" if platform.system() == "Darwin" else "ctrl+alt+m")

        # Tauri binary (cross-platform)
        pet = _launch_pet_tauri(PET_URL, pet_hotkey, env, ROOT)
        if pet is None:
            print("[pet] Tauri binary not found — skipping pet. Build with: cd src-tauri && cargo build --release")
            return

        _pet_process = pet
        if _pet_children_list is not None:
            _pet_children_list.append(pet)

        # Write PID file
        try:
            if not _is_linux_desktop():
                (_pet_pid_dir() / ".pet.pid").write_text(str(pet.pid))
        except Exception:
            pass

    threading.Thread(target=_do, daemon=True).start()


@app.route("/api/pet/restart", methods=["POST"])
def api_pet_restart():
    """Kill and restart the desktop pet (e.g. after hotkey change)."""
    _respawn_pet()
    return jsonify({"ok": True, "message": "Pet restart triggered"})


@app.route("/api/launch-pet", methods=["POST"])
def api_launch_pet():
    """Launch the desktop pet on demand (called from web UI close)."""
    if _pet_process and _pet_process.poll() is None:
        return jsonify({"ok": True, "message": "Pet already running"})
    # In .app mode, signal the main thread to launch pet
    _launch_pet_requested.set()
    # Also launch directly for non-.app mode
    if not getattr(sys, "frozen", False):
        _launch_full_stack()
    return jsonify({"ok": True, "message": "Pet launched"})


@app.route("/api/open-webui", methods=["POST"])
def api_open_webui():
    """Signal the launcher to open the native WebView window."""
    _open_webui_requested.set()
    return jsonify({"ok": True})


def _launch_full_stack():
    """Launch the desktop pet window alongside Flask.

    Spawned only in the main process (not the Flask reloader child).
    All child processes are terminated when Flask exits (Ctrl+C).
    """
    import atexit
    import socket
    import subprocess
    from pathlib import Path

    ROOT = Path(__file__).resolve().parent
    PET_URL = "http://127.0.0.1:5001/pet"

    # PID files need a stable directory — PyInstaller's _MEIPASS is temporary
    if getattr(sys, "frozen", False):
        _pid_dir = _pet_pid_dir()
    else:
        _pid_dir = ROOT

    env = os.environ.copy()
    use_pid_files = not _is_linux_desktop(env)

    children: list[subprocess.Popen] = []
    global _pet_children_list, _pet_env
    _pet_children_list = children
    _pet_env = env

    _child_pid_file = _pid_dir / ".child_pids"

    def _save_child_pids():
        """Persist child PIDs to disk so the Flask worker can read them on shutdown."""
        if not use_pid_files:
            return
        try:
            pids = [str(p.pid) for p in children if p.poll() is None]
            _child_pid_file.write_text("\n".join(pids), encoding="utf-8")
        except Exception:
            pass

    def _cleanup():
        for proc in children:
            if proc.poll() is None:
                if os.name == "nt":
                    # Windows: kill the full process tree (shell=True wraps in cmd.exe)
                    subprocess.call(
                        ["taskkill", "/PID", str(proc.pid), "/F", "/T"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
                else:
                    proc.terminate()
        import time as _t
        deadline = _t.time() + 5
        while _t.time() < deadline:
            if all(p.poll() is not None for p in children):
                break
            _t.sleep(0.2)
        for proc in children:
            if proc.poll() is None:
                proc.kill()
        if not use_pid_files:
            return
        # Remove pet PID file
        try:
            (_pet_pid_dir() / ".pet.pid").unlink(missing_ok=True)
        except Exception:
            pass
        # Remove child PIDs file
        try:
            _child_pid_file.unlink(missing_ok=True)
        except Exception:
            pass

    atexit.register(_cleanup)

    # Pet page is served by Flask at /pet (vanilla JS + Live2D)

    # Desktop pet window (wait for Flask port only)
    def _start_pet():
        import time as _t
        import platform
        import sys as _sys
        import signal

        # Check if a pet process is already running (PID file guard)
        pet_pid_file = _pid_dir / ".pet.pid"
        if use_pid_files and pet_pid_file.exists():
            try:
                old_pid = int(pet_pid_file.read_text().strip())
                # Check if old process is still alive
                if platform.system() == "Windows":
                    from windows.platform import is_pet_process_alive
                    if is_pet_process_alive(old_pid):
                        print(f"[full] Pet process already running (PID {old_pid}) — skipping")
                        return
                else:
                    os.kill(old_pid, 0)  # signal 0 = existence check
                    print(f"[full] Pet process already running (PID {old_pid}) — skipping")
                    return
            except (ValueError, ProcessLookupError, PermissionError, OSError):
                pass  # old process is gone, proceed
            # Clean up stale PID file
            try:
                pet_pid_file.unlink(missing_ok=True)
            except Exception:
                pass

        deadline = _t.time() + 20
        while _t.time() < deadline:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                if s.connect_ex(("127.0.0.1", 5001)) == 0:
                    break
            _t.sleep(0.5)

        pet_hotkey = user_settings.get().get("pet_hotkey",
            "cmd+option+m" if platform.system() == "Darwin" else "ctrl+alt+m")

        # Tauri binary (cross-platform, handles macOS fullscreen)
        pet = _launch_pet_tauri(PET_URL, pet_hotkey, env, ROOT)
        if pet is None:
            print("[full] Tauri binary not found — skipping pet. Build with: cd src-tauri && cargo build --release")
            return

        children.append(pet)

        # Register in module-level ref for /api/pet/restart
        global _pet_process
        _pet_process = pet

        # Write PID file for duplicate-guard
        try:
            if use_pid_files:
                pet_pid_file.write_text(str(pet.pid))
        except Exception:
            pass
        _save_child_pids()

    t = threading.Thread(target=_start_pet, daemon=True)
    t.start()

    return children


def run_client_mode(server_url: str = "", auth_token: str = "", port: int = 5001):
    """Client mode entry point — callable from __main__ or miru_launcher.

    Runs Flask in the CALLING thread (blocking). Launches Tauri pet,
    ScreenSensor, device registration, and heartbeat exactly as
    `python3 app.py --client` does.

    If server_url/auth_token are empty, Flask still starts but without
    deferred VPS setup — the launcher's WebView will show /login and
    call /api/auth/login which handles post-login setup.

    IMPORTANT: All HTTP calls to VPS are deferred to a background thread
    so Flask starts immediately. This ensures the native WebView window
    appears without delay on first launch.
    """
    global _is_client_mode
    _is_client_mode = True

    # Before we do anything else: sweep orphan miru-pet processes from a
    # prior unclean Miru shutdown. Each old pet still holds a registration
    # on the global hotkey, so if we don't kill them first the user will
    # see multiple pets pop up on a single keypress.
    #
    # Scope is strictly the installed DMG path. Dev-mode orphans from
    # `cargo build` (binary named `app` under src-tauri/target/) are left
    # alone here — handled by the developer tool deploy/clean_mac_miru.sh
    # so production Miru behaves identically on every user's machine.
    try:
        if sys.platform == "darwin":
            import subprocess as _sp
            _sp.run(["pkill", "-TERM", "-f", "Miru.app/Contents/MacOS/miru-pet"],
                    check=False, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
            time.sleep(0.4)
            _sp.run(["pkill", "-KILL", "-f", "Miru.app/Contents/MacOS/miru-pet"],
                    check=False, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
        elif os.name == "nt":
            _kill_pet()
    except Exception as _e:
        print(f"[Client] orphan-pet sweep failed (non-fatal): {_e}")

    initial_cfg = {
        "server_url": server_url,
        "auth_token": auth_token,
        "mode": "",
        "local_only": False,
        "setup_complete": False,
    }

    # Try to read cached user_id from launcher config (instant, no HTTP)
    try:
        _cfg_path = _launcher_config_path()
        if _cfg_path.exists():
            _cached = json.loads(_cfg_path.read_text(encoding="utf-8"))
            if _cached.get("mode"):
                initial_cfg["mode"] = _cached.get("mode", "")
            if _cached.get("local_only") is not None:
                initial_cfg["local_only"] = bool(_cached.get("local_only"))
            if _cached.get("user_id"):
                initial_cfg["user_id"] = _cached["user_id"]
            initial_cfg["setup_complete"] = _setup_complete_from_launcher_config(_cached)
    except Exception:
        pass

    initial_generation, initial_cancel_event = _replace_client_runtime_config(initial_cfg)

    print("=" * 50)
    print("  Miru Client Mode")
    print(f"  Backend: {server_url or '(pending login)'}")
    if initial_cfg.get("mode"):
        print(f"  Mode: {initial_cfg.get('mode')}")
    print(f"  Local UI: http://localhost:{port}")
    if initial_cfg.get("user_id"):
        print(f"  User: {initial_cfg['user_id']}")
    print("=" * 50)

    # Signal launcher to open web UI window (not browser)
    if os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        _open_webui_requested.set()

    # Deferred setup: VPS registration, sensor, heartbeat — all in background
    # so Flask starts serving immediately and the WebView window can open fast.
    def _deferred_client_setup(override_url: str = "", override_token: str = "",
                               expected_generation: int | None = None,
                               cancel_event: threading.Event | None = None):
        _url = override_url or server_url
        _tok = override_token or auth_token
        if not _url or not _tok:
            return
        if expected_generation is None or cancel_event is None:
            expected_generation, cancel_event = _client_runtime_snapshot()
        if not _client_session_is_current(
                expected_generation, cancel_event, server_url=_url, token=_tok):
            return
        import socket as _sock
        # Wait for Flask to be ready first
        for _ in range(30):
            if not _client_session_is_current(
                    expected_generation, cancel_event, server_url=_url, token=_tok):
                return
            try:
                s = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
                s.settimeout(0.5)
                s.connect(("127.0.0.1", port))
                s.close()
                break
            except Exception:
                if cancel_event.wait(timeout=0.5):
                    return

        # Resolve user_id from backend — MUST succeed before starting sensor,
        # otherwise we don't know which user directory to use and the
        # screen capture permission dialog would appear prematurely.
        _user_verified = False
        try:
            import requests as _req
            _me = _req.get(f"{_url}/api/auth/me",
                           headers={"Authorization": f"Bearer {_tok}"}, timeout=10)
            if not _client_session_is_current(
                    expected_generation, cancel_event, server_url=_url, token=_tok):
                return
            if _me.status_code == 200:
                _uid = _me.json().get("user_id")
                if _uid:
                    with _client_runtime_lock:
                        if (expected_generation != _client_runtime_generation
                                or cancel_event is not _client_runtime_cancel_event):
                            return
                        _client_mode_config["user_id"] = _uid
                    _user_verified = True
                    print(f"[Client] User verified: {_uid}")
            elif _me.status_code in (401, 403):
                # Token rejected — admin suspended user or rotated token.
                # Force re-login flow before any sensor/permission prompt.
                _force_relogin(
                    f"/api/auth/me returned {_me.status_code}",
                    expected_generation=expected_generation,
                )
                return
            else:
                print(f"[Client] Warning: /api/auth/me returned {_me.status_code}")
        except Exception as _e:
            print(f"[Client] Warning: Cannot verify user ({_e})")

        if not _client_session_is_current(
                expected_generation, cancel_event, server_url=_url, token=_tok):
            return

        # Test backend connection
        try:
            import requests as _req
            _resp = _req.get(f"{_url}/api/settings/app",
                             headers={"Authorization": f"Bearer {_tok}"}, timeout=5)
            if _resp.status_code == 200:
                print("[Client] Backend connected")
            else:
                print(f"[Client] Warning: Backend returned {_resp.status_code}")
        except Exception as _e:
            print(f"[Client] Warning: Cannot reach backend ({_e})")

        # Only start sensor + heartbeat AFTER user is verified.
        # This prevents screen capture permission from firing before login.
        if not _user_verified:
            print("[Client] Skipping sensor — user not verified. "
                  "Sensor will start after successful login via WebView.")
            return

        if _is_local_single_device_mode():
            _start_local_single_device_backend_services_once(
                port=port,
                expected_generation=expected_generation,
                cancel_event=cancel_event,
            )

        if not _client_session_is_current(
                expected_generation, cancel_event, server_url=_url, token=_tok):
            return

        # Seed local per-device settings before ScreenSensor starts. This
        # fixes clean install / cache wipe cases where the private server says
        # screenshots are enabled but the local DMG still has no
        # user_settings.json, so the sensor would default to disabled.
        try:
            import requests as _req
            _settings = _req.get(
                f"{_url}/api/user-settings",
                headers={"Authorization": f"Bearer {_tok}"},
                timeout=5,
            )
            if not _client_session_is_current(
                    expected_generation, cancel_event, server_url=_url, token=_tok):
                return
            if _settings.status_code == 200:
                _body = _settings.json()
                if _seed_local_user_settings_from_remote(
                        _body.get("settings") or {}, _uid):
                    source_label = "local backend" if _is_local_single_device_mode() else "private server"
                    print(f"[Client] Seeded local user settings from {source_label}")
            else:
                print(f"[Client] Warning: /api/user-settings returned {_settings.status_code}")
        except Exception as _e:
            print(f"[Client] Warning: Cannot sync user settings ({_e})")

        if not _client_session_is_current(
                expected_generation, cancel_event, server_url=_url, token=_tok):
            return

        # User confirmed — release the gate so the launcher can spawn the pet.
        # Idempotent: re-verifications during a session simply re-set the flag.
        _pet_ready_event.set()
        _pet_should_hide_event.clear()

        try:
            import device_manager
            _local_id = device_manager.get_local_device_id()
            _device_payload = _client_desktop_device_payload(_local_id)
            _restore_missing_device = _is_local_single_device_mode()

            # Register device with backend
            try:
                _device_registered = _register_client_desktop_device(
                        _url, _tok, _device_payload,
                        expected_generation, cancel_event)
                if not _device_registered:
                    print("[Client] Device registration was not accepted (non-fatal)")
                if not _client_session_is_current(
                        expected_generation, cancel_event,
                        server_url=_url, token=_tok):
                    return
                if _device_registered:
                    print("[Client] Device registered with backend")
            except Exception as _e:
                print(f"[Client] Device registration failed (non-fatal): {_e}")

            # ScreenSensor → upload to the currently selected backend. The
            # bind/start helper serializes against account replacement so a
            # stale deferred thread cannot restore an old token afterward.
            if not _start_client_screen_sensor(
                    _url, _local_id, _tok, expected_generation, cancel_event):
                return
            print(f"[Client] ScreenSensor started → {_url}")

            # Heartbeat is session-scoped: switching accounts signals the
            # captured cancel event, so an old token cannot evict a new login.
            threading.Thread(
                target=_client_heartbeat_loop,
                args=(_url, _tok, _local_id, expected_generation, cancel_event),
                kwargs={
                    "restore_missing_device": _restore_missing_device,
                    "device_registration": _device_payload,
                },
                daemon=True,
            ).start()
        except Exception as _e:
            print(f"[Client] Sensor setup failed: {_e}")

    # Expose a trigger so /api/auth/login (post-login flow) can kick off
    # the deferred setup immediately after the user enters their code.
    global _deferred_client_setup_trigger
    def _trigger(url, tok):
        generation, cancel_event = _client_runtime_snapshot()
        threading.Thread(
            target=_deferred_client_setup,
            args=(url, tok, generation, cancel_event),
            daemon=True,
        ).start()
    _deferred_client_setup_trigger = _trigger

    # If we already have completed credentials, run setup now.  Pending
    # first-run API setup deliberately keeps pet/sensor/runtime inactive.
    if server_url and auth_token and bool(initial_cfg.get("setup_complete")):
        threading.Thread(
            target=_deferred_client_setup,
            args=("", "", initial_generation, initial_cancel_event),
            daemon=True,
        ).start()

    # Run Flask — blocks in the calling thread (starts IMMEDIATELY now)
    app.run(debug=False, host="127.0.0.1", port=port)


def _preflight_check():
    """Check environment and auto-fix missing dependencies on startup."""
    import shutil
    from pathlib import Path

    ROOT = Path(__file__).resolve().parent
    warnings = []
    fixed = []

    # --- Live2D SDK files ---
    live2d_js = ROOT / "assets" / "js" / "live2d" / "live2d.min.js"
    cubism_js = ROOT / "assets" / "js" / "live2d" / "CubismSdkForWeb-5-r.3" / "Core" / "live2dcubismcore.min.js"
    missing_sdk = []
    if not live2d_js.exists():
        missing_sdk.append("live2d.min.js (Cubism2)")
    if not cubism_js.exists():
        missing_sdk.append("live2dcubismcore.min.js (Cubism4/5)")
    if missing_sdk:
        warnings.append(f"Live2D SDK missing: {', '.join(missing_sdk)}\n"
                         "         These should be in assets/js/live2d/")

    # --- AI config ---
    try:
        missing_ai = []
        for tier in ai_config.TIERS:
            cfg = ai_config.get_tier_config(tier)
            if not (cfg.get("host") and cfg.get("api_key") and cfg.get("model")):
                missing_ai.append(tier)
        if missing_ai:
            warnings.append(
                "模型配置未完成: "
                + ", ".join(missing_ai)
                + " — 请在设置 > 模型中填写，或通过 AI_* 环境变量提供"
            )
    except Exception as exc:
        warnings.append(f"AI provider config check failed: {exc}")

    # --- Print summary ---
    if fixed:
        for f in fixed:
            print(f"[preflight] ✓ {f}")
    if warnings:
        print("\n" + "=" * 60)
        print("  STARTUP WARNINGS")
        print("=" * 60)
        for w in warnings:
            print(f"  ⚠  {w}")
        print("=" * 60 + "\n")
    else:
        print("[preflight] All checks passed")


def _start_tunnel(port: int):
    """Start cloudflared tunnel in background for external access.

    Launches cloudflared and polls for the URL in a background thread
    so it doesn't block Flask startup. Sets TUNNEL_URL env var when ready.
    Skips silently if cloudflared is not installed.
    """
    import shutil
    if not shutil.which("cloudflared"):
        return

    import subprocess, re, atexit

    log_path = "/tmp/miru_tunnel.log"
    try:
        # Clear proxy env vars so cloudflared's traffic isn't intercepted
        tunnel_env = {k: v for k, v in os.environ.items()}
        for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                     "all_proxy", "ALL_PROXY", "no_proxy", "NO_PROXY"):
            tunnel_env.pop(key, None)
        proc = subprocess.Popen(
            ["cloudflared", "tunnel", "--url", f"http://localhost:{port}",
             "--protocol", "http2"],
            stdout=open(log_path, "w"),
            stderr=subprocess.STDOUT,
            env=tunnel_env,
        )
    except Exception as e:
        print(f"[Tunnel] Failed to start cloudflared: {e}")
        return

    def _kill_tunnel():
        try:
            proc.terminate()
        except Exception:
            pass
    atexit.register(_kill_tunnel)

    # Poll for URL in background thread — doesn't block Flask startup
    def _wait_for_url():
        import time as _t
        for _ in range(30):
            _t.sleep(1)
            try:
                with open(log_path, "r") as f:
                    content = f.read()
                match = re.search(r'https://[a-z0-9-]+\.trycloudflare\.com', content)
                if match:
                    os.environ["TUNNEL_URL"] = match.group(0)
                    print(f"[Tunnel] Ready: {match.group(0)}")
                    return
            except Exception:
                pass
        print(f"[Tunnel] Could not detect URL after 30s (check {log_path})")

    t = threading.Thread(target=_wait_for_url, daemon=True)
    t.start()


if __name__ == "__main__":
    import argparse
    _parser = argparse.ArgumentParser(description="Miru AI Companion")
    _parser.add_argument("--client", action="store_true",
                         help="Client mode: UI shell + sensor, backend on VPS")
    _parser.add_argument("--server", help="VPS server URL (client mode)")
    _parser.add_argument("--token", help="Auth token (client mode)")
    _args = _parser.parse_args()

    # ---------------------------------------------------------------
    # CLIENT MODE: lightweight UI shell + sensor, backend on VPS
    # ---------------------------------------------------------------
    if _args.client:
        from pathlib import Path as _Path
        _cfg_path = _Path(__file__).resolve().parent / "data" / "client_config.json"

        # Load or create client config
        _cfg = {}
        if _cfg_path.exists():
            try:
                with open(_cfg_path) as _f:
                    _cfg = json.load(_f)
            except Exception:
                pass
        if _args.server:
            _cfg["server_url"] = _args.server.rstrip("/")
        if _args.token:
            _cfg["auth_token"] = _args.token

        if not _cfg.get("server_url") or not _cfg.get("auth_token"):
            print("=" * 50)
            print("  Miru Client Mode — First Time Setup")
            print("=" * 50)
            if not _cfg.get("server_url"):
                _cfg["server_url"] = input("Private server URL (for example http://203.0.113.42:5001): ").strip().rstrip("/")
            if not _cfg.get("auth_token"):
                _cfg["auth_token"] = input("Auth token (from VPS data/auth.json): ").strip()
            if not _cfg.get("server_url") or not _cfg.get("auth_token"):
                print("Error: server URL and token are required")
                sys.exit(1)
            _cfg_path.parent.mkdir(parents=True, exist_ok=True)
            with open(_cfg_path, "w") as _f:
                json.dump(_cfg, _f, indent=2)
            print(f"[Client] Config saved to {_cfg_path}")

        _vps = _cfg["server_url"]
        _token = _cfg["auth_token"]
        import server_config as _sc
        runtime = _sc.get()
        port = int(os.environ.get("PORT", runtime.get("port", 5001)))

        run_client_mode(_vps, _token, port)
        sys.exit(0)

    # ---------------------------------------------------------------
    # NORMAL MODE (full backend) — unchanged
    # ---------------------------------------------------------------
    _preflight_check()
    storage._ensure_dirs()
    # Split legacy app_settings.json into server_config + user_settings (one-time, idempotent)
    try:
        import server_config as _sc_migrate
        _sc_migrate.migrate_legacy_settings()
    except Exception as _e:
        print(f"[Migration] Failed to split legacy app_settings.json: {_e}")
    # Backfill persona files for models registered before the persona system
    import model_library as _ml_init
    _ml_init.ensure_existing_models_have_personas()
    _ml_init.refresh_all_model_avatars()
    core.migrate_timestamps_utc_to_cst()
    core.migrate_reminders_to_chat_history()
    # Note: ensure_first_greeting() removed — it ran at server startup with
    # no Flask user context, so it wrote to the global data/ root rather
    # than any user's directory and never actually delivered a greeting in
    # multi-user mode. First greeting is now produced per-user inside
    # core.initialize_from_questionnaire (POST /api/onboarding/submit),
    # using the answers to make it personalized.
    import server_config
    runtime = server_config.get()
    port = int(os.environ.get("PORT", runtime.get("port", 5001)))
    # Headless/Docker: default debug=False; local: respect setting
    if os.environ.get("MIRU_HEADLESS"):
        debug = False
    else:
        debug = bool(runtime.get("flask_debug", True))

    # Launch Tauri shell only in the master process,
    # not in the Flask reloader child. Skip in headless/Docker mode.
    if os.environ.get("WERKZEUG_RUN_MAIN") != "true" and not os.environ.get("MIRU_HEADLESS"):
        _launch_full_stack()

    # In debug mode Flask reloader spawns a child process — only start
    # background threads in the child (WERKZEUG_RUN_MAIN=true) or when
    # the reloader is not active.
    if not debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        # Auto-register local Mac as a device
        try:
            import device_manager
            local_id = device_manager.get_local_device_id()
            import platform as _plat
            raw_name = _plat.node() or "Local Machine"
            pretty_name = device_manager.beautify_device_name(raw_name, sys.platform)
            device_manager.register_device(
                name=pretty_name,
                device_type="desktop",
                device_platform=sys.platform,
                device_id=local_id,
            )
        except Exception as e:
            print(f"[Startup] Device registration failed: {e}")

        _start_reminder_loop()
        if sys.platform in ("darwin", "linux", "win32"):
            # Start ScreenAnalyzer (VLM) — always needed to process incoming screenshots
            # Start ScreenSensor (local capture) — only on desktop, not headless server
            try:
                from screen_analyzer import get_analyzer
                vlm_interval = runtime.get("auto_screenshot_interval", 30)
                get_analyzer().set_vlm_interval(vlm_interval)
                if not os.environ.get("MIRU_HEADLESS"):
                    import auth as _auth_startup
                    from sensor import get_sensor
                    get_sensor(
                        backend_url=f"http://localhost:{port}",
                        device_id=local_id,
                        auth_token=_auth_startup.get_or_create_token(),
                    ).start()
                else:
                    print("[Startup] Headless mode — ScreenSensor disabled (no local screen)")
            except Exception as e:
                print(f"[Startup] ScreenSensor/Analyzer failed to start: {e}")
            # Start AttentionEngine spawner — creates one inner-presence engine
            # per engaged user. It writes attention state/emotions and, when a
            # speak_intent passes the delivery gate, sends one proactive message.
            try:
                _start_attention_engine_spawner(interval=300)
            except Exception as e:
                print(f"[Startup] AttentionEngine spawner failed to start: {e}")
        # Ensure memory directory structure exists
        try:
            import memory
            memory.ensure_dirs()
        except Exception:
            pass
        # Initialize core memory (seed from existing data if empty)
        try:
            import core_memory
            result = core_memory.initialize_from_existing()
            if result.get("actions"):
                print(f"[Startup] Core memory initialized: {result['actions']}")
        except Exception as e:
            print(f"[Startup] Core memory init failed (non-fatal): {e}")

        # Start Cloudflare Tunnel if cloudflared is installed (for phone access)
        # Skip in headless/Docker mode (VPS has public IP, no tunnel needed)
        if not os.environ.get("MIRU_HEADLESS"):
            _start_tunnel(port)

    app.run(debug=debug, host="0.0.0.0", port=port)
