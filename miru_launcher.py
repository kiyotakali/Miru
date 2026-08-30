"""Miru Mac Launcher — thin entry point for PyInstaller .app bundle.

On launch:
1. Start Flask (run_client_mode) — with cached config if available, otherwise empty.
2. Open WKWebView window pointing to:
   - /login                       (first run — HTML-based invitation code entry)
   - /app?token=<cached_token>    (returning user)
3. After a successful login in /login, the page redirects to
   /app?token=xxx. The local /api/auth/login persists the token and
   triggers the deferred client setup (sensor / device register).

No rumps dialog. Login is an HTML page — so ⌘V paste works, and the
design matches the landing page aesthetic.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import quote

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_APP_SUPPORT = Path.home() / "Library" / "Application Support" / "Miru"
_CONFIG_PATH = _APP_SUPPORT / "config.json"

# Root of the codebase (source tree or PyInstaller _MEIPASS)
if getattr(sys, "frozen", False):
    _BUNDLE_DIR = Path(sys._MEIPASS)
else:
    _BUNDLE_DIR = Path(__file__).resolve().parent


# Backend URL is now resolved at runtime via discovery (see discovery.py).
# Cached server_url in config takes priority; on first launch the launcher
# defers to app.py's resolver, which queries the discovery endpoint and
# falls back to the bundled default if unreachable.


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    if _CONFIG_PATH.exists():
        try:
            return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_config(cfg: dict):
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False),
                            encoding="utf-8")


def _setup_complete_from_config(cfg: dict) -> bool:
    """Return setup completion for cached launcher config.

    `setup_complete` was introduced after early DMG builds. A missing field
    means "legacy returning account" when a token is already cached, not a
    newly-created pending setup. Fresh pending accounts explicitly persist
    setup_complete=false.
    """
    if "setup_complete" in cfg:
        return bool(cfg.get("setup_complete"))
    return bool(cfg.get("auth_token"))


def _webview_target_from_config(cfg: dict, cache_buster: int) -> str:
    """Return the visible Mac window URL for the current client session."""
    auth_token = cfg.get("auth_token") or ""
    server_url = (cfg.get("server_url") or "").rstrip("/")
    if auth_token and _setup_complete_from_config(cfg):
        if server_url:
            return f"{server_url}/app?token={quote(auth_token)}&v={cache_buster}"
        return f"http://localhost:5001/app?token={quote(auth_token)}&v={cache_buster}"
    return "http://localhost:5001/login"


def _sync_model_data(cfg: dict):
    """Sync model_library.json from VPS to local data dir (runs in background)."""
    server_url = cfg.get("server_url", "")
    token = cfg.get("auth_token", "")
    user_id = cfg.get("user_id", "")
    if not server_url or not token:
        return

    if not user_id:
        try:
            import requests
            resp = requests.get(
                f"{server_url}/api/auth/me",
                headers={"Authorization": f"Bearer {token}"},
                timeout=5,
            )
            if resp.status_code == 200:
                user_id = resp.json().get("user_id", "")
                if user_id:
                    cfg["user_id"] = user_id
                    _save_config(cfg)
        except Exception:
            pass

    if not user_id:
        return

    data_dir = _APP_SUPPORT / "data" / "users" / user_id
    data_dir.mkdir(parents=True, exist_ok=True)

    try:
        import requests
        resp = requests.get(
            f"{server_url}/api/models/active",
            headers={"Authorization": f"Bearer {token}"},
            timeout=5,
        )
        if resp.status_code == 200:
            model_data = resp.json()
            if model_data and model_data.get("id"):
                lib = {
                    "active_model_id": model_data["id"],
                    "models": [model_data],
                }
                lib_path = data_dir / "model_library.json"
                lib_path.write_text(
                    json.dumps(lib, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                print(f"[Launcher] Synced model: {model_data.get('name', model_data['id'])}")
    except Exception as e:
        print(f"[Launcher] Model sync skipped: {e}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    _APP_SUPPORT.mkdir(parents=True, exist_ok=True)

    # Set up paths so imports work from PyInstaller bundle
    os.chdir(str(_BUNDLE_DIR))
    if str(_BUNDLE_DIR) not in sys.path:
        sys.path.insert(0, str(_BUNDLE_DIR))

    # Load cached config — may be empty on first launch
    cfg = _load_config()
    server_url = cfg.get("server_url") or ""
    auth_token = cfg.get("auth_token") or ""
    setup_complete = _setup_complete_from_config(cfg)

    # Point DATA_DIR to persistent location (NOT _MEIPASS which is ephemeral)
    _data_dir = str(_APP_SUPPORT / "data")
    os.environ["DATA_DIR"] = _data_dir
    os.makedirs(_data_dir, exist_ok=True)

    # Run app.py client mode
    import app as _app_mod

    # Determine initial WebView URL.
    # Client mode = thin shell over VPS: once we have a token + cached server
    # URL, point the WebView straight at VPS so all API calls (chat / memory /
    # SSE) hit the source of truth. Login screen still served locally so the
    # invitation-code POST can be forwarded by app.py to VPS.
    # Cache-bust query param tied to this launcher's startup time. WKWebView's
    # disk NetworkCache aggressively keeps /app HTML across restarts even
    # though VPS sends `Cache-Control: no-cache`. Adding `?v=<timestamp>` makes
    # every launch a fresh URL → forced cache miss → always fetch latest
    # templates/index.html (which contains all SPA JS inline). Without this,
    # template changes don't reach users until they manually nuke the WebKit
    # disk cache. See: 2026-05-17 slot-delete confirm() debugging session.
    _cache_buster = int(time.time())

    initial_url = _webview_target_from_config(cfg, _cache_buster)

    if getattr(sys, "frozen", False):
        # .app bundle: Flask in background thread, main thread runs proper
        # macOS NSApp.run() event loop with NSTimer for periodic checks.
        #
        # Design C: pet is a *sidecar* process owned by the app lifetime.
        # Red close fully quits Miru; Dock click only re-shows the window
        # while the current app runtime is still alive.
        import atexit
        import signal
        import socket
        import threading
        from AppKit import (NSApplication, NSWindow, NSBackingStoreBuffered,
                            NSObject, NSApplicationActivationPolicyRegular,
                            NSMenu, NSMenuItem, NSEventModifierFlagCommand,
                            NSEventModifierFlagShift, NSOpenPanel,
                            NSModalResponseOK)
        from Foundation import (NSMakeRect, NSURL, NSURLRequest, NSTimer,
                                NSString)
        from WebKit import (WKWebView, WKWebViewConfiguration,
                            WKUserScript, WKUserContentController,
                            WKUserScriptInjectionTimeAtDocumentStart)
        import objc
        import json as _json_mod

        # =====================================================================
        # MiruDesktop native bridge — let chat WebView (on VPS origin) reach
        # LOCAL Flask without going through NSURLSession (which honors macOS
        # system proxy, e.g. Surge, and would intercept localhost requests).
        #
        # Flow:
        #   JS: window.webkit.messageHandlers.miruDesktop.postMessage({
        #         op: "get_settings" | "set_settings",
        #         payload: {...},
        #         req_id: N,
        #       })
        #   Python (this handler) → urllib.request to http://127.0.0.1:5001
        #     (urllib does NOT honor system proxy for localhost by default)
        #   Python → evaluateJavaScript: window.__miruDesktopResolve(N, result)
        #   JS Promise resolves with result
        #
        # All requests stay inside the .app process: JS message → Python →
        # urllib → in-process Flask. No socket goes through Surge.
        # =====================================================================
        class _MiruDesktopBridge(NSObject):
            # Only userContentController:didReceiveScriptMessage: is a real
            # ObjC selector — all other helpers are Python-only and must be
            # tagged with @objc.python_method so PyObjC doesn't try to bridge
            # them as ObjC selectors (which would BadPrototypeError on import
            # because their arg counts don't match any ObjC method signature).
            def userContentController_didReceiveScriptMessage_(self, ucc, msg):
                try:
                    body = msg.body()  # NSDictionary
                    op = str(body.get("op", ""))
                    req_id = body.get("req_id")
                    payload = body.get("payload")
                    if hasattr(payload, "items") and not isinstance(payload, dict):
                        payload = {str(k): payload[k] for k in payload.keys()}

                    result = self._dispatch(op, payload)

                    wv = None
                    try:
                        wv = msg.webView()  # WKScriptMessage.webView
                    except Exception:
                        pass
                    if wv is None:
                        return
                    js = ("(function(){try{"
                          "window.__miruDesktopResolve && window.__miruDesktopResolve("
                          + _json_mod.dumps(req_id) + ","
                          + _json_mod.dumps(result, ensure_ascii=False)
                          + ");}catch(e){console.warn('[MiruDesktop] resolve err',e);}})();")
                    wv.evaluateJavaScript_completionHandler_(js, None)
                except Exception as exc:
                    print(f"[MiruDesktopBridge] handler error: {exc}")

            @objc.python_method
            def _dispatch(self, op, payload):
                if op == "get_settings":
                    return self._http("GET", None)
                if op == "set_settings":
                    return self._http("POST", payload or {})
                if op == "open_external_url":
                    return self._open_external_url(payload or {})
                if op == "check_screen_permission":
                    return self._check_screen_permission()
                if op == "app_ready":
                    return self._mark_app_ready(payload or {})
                return {"ok": False, "error": f"unknown op: {op!r}"}

            @objc.python_method
            def _mark_app_ready(self, payload):
                path = str((payload or {}).get("path", ""))
                if path and "/app" not in path:
                    return {"ok": False, "error": "not on app page"}
                _state["webui_app_ready"] = True
                _state["pet_waiting_for_app_logged"] = False
                return {"ok": True}

            @objc.python_method
            def _check_screen_permission(self):
                """Return whether this app currently has macOS Screen Recording
                permission. Uses CGPreflightScreenCaptureAccess (non-prompting).

                Returns:
                  {ok: True, granted: True}  — app can capture screen
                  {ok: True, granted: False} — TCC will deny capture attempts
                  {ok: False, error: "..."}  — non-macOS / runtime error
                """
                try:
                    import Quartz
                    granted = bool(Quartz.CGPreflightScreenCaptureAccess())
                    return {"ok": True, "granted": granted}
                except ImportError:
                    return {"ok": False, "error": "Quartz not available"}
                except Exception as exc:
                    return {"ok": False, "error": str(exc)}

            @objc.python_method
            def _open_external_url(self, payload):
                """Open a non-http URL (e.g. x-apple.systempreferences:) via
                NSWorkspace without letting the WebView navigate to it.

                Why this matters: when the perm-modal calls
                `window.location.assign('x-apple.systempreferences:...')`,
                WKWebView treats it as a navigation away from /app — the
                document gets destroyed mid-flight, and any LocalStorage
                writes queued in the same task may not flush to disk. Routing
                the URL through native NSWorkspace.openURL keeps /app alive
                so the `miru_desktop_perm_guided` flag persists, and the
                modal never re-shows on relaunch.
                """
                url = str(payload.get("url", "")).strip()
                if not url:
                    return {"ok": False, "error": "missing url"}
                try:
                    from AppKit import NSWorkspace
                    from Foundation import NSURL
                    ns_url = NSURL.URLWithString_(url)
                    if ns_url is None:
                        return {"ok": False, "error": "invalid url"}
                    NSWorkspace.sharedWorkspace().openURL_(ns_url)
                    return {"ok": True}
                except Exception as exc:
                    return {"ok": False, "error": str(exc)}

            @objc.python_method
            def _http(self, method, body):
                import urllib.request
                import urllib.error
                # Build opener with NO proxy so Surge/proxies are bypassed.
                # urllib's default ProxyHandler({}) explicitly disables proxies.
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                url = "http://127.0.0.1:5001/api/user-settings"
                data_bytes = None
                headers = {}
                if method == "POST":
                    data_bytes = _json_mod.dumps(body or {}).encode("utf-8")
                    headers["Content-Type"] = "application/json"
                req = urllib.request.Request(url, data=data_bytes,
                                              method=method, headers=headers)
                try:
                    with opener.open(req, timeout=5) as resp:
                        body_bytes = resp.read()
                    try:
                        parsed = _json_mod.loads(body_bytes)
                    except Exception:
                        parsed = {}
                    settings = parsed.get("settings") if isinstance(parsed, dict) else None
                    return {"ok": True, "settings": settings if settings is not None else parsed}
                except urllib.error.HTTPError as e:
                    return {"ok": False, "error": f"HTTP {e.code}", "status": e.code}
                except Exception as e:
                    return {"ok": False, "error": str(e)}

        _MIRU_DESKTOP_BRIDGE_JS = (
            "(function(){"
            "if(window.MiruDesktop)return;"
            "var pending=Object.create(null);"
            "var nextId=1;"
            "window.__miruDesktopResolve=function(id,res){"
            "  var p=pending[id];if(!p)return;delete pending[id];"
            "  try{p.resolve(res||{ok:false,error:'no result'});}catch(e){console.warn(e);}"
            "};"
            "function call(op,payload){"
            "  return new Promise(function(resolve,reject){"
            "    var id=nextId++;pending[id]={resolve:resolve,reject:reject};"
            "    try{window.webkit.messageHandlers.miruDesktop.postMessage("
            "      {op:op,payload:payload||null,req_id:id});}"
            "    catch(e){delete pending[id];reject(e);}"
            "    setTimeout(function(){"
            "      if(pending[id]){delete pending[id];"
            "       resolve({ok:false,error:'timeout'});}"
            "    },10000);"
            "  });"
            "}"
            "window.MiruDesktop={"
            "  getLocalSettings:function(){return call('get_settings');},"
            "  setLocalSettings:function(p){return call('set_settings',p||{});},"
            "  openExternalUrl:function(url){return call('open_external_url',{url:url});},"
            "  checkScreenPermission:function(){return call('check_screen_permission');},"
            "  notifyAppReady:function(p){return call('app_ready',p||{});}"
            "};"
            "console.log('[MiruDesktop] bridge ready');"
            "})();"
        )

        # --- Shared mutable state ---
        _state = {
            "webui_window": None,
            "webui_app_loaded": False,
            "webui_app_ready": False,
            "pet_waiting_for_app_logged": False,
            "flask_ready": False,
            "pet_launched": False,
            "ns_app": None,
            "initial_url": initial_url,
        }

        # --- Build NSApp main menu (File / Edit) so Cmd+V/C/X/A routes to
        # the first responder (WKWebView inside the invitation code page).
        # Without a mainMenu, Cmd+V is swallowed by NSApp and never reaches
        # the WebView's text field.
        def _build_main_menu(ns_app):
            main_menu = NSMenu.alloc().init()

            # --- App menu ---
            app_item = NSMenuItem.alloc().init()
            main_menu.addItem_(app_item)
            app_menu = NSMenu.alloc().init()
            app_item.setSubmenu_(app_menu)

            quit_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                "Quit Miru", b"terminate:", "q")
            app_menu.addItem_(quit_item)

            # --- Edit menu (critical for Cmd+V) ---
            edit_item = NSMenuItem.alloc().init()
            main_menu.addItem_(edit_item)
            edit_menu = NSMenu.alloc().initWithTitle_("Edit")
            edit_item.setSubmenu_(edit_menu)

            def _add(title, selector, key, modifier=NSEventModifierFlagCommand):
                item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                    title, selector, key)
                item.setKeyEquivalentModifierMask_(modifier)
                edit_menu.addItem_(item)

            _add("Undo", b"undo:", "z")
            _add("Redo", b"redo:", "z",
                 NSEventModifierFlagCommand | NSEventModifierFlagShift)
            edit_menu.addItem_(NSMenuItem.separatorItem())
            _add("Cut", b"cut:", "x")
            _add("Copy", b"copy:", "c")
            _add("Paste", b"paste:", "v")
            _add("Paste and Match Style", b"pasteAsPlainText:", "v",
                 NSEventModifierFlagCommand | NSEventModifierFlagShift)
            _add("Select All", b"selectAll:", "a")

            ns_app.setMainMenu_(main_menu)

        # --- JS snippet to tag the page for client-mode (hides companion
        # panel button, since DMG users use the floating Tauri pet instead).
        #
        # Injected at *documentStart* via WKUserScript so the flag is set
        # before any of the page's own JS runs — critical because the UI
        # heartbeat IIFE (templates/index.html line 8689) runs at parse
        # time and needs to see the flag to skip the heartbeat loop.
        # Without this, the heartbeat tells the pet to auto-hide (old UI
        # mutex logic), which was the "pet flashes and disappears" bug.
        _CLIENT_MODE_JS = (
            "window.__MIRU_CLIENT_MODE__ = true;"
            "window.__MIRU_DESKTOP_PLATFORM__ = 'macos';"
            "(function(){"
            "  function tag(){try{document.body.classList.add('client-mode');}catch(e){}}"
            "  if(document.body){tag();}"
            "  else{document.addEventListener('DOMContentLoaded',tag);}"
            "  window.__MIRU_LOCAL_BRAND_ASSET_BASE__='http://127.0.0.1:5001';"
            "  var assetBase=window.__MIRU_LOCAL_BRAND_ASSET_BASE__;"
            "  var localAssets={"
            "    '/icon-192.png':assetBase+'/icon-192.png',"
            "    '/icon-512.png':assetBase+'/icon-512.png',"
            "    '/manifest.json':assetBase+'/manifest.json'"
            "  };"
            "  function localize(v){"
            "    if(!v)return v;"
            "    try{var u=new URL(v,location.href);return localAssets[u.pathname]||v;}"
            "    catch(e){return localAssets[v]||v;}"
            "  }"
            "  function rewrite(el){"
            "    if(!el||!el.getAttribute)return;"
            "    if(el.hasAttribute('href')){var h=el.getAttribute('href');var nh=localize(h);if(nh!==h)el.setAttribute('href',nh);}"
            "    if(el.hasAttribute('src')){var s=el.getAttribute('src');var ns=localize(s);if(ns!==s)el.setAttribute('src',ns);}"
            "    if(el.hasAttribute('srcset')){"
            "      var ss=el.getAttribute('srcset');"
            "      var nss=ss.split(',').map(function(part){"
            "        var bits=part.trim().split(/\\s+/);"
            "        if(bits[0])bits[0]=localize(bits[0]);"
            "        return bits.join(' ');"
            "      }).join(', ');"
            "      if(nss!==ss)el.setAttribute('srcset',nss);"
            "    }"
            "  }"
            "  function scan(root){"
            "    try{(root||document).querySelectorAll('link[href],img[src],source[src],source[srcset]').forEach(rewrite);}catch(e){}"
            "  }"
            "  function start(){"
            "    scan(document);"
            "    try{new MutationObserver(function(ms){ms.forEach(function(m){"
            "      if(m.type==='attributes')rewrite(m.target);"
            "      (m.addedNodes||[]).forEach(function(n){"
            "        if(n.nodeType!==1)return;"
            "        rewrite(n);scan(n);"
            "      });"
            "    });}).observe(document.documentElement,{subtree:true,childList:true,attributes:true,attributeFilter:['href','src','srcset']});}catch(e){}"
            "  }"
            "  if(document.documentElement)start();"
            "  else document.addEventListener('DOMContentLoaded',start);"
            "})();"
        )

        # Keep ONE bridge instance alive for the whole app lifetime — otherwise
        # PyObjC may GC the handler and WKWebView calls into a dead pointer.
        _state["miru_desktop_bridge"] = _MiruDesktopBridge.alloc().init()

        def _make_configuration_with_client_mode():
            """Build a WKWebViewConfiguration with a documentStart user
            script that sets window.__MIRU_CLIENT_MODE__ = true before any
            page JS executes, plus a MiruDesktop bridge for LOCAL Flask
            access without going through NSURLSession's system proxy."""
            wk_config = WKWebViewConfiguration.alloc().init()
            ucc = WKUserContentController.alloc().init()
            # Client-mode flag (existing)
            script = WKUserScript.alloc() \
                .initWithSource_injectionTime_forMainFrameOnly_(
                    _CLIENT_MODE_JS,
                    WKUserScriptInjectionTimeAtDocumentStart,
                    False,
                )
            ucc.addUserScript_(script)
            # MiruDesktop bridge script (new, also documentStart so that
            # window.MiruDesktop is ready before any page JS that wants to
            # call it).
            bridge_script = WKUserScript.alloc() \
                .initWithSource_injectionTime_forMainFrameOnly_(
                    _MIRU_DESKTOP_BRIDGE_JS,
                    WKUserScriptInjectionTimeAtDocumentStart,
                    False,
                )
            ucc.addUserScript_(bridge_script)
            # Register Python message handler for the bridge.
            ucc.addScriptMessageHandler_name_(
                _state["miru_desktop_bridge"], "miruDesktop")
            wk_config.setUserContentController_(ucc)
            return wk_config

        # --- Helper: create the single WKWebView window, or show the existing
        # one while the current app runtime is alive.
        def _open_webui():
            ns_app = _state["ns_app"]
            win = _state["webui_window"]
            if win is not None:
                # Fast path: window exists (possibly hidden) → show it.
                try:
                    win.makeKeyAndOrderFront_(None)
                    ns_app.activateIgnoringOtherApps_(True)
                    # Re-inject client-mode tag in case the page was reloaded
                    wv = win.contentView()
                    if wv and hasattr(wv, 'evaluateJavaScript_completionHandler_'):
                        wv.evaluateJavaScript_completionHandler_(
                            _CLIENT_MODE_JS, None)
                    return
                except Exception:
                    # Fall through to recreate — should not happen in practice
                    _state["webui_window"] = None

            frame = NSMakeRect(100, 100, 1200, 820)
            style = (1 << 0 | 1 << 1 | 1 << 2 | 1 << 3)
            win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                frame, style, NSBackingStoreBuffered, False)
            win.setTitle_("Miru")
            win.setReleasedWhenClosed_(False)  # prevent ObjC dealloc on close
            win.center()
            win.setDelegate_(controller)  # controller handles windowShouldClose_

            # Use a configuration with a documentStart user script so the
            # client-mode flag is available before any page JS runs.
            wk_config = _make_configuration_with_client_mode()
            wv = WKWebView.alloc().initWithFrame_configuration_(
                win.contentView().bounds(), wk_config)
            wv.setAutoresizingMask_(0x12)  # flexible width + height
            wv.setNavigationDelegate_(controller)  # belt-and-braces re-inject
            wv.setUIDelegate_(controller)  # file input / native open panel

            # If credentials arrived after first window creation, always use
            # the latest private server URL + token. Client mode should only
            # serve /login locally; authenticated app traffic belongs on the
            # private server encoded by the invitation code.
            latest_cfg = _app_mod._client_mode_config or {}
            target = _webview_target_from_config(latest_cfg, int(time.time()))
            url = NSURL.URLWithString_(target)
            _state["webui_app_loaded"] = False
            _state["webui_app_ready"] = False
            wv.loadRequest_(NSURLRequest.requestWithURL_(url))
            win.setContentView_(wv)
            win.makeKeyAndOrderFront_(None)

            ns_app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
            ns_app.activateIgnoringOtherApps_(True)

            _state["webui_window"] = win

        # --- Hard-quit: stop sensor + pet + Flask, then exit process. ---
        # Used by Dock/Cmd+Q and the red window close button. Miru's Mac app
        # lifecycle is intentionally simple: closing the app stops everything
        # local, and the next launch starts a fresh visible pet.
        def _hard_quit():
            try:
                # 1. Stop the ScreenSensor loop immediately so no more
                #    screenshots are taken during the shutdown window.
                try:
                    import sensor as _sensor_mod
                    if getattr(_sensor_mod, "_instance", None) is not None:
                        _sensor_mod._instance.set_enabled(False)
                        _sensor_mod._instance.stop()
                except Exception as e:
                    print(f"[Launcher] sensor.stop() failed: {e}")
                # 2. Kill the desktop pet sidecar process.
                try:
                    _app_mod._kill_pet()
                except Exception:
                    pass
                # 2b. Orphan-sweep fallback: _kill_pet only targets the pet
                #     that _this_ Miru instance launched (via Popen handle or
                #     on-disk PID file). If a prior Miru crashed mid-session
                #     its pet became an orphan and still holds a registration
                #     on the global hotkey — kill by install-path name so
                #     "完全退出" really stops *everything* pet-related.
                #     Scope: installed DMG path only. Dev cargo builds are
                #     the developer's responsibility (clean_mac_miru.sh).
                try:
                    import subprocess as _sp
                    _sp.run(["pkill", "-KILL", "-f",
                             "Miru.app/Contents/MacOS/miru-pet"],
                            check=False, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
                except Exception:
                    pass
                # 3. Also nuke any child PIDs recorded on disk (summarizer,
                #    background workers) via the PID file written at boot.
                try:
                    from pathlib import Path as _P
                    pid_file = _P(_APP_SUPPORT) / "data" / ".child_pids"
                    if pid_file.exists():
                        for _line in pid_file.read_text().splitlines():
                            try:
                                os.kill(int(_line.strip()), 15)
                            except Exception:
                                pass
                        try: pid_file.unlink()
                        except Exception: pass
                except Exception:
                    pass
            finally:
                # 4. Force-exit the whole python process. os._exit bypasses
                #    daemon-thread cleanup and atexit, which is what we want
                #    since any running VLM HTTP request is just screen data
                #    we no longer care about.
                os._exit(0)

        # --- Launch the Tauri pet once, as soon as Flask is ready. ---
        def _ensure_pet_launched():
            if _state["pet_launched"]:
                return
            _state["pet_launched"] = True
            _state["pet_waiting_for_app_logged"] = False
            try:
                # Kill any stale pet process from a prior crash before
                # _launch_full_stack's PID guard sees it and skips.
                try:
                    _app_mod._kill_pet()
                except Exception:
                    pass
                _app_mod._launch_full_stack()
            except Exception as e:
                print(f"[Launcher] Pet launch error: {e}")
                _state["pet_launched"] = False  # allow retry

        # --- Unified controller: app delegate + window delegate
        #     + navigation delegate + timer target ---
        class _MiruController(NSObject):
            """Single NSObject for all delegate callbacks and timer."""

            @objc.python_method
            def _webview_is_on_app_page(self, webview):
                try:
                    current = webview.URL()
                    if current is None:
                        return False
                    value = str(current.absoluteString() or "")
                    return "/app" in value
                except Exception:
                    return False

            @objc.python_method
            def _pet_launch_allowed(self):
                if not _app_mod._pet_ready_event.is_set():
                    return False
                if _state["webui_app_loaded"] and _state["webui_app_ready"]:
                    return True
                if not _state["pet_waiting_for_app_logged"]:
                    print("[Launcher] Pet ready; waiting for /app bootstrap")
                    _state["pet_waiting_for_app_logged"] = True
                return False

            # App delegate: the red close button already performs _hard_quit.
            # Keep this true as a safety net if AppKit closes the last window
            # through another path.
            @objc.typedSelector(b'B@:@')
            def applicationShouldTerminateAfterLastWindowClosed_(self, app):
                return True

            # App delegate: dock icon click → bring existing window forward
            # (no WebView recreation — near-instant)
            @objc.typedSelector(b'B@:@B')
            def applicationShouldHandleReopen_hasVisibleWindows_(self, app, flag):
                print("[Launcher] Dock icon clicked, flask_ready=%s"
                      % _state["flask_ready"])
                if _state["flask_ready"]:
                    _open_webui()
                return True

            # App delegate: cleanup on quit (Cmd+Q / Dock "Quit Miru" /
            # menu Quit). We intercept *before* NSApp exits to ensure the
            # screen-capture sensor is stopped — otherwise a daemon thread
            # could still fire one more screencapture call during Python's
            # shutdown sequence.
            def applicationWillTerminate_(self, notification):
                _hard_quit()  # does sensor.stop + kill pet + os._exit

            # Window delegate: red × means full quit. Do not keep the pet,
            # sensor, or local Flask running after the user closes Miru.
            @objc.typedSelector(b'B@:@')
            def windowShouldClose_(self, sender):
                _hard_quit()
                return True  # unreachable, os._exit fired

            # WKNavigationDelegate: re-inject client-mode tag after each
            # navigation (login → /app, SPA route changes, reloads).
            def webView_didFinishNavigation_(self, webview, navigation):
                _state["webui_app_loaded"] = self._webview_is_on_app_page(webview)
                if not _state["webui_app_loaded"]:
                    _state["webui_app_ready"] = False
                    _state["pet_waiting_for_app_logged"] = False
                try:
                    webview.evaluateJavaScript_completionHandler_(
                        _CLIENT_MODE_JS, None)
                except Exception:
                    pass

            # WKUIDelegate: native file picker for <input type="file">.
            # Without this callback, the first-run self-server wizard shows a
            # Browse button but WKWebView never opens the macOS file panel.
            def webView_runOpenPanelWithParameters_initiatedByFrame_completionHandler_(
                    self, webview, parameters, frame, completionHandler):
                panel = NSOpenPanel.openPanel()
                panel.setCanChooseFiles_(True)
                panel.setCanChooseDirectories_(False)
                try:
                    panel.setAllowsMultipleSelection_(
                        bool(parameters.allowsMultipleSelection()))
                except Exception:
                    panel.setAllowsMultipleSelection_(False)
                if panel.runModal() == NSModalResponseOK:
                    completionHandler(panel.URLs())
                else:
                    completionHandler(None)

            # NSTimer callback: poll Flask readiness + cross-thread flags
            def periodicCheck_(self, timer):
                if not _state["flask_ready"]:
                    try:
                        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                            s.settimeout(0.1)
                            if s.connect_ex(("127.0.0.1", 5001)) == 0:
                                _state["flask_ready"] = True
                                _open_webui()
                                # NB: Pet is no longer launched here — wait
                                # until app.py's `_pet_ready_event` fires,
                                # which happens only after the user's token
                                # is verified against the VPS. This avoids
                                # the screen-recording prompt firing on the
                                # invitation-code screen.
                    except Exception:
                        pass

                if _app_mod._open_webui_requested.is_set():
                    _app_mod._open_webui_requested.clear()
                    if _state["flask_ready"]:
                        _open_webui()

                # Pet readiness gate — fires once verification completes,
                # whether via cached token boot or fresh /api/auth/login.
                if _app_mod._pet_ready_event.is_set() and not _state["pet_launched"]:
                    if self._pet_launch_allowed():
                        _ensure_pet_launched()

                # Token invalidated → kill pet so user only sees /login.
                if _app_mod._pet_should_hide_event.is_set():
                    _app_mod._pet_should_hide_event.clear()
                    try:
                        _app_mod._kill_pet()
                    except Exception:
                        pass
                    _state["pet_launched"] = False
                    # Clear ready flag so a fresh re-login is required to
                    # bring the pet back.
                    _app_mod._pet_ready_event.clear()
                    # Bounce the WebView (if open) over to /login so the
                    # user can re-enter their invitation code. Without
                    # this they'd see a stale /app page that 401s on every
                    # request.
                    try:
                        win = _state["webui_window"]
                        if win is not None:
                            wv = win.contentView()
                            if wv and hasattr(wv, "loadRequest_"):
                                _login_url = NSURL.URLWithString_(
                                    "http://localhost:5001/login")
                                _state["webui_app_loaded"] = False
                                _state["webui_app_ready"] = False
                                wv.loadRequest_(
                                    NSURLRequest.requestWithURL_(_login_url))
                                win.makeKeyAndOrderFront_(None)
                    except Exception:
                        pass

                # Legacy flag — still handle it in case app.py sets it,
                # but gated behind _pet_ready_event to prevent premature
                # launch before the user has verified their token.
                if _app_mod._launch_pet_requested.is_set():
                    if self._pet_launch_allowed():
                        _app_mod._launch_pet_requested.clear()
                        _ensure_pet_launched()

        controller = _MiruController.alloc().init()

        def _handle_term(signum, frame):
            _hard_quit()  # sensor.stop + kill pet + os._exit

        signal.signal(signal.SIGTERM, _handle_term)
        signal.signal(signal.SIGINT, _handle_term)

        # Start Flask in background thread — empty creds are OK, /login handles entry
        t = threading.Thread(
            target=_app_mod.run_client_mode,
            args=(server_url, auth_token),
            daemon=True,
        )
        t.start()

        # Sync model data in background (only meaningful if we already have creds)
        if server_url and auth_token and setup_complete:
            threading.Thread(target=_sync_model_data, args=(cfg,), daemon=True).start()

        # --- macOS event loop (proper NSApp.run()) ---
        ns_app = NSApplication.sharedApplication()
        _state["ns_app"] = ns_app
        ns_app.setDelegate_(controller)
        # Start as Regular app so dock icon is visible from the beginning
        ns_app.setActivationPolicy_(NSApplicationActivationPolicyRegular)

        # Build the main menu — critical so Cmd+V (and Cmd+C/X/A/Z) reach
        # the WKWebView's first responder instead of being swallowed.
        _build_main_menu(ns_app)

        # Repeating timer for Flask readiness + flag checks
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.3, controller, b"periodicCheck:", None, True)

        # Standard macOS event loop — properly processes all events
        ns_app.run()
    else:
        # Terminal: block main thread, same as `python3 app.py --client`
        # Sync model data in background
        import threading
        if server_url and auth_token and setup_complete:
            threading.Thread(target=_sync_model_data, args=(cfg,), daemon=True).start()
        _app_mod.run_client_mode(server_url, auth_token)


if __name__ == "__main__":
    main()
