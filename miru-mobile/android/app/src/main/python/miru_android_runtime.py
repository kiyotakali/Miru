from __future__ import annotations

import importlib
import json
import os
import platform
import socket
import sys
import threading
import time
from pathlib import Path


_REQUIRED_MODULES = (
    "flask",
    "flask_compress",
    "PIL",
    "openai",
    "httpx",
    "pydantic",
    "requests",
    "paramiko",
    "cryptography",
    "qrcode",
)

_runtime_lock = threading.RLock()
_http_server = None
_http_thread: threading.Thread | None = None
_runtime_port = 5001
_runtime_files_dir = ""


def _install_pydantic_v2_compat() -> None:
    """Expose the small Pydantic v2 surface used by Miru on Pydantic v1.

    Chaquopy currently has no Android wheel for pydantic-core, so the APK uses
    the pure-Python Pydantic 1 runtime. Desktop and Docker keep their native
    Pydantic 2 dependency and never execute this adapter.
    """
    import pydantic

    if hasattr(pydantic, "field_validator"):
        return

    from pydantic import root_validator, validator

    if not hasattr(pydantic.BaseModel, "model_validate"):
        pydantic.BaseModel.model_validate = classmethod(
            lambda cls, value: cls.parse_obj(value)
        )
    if not hasattr(pydantic.BaseModel, "model_dump"):
        pydantic.BaseModel.model_dump = lambda self, *args, **kwargs: self.dict(
            *args, **kwargs
        )

    def field_validator(*fields, mode="after", **kwargs):
        def decorate(func):
            raw = func.__func__ if isinstance(func, classmethod) else func
            return validator(
                *fields,
                pre=(mode == "before"),
                allow_reuse=True,
                **kwargs,
            )(raw)

        return decorate

    def model_validator(*, mode="after"):
        def decorate(func):
            raw = func.__func__ if isinstance(func, classmethod) else func
            if mode == "before":
                return root_validator(pre=True, allow_reuse=True)(raw)

            @root_validator(pre=False, allow_reuse=True)
            def validate_after(cls, values):
                instance = cls.construct(**values)
                result = raw(instance)
                if isinstance(result, cls):
                    return dict(result.__dict__)
                if isinstance(result, dict):
                    return result
                return values

            return validate_after

        return decorate

    pydantic.field_validator = field_validator
    pydantic.model_validator = model_validator


def probe(files_dir: str) -> str:
    """Verify the embedded runtime without starting Miru services."""
    root = Path(files_dir).resolve() / "miru-runtime-probe"
    root.mkdir(parents=True, exist_ok=True)
    marker = root / "write-test.json"
    marker.write_text(json.dumps({"ok": True}), encoding="utf-8")

    imported: dict[str, str] = {}
    for module_name in _REQUIRED_MODULES:
        module = importlib.import_module(module_name)
        imported[module_name] = str(getattr(module, "__version__", "ok"))

    payload = {
        "ok": True,
        "python": sys.version.split()[0],
        "platform": platform.system().lower(),
        "files_dir": str(Path(files_dir).resolve()),
        "home": os.environ.get("HOME", ""),
        "write_test": json.loads(marker.read_text(encoding="utf-8")),
        "imports": imported,
    }
    return json.dumps(payload, ensure_ascii=True, sort_keys=True)


def _configure_environment(files_dir: str, client_secret: str = "") -> tuple[Path, Path]:
    root = Path(files_dir).resolve() / "miru-local"
    data_dir = root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    os.environ["DATA_DIR"] = str(data_dir)
    os.environ["MIRU_ANDROID"] = "1"
    os.environ["MIRU_HEADLESS"] = "1"
    os.environ["MIRU_CLIENT_CONFIG_PATH"] = str(root / "config.json")
    if client_secret:
        os.environ["MIRU_ANDROID_CLIENT_SECRET"] = client_secret
    os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
    os.environ.setdefault("no_proxy", "127.0.0.1,localhost")
    _install_pydantic_v2_compat()
    return root, data_dir


def probe_miru_core(files_dir: str) -> str:
    """Import the packaged Miru backend under Android-safe environment flags."""
    _, data_dir = _configure_environment(files_dir)

    import app as miru_app
    import memory_prompts_v2
    import memory_prompts_v3

    schema_probe = memory_prompts_v3.ScreenSemanticGateOutput.model_validate(
        {"should_continue": True}
    )
    payload = {
        "ok": True,
        "app_file": str(Path(miru_app.__file__).resolve()),
        "template_folder": str(Path(miru_app.app.template_folder or "")),
        "routes": len(miru_app.app.url_map._rules),
        "data_dir": str(data_dir),
        "schemas": {
            "v2": bool(memory_prompts_v2.BaseModel),
            "v3_gate": bool(schema_probe.should_continue),
        },
    }
    return json.dumps(payload, ensure_ascii=True, sort_keys=True)


def _load_client_config(miru_app) -> dict:
    config_path = miru_app._launcher_config_path()
    if not config_path.exists():
        return {}
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _runtime_config_from_disk(miru_app) -> dict:
    saved = _load_client_config(miru_app)
    return {
        "server_url": str(saved.get("server_url") or "").rstrip("/"),
        "auth_token": str(saved.get("auth_token") or ""),
        "user_id": str(saved.get("user_id") or ""),
        "mode": str(saved.get("mode") or ""),
        "local_only": bool(saved.get("local_only")),
        "setup_complete": miru_app._setup_complete_from_launcher_config(saved),
    }


def _wait_for_loopback(port: int, cancel_event: threading.Event,
                       timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not cancel_event.is_set():
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return True
        except OSError:
            cancel_event.wait(0.1)
    return False


def _android_deferred_setup(miru_app, server_url: str, token: str,
                            generation: int,
                            cancel_event: threading.Event) -> None:
    """Activate shared backend loops without starting desktop-only services."""
    if not server_url or not token:
        return
    if not _wait_for_loopback(_runtime_port, cancel_event):
        print("[MiruAndroid] loopback backend did not become ready", flush=True)
        return
    if not miru_app._client_session_is_current(
            generation, cancel_event, server_url=server_url, token=token):
        return

    try:
        import requests
        response = requests.get(
            f"{server_url.rstrip('/')}/api/auth/me",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
    except Exception as exc:
        print(f"[MiruAndroid] user verification failed: {exc}", flush=True)
        return

    if not miru_app._client_session_is_current(
            generation, cancel_event, server_url=server_url, token=token):
        return
    if response.status_code in (401, 403):
        miru_app._force_relogin(
            f"Android /api/auth/me returned {response.status_code}",
            expected_generation=generation,
        )
        return
    if response.status_code != 200:
        print(
            f"[MiruAndroid] /api/auth/me returned {response.status_code}",
            flush=True,
        )
        return

    user_id = str((response.json() or {}).get("user_id") or "").strip()
    if not user_id:
        print("[MiruAndroid] /api/auth/me returned no user_id", flush=True)
        return

    with miru_app._client_runtime_lock:
        if (generation != miru_app._client_runtime_generation
                or cancel_event is not miru_app._client_runtime_cancel_event
                or cancel_event.is_set()):
            return
        miru_app._client_mode_config["user_id"] = user_id

    saved = _load_client_config(miru_app)
    if saved.get("user_id") != user_id:
        saved["user_id"] = user_id
        miru_app._save_launcher_config(saved)

    if miru_app._is_local_single_device_mode():
        miru_app._start_local_single_device_backend_services_once(
            port=_runtime_port,
            expected_generation=generation,
            cancel_event=cancel_event,
        )
    print(
        f"[MiruAndroid] session ready: mode={miru_app._client_mode_config.get('mode') or 'remote'} "
        f"user={user_id}",
        flush=True,
    )


def _install_android_trigger(miru_app) -> None:
    def trigger(server_url: str, token: str) -> None:
        generation, cancel_event = miru_app._client_runtime_snapshot()
        threading.Thread(
            target=_android_deferred_setup,
            args=(miru_app, server_url, token, generation, cancel_event),
            name=f"MiruAndroidSetup-{generation}",
            daemon=True,
        ).start()

    miru_app._deferred_client_setup_trigger = trigger


def _launch_url(miru_app, port: int) -> str:
    cfg = dict(miru_app._client_mode_config or {})
    token = str(cfg.get("auth_token") or "")
    server_url = str(cfg.get("server_url") or "").rstrip("/")
    if token and server_url and bool(cfg.get("setup_complete")):
        from urllib.parse import quote
        return f"{server_url}/app?token={quote(token)}&android=1"
    return f"http://127.0.0.1:{port}/login?android=1"


def start(files_dir: str, port: int = 5001, client_secret: str = "") -> str:
    """Start the Android-private loopback backend and return its launch URL."""
    global _http_server, _http_thread, _runtime_port, _runtime_files_dir
    with _runtime_lock:
        _configure_environment(files_dir, client_secret)
        _runtime_files_dir = str(Path(files_dir).resolve())
        _runtime_port = int(port)

        import app as miru_app
        miru_app._is_client_mode = True
        runtime_cfg = _runtime_config_from_disk(miru_app)
        generation, cancel_event = miru_app._replace_client_runtime_config(runtime_cfg)
        _install_android_trigger(miru_app)

        if _http_server is None:
            from werkzeug.serving import make_server
            _http_server = make_server(
                "127.0.0.1", _runtime_port, miru_app.app, threaded=True
            )
            _http_thread = threading.Thread(
                target=_http_server.serve_forever,
                name="MiruAndroidLoopback",
                daemon=True,
            )
            _http_thread.start()

        if not _wait_for_loopback(_runtime_port, cancel_event, timeout=5):
            raise RuntimeError("Android local backend failed to bind loopback port")

        if (runtime_cfg.get("server_url") and runtime_cfg.get("auth_token")
                and runtime_cfg.get("setup_complete")):
            miru_app._deferred_client_setup_trigger(
                runtime_cfg["server_url"], runtime_cfg["auth_token"]
            )

        payload = {
            "ok": True,
            "port": _runtime_port,
            "base_url": f"http://127.0.0.1:{_runtime_port}",
            "launch_url": _launch_url(miru_app, _runtime_port),
            "generation": generation,
        }
        return json.dumps(payload, ensure_ascii=True, sort_keys=True)


def get_state() -> str:
    with _runtime_lock:
        running = bool(_http_thread and _http_thread.is_alive())
        launch_url = ""
        mode = ""
        setup_complete = False
        try:
            import app as miru_app
            launch_url = _launch_url(miru_app, _runtime_port) if running else ""
            cfg = miru_app._client_mode_config or {}
            mode = str(cfg.get("mode") or "")
            setup_complete = bool(cfg.get("setup_complete"))
        except Exception:
            pass
        return json.dumps({
            "ok": running,
            "running": running,
            "port": _runtime_port,
            "launch_url": launch_url,
            "mode": mode,
            "setup_complete": setup_complete,
        }, ensure_ascii=True, sort_keys=True)


def clear_client_session() -> str:
    """Clear only the selected session; local account data remains on disk."""
    import app as miru_app
    cleared = miru_app._force_relogin("Android account switch")
    return json.dumps({
        "ok": bool(cleared),
        "launch_url": f"http://127.0.0.1:{_runtime_port}/login?android=1",
    }, ensure_ascii=True, sort_keys=True)


def stop() -> str:
    global _http_server, _http_thread
    with _runtime_lock:
        try:
            import app as miru_app
            miru_app._force_relogin("Android runtime stop")
        except Exception:
            pass
        if _http_server is not None:
            _http_server.shutdown()
            _http_server.server_close()
        if _http_thread is not None:
            _http_thread.join(timeout=3)
        _http_server = None
        _http_thread = None
    return json.dumps({"ok": True}, ensure_ascii=True)
