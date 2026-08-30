#!/usr/bin/env python3
"""Audit multi-user metadata/data-dir isolation.

This is intentionally read-only. It checks whether _admin/users.json,
_admin/invitations.json and data/users/<uid>/account_manifest.json agree with
each other, and surfaces orphan user directories left by failed deletes.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return {"__read_error__": str(e)}


def audit(data_dir: str | os.PathLike = "data") -> dict:
    root = Path(data_dir)
    admin = root / "_admin"
    users_root = root / "users"
    users = _read_json(admin / "users.json", {})
    invitations = _read_json(admin / "invitations.json", {})
    if not isinstance(users, dict):
        users = {}
    if not isinstance(invitations, dict):
        invitations = {}

    user_dirs = sorted(
        p.name for p in users_root.iterdir()
        if users_root.exists() and p.is_dir()
    )
    user_ids = set(users.keys())
    dir_ids = set(user_dirs)

    manifest_missing = []
    manifest_mismatch = []
    for uid in sorted(user_ids & dir_ids):
        manifest_path = users_root / uid / "account_manifest.json"
        if not manifest_path.exists():
            manifest_missing.append(uid)
            continue
        data = _read_json(manifest_path, {})
        if data.get("user_id") != uid:
            manifest_mismatch.append({
                "user_id": uid,
                "manifest_user_id": data.get("user_id"),
                "path": str(manifest_path),
            })

    invitation_used_by_missing = []
    for code, inv in invitations.items():
        if not isinstance(inv, dict):
            continue
        used_by = inv.get("used_by")
        if used_by and used_by not in user_ids:
            invitation_used_by_missing.append({"code": code, "used_by": used_by})

    user_invitation_missing = []
    for uid, user in users.items():
        if not isinstance(user, dict):
            continue
        code = user.get("invitation_code")
        if code and code not in invitations:
            user_invitation_missing.append({"user_id": uid, "invitation_code": code})

    report = {
        "data_dir": str(root),
        "user_count": len(user_ids),
        "user_dir_count": len(dir_ids),
        "active_users": sorted(uid for uid, u in users.items()
                               if isinstance(u, dict) and u.get("status") == "active"),
        "delete_failed_users": sorted(uid for uid, u in users.items()
                                      if isinstance(u, dict) and u.get("status") == "delete_failed"),
        "orphan_dirs": sorted(dir_ids - user_ids),
        "missing_dirs": sorted(user_ids - dir_ids),
        "manifest_missing": manifest_missing,
        "manifest_mismatch": manifest_mismatch,
        "invitation_used_by_missing": invitation_used_by_missing,
        "user_invitation_missing": user_invitation_missing,
    }
    issue_keys = [
        "delete_failed_users",
        "orphan_dirs",
        "missing_dirs",
        "manifest_missing",
        "manifest_mismatch",
        "invitation_used_by_missing",
        "user_invitation_missing",
    ]
    report["ok"] = not any(report[k] for k in issue_keys)
    return report


def _print_human(report: dict):
    print(f"Data dir: {report['data_dir']}")
    print(f"Users: {report['user_count']} metadata / {report['user_dir_count']} dirs")
    print(f"Active users: {', '.join(report['active_users']) or '-'}")
    issue_keys = [
        "delete_failed_users",
        "orphan_dirs",
        "missing_dirs",
        "manifest_missing",
        "manifest_mismatch",
        "invitation_used_by_missing",
        "user_invitation_missing",
    ]
    if report["ok"]:
        print("OK: no isolation issues found")
        return
    print("ISSUES:")
    for key in issue_keys:
        value = report.get(key) or []
        if value:
            print(f"- {key}: {json.dumps(value, ensure_ascii=False)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit Miru user data isolation.")
    parser.add_argument("--data-dir", default=os.environ.get("DATA_DIR", "data"))
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument("--fail-on-issues", action="store_true", help="Exit 1 when issues are found.")
    args = parser.parse_args()

    report = audit(args.data_dir)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_human(report)
    return 1 if args.fail_on_issues and not report["ok"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
