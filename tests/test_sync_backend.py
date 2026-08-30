import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sync_backend import SyncBackend, _sanitize_database_url


class _FakeExecCursor:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.calls = []
        self.rowcount = 0

    def execute(self, query, params=None):
        self.calls.append((query, params))
        self.rowcount = 1

    def fetchall(self):
        return list(self.rows)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_sanitize_database_url_hides_password():
    raw = "postgresql://alice:secret@localhost:5432/contextlife"
    masked = _sanitize_database_url(raw)
    assert masked == "postgresql://alice:***@localhost:5432/contextlife"


def test_iter_data_json_paths_includes_known_and_extra(tmp_path):
    (tmp_path / "timeline.json").write_text("[]", encoding="utf-8")
    (tmp_path / "custom_debug.json").write_text(json.dumps({"x": 1}), encoding="utf-8")

    backend = SyncBackend(data_dir=str(tmp_path))
    names = [p.name for p in backend._iter_data_json_paths()]

    assert "timeline.json" in names
    assert "custom_debug.json" in names


def test_push_uploads_respects_inline_size_limit(tmp_path):
    backend = SyncBackend(data_dir=str(tmp_path))
    backend.sync_uploads = True
    backend.upload_inline = True
    backend.max_inline_bytes = 8
    backend.uploads_dir = tmp_path / "uploads"
    backend.uploads_dir.mkdir(parents=True, exist_ok=True)

    (backend.uploads_dir / "small.txt").write_bytes(b"small")
    (backend.uploads_dir / "large.txt").write_bytes(b"0123456789ABCDEF")

    cur = _FakeExecCursor()
    result = backend._push_uploads(cur)

    assert result["enabled"] is True
    assert result["pushed"] == 2
    assert any(item["file"] == "large.txt" for item in result["skipped_large"])

    inline_by_file = {}
    for _query, params in cur.calls:
        file_name = params[1]
        is_inline = params[5]
        inline_by_file[file_name] = is_inline

    assert inline_by_file["small.txt"] is True
    assert inline_by_file["large.txt"] is False


def test_pull_uploads_restores_inline_blobs_only(tmp_path):
    backend = SyncBackend(data_dir=str(tmp_path))
    backend.sync_uploads = True
    backend.upload_inline = True
    backend.uploads_dir = tmp_path / "uploads"
    backend.uploads_dir.mkdir(parents=True, exist_ok=True)

    same_content = b"same"
    (backend.uploads_dir / "same.jpg").write_bytes(same_content)

    rows = [
        ("same.jpg", _sha256(same_content), True, same_content),
        ("new.jpg", _sha256(b"new"), True, b"new"),
        ("meta_only.jpg", _sha256(b"meta"), False, None),
    ]
    cur = _FakeExecCursor(rows=rows)
    result = backend._pull_uploads(cur)

    assert result["enabled"] is True
    assert result["unchanged"] == 1
    assert result["pulled"] == 1
    assert result["metadata_only"] == 1
    assert (backend.uploads_dir / "new.jpg").read_bytes() == b"new"


def test_update_config_persists_for_ui(tmp_path):
    backend = SyncBackend(data_dir=str(tmp_path))
    result = backend.update_config(
        {
            "database_url": "postgresql://u:p@localhost:5432/db",
            "user_id": "mobile-user",
            "sync_uploads": True,
            "upload_inline": True,
            "max_inline_mb": 16,
            "sync_interval_seconds": 120,
        }
    )
    assert result["ok"] is True

    cfg = backend.get_config()
    assert cfg["has_database_url"] is True
    assert cfg["user_id"] == "mobile-user"
    assert cfg["upload_inline"] is True
    assert cfg["max_inline_mb"] == 16.0
    assert cfg["sync_interval_seconds"] == 120
    assert "sync_config.json" in cfg["config_file"]
