"""Optional PostgreSQL sync backend for ContextLife.

This module keeps the local JSON-first architecture and adds an opt-in
database sync layer for multi-device use.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import storage

try:
    import psycopg
except Exception:  # pragma: no cover - optional dependency at runtime
    psycopg = None


_SYNC_DOC_TABLE = "contextlife_sync_documents"
_SYNC_UPLOAD_TABLE = "contextlife_sync_uploads"
_DEFAULT_UPLOAD_MAX_INLINE_MB = 8
_MB = 1024 * 1024


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_canonical_bytes(payload) -> bytes:
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return text.encode("utf-8")


def _sanitize_database_url(url: str) -> str:
    """Hide password part for API/status display."""
    if not url:
        return ""
    parsed = urlsplit(url)
    if "@" not in parsed.netloc or ":" not in parsed.netloc.split("@")[0]:
        return url
    auth, host = parsed.netloc.rsplit("@", 1)
    user, _pwd = auth.split(":", 1)
    return urlunsplit((parsed.scheme, f"{user}:***@{host}", parsed.path, parsed.query, parsed.fragment))


def _coerce_payload(value):
    if isinstance(value, (dict, list)):
        return value
    if value is None:
        return {}
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        return json.loads(text)
    return value


class SyncBackend:
    """Best-effort local<->PostgreSQL sync for JSON data and optional uploads."""

    DEFAULT_DOC_KEYS = [
        "timeline.json",
        "daily_reviews.json",
        "tomorrow_plan.json",
        "tomorrow_plans.json",
        "daily_reminder_plan.json",
        "reminders.json",
        "chat_history.json",
        "push_subscriptions.json",
    ]

    def __init__(self, data_dir: str | None = None):
        self.data_dir = Path(data_dir or storage.get_data_dir())
        self.uploads_dir = Path(storage._uploads_dir())
        self.config_path = self.data_dir / "sync_config.json"
        self._env_defaults = {
            "database_url": os.environ.get("SYNC_DATABASE_URL", "").strip(),
            "user_id": os.environ.get("SYNC_USER_ID", "default").strip() or "default",
            "sync_uploads": _env_bool("SYNC_UPLOADS_ENABLED", True),
            "upload_inline": _env_bool("SYNC_UPLOAD_INLINE", False),
            "sync_interval_seconds": int(os.environ.get("SYNC_INTERVAL_SECONDS", "0") or "0"),
            "max_inline_mb": float(os.environ.get("SYNC_UPLOAD_MAX_INLINE_MB", str(_DEFAULT_UPLOAD_MAX_INLINE_MB))),
        }

        self.database_url = ""
        self.user_id = "default"
        self.sync_uploads = True
        self.upload_inline = False
        self.sync_interval_seconds = 0
        self.max_inline_bytes = int(_DEFAULT_UPLOAD_MAX_INLINE_MB * _MB)
        self.enabled = False
        self._lock = threading.Lock()
        self._last_result = None
        self._config_source = "env"
        self.refresh_config()

    def _load_saved_config(self) -> dict:
        if not self.config_path.exists():
            return {}
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_saved_config(self, cfg: dict) -> None:
        storage._ensure_dirs()
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)

    def refresh_config(self) -> None:
        saved = self._load_saved_config()

        def _take(name):
            if name in saved:
                return saved.get(name)
            return self._env_defaults.get(name)

        self.database_url = str(_take("database_url") or "").strip()
        self.user_id = str(_take("user_id") or "default").strip() or "default"
        self.sync_uploads = bool(_take("sync_uploads"))
        self.upload_inline = bool(_take("upload_inline"))

        try:
            interval = int(_take("sync_interval_seconds") or 0)
        except Exception:
            interval = 0
        self.sync_interval_seconds = max(0, interval)

        try:
            max_inline_mb = float(_take("max_inline_mb") or _DEFAULT_UPLOAD_MAX_INLINE_MB)
        except Exception:
            max_inline_mb = float(_DEFAULT_UPLOAD_MAX_INLINE_MB)
        max_inline_mb = min(max(max_inline_mb, 1.0), 1024.0)
        self.max_inline_bytes = int(max_inline_mb * _MB)

        self._config_source = "saved" if saved else "env"
        self.enabled = bool(self.database_url) and psycopg is not None

    def get_config(self) -> dict:
        self.refresh_config()
        return {
            "ok": True,
            "source": self._config_source,
            "config_file": str(self.config_path),
            "driver_ready": psycopg is not None,
            "has_database_url": bool(self.database_url),
            "database_url_masked": _sanitize_database_url(self.database_url),
            "user_id": self.user_id,
            "sync_uploads": self.sync_uploads,
            "upload_inline": self.upload_inline,
            "max_inline_mb": round(self.max_inline_bytes / _MB, 2),
            "sync_interval_seconds": self.sync_interval_seconds,
        }

    def update_config(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            return {"ok": False, "error": "invalid payload"}

        saved = self._load_saved_config()

        if payload.get("clear_database_url"):
            saved["database_url"] = ""
        elif "database_url" in payload and payload.get("database_url") is not None:
            saved["database_url"] = str(payload.get("database_url") or "").strip()

        if "user_id" in payload and payload.get("user_id") is not None:
            saved["user_id"] = str(payload.get("user_id") or "default").strip() or "default"

        if "sync_uploads" in payload:
            saved["sync_uploads"] = bool(payload.get("sync_uploads"))

        if "upload_inline" in payload:
            saved["upload_inline"] = bool(payload.get("upload_inline"))

        if "max_inline_mb" in payload and payload.get("max_inline_mb") is not None:
            try:
                saved["max_inline_mb"] = min(max(float(payload.get("max_inline_mb")), 1.0), 1024.0)
            except Exception:
                return {"ok": False, "error": "max_inline_mb must be a number"}

        if "sync_interval_seconds" in payload and payload.get("sync_interval_seconds") is not None:
            try:
                saved["sync_interval_seconds"] = max(0, int(payload.get("sync_interval_seconds")))
            except Exception:
                return {"ok": False, "error": "sync_interval_seconds must be an integer"}

        self._save_saved_config(saved)
        self.refresh_config()
        return {"ok": True, "config": self.get_config()}

    def ping(self) -> dict:
        self.refresh_config()
        if not self.enabled:
            reason = "SYNC_DATABASE_URL is not configured"
            if self.database_url and psycopg is None:
                reason = "psycopg unavailable: install psycopg[binary]"
            return {"ok": False, "error": reason}
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    self._ensure_schema(cur)
                conn.commit()
            return {
                "ok": True,
                "database": _sanitize_database_url(self.database_url),
                "user_id": self.user_id,
                "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def status(self) -> dict:
        self.refresh_config()
        return {
            "enabled": self.enabled,
            "driver_ready": psycopg is not None,
            "database": _sanitize_database_url(self.database_url),
            "user_id": self.user_id,
            "sync_interval_seconds": self.sync_interval_seconds,
            "uploads": {
                "enabled": self.sync_uploads,
                "mode": "inline" if self.upload_inline else "metadata_only",
                "max_inline_mb": round(self.max_inline_bytes / 1024 / 1024, 2),
            },
            "last_result": self._last_result,
        }

    def _connect(self):
        return psycopg.connect(self.database_url)

    def _ensure_schema(self, cur) -> None:
        cur.execute(
            f"""
            create table if not exists {_SYNC_DOC_TABLE} (
              user_id text not null,
              doc_key text not null,
              payload jsonb not null,
              sha256 text not null,
              updated_at timestamptz not null default now(),
              primary key (user_id, doc_key)
            );
            """
        )
        cur.execute(
            f"""
            create table if not exists {_SYNC_UPLOAD_TABLE} (
              user_id text not null,
              file_name text not null,
              sha256 text not null,
              size_bytes bigint not null,
              mime_type text not null default '',
              is_inline boolean not null default false,
              content bytea,
              updated_at timestamptz not null default now(),
              primary key (user_id, file_name)
            );
            """
        )

    def _iter_data_json_paths(self) -> list[Path]:
        seen = set()
        result = []

        for key in self.DEFAULT_DOC_KEYS:
            p = self.data_dir / key
            if p.exists() and p.is_file():
                result.append(p)
                seen.add(p.name)

        for p in sorted(self.data_dir.glob("*.json")):
            if p.name in seen:
                continue
            if p.is_file():
                result.append(p)
        return result

    def _push_documents(self, cur) -> dict:
        pushed = 0
        unchanged = 0
        failed = []

        for path in self._iter_data_json_paths():
            try:
                payload = storage.read_json(str(path))
                payload_raw = _json_canonical_bytes(payload)
                sha = _sha256_bytes(payload_raw)
                cur.execute(
                    f"""
                    insert into {_SYNC_DOC_TABLE}
                      (user_id, doc_key, payload, sha256, updated_at)
                    values
                      (%s, %s, %s::jsonb, %s, now())
                    on conflict (user_id, doc_key)
                    do update set
                      payload = excluded.payload,
                      sha256 = excluded.sha256,
                      updated_at = now()
                    where {_SYNC_DOC_TABLE}.sha256 is distinct from excluded.sha256
                    """,
                    (self.user_id, path.name, payload_raw.decode("utf-8"), sha),
                )
                if cur.rowcount:
                    pushed += 1
                else:
                    unchanged += 1
            except Exception as exc:
                failed.append({"doc": path.name, "error": str(exc)})

        return {
            "pushed": pushed,
            "unchanged": unchanged,
            "failed": failed,
        }

    def _pull_documents(self, cur) -> dict:
        pulled = 0
        unchanged = 0
        failed = []

        cur.execute(
            f"""
            select doc_key, payload, sha256
            from {_SYNC_DOC_TABLE}
            where user_id = %s
            order by doc_key asc
            """,
            (self.user_id,),
        )
        rows = cur.fetchall()

        for doc_key, remote_payload, remote_sha in rows:
            path = self.data_dir / doc_key
            try:
                remote_payload = _coerce_payload(remote_payload)
                remote_bytes = _json_canonical_bytes(remote_payload)
                if remote_sha and remote_sha != _sha256_bytes(remote_bytes):
                    remote_sha = _sha256_bytes(remote_bytes)

                local_sha = None
                if path.exists():
                    local_payload = storage.read_json(str(path))
                    local_sha = _sha256_bytes(_json_canonical_bytes(local_payload))

                if local_sha and local_sha == remote_sha:
                    unchanged += 1
                    continue

                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(remote_payload, f, ensure_ascii=False, indent=2)
                pulled += 1
            except Exception as exc:
                failed.append({"doc": doc_key, "error": str(exc)})

        return {
            "pulled": pulled,
            "unchanged": unchanged,
            "failed": failed,
        }

    def _push_uploads(self, cur) -> dict:
        if not self.sync_uploads:
            return {"enabled": False, "reason": "SYNC_UPLOADS_ENABLED=false"}

        self.uploads_dir.mkdir(parents=True, exist_ok=True)

        pushed = 0
        unchanged = 0
        failed = []
        skipped_large = []

        for path in sorted(self.uploads_dir.glob("*")):
            if not path.is_file():
                continue
            try:
                data = path.read_bytes()
                size = len(data)
                sha = _sha256_bytes(data)
                mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"

                is_inline = self.upload_inline and size <= self.max_inline_bytes
                blob = data if is_inline else None

                if self.upload_inline and size > self.max_inline_bytes:
                    skipped_large.append({"file": path.name, "size_bytes": size})

                cur.execute(
                    f"""
                    insert into {_SYNC_UPLOAD_TABLE}
                      (user_id, file_name, sha256, size_bytes, mime_type, is_inline, content, updated_at)
                    values
                      (%s, %s, %s, %s, %s, %s, %s, now())
                    on conflict (user_id, file_name)
                    do update set
                      sha256 = excluded.sha256,
                      size_bytes = excluded.size_bytes,
                      mime_type = excluded.mime_type,
                      is_inline = excluded.is_inline,
                      content = excluded.content,
                      updated_at = now()
                    where {_SYNC_UPLOAD_TABLE}.sha256 is distinct from excluded.sha256
                       or {_SYNC_UPLOAD_TABLE}.is_inline is distinct from excluded.is_inline
                    """,
                    (self.user_id, path.name, sha, size, mime, is_inline, blob),
                )
                if cur.rowcount:
                    pushed += 1
                else:
                    unchanged += 1
            except Exception as exc:
                failed.append({"file": path.name, "error": str(exc)})

        return {
            "enabled": True,
            "mode": "inline" if self.upload_inline else "metadata_only",
            "pushed": pushed,
            "unchanged": unchanged,
            "skipped_large": skipped_large,
            "failed": failed,
        }

    def _pull_uploads(self, cur) -> dict:
        if not self.sync_uploads:
            return {"enabled": False, "reason": "SYNC_UPLOADS_ENABLED=false"}

        self.uploads_dir.mkdir(parents=True, exist_ok=True)

        pulled = 0
        unchanged = 0
        failed = []
        metadata_only = 0

        cur.execute(
            f"""
            select file_name, sha256, is_inline, content
            from {_SYNC_UPLOAD_TABLE}
            where user_id = %s
            order by file_name asc
            """,
            (self.user_id,),
        )
        rows = cur.fetchall()

        for file_name, remote_sha, is_inline, content in rows:
            if not is_inline or content is None:
                metadata_only += 1
                continue

            path = self.uploads_dir / file_name
            try:
                local_sha = None
                if path.exists() and path.is_file():
                    local_sha = _sha256_bytes(path.read_bytes())
                if local_sha and local_sha == remote_sha:
                    unchanged += 1
                    continue
                path.write_bytes(content)
                pulled += 1
            except Exception as exc:
                failed.append({"file": file_name, "error": str(exc)})

        return {
            "enabled": True,
            "mode": "inline" if self.upload_inline else "metadata_only",
            "pulled": pulled,
            "unchanged": unchanged,
            "metadata_only": metadata_only,
            "failed": failed,
        }

    def run(self, mode: str = "bidirectional", include_uploads: bool = True) -> dict:
        self.refresh_config()
        if mode not in ("push", "pull", "bidirectional"):
            return {"ok": False, "error": f"invalid mode: {mode}"}
        if not self.enabled:
            reason = "sync disabled: set SYNC_DATABASE_URL and install psycopg[binary]"
            if self.database_url and psycopg is None:
                reason = "psycopg unavailable: install psycopg[binary]"
            elif not self.database_url:
                reason = "SYNC_DATABASE_URL is not configured"
            result = {"ok": False, "error": reason, "mode": mode}
            self._last_result = result
            return result

        with self._lock:
            started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            result = {
                "ok": True,
                "mode": mode,
                "started_at": started,
                "include_uploads": include_uploads,
            }
            try:
                with self._connect() as conn:
                    with conn.cursor() as cur:
                        self._ensure_schema(cur)

                        if mode in ("push", "bidirectional"):
                            result["push"] = self._push_documents(cur)
                            if include_uploads:
                                result["push_uploads"] = self._push_uploads(cur)

                        if mode in ("pull", "bidirectional"):
                            result["pull"] = self._pull_documents(cur)
                            if include_uploads:
                                result["pull_uploads"] = self._pull_uploads(cur)

                    conn.commit()
            except Exception as exc:
                result = {
                    "ok": False,
                    "mode": mode,
                    "started_at": started,
                    "include_uploads": include_uploads,
                    "error": str(exc),
                }

            result["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._last_result = result
            return result


_SYNC_BACKEND = None


def get_sync_backend() -> SyncBackend:
    global _SYNC_BACKEND
    if _SYNC_BACKEND is None:
        _SYNC_BACKEND = SyncBackend()
    return _SYNC_BACKEND
