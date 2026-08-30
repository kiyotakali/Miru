"""Admin API blueprint — kept structurally separate from the user pipeline.

All /api/admin/* routes live here. Mounted via app.register_blueprint() in
app.py. This module:

  * Does NOT import from app.py (no circular deps with main routing)
  * Does NOT call any user-facing helper that touches the SPA pipeline
  * Reads/writes only through auth.py (user records, invitations) and
    admin_stats.py (read-only aggregation)

Every route is gated by @auth.require_admin in addition to the prefix-based
check that auth.check_request() already performs for /api/admin/*.
"""
import os
import shutil
import ipaddress

from flask import Blueprint, g, jsonify, request

import auth
import admin_stats


admin_bp = Blueprint("admin", __name__, url_prefix="/api/admin")


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

@admin_bp.route("/auth/verify", methods=["POST"])
def auth_verify():
    """Verify an admin token. Used by the admin login page after the user
    pastes the token — a successful response means it's safe to store the
    token in localStorage and load the dashboard.

    NOTE: We deliberately bypass require_admin here because the request
    arrives before the token is plumbed into the Authorization header.
    The caller submits the candidate token in the JSON body.
    """
    body = request.get_json(silent=True) or {}
    token = (body.get("token") or "").strip()
    if not token:
        return jsonify({"ok": False, "error": "missing token"}), 400
    if not auth.is_admin_token(token):
        return jsonify({"ok": False, "error": "invalid token"}), 401
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

@admin_bp.route("/users", methods=["GET"])
@auth.require_admin
def users_list():
    """Return the full users dictionary (with token redacted).

    Each entry is augmented with:
      * token_masked    — preview of the auth token (first/last 4 chars)
      * invitation_full_code — 16-char reconstructed code (admin convenience)
      * assigned_to     — admin-side label from the original invitation
    """
    users = auth.list_users()
    invitations = auth.list_invitations()
    try:
        ip, port = _resolve_server_address()
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    redacted = {}
    for uid, u in users.items():
        copy = dict(u)
        if "token" in copy:
            copy["token_masked"] = copy["token"][:4] + "…" + copy["token"][-4:]
            del copy["token"]
        # Enrich with full invitation code + admin label
        inv_code = copy.get("invitation_code")
        if inv_code:
            copy["invitation_full_code"] = _attach_full_code(inv_code, ip, port)
            inv_record = invitations.get(inv_code) or {}
            copy["assigned_to"] = inv_record.get("assigned_to") or None
        else:
            copy["invitation_full_code"] = None
            copy["assigned_to"] = None
        redacted[uid] = copy
    return jsonify({"users": redacted, "count": len(redacted)})


@admin_bp.route("/users/<uid>", methods=["GET"])
@auth.require_admin
def user_detail(uid):
    """Return stats + metadata for a single user."""
    detail = admin_stats.get_user_detail(uid)
    if detail is None:
        return jsonify({"error": "user not found"}), 404
    return jsonify(detail)


@admin_bp.route("/users/<uid>/attention-delivery", methods=["POST"])
@auth.require_admin
def user_attention_delivery(uid):
    """Run one AttentionEngine speak_intent through the live delivery path.

    Admin-only diagnostic endpoint used for end-to-end verification:
    attention_intent_queue.json -> delivery preflight -> proactive main agent
    -> chat_history/SSE/pending notification. The endpoint does not create or
    modify intents before the gate; it only attempts delivery for an existing
    pending intent in the selected user's data directory.
    """
    if not uid or uid == "_admin":
        return jsonify({"error": "real user id required"}), 400

    user = auth.get_user(uid)
    if not user:
        return jsonify({"error": "user not found"}), 404
    if user.get("status") != "active":
        return jsonify({"error": "user is not active"}), 409

    body = request.get_json(silent=True) or {}
    intent_id = (body.get("intent_id") or "").strip() or None

    old_user_id = getattr(g, "user_id", None)
    old_user_data_dir = getattr(g, "user_data_dir", None)
    old_is_admin = getattr(g, "is_admin", False)
    try:
        g.user_id = uid
        g.user_data_dir = auth.get_user_data_dir(uid)
        g.is_admin = False
        try:
            auth.ensure_account_manifest(uid)
        except Exception as e:
            print(f"[admin attention-delivery] manifest ensure failed for {uid}: {e}")

        import core
        entry = core.deliver_attention_intent_once(intent_id=intent_id)
    finally:
        g.user_id = old_user_id
        g.user_data_dir = old_user_data_dir
        g.is_admin = old_is_admin

    return jsonify({
        "ok": bool(entry),
        "user_id": uid,
        "intent_id": intent_id,
        "entry": entry,
    })


@admin_bp.route("/users/<uid>", methods=["PATCH"])
@auth.require_admin
def user_patch(uid):
    """Update a user's status. Body: {"status": "active" | "suspended"}.

    Suspending evicts every per-user backend service immediately
    (AttentionEngine, screen_analyzer, sleep_agent, character) AND closes any
    live SSE streams for that user.
    """
    body = request.get_json(silent=True) or {}
    new_status = (body.get("status") or "").strip()
    if new_status not in ("active", "suspended"):
        return jsonify({"error": "status must be 'active' or 'suspended'"}), 400

    if new_status == "suspended":
        ok = auth.suspend_user(uid)  # also evicts singletons + SSE
    else:
        ok = auth.activate_user(uid)

    if not ok:
        return jsonify({"error": "user not found"}), 404

    admin_stats.invalidate_cache()
    return jsonify({"ok": True, "user_id": uid, "status": new_status})


@admin_bp.route("/users/<uid>", methods=["DELETE"])
@auth.require_admin
def user_delete(uid):
    """Hard delete a user: data dir + record + revoke their invitation.

    Defence in depth — multiple guards before rmtree:
      * uid must be non-empty and not '_admin'
      * computed user_dir must resolve under data/users/
      * user must exist in users.json (avoid blind rmtree)

    Body: {"confirm": "<last 6 chars of uid>"} required to prevent fat-finger.
    """
    body = request.get_json(silent=True) or {}
    confirm = (body.get("confirm") or "").strip()

    # L1 — uid sanity
    if not uid or uid == "_admin":
        return jsonify({"error": "cannot delete admin or empty uid"}), 400

    # L2 — confirm must match last 6 chars
    if confirm != uid[-6:]:
        return jsonify({
            "error": "confirm mismatch",
            "hint": f"send body.confirm equal to the last 6 chars of uid ({uid[-6:]})",
        }), 400

    # L3 — user must exist (avoid rmtree on stale uid)
    users = auth.list_users()
    if uid not in users:
        return jsonify({"error": "user not found"}), 404

    # L4 — path resolution must stay under data/users/
    user_dir = os.path.realpath(os.path.join(auth._BASE_DATA_DIR, "users", uid))
    expected_root = os.path.realpath(os.path.join(auth._BASE_DATA_DIR, "users"))
    if not user_dir.startswith(expected_root + os.sep):
        return jsonify({"error": "refusing: resolved path escapes data/users/"}), 400

    # Reuse the self-deletion routine — same audit + cleanup path,
    # different reason tag for the audit log.
    result = auth.delete_user(uid, reason="admin_deleted")
    if not result.get("ok"):
        return jsonify({"error": result.get("error", "delete failed")}), 500

    # Also kick any SSE clients still connected (delete_account already calls
    # _cleanup_user_singletons but not SSE).
    try:
        import sse
        sse.disconnect_user(uid)
    except Exception:
        pass

    admin_stats.invalidate_cache()
    return jsonify({
        "ok": True,
        "user_id": uid,
        "bytes_freed": result.get("bytes_freed", 0),
        "invitation_code": result.get("invitation_code"),
    })


# ---------------------------------------------------------------------------
# Invitation codes
# ---------------------------------------------------------------------------

def _attach_full_code(short_code: str, ip: str, port: int) -> str:
    """Compose a 16-char full invitation code from a short code by
    prepending the encoded server segment. Pure helper, no IO."""
    user_part = short_code.replace("MIRU-", "", 1)
    server_encoded = auth.encode_server(ip, port)
    return f"MIRU-{server_encoded}-{user_part}"


def _resolve_server_address() -> tuple[str, int]:
    """Resolve the IP+port to embed in invitation codes.

    Private-server v1 invitation codes encode IPv4+port only. Do not fall
    back to DOMAIN or localhost; that would generate codes clients cannot use.
    """
    ip = os.environ.get("SERVER_IP", "").strip()
    if not ip:
        raise ValueError("SERVER_IP is required to compose full invitation codes")
    ip = str(ipaddress.IPv4Address(ip))
    try:
        port = int(os.environ.get("SERVER_PORT", os.environ.get("PORT", "5001")))
    except (ValueError, TypeError):
        raise ValueError("SERVER_PORT must be an integer")
    if port < 1 or port > 65535:
        raise ValueError("SERVER_PORT must be between 1 and 65535")
    return ip, port


@admin_bp.route("/invitations", methods=["GET"])
@auth.require_admin
def invitations_list():
    """List invitations with full_code (admin convenience) + assigned_to."""
    invitations = auth.list_invitations()
    try:
        ip, port = _resolve_server_address()
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    enriched = {}
    for short_code, inv in invitations.items():
        enriched[short_code] = {
            **inv,
            "full_code": _attach_full_code(short_code, ip, port),
            # ensure assigned_to surfaces even on legacy entries
            "assigned_to": inv.get("assigned_to") or None,
        }
    return jsonify({"invitations": enriched, "count": len(enriched)})


@admin_bp.route("/invitations", methods=["POST"])
@auth.require_admin
def invitations_create():
    """Generate N new invitation codes.

    Body:
        count: int (1-100)
        ip:    optional override server IP for the embedded segment
        port:  optional override server port
        assigned_to: optional admin label ("for-mom" / "给小明") — admin-only
    """
    body = request.get_json(silent=True) or {}
    count = max(1, min(int(body.get("count", 1)), 100))
    try:
        if body.get("ip"):
            ip = str(ipaddress.IPv4Address(str(body.get("ip")).strip()))
        else:
            ip, _ = _resolve_server_address()
        port = int(body.get("port", os.environ.get("SERVER_PORT", os.environ.get("PORT", "5001"))))
    except (ValueError, TypeError) as e:
        return jsonify({"error": str(e)}), 400
    assigned_to = (body.get("assigned_to") or "").strip() or None
    try:
        codes = auth.generate_invitation_codes(
            count, ip=ip, port=port, assigned_to=assigned_to,
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    admin_stats.invalidate_cache()
    return jsonify({
        "codes": codes,
        "count": len(codes),
        "assigned_to": assigned_to,
    })


@admin_bp.route("/invitations/<code>", methods=["PATCH"])
@auth.require_admin
def invitations_patch(code):
    """Update admin-side metadata on an invitation. Currently only
    assigned_to (the admin label) is mutable. Body: {"assigned_to": str|null}.
    """
    code = (code or "").strip()
    if not code:
        return jsonify({"error": "missing code"}), 400

    body = request.get_json(silent=True) or {}
    if "assigned_to" not in body:
        return jsonify({"error": "body must include 'assigned_to'"}), 400

    new_label = body.get("assigned_to")
    new_label = (new_label or "").strip() or None  # empty string → null

    invitations = auth.list_invitations()
    if code not in invitations:
        return jsonify({"error": "invitation not found"}), 404

    invitations[code]["assigned_to"] = new_label
    auth._save_json(auth._invitations_path(), invitations)
    return jsonify({"ok": True, "code": code, "assigned_to": new_label})


@admin_bp.route("/invitations/<code>", methods=["DELETE"])
@auth.require_admin
def invitations_delete(code):
    """Delete an invitation code, but only if it has not been used.

    Used codes are tied to a user record — deleting them would break the
    audit chain. To deal with a used code, suspend or delete the user
    instead (which marks the invitation revoked).
    """
    code = (code or "").strip()
    if not code:
        return jsonify({"error": "missing code"}), 400

    invitations = auth.list_invitations()
    inv = invitations.get(code)
    if inv is None:
        # Allow alternate lookup if the user pasted the full code with prefix
        if code.startswith("MIRU-") and code in invitations:
            inv = invitations[code]
        if inv is None:
            return jsonify({"error": "invitation not found"}), 404

    # Active used codes (still bound to a live user) are protected to avoid
    # breaking audit chain. Once the user is deleted/suspended the invitation
    # is flagged `revoked` — at that point deletions.json holds the audit
    # entry, so removing the invitations.json row is safe and just cleans up
    # the admin view.
    if inv.get("used_by") and not inv.get("revoked"):
        return jsonify({
            "error": "cannot delete an active used invitation",
            "hint": "Delete the user first (which revokes the code), then this row can be removed.",
        }), 409

    invitations.pop(code, None)
    auth._save_json(auth._invitations_path(), invitations)
    admin_stats.invalidate_cache()
    return jsonify({"ok": True, "deleted": code})


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

@admin_bp.route("/stats", methods=["GET"])
@auth.require_admin
def stats_global():
    """Global rollup. Cached for 60s; pass ?force=1 to bypass."""
    force = request.args.get("force") == "1"
    return jsonify(admin_stats.get_stats(force_refresh=force))


# ---------------------------------------------------------------------------
# AI Configuration (3-tier: vision / chat / memory)
# ---------------------------------------------------------------------------

@admin_bp.route("/ai-config", methods=["GET"])
@auth.require_admin
def ai_config_list():
    """Return all 3 tiers with masked api_keys."""
    import ai_config
    return jsonify(ai_config.get_public_config())


@admin_bp.route("/ai-config/<tier>", methods=["PATCH"])
@auth.require_admin
def ai_config_update(tier):
    """Update one tier's host/api_key/model/max_tokens. Hot-reloads."""
    import ai_config
    body = request.get_json(silent=True) or {}
    result = ai_config.update_tier_config(tier, body)
    if not result.get("ok"):
        return jsonify(result), 400
    return jsonify(result)


@admin_bp.route("/ai-config/<tier>/test", methods=["POST"])
@auth.require_admin
def ai_config_test(tier):
    """Make a real API call to verify the tier's configuration works.

    Returns latency + model info on success, error string on failure.
    """
    import ai_config
    result = ai_config.ping_tier(tier, timeout_seconds=20)
    if not result.get("ok"):
        return jsonify(result), 502
    return jsonify(result)


# ---------------------------------------------------------------------------
# Admin token rotation
# ---------------------------------------------------------------------------

@admin_bp.route("/rotate-token", methods=["POST"])
@auth.require_admin
def rotate_admin_token():
    """Rotate the admin token. Returns the new token (display once).

    The current admin session is invalidated immediately; admin must re-login
    with the new token. The old token is preserved in audit log only.
    """
    import secrets
    from datetime import datetime
    new_token = secrets.token_urlsafe(32)
    old_path = auth._legacy_auth_path()
    old_data = auth._load_json(old_path) if os.path.exists(old_path) else {}
    old_token = old_data.get("token", "")
    # Backup current token to audit log
    try:
        audit_path = os.path.join(auth._ADMIN_DIR, "rotated_admin_tokens.json")
        os.makedirs(auth._ADMIN_DIR, exist_ok=True)
        log = auth._load_json(audit_path) if os.path.exists(audit_path) else []
        if not isinstance(log, list):
            log = []
        log.append({
            "rotated_at": datetime.now().isoformat(),
            "old_token_prefix": old_token[:8] if old_token else "",
            "old_created_at": old_data.get("created_at", ""),
        })
        auth._save_json(audit_path, log)
    except Exception as e:
        print(f"[admin rotate-token] audit log failed: {e}")
    # Atomic swap
    payload = {"token": new_token, "created_at": datetime.now().isoformat()}
    tmp = old_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        import json as _json
        _json.dump(payload, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, old_path)
    return jsonify({
        "ok": True,
        "new_token": new_token,
        "warning": "保存这个 token — 老 token 立即失效，本接口下次将拒绝旧 token。"
    })
