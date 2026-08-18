from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

TERMINAL_STATES = {"completed", "failed", "cancelled", "planned"}


class MigrationStore:
    """Thread-safe SQLite WAL state, checkpoint and audit store."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=FULL;
                CREATE TABLE IF NOT EXISTS jobs (
                  id TEXT PRIMARY KEY, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                  status TEXT NOT NULL, source_uri TEXT NOT NULL, target_uri TEXT NOT NULL,
                  options_json TEXT NOT NULL, error TEXT NOT NULL DEFAULT '',
                  owner_id TEXT NOT NULL DEFAULT '', lease_expires_at REAL,
                  copied_docs INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS collection_tasks (
                  job_id TEXT NOT NULL, collection_name TEXT NOT NULL, target_name TEXT NOT NULL,
                  status TEXT NOT NULL DEFAULT 'pending', copied_docs INTEGER NOT NULL DEFAULT 0,
                  source_docs INTEGER NOT NULL DEFAULT 0, target_docs INTEGER NOT NULL DEFAULT 0,
                  verified INTEGER NOT NULL DEFAULT 0, sync_rounds INTEGER NOT NULL DEFAULT 0,
                  sampled_docs INTEGER NOT NULL DEFAULT 0,
                  sample_mismatches INTEGER NOT NULL DEFAULT 0,
                  source_digest TEXT NOT NULL DEFAULT '',
                  target_digest TEXT NOT NULL DEFAULT '',
                  error TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL,
                  PRIMARY KEY(job_id, collection_name)
                );
                CREATE TABLE IF NOT EXISTS checkpoints (
                  job_id TEXT NOT NULL, collection_name TEXT NOT NULL, key TEXT NOT NULL,
                  value_json TEXT NOT NULL, updated_at REAL NOT NULL,
                  PRIMARY KEY(job_id, collection_name, key)
                );
                CREATE TABLE IF NOT EXISTS events (
                  seq INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
                  created_at REAL NOT NULL, level TEXT NOT NULL, type TEXT NOT NULL,
                  collection_name TEXT NOT NULL DEFAULT '', message TEXT NOT NULL,
                  payload_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_events_job ON events(job_id, seq);
                CREATE TABLE IF NOT EXISTS target_leases (
                  target_endpoint TEXT NOT NULL, target_db TEXT NOT NULL,
                  target_name TEXT NOT NULL, job_id TEXT NOT NULL,
                  owner_id TEXT NOT NULL, lease_expires_at REAL NOT NULL,
                  PRIMARY KEY(target_endpoint, target_db, target_name)
                );
                CREATE TABLE IF NOT EXISTS backup_assets (
                  id TEXT PRIMARY KEY, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                  kind TEXT NOT NULL, status TEXT NOT NULL, path TEXT NOT NULL UNIQUE,
                  source_endpoint TEXT NOT NULL DEFAULT '', source_db TEXT NOT NULL DEFAULT '',
                  collections INTEGER NOT NULL DEFAULT 0, documents INTEGER NOT NULL DEFAULT 0,
                  size_bytes INTEGER NOT NULL DEFAULT 0, sha256 TEXT NOT NULL DEFAULT '',
                  encrypted INTEGER NOT NULL DEFAULT 0, verified_at REAL,
                  retention_until REAL, metadata_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_backup_assets_created
                  ON backup_assets(created_at DESC);
                """
            )
            columns = {
                row["name"]
                for row in self._conn.execute("PRAGMA table_info(collection_tasks)")
            }
            for name, definition in {
                "sampled_docs": "INTEGER NOT NULL DEFAULT 0",
                "sample_mismatches": "INTEGER NOT NULL DEFAULT 0",
                "source_digest": "TEXT NOT NULL DEFAULT ''",
                "target_digest": "TEXT NOT NULL DEFAULT ''",
            }.items():
                if name not in columns:
                    self._conn.execute(
                        f"ALTER TABLE collection_tasks ADD COLUMN {name} {definition}"
                    )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def create_or_resume(
        self, source_uri: str, target_uri: str, options: dict[str, Any], job_id: str = ""
    ) -> str:
        now = time.time()
        payload = json.dumps(options, sort_keys=True, ensure_ascii=False)
        job_id = job_id or uuid.uuid4().hex
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row:
                if (
                    row["source_uri"] != source_uri
                    or row["target_uri"] != target_uri
                    or json.loads(row["options_json"]) != json.loads(payload)
                ):
                    raise ValueError("job id exists with different endpoints or options")
                return job_id
            self._conn.execute(
                """INSERT INTO jobs(id,created_at,updated_at,status,source_uri,target_uri,options_json)
                   VALUES(?,?,?,'created',?,?,?)""",
                (job_id, now, now, source_uri, target_uri, payload),
            )
            self._conn.commit()
        return job_id

    def acquire(self, job_id: str, owner_id: str, ttl: int) -> None:
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT owner_id,lease_expires_at,status FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            if not row:
                raise KeyError(job_id)
            if (
                row["owner_id"]
                and row["owner_id"] != owner_id
                and (row["lease_expires_at"] or 0) > now
            ):
                raise RuntimeError("job is already running in another process")
            self._conn.execute(
                """UPDATE jobs SET owner_id=?,lease_expires_at=?,status='running',updated_at=?
                   WHERE id=?""",
                (owner_id, now + ttl, now, job_id),
            )
            self._conn.commit()

    def heartbeat(self, job_id: str, owner_id: str, ttl: int) -> None:
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                """UPDATE jobs SET lease_expires_at=?,updated_at=?
                   WHERE id=? AND owner_id=?""",
                (now + ttl, now, job_id, owner_id),
            )
            self._conn.commit()
            if cur.rowcount != 1:
                raise RuntimeError("job lease was lost")
            self._conn.execute(
                """UPDATE target_leases SET lease_expires_at=?
                   WHERE job_id=? AND owner_id=?""",
                (now + ttl, job_id, owner_id),
            )
            self._conn.commit()

    def acquire_target_leases(
        self,
        job_id: str,
        owner_id: str,
        target_endpoint: str,
        target_db: str,
        ttl: int,
    ) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "DELETE FROM target_leases WHERE lease_expires_at<=?", (now,)
                )
                rows = self._conn.execute(
                    "SELECT target_name FROM collection_tasks WHERE job_id=?", (job_id,)
                ).fetchall()
                for row in rows:
                    target_name = str(row["target_name"])
                    conflict = self._conn.execute(
                        """SELECT job_id FROM target_leases
                           WHERE target_endpoint=? AND target_db=? AND target_name=?
                           AND NOT(job_id=? AND owner_id=?)""",
                        (target_endpoint, target_db, target_name, job_id, owner_id),
                    ).fetchone()
                    if conflict:
                        raise RuntimeError(
                            f"target collection {target_name!r} is leased by job "
                            f"{conflict['job_id']}"
                        )
                    self._conn.execute(
                        """INSERT INTO target_leases(
                             target_endpoint,target_db,target_name,job_id,owner_id,lease_expires_at
                           ) VALUES(?,?,?,?,?,?)
                           ON CONFLICT(target_endpoint,target_db,target_name) DO UPDATE SET
                             job_id=excluded.job_id,owner_id=excluded.owner_id,
                             lease_expires_at=excluded.lease_expires_at""",
                        (
                            target_endpoint, target_db, target_name,
                            job_id, owner_id, now + ttl,
                        ),
                    )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def add_collection(self, job_id: str, name: str, target: str) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                """INSERT INTO collection_tasks(job_id,collection_name,target_name,updated_at)
                   VALUES(?,?,?,?) ON CONFLICT(job_id,collection_name) DO NOTHING""",
                (job_id, name, target, now),
            )
            self._conn.commit()

    def collection(self, job_id: str, name: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM collection_tasks WHERE job_id=? AND collection_name=?",
                (job_id, name),
            ).fetchone()
        return dict(row) if row else None

    def update_collection(self, job_id: str, name: str, **fields: Any) -> None:
        allowed = {
            "status", "copied_docs", "source_docs", "target_docs",
            "verified", "sync_rounds", "sampled_docs", "sample_mismatches",
            "source_digest", "target_digest", "error",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown fields: {sorted(unknown)}")
        fields["updated_at"] = time.time()
        assignments = ",".join(f"{key}=?" for key in fields)
        with self._lock:
            self._conn.execute(
                f"UPDATE collection_tasks SET {assignments} WHERE job_id=? AND collection_name=?",
                (*fields.values(), job_id, name),
            )
            self._conn.commit()

    def checkpoint(self, job_id: str, name: str, key: str, value: Any) -> None:
        from bson import json_util

        payload = json_util.dumps(value)
        with self._lock:
            self._conn.execute(
                """INSERT INTO checkpoints(job_id,collection_name,key,value_json,updated_at)
                   VALUES(?,?,?,?,?) ON CONFLICT(job_id,collection_name,key)
                   DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at""",
                (job_id, name, key, payload, time.time()),
            )
            self._conn.commit()

    def get_checkpoint(self, job_id: str, name: str, key: str) -> Any:
        from bson import json_util

        with self._lock:
            row = self._conn.execute(
                "SELECT value_json FROM checkpoints WHERE job_id=? AND collection_name=? AND key=?",
                (job_id, name, key),
            ).fetchone()
        return json_util.loads(row[0]) if row else None

    def event(
        self, job_id: str, level: str, kind: str, message: str,
        collection: str = "", payload: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO events(job_id,created_at,level,type,collection_name,message,payload_json)
                   VALUES(?,?,?,?,?,?,?)""",
                (
                    job_id, time.time(), level, kind, collection, message,
                    json.dumps(payload or {}, ensure_ascii=False, default=str),
                ),
            )
            self._conn.commit()

    def finish(self, job_id: str, status: str, error: str = "") -> None:
        if status not in TERMINAL_STATES:
            raise ValueError(status)
        with self._lock:
            self._conn.execute("DELETE FROM target_leases WHERE job_id=?", (job_id,))
            self._conn.execute(
                """UPDATE jobs SET status=?,error=?,owner_id='',lease_expires_at=NULL,updated_at=?
                   WHERE id=?""",
                (status, error, time.time(), job_id),
            )
            self._conn.commit()

    def list_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def report(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            tasks = self._conn.execute(
                "SELECT * FROM collection_tasks WHERE job_id=? ORDER BY collection_name", (job_id,)
            ).fetchall()
            events = self._conn.execute(
                "SELECT * FROM events WHERE job_id=? ORDER BY seq", (job_id,)
            ).fetchall()
        if not job:
            raise KeyError(job_id)
        return {
            "job": dict(job),
            "collections": [dict(row) for row in tasks],
            "events": [dict(row) for row in events],
        }

    def register_asset(
        self,
        *,
        kind: str,
        path: str | Path,
        status: str = "ready",
        source_endpoint: str = "",
        source_db: str = "",
        collections: int = 0,
        documents: int = 0,
        size_bytes: int = 0,
        sha256: str = "",
        encrypted: bool = False,
        verified: bool = False,
        retention_days: int = 0,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        if kind not in {"backup", "export"}:
            raise ValueError("asset kind must be backup or export")
        now = time.time()
        resolved = str(Path(path).expanduser().resolve())
        asset_id = uuid.uuid5(uuid.NAMESPACE_URL, resolved).hex
        retention_until = now + retention_days * 86400 if retention_days > 0 else None
        verified_at = now if verified else None
        with self._lock:
            self._conn.execute(
                """INSERT INTO backup_assets(
                     id,created_at,updated_at,kind,status,path,source_endpoint,source_db,
                     collections,documents,size_bytes,sha256,encrypted,verified_at,
                     retention_until,metadata_json
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(path) DO UPDATE SET
                     updated_at=excluded.updated_at,kind=excluded.kind,status=excluded.status,
                     source_endpoint=excluded.source_endpoint,source_db=excluded.source_db,
                     collections=excluded.collections,documents=excluded.documents,
                     size_bytes=excluded.size_bytes,sha256=excluded.sha256,
                     encrypted=excluded.encrypted,
                     verified_at=COALESCE(excluded.verified_at,backup_assets.verified_at),
                     retention_until=excluded.retention_until,
                     metadata_json=excluded.metadata_json""",
                (
                    asset_id, now, now, kind, status, resolved, source_endpoint, source_db,
                    collections, documents, size_bytes, sha256, int(encrypted), verified_at,
                    retention_until,
                    json.dumps(metadata or {}, ensure_ascii=False, default=str),
                ),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT id FROM backup_assets WHERE path=?", (resolved,)
            ).fetchone()
        return str(row["id"])

    def mark_asset_verified(self, path: str | Path) -> None:
        resolved = str(Path(path).expanduser().resolve())
        now = time.time()
        with self._lock:
            cursor = self._conn.execute(
                """UPDATE backup_assets SET status='ready',verified_at=?,updated_at=?
                   WHERE path=?""",
                (now, now, resolved),
            )
            self._conn.commit()
        if cursor.rowcount != 1:
            raise KeyError(resolved)

    def list_assets(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM backup_assets ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def forget_asset(self, asset_id: str) -> None:
        """Remove only the catalog record; the archive itself is never deleted here."""
        with self._lock:
            self._conn.execute("DELETE FROM backup_assets WHERE id=?", (asset_id,))
            self._conn.commit()

    def expired_assets(self, now: float | None = None) -> list[dict[str, Any]]:
        timestamp = time.time() if now is None else now
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM backup_assets
                   WHERE retention_until IS NOT NULL AND retention_until<=?
                   ORDER BY retention_until""",
                (timestamp,),
            ).fetchall()
        return [dict(row) for row in rows]
