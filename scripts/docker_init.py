#!/usr/bin/env python3
"""Initialize a single-user Miru Docker server.

This script runs before Flask starts. It is intentionally small and
idempotent: data lives on /opt/miru/data, so container restarts must reuse the
same local invitation record and simply re-compose the current long code from
SERVER_IP/SERVER_PORT.
"""

from __future__ import annotations

import ipaddress
import json
import os
import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import auth  # noqa: E402


def _fail(message: str) -> None:
    print(f"[docker-init] ERROR: {message}", file=sys.stderr)
    sys.exit(2)


def _server_address() -> tuple[str, int]:
    raw_ip = os.environ.get("SERVER_IP", "").strip()
    if not raw_ip:
        _fail("SERVER_IP is required. Use 127.0.0.1 for local browser tests, "
              "or the Mac LAN IPv4 for Android-on-Wi-Fi tests.")
    try:
        ip = str(ipaddress.IPv4Address(raw_ip))
    except Exception:
        _fail(f"SERVER_IP must be an IPv4 address, got {raw_ip!r}")

    raw_port = os.environ.get("SERVER_PORT", os.environ.get("PORT", "5001"))
    try:
        port = int(raw_port)
    except Exception:
        _fail(f"SERVER_PORT must be an integer, got {raw_port!r}")
    if port < 1 or port > 65535:
        _fail("SERVER_PORT must be between 1 and 65535")
    return ip, port


def _bootstrap_path() -> Path:
    data_dir = Path(os.environ.get("DATA_DIR", str(ROOT / "data")))
    return data_dir / "_admin" / "docker_bootstrap.json"


def _load_bootstrap() -> dict:
    path = _bootstrap_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_bootstrap(payload: dict) -> None:
    path = _bootstrap_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _usable_local_code(local_code: str, invitations: dict) -> bool:
    if not local_code or not local_code.startswith("MIRU-"):
        return False
    inv = invitations.get(local_code)
    if not isinstance(inv, dict):
        return False
    return not bool(inv.get("revoked"))


def _choose_or_create_local_code(ip: str, port: int) -> str:
    invitations = auth.list_invitations()
    bootstrap = _load_bootstrap()
    preferred = str(bootstrap.get("local_code") or "")
    if _usable_local_code(preferred, invitations):
        return preferred

    for code, info in invitations.items():
        if _usable_local_code(code, invitations):
            return code

    full_codes = auth.generate_invitation_codes(
        1,
        created_by="docker-init",
        ip=ip,
        port=port,
        assigned_to=os.environ.get("MIRU_SINGLE_USER_LABEL", "docker-single-user"),
    )
    parsed = auth.parse_invitation_code(full_codes[0])
    if not parsed:
        _fail("generated invitation code could not be parsed")
    return parsed["local_code"]


def main() -> int:
    data_dir = Path(os.environ.get("DATA_DIR", str(ROOT / "data")))
    log_dir = Path(os.environ.get("LOG_DIR", "/opt/miru/logs"))
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "_admin").mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    ip, port = _server_address()
    local_code = _choose_or_create_local_code(ip, port)
    user_part = local_code.replace("MIRU-", "", 1)
    full_code = f"MIRU-{auth.encode_server(ip, port)}-{user_part}"

    _save_bootstrap({
        "version": 1,
        "local_code": local_code,
        "current_full_code": full_code,
        "server_ip": ip,
        "server_port": port,
        "data_dir": str(data_dir),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    })

    print("[docker-init] Miru private server initialized")
    print(f"[docker-init] DATA_DIR={data_dir}")
    print(f"[docker-init] LOG_DIR={log_dir}")
    print(f"[docker-init] SERVER={ip}:{port}")
    print(f"[docker-init] MIRU_INVITATION_CODE={full_code}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
