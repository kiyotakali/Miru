import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def test_audit_reports_orphans_and_manifest_issues(tmp_path):
    from scripts.audit_user_data_isolation import audit

    data_dir = tmp_path / "data"
    admin_dir = data_dir / "_admin"
    users_dir = data_dir / "users"
    admin_dir.mkdir(parents=True)
    users_dir.mkdir()

    (admin_dir / "users.json").write_text(json.dumps({
        "u_active": {
            "token": "tok",
            "status": "active",
            "invitation_code": "MIRU-ACTIVE",
            "created_at": "2026-05-18T00:00:00",
        },
        "u_missing": {
            "token": "tok2",
            "status": "active",
            "invitation_code": "MIRU-MISSING",
        },
        "u_failed": {
            "token": "tok3",
            "status": "delete_failed",
        },
    }), encoding="utf-8")
    (admin_dir / "invitations.json").write_text(json.dumps({
        "MIRU-ACTIVE": {"used_by": "u_active"},
        "MIRU-GHOST": {"used_by": "u_ghost"},
    }), encoding="utf-8")
    (users_dir / "u_active").mkdir()
    (users_dir / "u_orphan").mkdir()

    report = audit(data_dir)

    assert report["ok"] is False
    assert report["orphan_dirs"] == ["u_orphan"]
    assert "u_missing" in report["missing_dirs"]
    assert report["manifest_missing"] == ["u_active"]
    assert report["delete_failed_users"] == ["u_failed"]
    assert report["invitation_used_by_missing"] == [{"code": "MIRU-GHOST", "used_by": "u_ghost"}]
