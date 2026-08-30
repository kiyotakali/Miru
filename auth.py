"""Multi-user authentication with invitation codes.

Invitation code format: MIRU-<server 10 chars>-<user 6 chars>
  Server part encodes IPv4:port into 6 bytes -> 10 base31 chars.
  User part is random 6 chars.

Login flow:
1. Admin generates invitation codes via /api/admin/invitations
2. User enters code -> client decodes server address -> connects -> POST /api/auth/login
3. First use: creates user account + returns token
4. Re-use same code: returns same user's token (re-login)

Admin: local requests or legacy auth.json token -> full access.
User data: each user gets data/users/<user_id>/ directory.
"""

import json
import os
import secrets
import shutil
import stat
import struct
import threading
import ipaddress
from datetime import datetime

from flask import abort, g, request

_BASE_DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
_ADMIN_DIR = os.path.join(_BASE_DATA_DIR, "_admin")
_lock = threading.Lock()

# 31 unambiguous chars (no O/0/I/1/L)
_CODE_CHARS = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_BASE = len(_CODE_CHARS)  # 31
LOCAL_SINGLE_DEVICE_ACCOUNT_TYPE = "local_single_device"


# ---------------------------------------------------------------------------
# Server address encoding: IPv4:port <-> 10 base31 chars
# ---------------------------------------------------------------------------
#
# Obfuscation (NOT encryption):
#   The 6 raw bytes (4-byte IP + 2-byte port) are XOR'd with a fixed mask
#   before base31 encoding. This stops the trivial "feed base31 chars into
#   any decoder online → see plaintext IP" attack — without the mask,
#   decoders return scrambled garbage that doesn't look like an IP.
#
#   This is obfuscation, not security: anyone who can read this source file
#   (or `strings` the DMG/APK) can recover the mask and de-obfuscate.
#   Threat model is "casual reverse-engineering of leaked invitation codes",
#   not "determined attacker". A future v2 can move to opaque server IDs
#   resolved by an explicit directory service, but v1 intentionally keeps
#   direct IPv4+port in the long invitation code.
#
#   Mask was generated once via `secrets.token_hex(6)` and is never rotated.
#   Rotating would invalidate every previously-issued invitation code, since
#   server and clients (Mac DMG / Android APK / web) all hardcode the same
#   mask in their bundled copy of this file.
_OBFUSCATION_MASK = bytes.fromhex("9c4ab73e51f8")  # 6 bytes, do not change


def _apply_mask(data: bytes) -> bytes:
    """XOR the 6 input bytes with the obfuscation mask. Self-inverse."""
    return bytes(b ^ m for b, m in zip(data, _OBFUSCATION_MASK))


def encode_server(ip: str, port: int = 5001) -> str:
    """Encode IP:port into 10-char string using base31 alphabet (obfuscated)."""
    ip_addr = ipaddress.IPv4Address(str(ip).strip())
    port = int(port)
    if port < 1 or port > 65535:
        raise ValueError("port must be between 1 and 65535")
    octets = [int(x) for x in str(ip_addr).split(".")]
    raw = struct.pack("!4BH", *octets, port)  # 6 bytes big-endian
    raw = _apply_mask(raw)
    num = int.from_bytes(raw, "big")
    chars = []
    for _ in range(10):
        chars.append(_CODE_CHARS[num % _BASE])
        num //= _BASE
    return "".join(reversed(chars))


def decode_server(encoded: str) -> tuple[str, int] | None:
    """Decode 10-char string back to (ip, port). Returns None on failure."""
    if len(encoded) != 10:
        return None
    try:
        num = 0
        for ch in encoded:
            idx = _CODE_CHARS.index(ch)
            num = num * _BASE + idx
        # 31^10 ≈ 8.2e14 > 256^6 ≈ 2.8e14, so a random 10-char base31 string
        # has roughly 1/3 chance of decoding to a valid 6-byte big-endian
        # integer. Reject anything that overflows so garbage codes fail fast.
        if num >= (1 << 48):
            return None
        raw = num.to_bytes(6, "big")
        raw = _apply_mask(raw)
        octets = struct.unpack("!4BH", raw)
        ip = f"{octets[0]}.{octets[1]}.{octets[2]}.{octets[3]}"
        port = octets[4]
        return ip, port
    except (ValueError, struct.error, OverflowError):
        return None


def _normalize_full_invitation_code(code: str) -> str | None:
    """Return canonical full code or None.

    Public login accepts only the long form:
        MIRU-<10 char server>-<6 char invite>

    Separators are tolerated so copy/paste remains friendly, but short
    local-only codes are not a public login shape anymore.
    """
    raw = (code or "").strip().upper().replace(" ", "")
    if not raw.startswith("MIRU-"):
        return None
    body = raw[5:].replace("-", "")
    if len(body) != 16:
        return None
    for ch in body:
        if ch not in _CODE_CHARS:
            return None
    return f"MIRU-{body[:10]}-{body[10:]}"


def parse_invitation_code(code: str) -> dict | None:
    """Parse a full invitation code into its components.

    Input:  "MIRU-XXXXXXXXXX-YYYYYY" or "MIRU-XXXXXXXXXXXYYYYY"
    Output: {"server": "1.2.3.4:5001", "ip": "1.2.3.4", "port": 5001,
             "user_code": "YYYYYY", "local_code": "MIRU-YYYYYY"}
    """
    code = _normalize_full_invitation_code(code)
    if not code:
        return None
    body = code[5:].replace("-", "")
    server_part = body[:10]
    user_part = body[10:]
    result = decode_server(server_part)
    if not result:
        return None
    ip, port = result
    return {
        "ip": ip,
        "port": port,
        "server": f"{ip}:{port}",
        "user_code": user_part,
        "local_code": f"MIRU-{user_part}",
    }


def server_url_from_invitation_code(code: str, *, scheme: str = "http") -> str | None:
    """Return the private server URL encoded in a full invitation code."""
    parsed = parse_invitation_code(code)
    if not parsed:
        return None
    scheme = (scheme or "http").strip().rstrip(":/")
    if scheme not in {"http", "https"}:
        raise ValueError("scheme must be http or https")
    return f"{scheme}://{parsed['ip']}:{parsed['port']}"


def _configured_server_address() -> tuple[str, int] | None:
    """Return this deployment's public IPv4+port, if configured.

    Private-server v1 requires SERVER_IP to be a real public-facing IPv4.
    SERVER_PORT is the public port that clients should dial; it may differ
    from Flask's internal PORT in future Docker/proxy setups.
    """
    ip = os.environ.get("SERVER_IP", "").strip()
    if not ip:
        return None
    try:
        ip = str(ipaddress.IPv4Address(ip))
    except Exception:
        return None
    try:
        port = int(os.environ.get("SERVER_PORT", os.environ.get("PORT", "5001")))
    except (ValueError, TypeError):
        return None
    if port < 1 or port > 65535:
        return None
    return ip, port


def invitation_matches_this_server(code: str) -> bool:
    """Whether a full invitation code is addressed to this deployment."""
    parsed = parse_invitation_code(code)
    configured = _configured_server_address()
    if not parsed or not configured:
        return False
    return parsed["ip"] == configured[0] and parsed["port"] == configured[1]


def get_user_invitation_info(user_id: str) -> dict | None:
    """Return the saved long invitation code for the current user.

    This intentionally reads from persisted user/account data. It does not
    synthesize a fresh code from the deployment address, because a user should
    see the exact login credential that was issued to their account.
    """
    user = get_user(user_id)
    if not user or user.get("status") != "active":
        return None

    if user.get("account_type") == LOCAL_SINGLE_DEVICE_ACCOUNT_TYPE:
        return {
            "mode": "local",
            "local_only": True,
            "invitation_code": "",
            "local_invitation_code": "",
            "server_url": "",
        }

    full_code = user.get("full_invitation_code") or ""
    try:
        manifest = _load_json(account_manifest_path(user_id))
        if isinstance(manifest, dict):
            full_code = manifest.get("full_invitation_code") or full_code
    except Exception:
        pass

    full_code = _normalize_full_invitation_code(full_code) or ""
    if not full_code:
        return None
    parsed = parse_invitation_code(full_code)
    if not parsed:
        return None
    local_code = user.get("invitation_code") or ""
    if local_code and parsed["local_code"] != local_code:
        return None
    return {
        "invitation_code": full_code,
        "local_invitation_code": parsed["local_code"],
        "server_url": f"http://{parsed['ip']}:{parsed['port']}",
    }


def _ensure_admin_dir():
    os.makedirs(_ADMIN_DIR, exist_ok=True)


def _users_path():
    return os.path.join(_ADMIN_DIR, "users.json")


def _invitations_path():
    return os.path.join(_ADMIN_DIR, "invitations.json")


def _legacy_auth_path():
    return os.path.join(_BASE_DATA_DIR, "auth.json")


def account_manifest_path(user_id: str) -> str:
    return os.path.join(get_user_data_dir(user_id), "account_manifest.json")


def _load_json(path):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_json(path, data):
    _ensure_admin_dir()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _new_user_id(users: dict, *, max_attempts: int = 32) -> str:
    """Generate a user id that has no metadata entry and no data dir.

    The dir check matters for privacy: if a prior delete left an orphan
    data/users/<uid>/ directory behind, a new account must never inherit it.
    """
    users_root = os.path.join(_BASE_DATA_DIR, "users")
    for _ in range(max_attempts):
        uid = "u_" + secrets.token_hex(6)
        if uid in users:
            continue
        if os.path.exists(os.path.join(users_root, uid)):
            continue
        return uid
    raise RuntimeError("failed to generate a fresh user id")


def _write_account_manifest(user_id: str, user: dict):
    """Write a per-user manifest that binds a directory to exactly one uid."""
    if not user_id or user_id == "_admin":
        raise ValueError("_admin has no account manifest")
    user_dir = get_user_data_dir(user_id)
    os.makedirs(user_dir, exist_ok=True)
    manifest = {
        "version": 1,
        "user_id": user_id,
        "invitation_code": user.get("invitation_code", ""),
        "full_invitation_code": user.get("full_invitation_code", ""),
        "account_type": user.get("account_type", ""),
        "created_at": user.get("created_at", ""),
        "status_at_write": user.get("status", ""),
        "written_at": datetime.now().isoformat(),
    }
    path = os.path.join(user_dir, "account_manifest.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def ensure_account_manifest(user_id: str) -> bool:
    """Ensure data/users/<uid>/account_manifest.json exists and matches uid."""
    if not user_id or user_id == "_admin":
        return False
    users = _load_json(_users_path())
    user = users.get(user_id)
    if not user:
        return False
    try:
        path = account_manifest_path(user_id)
    except ValueError:
        return False
    if os.path.exists(path):
        try:
            data = _load_json(path)
            if data.get("user_id") == user_id:
                return True
        except Exception:
            pass
    _write_account_manifest(user_id, user)
    return True


def validate_account_manifest(user_id: str) -> bool:
    """Return True only if the user's data dir manifest matches user_id."""
    if not user_id or user_id == "_admin":
        return False
    try:
        path = account_manifest_path(user_id)
    except ValueError:
        return False
    if not os.path.exists(path):
        return ensure_account_manifest(user_id)
    try:
        data = _load_json(path)
    except Exception:
        return False
    return data.get("user_id") == user_id


# ---------------------------------------------------------------------------
# Admin token (backward compat with legacy single-user auth.json)
# ---------------------------------------------------------------------------

def get_admin_token() -> str:
    data = _load_json(_legacy_auth_path())
    if data.get("token"):
        return data["token"]
    token = secrets.token_urlsafe(32)
    os.makedirs(_BASE_DATA_DIR, exist_ok=True)
    with open(_legacy_auth_path(), "w") as f:
        json.dump({"token": token, "created_at": datetime.now().isoformat()}, f)
    print(f"[Auth] Generated admin token: {token[:8]}...")
    return token


# Keep old name for backward compat (app.py QR code generation etc.)
get_or_create_token = get_admin_token


def is_admin_token(token: str) -> bool:
    return secrets.compare_digest(token, get_admin_token())


# ---------------------------------------------------------------------------
# Invitation codes
# ---------------------------------------------------------------------------

def generate_invitation_codes(
    count: int = 1,
    created_by: str = "admin",
    ip: str | None = None,
    port: int = 5001,
    assigned_to: str | None = None,
) -> list:
    """Generate N invitation codes with embedded server address.

    Args:
        count: Number of codes to generate.
        ip: Public IPv4. If None, read SERVER_IP env. Domains are not
            valid in invitation v1.
        port: Server port (default 5001).
        assigned_to: Admin-side label ("给小明" / "for-mom"), private to admin.
                     Never exposed to the user side.

    Returns list of full codes like "MIRU-XXXXXXXXXX-YYYYYY".
    """
    if not ip:
        ip = os.environ.get("SERVER_IP", "").strip()
    if not ip:
        raise ValueError("SERVER_IP is required to generate full invitation codes")

    # Fail early with a clear error. The v1 server segment encodes IPv4+port;
    # domains/hostnames belong to a future v2 format.
    ip = str(ipaddress.IPv4Address(str(ip).strip()))
    port = int(port)
    if port < 1 or port > 65535:
        raise ValueError("port must be between 1 and 65535")

    server_encoded = encode_server(ip, port)
    codes = []
    full_codes = []
    invitations = _load_json(_invitations_path())
    with _lock:
        for _ in range(count):
            user_code = "".join(secrets.choice(_CODE_CHARS) for _ in range(6))
            local_code = f"MIRU-{user_code}"
            while local_code in invitations:
                user_code = "".join(secrets.choice(_CODE_CHARS) for _ in range(6))
                local_code = f"MIRU-{user_code}"
            invitations[local_code] = {
                "created_by": created_by,
                "created_at": datetime.now().isoformat(),
                "used_by": None,
                "used_at": None,
                "assigned_to": (assigned_to or "").strip() or None,
            }
            # Full code = server + user, with dash for readability
            full_code = f"MIRU-{server_encoded}-{user_code}"
            codes.append(local_code)
            full_codes.append(full_code)
        _save_json(_invitations_path(), invitations)
    return full_codes


def login_with_code(code: str) -> dict | None:
    """Use invitation code to login/register.

    Public login accepts only full codes (MIRU-XXXXXXXXXX-YYYYYY).
    The server segment must address this deployment's configured
    SERVER_IP/SERVER_PORT; invitations.json still stores the local
    MIRU-YYYYYY key internally.

    Returns {"token": ..., "user_id": ..., "is_new": bool} or None.
    """
    parsed = parse_invitation_code(code)
    if not parsed:
        return None
    full_code = _normalize_full_invitation_code(code)
    if not full_code:
        return None

    configured = _configured_server_address()
    if not configured:
        print("[Auth] Rejecting invitation login: SERVER_IP/SERVER_PORT not configured")
        return None
    if parsed["ip"] != configured[0] or parsed["port"] != configured[1]:
        print(
            "[Auth] Rejecting invitation login for wrong server: "
            f"code={parsed['ip']}:{parsed['port']} "
            f"configured={configured[0]}:{configured[1]}"
        )
        return None

    local_code = parsed["local_code"]

    invitations = _load_json(_invitations_path())
    users = _load_json(_users_path())

    inv = invitations.get(local_code)
    if not inv:
        return None

    with _lock:
        # Reload inside lock
        invitations = _load_json(_invitations_path())
        users = _load_json(_users_path())
        inv = invitations.get(local_code)
        if not inv:
            return None

        # Revoked (e.g. after user self-delete) → permanently unusable.
        if inv.get("revoked"):
            return None

        if inv["used_by"]:
            # Re-login: return existing user's token
            user_id = inv["used_by"]
            user = users.get(user_id)
            if user and user.get("status") == "active":
                # Legacy accounts created before full-code persistence can be
                # upgraded here because the user just presented the exact
                # long invitation code again.
                if user.get("full_invitation_code") != full_code:
                    user["full_invitation_code"] = full_code
                    _save_json(_users_path(), users)
                _write_account_manifest(user_id, user)
                return {"token": user["token"], "user_id": user_id, "is_new": False}
            return None

        # First use: create new user
        user_id = _new_user_id(users)
        token = secrets.token_urlsafe(32)
        created_at = datetime.now().isoformat()

        users[user_id] = {
            "token": token,
            "invitation_code": local_code,
            "full_invitation_code": full_code,
            "created_at": created_at,
            "status": "active",
        }
        invitations[local_code]["used_by"] = user_id
        invitations[local_code]["used_at"] = created_at

        # Create user data directory
        user_dir = os.path.join(_BASE_DATA_DIR, "users", user_id)
        if os.path.exists(user_dir):
            # Defence in depth; _new_user_id should have avoided this.
            raise RuntimeError(f"refusing to reuse existing user data dir: {user_dir}")
        os.makedirs(user_dir, exist_ok=False)
        os.makedirs(os.path.join(user_dir, "uploads"), exist_ok=True)
        _write_account_manifest(user_id, users[user_id])

        _save_json(_users_path(), users)
        _save_json(_invitations_path(), invitations)

    return {"token": token, "user_id": user_id, "is_new": True}


def get_or_create_local_single_device_user() -> dict:
    """Create or reuse the one local-only account for Mac single-device mode.

    Local mode has no invitation code. The launcher stores the returned token
    in its local config and all chat/memory/screenshot data stays in this
    machine's data/users/<uid> directory.
    """
    with _lock:
        users = _load_json(_users_path())

        for uid, user in sorted(users.items(), key=lambda item: item[1].get("created_at", "")):
            if (
                user.get("status") == "active"
                and user.get("account_type") == LOCAL_SINGLE_DEVICE_ACCOUNT_TYPE
            ):
                user_dir = os.path.join(_BASE_DATA_DIR, "users", uid)
                os.makedirs(os.path.join(user_dir, "uploads"), exist_ok=True)
                _write_account_manifest(uid, user)
                return {
                    "token": user["token"],
                    "user_id": uid,
                    "is_new": False,
                    "mode": "local",
                    "local_only": True,
                }

        user_id = _new_user_id(users)
        token = secrets.token_urlsafe(32)
        created_at = datetime.now().isoformat()
        users[user_id] = {
            "token": token,
            "invitation_code": "",
            "full_invitation_code": "",
            "account_type": LOCAL_SINGLE_DEVICE_ACCOUNT_TYPE,
            "created_at": created_at,
            "status": "active",
        }

        user_dir = os.path.join(_BASE_DATA_DIR, "users", user_id)
        if os.path.exists(user_dir):
            raise RuntimeError(f"refusing to reuse existing user data dir: {user_dir}")
        os.makedirs(os.path.join(user_dir, "uploads"), exist_ok=False)
        _write_account_manifest(user_id, users[user_id])
        _save_json(_users_path(), users)

    return {
        "token": token,
        "user_id": user_id,
        "is_new": True,
        "mode": "local",
        "local_only": True,
    }


# ---------------------------------------------------------------------------
# User lookup
# ---------------------------------------------------------------------------

def get_user_by_token(token: str):
    """Find user by token. Returns (user_id, user_dict) or (None, None)."""
    users = _load_json(_users_path())
    for uid, u in users.items():
        if u.get("status") == "active" and secrets.compare_digest(token, u["token"]):
            return uid, u
    return None, None


def get_user(user_id: str) -> dict | None:
    """Look up a user record by ID. Returns the full dict (token,
    invitation_code, status, created_at, ...) or None if not found.

    2026-05-09: introduced for /api/device/qr to return the *current user's*
    token instead of the admin token (a P0 bug — see commit message). Without
    this helper, callers had to reach into private _load_json + _users_path,
    which encouraged copy-paste and forgetting the active-status check.
    """
    users = _load_json(_users_path())
    return users.get(user_id)


def get_user_data_dir(user_id: str) -> str:
    """Return per-user data directory. _admin has NO data dir."""
    if not user_id or user_id == "_admin":
        raise ValueError("_admin has no data directory — use a real user account")
    return os.path.join(_BASE_DATA_DIR, "users", user_id)


def is_managed_user_data_dir(path: str | None) -> bool:
    """Return True when *path* is under this deployment's data/users tree."""
    if not path:
        return False
    try:
        users_root = os.path.realpath(os.path.join(_BASE_DATA_DIR, "users"))
        candidate = os.path.realpath(path)
        return candidate == users_root or candidate.startswith(users_root + os.sep)
    except Exception:
        return False


def is_active_user_context(user_id: str | None, user_data_dir: str | None = None) -> bool:
    """Whether a background context may still read/write this user's data.

    Background LLM/screenshot threads can outlive account deletion. If they
    keep an old ``g.user_data_dir`` after ``delete_user()`` removes metadata,
    any storage write would recreate an orphan data/users/<uid>/ directory.
    Managed user dirs therefore require an active user record. Non-managed
    dirs are allowed for tests that intentionally use temporary Flask contexts.
    """
    if user_id == "_admin":
        return True
    if user_data_dir and not is_managed_user_data_dir(user_data_dir):
        return True
    if not user_id:
        return False
    # DMG client mode is a local bridge for a private server. The real account
    # is validated by the server that issued the cached token, so the local app
    # intentionally does not have this user in data/_admin/users.json. Allow
    # only the currently cached client-mode user and only for that user's local
    # data directory; server mode still requires a local active user record.
    try:
        import app as _app_mod
        cfg = getattr(_app_mod, "_client_mode_config", None) or {}
        if getattr(_app_mod, "_is_client_mode", False) and cfg.get("user_id") == user_id:
            expected = os.path.realpath(get_user_data_dir(user_id))
            actual = os.path.realpath(user_data_dir or expected)
            if actual == expected:
                return True
    except Exception:
        pass
    user = get_user(user_id)
    return bool(user and user.get("status") == "active")


def list_users() -> dict:
    return _load_json(_users_path())


def list_invitations() -> dict:
    return _load_json(_invitations_path())


def get_active_user_ids() -> list:
    """Return list of active user IDs (for background task iteration)."""
    users = _load_json(_users_path())
    ids = []
    for uid, u in users.items():
        if u.get("status") == "active":
            ids.append(uid)
    return ids


def _latest_user_chat_time(user_dir: str) -> str:
    """ISO-ish timestamp of the most recent user-role message in chat_history.json,
    or "" if none. Used by get_engaged_user_ids."""
    chat_path = os.path.join(user_dir, "chat_history.json")
    if not os.path.exists(chat_path):
        return ""
    try:
        with open(chat_path, "r", encoding="utf-8") as f:
            history = json.load(f)
        if not isinstance(history, list):
            return ""
        for msg in reversed(history):
            if isinstance(msg, dict) and msg.get("role") == "user":
                t = msg.get("time") or ""
                if t:
                    return t
        return ""
    except Exception:
        return ""


def _latest_screenshot_time(user_dir: str) -> str:
    """ISO-ish timestamp of the most recent screenshot in screenshot_log.json,
    or "" if none.

    On-disk shape is `{"YYYY-MM-DD": [{"t": "...", "d": "..."}, ...]}` — keyed
    by date with a list per day. Older code expected a flat list; we still
    accept that for backward-compat. Without the dict path the check returns
    "" for every screenshot-only user and the background engine evicts them — that was
    the May-03 → May-04 silent-Miru bug.
    """
    log_path = os.path.join(user_dir, "screenshot_log.json")
    if not os.path.exists(log_path):
        return ""
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Modern shape: dict keyed by date
        if isinstance(data, dict) and data:
            for date_key in sorted(data.keys(), reverse=True):
                entries = data.get(date_key) or []
                if not isinstance(entries, list):
                    continue
                for entry in reversed(entries):
                    if not isinstance(entry, dict):
                        continue
                    t = (entry.get("captured_at")
                         or entry.get("t")
                         or entry.get("ts")
                         or "")
                    if t:
                        return t
            return ""
        # Legacy shape: flat list
        if isinstance(data, list) and data:
            for entry in reversed(data):
                if isinstance(entry, dict):
                    t = (entry.get("captured_at")
                         or entry.get("t")
                         or entry.get("ts")
                         or "")
                    if t:
                        return t
        return ""
    except Exception:
        return ""


def get_engaged_user_ids(days: int = 7) -> list:
    """Return IDs of active users who have either sent a chat message OR had a
    screenshot recorded in the last `days` days.

    Only these users get a running AttentionEngine. Accounts idle for >7 days are
    skipped to (a) avoid wasting LLM quota and (b) free AttentionEngine memory —
    the spawner stops their AttentionEngine instance the next scan after they fall
    out of this set, and re-spawns it the next scan after activity resumes.

    The dual signal (chat OR screenshot) matters because a user can be
    actively using Miru via screen observation alone (e.g. desktop pet running
    quietly while user works); without the screenshot signal, those users
    would be falsely classified as idle and have their AttentionEngine torn down.
    """
    from datetime import datetime, timedelta
    cutoff = datetime.now() - timedelta(days=days)
    cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S")

    engaged = []
    for uid in get_active_user_ids():
        try:
            user_dir = get_user_data_dir(uid)
        except ValueError:
            continue  # _admin has no data dir
        latest_chat = _latest_user_chat_time(user_dir)
        latest_shot = _latest_screenshot_time(user_dir)
        # Either signal within the window keeps the user engaged.
        if (latest_chat and latest_chat >= cutoff_str) or \
           (latest_shot and latest_shot >= cutoff_str):
            engaged.append(uid)
    return engaged


def suspend_user(user_id: str) -> bool:
    """Mark user as suspended AND immediately evict their backend services.

    Status flip alone is not enough: per-user AttentionEngine / screen_analyzer /
    sleep_agent threads keep running until they're popped from their _instances
    dicts. Active SSE clients also stay connected until their next request
    fails auth. This function tears all of that down so admin suspension
    takes effect within seconds, not minutes.

    Activation is the inverse — but no proactive spawn is needed; the regular
    request path will lazily create instances again on the user's next call.

    2026-05-08: status flip wrapped in _lock — concurrent suspend/activate
    requests would otherwise race on the read-modify-write of users.json
    and lose updates (audit finding).
    """
    with _lock:
        users = _load_json(_users_path())
        if user_id not in users:
            return False
        users[user_id]["status"] = "suspended"
        _save_json(_users_path(), users)

    # Evict per-user singletons (AttentionEngine, screen_analyzer, sleep_agent, ...)
    _cleanup_user_singletons(user_id)

    # Disconnect any live SSE streams for this user
    try:
        import sse
        sse.disconnect_user(user_id)
    except Exception as e:
        print(f"[auth.suspend_user] SSE disconnect failed (non-fatal): {e}")

    return True


def activate_user(user_id: str) -> bool:
    # 2026-05-08: status flip locked — see suspend_user docstring.
    with _lock:
        users = _load_json(_users_path())
        if user_id not in users:
            return False
        users[user_id]["status"] = "active"
        _save_json(_users_path(), users)
        return True


# ---------------------------------------------------------------------------
# Admin auth decorator
# ---------------------------------------------------------------------------

def require_admin(view_func):
    """Decorator: 403 unless the request is authenticated as admin.

    Use this on every /api/admin/* endpoint registered through the admin
    blueprint, instead of the per-endpoint `if not g.is_admin: abort()` pattern.

    The middleware in check_request() still enforces admin-only access for
    /api/admin/* prefixes — this decorator is a defence-in-depth check that
    runs even if a future refactor moves the prefix gate.
    """
    from functools import wraps
    from flask import g, jsonify

    @wraps(view_func)
    def wrapper(*args, **kwargs):
        if not getattr(g, "is_admin", False):
            return jsonify({"error": "admin only"}), 403
        return view_func(*args, **kwargs)
    return wrapper


# ---------------------------------------------------------------------------
# User self-deletion (GDPR / 个人隐私自助)
# ---------------------------------------------------------------------------

_DELETION_AUDIT_FILENAME = "deletions.json"


def _deletions_audit_path() -> str:
    """Audit log for deleted accounts (kept indefinitely, no PII)."""
    return os.path.join(_ADMIN_DIR, _DELETION_AUDIT_FILENAME)


def _resolve_user_dir_strict(user_id: str) -> str:
    """Resolve a user data dir and verify it stays under data/users/."""
    users_root = os.path.realpath(os.path.join(_BASE_DATA_DIR, "users"))
    user_dir = os.path.realpath(os.path.join(users_root, user_id))
    if os.path.commonpath([users_root, user_dir]) != users_root:
        raise ValueError("resolved path escapes data/users/")
    return user_dir


def _dir_size_bytes(path: str) -> int:
    total = 0
    if not os.path.isdir(path):
        return total
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def _chmod_and_retry(func, path, _exc_info):
    os.chmod(path, stat.S_IRWXU)
    func(path)


def _rmtree_user_dir_strict(user_dir: str) -> None:
    if not os.path.isdir(user_dir):
        return
    shutil.rmtree(user_dir, onerror=_chmod_and_retry)
    if os.path.exists(user_dir):
        raise OSError(f"user data dir still exists after deletion: {user_dir}")


def _append_deletion_audit(user_id: str, invitation_code: str | None,
                           created_at: str, reason: str):
    os.makedirs(_ADMIN_DIR, exist_ok=True)
    audit_path = _deletions_audit_path()
    log = _load_json(audit_path) if os.path.exists(audit_path) else []
    if not isinstance(log, list):
        log = []
    log.append({
        "user_id": user_id,
        "invitation_code": invitation_code,
        "created_at": created_at,
        "deleted_at": datetime.now().isoformat(),
        "reason": reason,
    })
    _save_json(audit_path, log)


def export_user_data(user_id: str) -> dict | None:
    """Collect all per-user JSON + MD files into a single dict for export.

    Excludes binary/upload files (those are listed by filename only). The
    caller zips/serializes the dict however they want. Returns None if the
    user doesn't exist.
    """
    try:
        user_dir = get_user_data_dir(user_id)
    except ValueError:
        return None
    if not os.path.isdir(user_dir):
        return None

    bundle = {
        "user_id": user_id,
        "exported_at": datetime.now().isoformat(),
        "files": {},
        "uploads": [],
    }

    for root, dirs, files in os.walk(user_dir):
        # Skip heavy dirs — report filenames only
        rel_root = os.path.relpath(root, user_dir)
        for name in files:
            full = os.path.join(root, name)
            rel = os.path.join(rel_root, name) if rel_root != "." else name
            rel = rel.replace(os.sep, "/")

            # uploads/ directory: list filenames only, don't inline binaries
            if rel.startswith("uploads/"):
                bundle["uploads"].append(rel)
                continue

            # Only export text-like files (.json, .md, .txt, .log)
            ext = os.path.splitext(name)[1].lower()
            if ext in (".json", ".md", ".txt", ".log"):
                try:
                    with open(full, "r", encoding="utf-8") as f:
                        bundle["files"][rel] = f.read()
                except Exception as e:
                    bundle["files"][rel] = f"<read_error: {e}>"
            else:
                # Binary / unknown: skip content but record filename
                bundle["uploads"].append(rel)

    return bundle


def delete_user(user_id: str, reason: str = "user_self_delete") -> dict:
    """Permanently delete a user account.

    Steps:
      1. Validate the resolved user dir stays under data/users/.
      2. Mark the user as 'deleting' so their token stops working.
      3. Stop per-user daemons/SSE and strictly remove data/users/<user_id>/.
      4. If file cleanup fails, keep metadata as status='delete_failed'.
      5. Only after files are gone, remove users.json entry, revoke invitation,
         and append a no-PII deletion audit entry.

    Returns:
        {
            "ok": bool,
            "user_id": str,
            "invitation_code": str | None,
            "bytes_freed": int,
            "error": str | None,
        }

    Safe to call concurrently — _lock guards metadata transitions. File deletion
    happens outside the lock, but metadata is never removed until it succeeds.
    """
    result = {
        "ok": False,
        "user_id": user_id,
        "invitation_code": None,
        "bytes_freed": 0,
        "error": None,
    }

    if not user_id or user_id == "_admin":
        result["error"] = "cannot delete admin or empty user_id"
        return result

    try:
        user_dir = _resolve_user_dir_strict(user_id)
    except ValueError as e:
        result["error"] = str(e)
        return result

    invitation_code = None
    created_at = ""

    with _lock:
        users = _load_json(_users_path())

        if user_id not in users:
            result["error"] = "user not found"
            return result

        user = users[user_id]
        invitation_code = user.get("invitation_code")
        created_at = user.get("created_at", "")
        result["invitation_code"] = invitation_code

        # First make the token unusable, but keep metadata until files are
        # strictly gone. This prevents the old ignore_errors=True orphan-dir
        # failure mode where deletion looked successful while private files
        # remained under data/users/<uid>/.
        users[user_id]["status"] = "deleting"
        users[user_id]["delete_started_at"] = datetime.now().isoformat()
        users[user_id]["delete_reason"] = reason
        _save_json(_users_path(), users)

    # Best-effort per-user instance cleanup before deleting files so no daemon
    # keeps reading/writing the directory while rmtree is running.
    _cleanup_user_singletons(user_id)

    try:
        import sse
        sse.disconnect_user(user_id)
    except Exception as e:
        print(f"[auth.delete_user] SSE disconnect failed (non-fatal): {e}")

    bytes_freed = _dir_size_bytes(user_dir)
    try:
        _rmtree_user_dir_strict(user_dir)
        result["bytes_freed"] = bytes_freed
    except Exception as e:
        err = f"file cleanup failed: {e}"
        result["error"] = err
        with _lock:
            users = _load_json(_users_path())
            if user_id in users:
                users[user_id]["status"] = "delete_failed"
                users[user_id]["delete_failed_at"] = datetime.now().isoformat()
                users[user_id]["delete_error"] = str(e)[:500]
                _save_json(_users_path(), users)
        return result

    with _lock:
        users = _load_json(_users_path())
        invitations = _load_json(_invitations_path())
        user = users.get(user_id, {})
        invitation_code = invitation_code or user.get("invitation_code")
        created_at = created_at or user.get("created_at", "")

        users.pop(user_id, None)

        # Revoke invitation — set used_by back to None but keep a revoked flag
        # so the code cannot be re-used by a new registrant. This is safer than
        # deleting the invitation entirely (we preserve the history for admin).
        if invitation_code and invitation_code in invitations:
            invitations[invitation_code]["used_by"] = None
            invitations[invitation_code]["used_at"] = None
            invitations[invitation_code]["revoked"] = True
            invitations[invitation_code]["revoked_at"] = datetime.now().isoformat()
            invitations[invitation_code]["revoked_reason"] = reason

        _save_json(_users_path(), users)
        _save_json(_invitations_path(), invitations)

        try:
            _append_deletion_audit(user_id, invitation_code, created_at, reason)
        except Exception as e:
            # Audit failure should not block deletion after files are gone.
            print(f"[auth] audit log write failed: {e}")

    result["invitation_code"] = invitation_code
    result["ok"] = True
    return result


def _cleanup_user_singletons(user_id: str) -> None:
    """Drop any cached per-user instances for a deleted/suspended user.

    Every module that keeps _instances: dict[user_id, X] gets a chance to
    forget this user. Wrapped in try/except since modules may not be loaded.

    Modules with daemon threads/timers (curator, attention_engine, care_engine, sleep_agent)
    need an explicit stop/cancel call BEFORE pop — plain pop() leaves the
    thread orphaned. The thread keeps reading the (just-deleted) user_data_dir
    every tick and crashes silently. Probe-tested 2026-05-09: pre-fix,
    care_engine._stop_event remained False and sleep_agent._timer remained
    alive after _cleanup_user_singletons returned.
    """
    import sys

    # 2026-05-09 [P1 fix]: stop daemon thread + cancel timer BEFORE pop
    # so the thread sees the stop_event/cancelled timer and exits cleanly.

    # AttentionEngine: signal stop_event so the daemon's wait returns ASAP.
    try:
        att_mod = sys.modules.get("attention_engine")
        if att_mod is not None:
            remover = getattr(att_mod, "remove_attention_engine", None)
            if callable(remover):
                remover(user_id)
            else:
                inst = getattr(att_mod, "_instances", {}).get(user_id)
                if inst is not None and hasattr(inst, "stop"):
                    inst.stop()
    except Exception as e:
        print(f"[auth._cleanup_user_singletons] AttentionEngine stop failed: {e}")

    # CareEngine: legacy stop path for old in-process instances.
    try:
        ce_mod = sys.modules.get("care_engine")
        if ce_mod is not None:
            inst = getattr(ce_mod, "_instances", {}).get(user_id)
            if inst is not None and hasattr(inst, "stop"):
                inst.stop()
    except Exception as e:
        print(f"[auth._cleanup_user_singletons] CareEngine stop failed: {e}")

    # SleepAgent + ScreenSleepAgent: cancel pending timers / drop queued
    # screenshots so no late LLM flush fires after user logout. Screen-side
    # drop is acceptable (losing 0-7 observations is fine; see policy).
    try:
        sa_mod = sys.modules.get("sleep_agent")
        if sa_mod is not None:
            inst = getattr(sa_mod, "_instances", {}).get(user_id)
            if inst is not None and hasattr(inst, "_cancel_timer"):
                inst._cancel_timer()
        # screen_sleep_agent removed 2026-05-16; replaced by per-screenshot
        # async fork in screen_analyzer.analyze — no per-user instance to clean.
    except Exception as e:
        print(f"[auth._cleanup_user_singletons] SleepAgent cancel failed: {e}")

    # (module_name, attr_name) — attr is the per-user dict to pop from.
    # 2026-05-08: character uses _configs not _instances; previously
    # listed under _instances which silently no-op'd → cached old user's
    # config + soul.md text would linger across user changes. model_library
    # has no per-user dict (kept for future-proofing; getattr None-safe).
    module_instance_dicts = [
        ("miru_emotion",    "_instances"),
        ("care_engine",     "_instances"),
        ("screen_analyzer", "_instances"),
        ("sleep_agent",     "_instances"),   # 2026-05-07 fix: was missing → leaked
        ("character",       "_configs"),     # 2026-05-08 fix: attr name was wrong
        ("model_library",   "_instances"),   # no-op today; future-proof
    ]
    for mod_name, attr in module_instance_dicts:
        mod = sys.modules.get(mod_name)
        if mod is None:
            continue
        d = getattr(mod, attr, None)
        if isinstance(d, dict):
            d.pop(user_id, None)

    # 2026-05-08: Curator daemon thread — needs stop_event, not just pop.
    # Without this, suspend_user / delete_user left the curator thread
    # running, which kept reading the (now-deleted) user_data_dir each
    # minute and re-running LLM cleanup on a suspended user's slots.
    try:
        curator_mod = sys.modules.get("curator")
        if curator_mod is not None and hasattr(curator_mod, "stop_loop_for_user"):
            curator_mod.stop_loop_for_user(user_id)
    except Exception as e:
        print(f"[auth._cleanup_user_singletons] curator stop failed (non-fatal): {e}")


# ---------------------------------------------------------------------------
# Request auth middleware
# ---------------------------------------------------------------------------

_EXEMPT_PREFIXES = (
    "/_page/",
    "/assets/",
    "/icon-",
    "/favicon",
    "/manifest.json",
    "/service-worker.js",
    "/apple-touch-icon",
    "/api/auth/login",
    "/api/auth/logout",
    "/api/client-config",
    "/api/client/local/start",
    "/api/client/provision/",
    # Public liveness probe — used by docker compose health checks and
    # integration tests. No data access, no auth required.
    "/api/health",
    # Admin web UI shell — token check happens client-side then via
    # POST /api/admin/auth/verify. The HTML itself is harmless.
    "/admin",
    # APK update channel: version metadata + binary download.
    # Both endpoints must be reachable pre-login so the updater works even
    # after a session expires.
    "/api/app/",
    "/downloads/",
    "/privacy",
)


def is_local_request(req=None) -> bool:
    req = req or request
    remote = req.remote_addr or ""
    return remote in ("127.0.0.1", "::1", "localhost")


def _set_anonymous_context():
    """Exempt routes: HTML shell, static assets, login API. No data access."""
    g.user_id = None
    g.user_data_dir = None
    g.is_admin = False


def _set_admin_context():
    """Admin management only — no data dir (admin cannot read user data)."""
    g.user_id = "_admin"
    g.user_data_dir = None
    g.is_admin = True


def _set_user_context(user_id: str):
    g.user_id = user_id
    g.user_data_dir = get_user_data_dir(user_id)
    g.is_admin = False
    try:
        ensure_account_manifest(user_id)
    except Exception as e:
        print(f"[auth] account manifest ensure failed for {user_id}: {e}")


def check_request(req=None):
    """Flask before_request hook. Sets g.user_id and g.user_data_dir.

    Rules:
    - Exempt routes (HTML, static, login): anonymous, no data context
    - /api/admin/*: admin token or local request → admin context (no data)
    - All other routes: MUST have valid user token → user context, or 401
    - No admin fallback for data routes. Ever.
    """
    req = req or request
    path = req.path

    # Exempt routes: serve HTML shell, static assets, login/config API
    # /         → landing page (public)
    # /app      → web app shell with login screen (anonymous; client JS handles token)
    # /login    → DMG invitation code entry page (anonymous; POSTs to /api/auth/login)
    # /pet      → desktop pet shell (auth handled by URL token param)
    if path in ("/", "/app", "/login", "/pet", "/privacy") or any(path.startswith(p) for p in _EXEMPT_PREFIXES):
        _set_anonymous_context()
        return None

    # OPTIONS preflight
    if req.method == "OPTIONS":
        _set_anonymous_context()
        return None

    # Extract token
    auth_header = req.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        provided_token = auth_header[7:].strip()
    else:
        provided_token = req.args.get("token", "").strip()

    # Admin-only routes: /api/admin/*
    if path.startswith("/api/admin/"):
        # The /api/admin/auth/verify endpoint must remain reachable BEFORE the
        # caller has proven their admin token — the whole point of that
        # endpoint is to verify a candidate token. The endpoint itself checks
        # the token in the request body and returns 401 on mismatch.
        if path == "/api/admin/auth/verify":
            _set_admin_context()  # gives the endpoint admin context to run
            return None
        if provided_token and is_admin_token(provided_token):
            _set_admin_context()
            return None
        # 2026-05-09 [P0 fix]: removed `if is_local_request(req): _set_admin_context()`
        # that previously bypassed admin auth for any 127.0.0.1 request. With nginx
        # reverse-proxying without X-Real-IP propagation, ALL public requests had
        # remote_addr=127.0.0.1, making `https://mirulife.top/api/admin/users` etc.
        # publicly accessible without any token. Admin UI uses POST /api/admin/auth/verify
        # to authenticate; no legitimate caller relies on the bypass.
        abort(403, description="Admin only")

    # All other routes: require a valid USER token
    if provided_token:
        user_id, _user = get_user_by_token(provided_token)
        if user_id:
            _set_user_context(user_id)
            return None
        # CLIENT MODE: local Flask has no users DB — VPS issues the token.
        # Accept the token if it matches _client_mode_config.auth_token (the
        # token we stored at login). Without this, every local-only API call
        # (/api/version, /api/user-settings, /api/models/*, /api/pet/*, etc.)
        # would 401 and bounce the user back to the login screen.
        if is_local_request(req):
            try:
                from app import _client_mode_config, _is_client_mode
                if (_is_client_mode and _client_mode_config
                        and provided_token == _client_mode_config.get("auth_token")
                        and _client_mode_config.get("user_id")):
                    _set_user_context(_client_mode_config["user_id"])
                    return None
            except Exception:
                pass
        # Reject admin token on data routes with a clear message
        if is_admin_token(provided_token):
            abort(401, description="Admin token不能访问用户数据，请使用邀请码登录")
        abort(401, description="Invalid token")

    # Local requests without token: client mode user or 401
    if is_local_request(req):
        if os.environ.get("MIRU_ANDROID") == "1":
            abort(401, description="Authentication required")
        from app import _client_mode_config
        if _client_mode_config and _client_mode_config.get("user_id"):
            _set_user_context(_client_mode_config["user_id"])
            return None
        # NO admin fallback — require login
        abort(401, description="Login required")

    abort(401, description="Authentication required")
