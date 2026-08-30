"""Tests for invitation-code obfuscation (auth.encode_server / decode_server).

Goal: verify that the XOR-mask obfuscation layer hides the raw IP bytes
from a naive base31 decoder, while still round-tripping correctly through
the official encode/decode pair.

Threat model is "casual reverse engineering of a leaked invitation code",
not a determined attacker. See auth.py docstring on _OBFUSCATION_MASK.
"""
from __future__ import annotations

import json
import struct

import auth


_CODE_CHARS = auth._CODE_CHARS
_BASE = auth._BASE


def _naive_decode_no_mask(encoded: str) -> tuple[str, int] | None:
    """Decode 10-char base31 → IP:port WITHOUT applying the mask.

    Simulates an attacker who reverse-engineered the base31 alphabet but
    doesn't know about the XOR mask. They get scrambled bytes back.
    """
    if len(encoded) != 10:
        return None
    try:
        num = 0
        for ch in encoded:
            idx = _CODE_CHARS.index(ch)
            num = num * _BASE + idx
        if num >= (1 << 48):
            return None
        raw = num.to_bytes(6, "big")
        octets = struct.unpack("!4BH", raw)
        return f"{octets[0]}.{octets[1]}.{octets[2]}.{octets[3]}", octets[4]
    except (ValueError, struct.error, OverflowError):
        return None


# ---------------------------------------------------------------------------
# 1. Round-trip
# ---------------------------------------------------------------------------

def test_round_trip_arbitrary_ip():
    cases = [
        ("203.0.113.42", 5001),
        ("1.2.3.4", 80),
        ("255.255.255.255", 65535),
        ("0.0.0.0", 1),
        ("192.168.1.100", 8080),
    ]
    for ip, port in cases:
        encoded = auth.encode_server(ip, port)
        assert len(encoded) == 10
        decoded = auth.decode_server(encoded)
        assert decoded == (ip, port), f"round-trip failed for {ip}:{port} → {encoded} → {decoded}"


def test_encoded_is_uppercase_base31():
    encoded = auth.encode_server("203.0.113.42", 5001)
    for ch in encoded:
        assert ch in _CODE_CHARS, f"non-base31 char in encoding: {ch}"


def test_encode_rejects_non_ipv4_and_bad_ports():
    for bad_ip in ["miru.example.com", "999.1.1.1", "", "2001:db8::1"]:
        try:
            auth.encode_server(bad_ip, 5001)
        except ValueError:
            pass
        else:
            raise AssertionError(f"bad ip was accepted: {bad_ip!r}")
    for bad_port in [0, 65536, -1]:
        try:
            auth.encode_server("203.0.113.42", bad_port)
        except ValueError:
            pass
        else:
            raise AssertionError(f"bad port was accepted: {bad_port!r}")


# ---------------------------------------------------------------------------
# 2. Obfuscation actually obfuscates (the whole point)
# ---------------------------------------------------------------------------

def test_naive_base31_decode_returns_garbage_not_real_ip():
    """An attacker decoding base31 without knowing the mask must NOT recover
    the real IP. They get scrambled bytes that don't match the input."""
    real_ip = "203.0.113.42"
    real_port = 5001
    encoded = auth.encode_server(real_ip, real_port)
    naive = _naive_decode_no_mask(encoded)
    # Naive decode must succeed mathematically (valid 6-byte int) but yield
    # something other than the real IP — that's the whole point of XOR.
    assert naive is not None, "naive decoder should produce *some* IP"
    naive_ip, naive_port = naive
    assert (naive_ip, naive_port) != (real_ip, real_port), (
        f"obfuscation FAILED: naive decoder recovered real IP from {encoded}"
    )


def test_two_different_ips_dont_collide():
    """Different inputs must produce different encodings."""
    a = auth.encode_server("203.0.113.42", 5001)
    b = auth.encode_server("110.40.153.45", 5001)
    c = auth.encode_server("203.0.113.42", 5002)
    assert a != b
    assert a != c
    assert b != c


def test_mask_is_self_inverse():
    """_apply_mask twice = identity."""
    sample = bytes([1, 2, 3, 4, 5, 6])
    once = auth._apply_mask(sample)
    twice = auth._apply_mask(once)
    assert twice == sample


# ---------------------------------------------------------------------------
# 3. Garbage inputs return None
# ---------------------------------------------------------------------------

def test_decode_rejects_wrong_length():
    assert auth.decode_server("") is None
    assert auth.decode_server("ABC") is None
    assert auth.decode_server("A" * 9) is None
    assert auth.decode_server("A" * 11) is None


def test_decode_rejects_invalid_chars():
    # 'O' is not in _CODE_CHARS (excluded as ambiguous)
    assert auth.decode_server("AAAAAAAAAO") is None
    # '0' likewise excluded
    assert auth.decode_server("AAAAAAAAA0") is None


def test_decode_rejects_overflow():
    """31^10 > 2^48, so some valid base31 strings overflow 6 bytes.
    Those must return None, not silently truncate."""
    # The largest possible 10-char base31 number (all '9's = 30s).
    # 30 * (31^9 + 31^8 + ... + 31^0) overflows 2^48.
    max_string = "9" * 10
    assert auth.decode_server(max_string) is None


# ---------------------------------------------------------------------------
# 4. Full invitation-code parsing
# ---------------------------------------------------------------------------

def test_parse_invitation_code_round_trip():
    server = auth.encode_server("203.0.113.42", 5001)
    code = f"MIRU-{server}-ABCDEF"
    parsed = auth.parse_invitation_code(code)
    assert parsed is not None
    assert parsed["ip"] == "203.0.113.42"
    assert parsed["port"] == 5001
    assert parsed["server"] == "203.0.113.42:5001"
    assert parsed["user_code"] == "ABCDEF"
    assert parsed["local_code"] == "MIRU-ABCDEF"


def test_parse_invitation_code_lowercase_normalized():
    server = auth.encode_server("1.2.3.4", 80)
    code = f"miru-{server.lower()}-abcdef"
    parsed = auth.parse_invitation_code(code)
    assert parsed is not None
    assert parsed["ip"] == "1.2.3.4"
    assert parsed["user_code"] == "ABCDEF"


def test_parse_invitation_code_with_spaces():
    server = auth.encode_server("1.2.3.4", 80)
    code = f"  MIRU-{server}-ABCDEF  "
    assert auth.parse_invitation_code(code) is not None


def test_parse_rejects_garbage():
    assert auth.parse_invitation_code("") is None
    assert auth.parse_invitation_code("MIRU-WRONG") is None
    assert auth.parse_invitation_code("MIRU-ABCDEF") is None
    assert auth.parse_invitation_code("NOTMIRU-ABCDEFGHJK-ABCDEF") is None
    # Right format, wrong length body
    assert auth.parse_invitation_code("MIRU-ABCDEF-ABCDEF") is None


def test_generate_invitation_requires_ipv4(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    admin_dir = data_dir / "_admin"
    admin_dir.mkdir(parents=True)
    monkeypatch.setattr(auth, "_BASE_DATA_DIR", str(data_dir))
    monkeypatch.setattr(auth, "_ADMIN_DIR", str(admin_dir))
    monkeypatch.delenv("SERVER_IP", raising=False)
    monkeypatch.delenv("DOMAIN", raising=False)
    try:
        auth.generate_invitation_codes(count=1)
    except ValueError as e:
        assert "SERVER_IP" in str(e)
    else:
        raise AssertionError("generate_invitation_codes accepted missing SERVER_IP")

    monkeypatch.setenv("SERVER_IP", "miru.example.com")
    try:
        auth.generate_invitation_codes(count=1)
    except ValueError:
        pass
    else:
        raise AssertionError("generate_invitation_codes accepted a domain SERVER_IP")


def test_login_requires_full_code_and_matching_server(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    admin_dir = data_dir / "_admin"
    admin_dir.mkdir(parents=True)
    (data_dir / "users").mkdir()
    monkeypatch.setattr(auth, "_BASE_DATA_DIR", str(data_dir))
    monkeypatch.setattr(auth, "_ADMIN_DIR", str(admin_dir))
    monkeypatch.setenv("SERVER_IP", "203.0.113.42")
    monkeypatch.setenv("SERVER_PORT", "5001")

    local_code = "MIRU-ABCDEF"
    (admin_dir / "invitations.json").write_text(
        json.dumps({local_code: {"used_by": None, "used_at": None}}),
        encoding="utf-8",
    )

    assert auth.login_with_code(local_code) is None

    wrong_server = f"MIRU-{auth.encode_server('203.0.113.43', 5001)}-ABCDEF"
    assert auth.login_with_code(wrong_server) is None

    full_code = f"MIRU-{auth.encode_server('203.0.113.42', 5001)}-ABCDEF"
    result = auth.login_with_code(full_code)
    assert result is not None
    assert result["token"]


def test_login_stores_full_invitation_code_for_new_user(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    admin_dir = data_dir / "_admin"
    admin_dir.mkdir(parents=True)
    monkeypatch.setattr(auth, "_BASE_DATA_DIR", str(data_dir))
    monkeypatch.setattr(auth, "_ADMIN_DIR", str(admin_dir))
    monkeypatch.setenv("SERVER_IP", "203.0.113.42")
    monkeypatch.setenv("SERVER_PORT", "5001")

    local_code = "MIRU-ABCDEF"
    full_code = f"MIRU-{auth.encode_server('203.0.113.42', 5001)}-ABCDEF"
    (admin_dir / "invitations.json").write_text(
        json.dumps({local_code: {"used_by": None, "used_at": None}}),
        encoding="utf-8",
    )

    result = auth.login_with_code(full_code)
    assert result is not None

    users = json.loads((admin_dir / "users.json").read_text(encoding="utf-8"))
    user = users[result["user_id"]]
    assert user["invitation_code"] == local_code
    assert user["full_invitation_code"] == full_code

    manifest_path = data_dir / "users" / result["user_id"] / "account_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["invitation_code"] == local_code
    assert manifest["full_invitation_code"] == full_code

    info = auth.get_user_invitation_info(result["user_id"])
    assert info["invitation_code"] == full_code
    assert info["local_invitation_code"] == local_code
    assert info["server_url"] == "http://203.0.113.42:5001"


def test_relogin_backfills_full_invitation_code_for_legacy_user(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    admin_dir = data_dir / "_admin"
    admin_dir.mkdir(parents=True)
    monkeypatch.setattr(auth, "_BASE_DATA_DIR", str(data_dir))
    monkeypatch.setattr(auth, "_ADMIN_DIR", str(admin_dir))
    monkeypatch.setenv("SERVER_IP", "203.0.113.42")
    monkeypatch.setenv("SERVER_PORT", "5001")

    local_code = "MIRU-ABCDEF"
    user_id = "u_legacy01"
    full_code = f"MIRU-{auth.encode_server('203.0.113.42', 5001)}-ABCDEF"
    (admin_dir / "invitations.json").write_text(
        json.dumps({local_code: {"used_by": user_id, "used_at": "2026-06-01T00:00:00"}}),
        encoding="utf-8",
    )
    (admin_dir / "users.json").write_text(
        json.dumps({
            user_id: {
                "token": "tok_legacy",
                "invitation_code": local_code,
                "created_at": "2026-06-01T00:00:00",
                "status": "active",
            }
        }),
        encoding="utf-8",
    )

    result = auth.login_with_code(full_code)
    assert result == {"token": "tok_legacy", "user_id": user_id, "is_new": False}

    users = json.loads((admin_dir / "users.json").read_text(encoding="utf-8"))
    assert users[user_id]["full_invitation_code"] == full_code
    manifest_path = data_dir / "users" / user_id / "account_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["full_invitation_code"] == full_code


# ---------------------------------------------------------------------------
# 5. Generated codes pre-deploy (regression guards)
# ---------------------------------------------------------------------------

def test_known_format_invariant():
    """Any IP+port encodes to exactly 10 chars and round-trips. Regression
    guard: if this breaks, every issued code dies."""
    # Use RFC 5737 documentation IP — never used as a real VPS IP.
    encoded = auth.encode_server("203.0.113.42", 5001)
    assert len(encoded) == 10
    assert auth.decode_server(encoded) == ("203.0.113.42", 5001)
