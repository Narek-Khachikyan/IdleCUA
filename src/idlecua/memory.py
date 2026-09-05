from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    state TEXT NOT NULL,
    plan_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actions (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    target_url TEXT,
    verdict TEXT,
    status TEXT NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES tasks(id)
);
CREATE TABLE IF NOT EXISTS queries (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    query TEXT NOT NULL,
    normalized TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES tasks(id)
);
CREATE TABLE IF NOT EXISTS urls (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    url TEXT NOT NULL,
    normalized TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES tasks(id)
);
CREATE TABLE IF NOT EXISTS findings (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    summary TEXT NOT NULL,
    relevance TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES tasks(id)
);
CREATE TABLE IF NOT EXISTS errors (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES tasks(id)
);
CREATE TABLE IF NOT EXISTS reports (
    task_id TEXT PRIMARY KEY,
    markdown TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES tasks(id)
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_queries_normalized_created ON queries(normalized, created_at);
CREATE INDEX IF NOT EXISTS idx_urls_fingerprint_created ON urls(fingerprint, created_at);
CREATE INDEX IF NOT EXISTS idx_urls_normalized_created ON urls(normalized, created_at);
CREATE TABLE IF NOT EXISTS active_session (
    task_id TEXT PRIMARY KEY,
    pid INTEGER NOT NULL,
    acquired_at TEXT NOT NULL
);
"""

# TaskLifecycle persistence version. v2 adds checkpoint columns + lease table
# plus safety-preserving legacy-state conversion.
LIFECYCLE_SCHEMA_VERSION = 2

_TASK_CHECKPOINT_COLUMNS: dict[str, str] = {
    "plan_progress": "INTEGER NOT NULL DEFAULT 0",
    "active_duration_s": "REAL NOT NULL DEFAULT 0",
    "skipped_types": "TEXT NOT NULL DEFAULT '[]'",
    "last_outcome": "TEXT",
    "stop_cause": "TEXT",
    "failure_cause": "TEXT",
}


class MemoryStore:
    """Single local SQLite database.

    Never stores: API keys, tokens, passwords, cookies, raw sensitive provider payloads.
    """

    def __init__(self, data_dir: Path | str, db_name: str = "memory.db") -> None:
        self.data_dir = Path(data_dir).expanduser()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / db_name
        self._lock = threading.RLock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(SCHEMA)
                conn.commit()
                self._migrate(conn)
            finally:
                conn.close()

    def _table_columns(self, conn: sqlite3.Connection, table: str) -> set[str]:
        cur = conn.execute(f"PRAGMA table_info({table})")
        return {str(r["name"]) for r in cur.fetchall()}

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """Additive transactional migration to LIFECYCLE_SCHEMA_VERSION.

        Safety-preserving legacy conversion; failure rolls back so partially
        migrated state never controls the computer (caller blocks execution).
        """
        try:
            cur = conn.execute("PRAGMA user_version")
            row = cur.fetchone()
            version = int(row[0]) if row else 0
        except Exception:
            version = 0
        if version >= LIFECYCLE_SCHEMA_VERSION:
            return
        try:
            conn.execute("BEGIN IMMEDIATE")
            cols = self._table_columns(conn, "tasks")
            for name, ddl in _TASK_CHECKPOINT_COLUMNS.items():
                if name not in cols:
                    conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {ddl}")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS active_session (
                    task_id TEXT PRIMARY KEY,
                    pid INTEGER NOT NULL,
                    acquired_at TEXT NOT NULL
                )"""
            )
            # Legacy-state conversion (only once, from version 0).
            if version == 0:
                # `disabled` Tasks never start without renewed intent.
                conn.execute(
                    "UPDATE tasks SET state='stopped', stop_cause='legacy_not_queued', updated_at=? "
                    "WHERE state='disabled'",
                    (_now_iso(),),
                )
                # Stale in-flight Tasks cannot be resumed safely.
                conn.execute(
                    "UPDATE tasks SET state='failed', failure_cause='interrupted_unknown', updated_at=? "
                    "WHERE state IN ('planning','running')",
                    (_now_iso(),),
                )
                # Pre-ADR UI alias.
                conn.execute(
                    "UPDATE tasks SET state='waiting_for_idle', updated_at=? WHERE state='queued'",
                    (_now_iso(),),
                )
            conn.execute(f"PRAGMA user_version={LIFECYCLE_SCHEMA_VERSION}")
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise

    # -- tasks --

    def upsert_task(self, task_id: str, description: str, state: str, plan_json: str | None = None) -> None:
        now = _now_iso()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO tasks(id, description, state, plan_json, created_at, updated_at)
                    VALUES(?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET
                        description=excluded.description,
                        state=excluded.state,
                        plan_json=COALESCE(excluded.plan_json, plan_json),
                        updated_at=excluded.updated_at
                    """,
                    (task_id, description, state, plan_json, now, now),
                )
                conn.commit()
            finally:
                conn.close()

    def update_task_state(self, task_id: str, state: str) -> None:
        now = _now_iso()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("UPDATE tasks SET state=?, updated_at=? WHERE id=?", (state, now, task_id))
                conn.commit()
            finally:
                conn.close()

    def update_task_checkpoint(
        self,
        task_id: str,
        *,
        plan_progress: int | None = None,
        active_duration_s: float | None = None,
        skipped_types: list[str] | None = None,
        last_outcome: str | None = None,
        stop_cause: str | None = None,
        failure_cause: str | None = None,
        plan_json: str | None = None,
    ) -> None:
        """Fail-closed checkpoint write — caller must stop before next Action on error."""
        now = _now_iso()
        sets: list[str] = ["updated_at=?"]
        vals: list[Any] = [now]
        if plan_progress is not None:
            sets.append("plan_progress=?")
            vals.append(int(plan_progress))
        if active_duration_s is not None:
            sets.append("active_duration_s=?")
            vals.append(float(active_duration_s))
        if skipped_types is not None:
            sets.append("skipped_types=?")
            vals.append(json.dumps(sorted(set(skipped_types))))
        if last_outcome is not None:
            sets.append("last_outcome=?")
            vals.append(str(last_outcome))
        if stop_cause is not None:
            sets.append("stop_cause=?")
            vals.append(str(stop_cause))
        if failure_cause is not None:
            sets.append("failure_cause=?")
            vals.append(str(failure_cause))
        if plan_json is not None:
            sets.append("plan_json=?")
            vals.append(plan_json)
        vals.append(task_id)
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", vals)
                if cur.rowcount == 0:
                    raise KeyError(f"task not found: {task_id}")
                conn.commit()
            finally:
                conn.close()

    def set_task_plan_if_absent(self, task_id: str, plan_json: str) -> bool:
        """Persist the immutable Plan exactly once. Returns True if stored."""
        now = _now_iso()
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute("SELECT plan_json FROM tasks WHERE id=?", (task_id,))
                row = cur.fetchone()
                if row is None:
                    raise KeyError(f"task not found: {task_id}")
                if row["plan_json"]:
                    return False
                conn.execute(
                    "UPDATE tasks SET plan_json=?, updated_at=? WHERE id=?",
                    (plan_json, now, task_id),
                )
                conn.commit()
                return True
            finally:
                conn.close()

    # -- active-Session lease (singleton per data dir) --

    def lease_get(self) -> dict | None:
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute("SELECT * FROM active_session LIMIT 1")
                row = cur.fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

    def lease_acquire(self, task_id: str, pid: int) -> None:
        now = _now_iso()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("DELETE FROM active_session")
                conn.execute(
                    "INSERT INTO active_session(task_id, pid, acquired_at) VALUES(?,?,?)",
                    (task_id, int(pid), now),
                )
                conn.commit()
            finally:
                conn.close()

    def lease_release(self, task_id: str | None = None) -> None:
        with self._lock:
            conn = self._connect()
            try:
                if task_id is None:
                    conn.execute("DELETE FROM active_session")
                else:
                    conn.execute("DELETE FROM active_session WHERE task_id=?", (task_id,))
                conn.commit()
            finally:
                conn.close()

    def get_task(self, task_id: str) -> dict | None:
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,))
                row = cur.fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

    def list_tasks(self, limit: int = 100) -> list[dict]:
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute("SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,))
                return [dict(r) for r in cur.fetchall()]
            finally:
                conn.close()

    def list_tasks_fifo(self, states: list[str], limit: int = 100) -> list[dict]:
        """Oldest-first listing for deterministic Watch-loop selection."""
        if not states:
            return []
        marks = ",".join("?" for _ in states)
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    f"SELECT * FROM tasks WHERE state IN ({marks}) ORDER BY created_at ASC LIMIT ?",
                    (*states, limit),
                )
                return [dict(r) for r in cur.fetchall()]
            finally:
                conn.close()

    # -- actions --

    def record_action(
        self,
        action_id: str,
        task_id: str,
        kind: str,
        target_url: str | None,
        verdict: str,
        status: str,
        error: str | None = None,
    ) -> None:
        now = _now_iso()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO actions(id, task_id, kind, target_url, verdict, status, error, created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (action_id, task_id, kind, target_url, verdict, status, error, now),
                )
                conn.commit()
            finally:
                conn.close()

    def list_actions(self, task_id: str | None = None) -> list[dict]:
        with self._lock:
            conn = self._connect()
            try:
                if task_id:
                    cur = conn.execute("SELECT * FROM actions WHERE task_id=? ORDER BY created_at", (task_id,))
                else:
                    cur = conn.execute("SELECT * FROM actions ORDER BY created_at DESC LIMIT 200")
                return [dict(r) for r in cur.fetchall()]
            finally:
                conn.close()

    def update_action_status(self, action_id: str, status: str, error: str | None = None) -> None:
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "UPDATE actions SET status=?, error=? WHERE id=?",
                    (status, error, action_id),
                )
                if cur.rowcount == 0:
                    raise KeyError(f"action not found: {action_id}")
                conn.commit()
            finally:
                conn.close()

    def count_actions_for_task(self, task_id: str) -> int:
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute("SELECT COUNT(*) as c FROM actions WHERE task_id=?", (task_id,))
                row = cur.fetchone()
                return int(row["c"]) if row else 0
            finally:
                conn.close()

    # -- queries --

    def record_query(self, query_id: str, task_id: str, query: str, normalized: str) -> None:
        now = _now_iso()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO queries(id, task_id, query, normalized, created_at) VALUES(?,?,?,?,?)",
                    (query_id, task_id, query, normalized, now),
                )
                conn.commit()
            finally:
                conn.close()

    def list_queries(self, limit: int = 200) -> list[dict]:
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute("SELECT * FROM queries ORDER BY created_at DESC LIMIT ?", (limit,))
                return [dict(r) for r in cur.fetchall()]
            finally:
                conn.close()

    def has_query_within_days(self, normalized: str, days: int = 7) -> bool:
        cutoff = (_utc_now() - timedelta(days=days)).isoformat()
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "SELECT 1 FROM queries WHERE normalized=? AND created_at >= ? LIMIT 1",
                    (normalized, cutoff),
                )
                return cur.fetchone() is not None
            finally:
                conn.close()

    # -- urls --

    def record_url(self, url_id: str, task_id: str, url: str, normalized: str, fingerprint: str) -> None:
        now = _now_iso()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO urls(id, task_id, url, normalized, fingerprint, created_at) VALUES(?,?,?,?,?,?)",
                    (url_id, task_id, url, normalized, fingerprint, now),
                )
                conn.commit()
            finally:
                conn.close()

    def list_urls(self, limit: int = 200) -> list[dict]:
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute("SELECT * FROM urls ORDER BY created_at DESC LIMIT ?", (limit,))
                return [dict(r) for r in cur.fetchall()]
            finally:
                conn.close()

    def has_url_within_days(self, fingerprint: str, days: int = 7) -> bool:
        cutoff = (_utc_now() - timedelta(days=days)).isoformat()
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "SELECT 1 FROM urls WHERE fingerprint=? AND created_at >= ? LIMIT 1",
                    (fingerprint, cutoff),
                )
                return cur.fetchone() is not None
            finally:
                conn.close()

    def has_normalized_url_within_days(self, normalized: str, days: int = 7) -> bool:
        cutoff = (_utc_now() - timedelta(days=days)).isoformat()
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "SELECT 1 FROM urls WHERE normalized=? AND created_at >= ? LIMIT 1",
                    (normalized, cutoff),
                )
                return cur.fetchone() is not None
            finally:
                conn.close()

    # -- findings --

    def record_finding(
        self, finding_id: str, task_id: str, title: str, url: str, summary: str, relevance: str | None = None
    ) -> None:
        now = _now_iso()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO findings(id, task_id, title, url, summary, relevance, created_at) VALUES(?,?,?,?,?,?,?)",
                    (finding_id, task_id, title, url, summary, relevance, now),
                )
                conn.commit()
            finally:
                conn.close()

    def list_findings(self, task_id: str | None = None, limit: int = 200) -> list[dict]:
        with self._lock:
            conn = self._connect()
            try:
                if task_id:
                    cur = conn.execute("SELECT * FROM findings WHERE task_id=? ORDER BY created_at", (task_id,))
                else:
                    cur = conn.execute("SELECT * FROM findings ORDER BY created_at DESC LIMIT ?", (limit,))
                return [dict(r) for r in cur.fetchall()]
            finally:
                conn.close()

    # -- errors --

    def record_error(self, error_id: str, task_id: str, message: str) -> None:
        now = _now_iso()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO errors(id, task_id, message, created_at) VALUES(?,?,?,?)",
                    (error_id, task_id, message, now),
                )
                conn.commit()
            finally:
                conn.close()

    def list_errors(self, task_id: str | None = None) -> list[dict]:
        with self._lock:
            conn = self._connect()
            try:
                if task_id:
                    cur = conn.execute("SELECT * FROM errors WHERE task_id=? ORDER BY created_at", (task_id,))
                else:
                    cur = conn.execute("SELECT * FROM errors ORDER BY created_at DESC LIMIT 200")
                return [dict(r) for r in cur.fetchall()]
            finally:
                conn.close()

    # -- reports --

    def save_report(self, task_id: str, markdown: str) -> None:
        now = _now_iso()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO reports(task_id, markdown, created_at) VALUES(?,?,?) ON CONFLICT(task_id) DO UPDATE SET markdown=excluded.markdown, created_at=excluded.created_at",
                    (task_id, markdown, now),
                )
                conn.commit()
            finally:
                conn.close()

    def get_report(self, task_id: str) -> dict | None:
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute("SELECT * FROM reports WHERE task_id=?", (task_id,))
                row = cur.fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

    def list_reports(self, limit: int = 50) -> list[dict]:
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute("SELECT * FROM reports ORDER BY created_at DESC LIMIT ?", (limit,))
                return [dict(r) for r in cur.fetchall()]
            finally:
                conn.close()

    # -- kv --

    def kv_set(self, key: str, value: str) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("INSERT INTO kv(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
                conn.commit()
            finally:
                conn.close()

    def kv_get(self, key: str) -> str | None:
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute("SELECT value FROM kv WHERE key=?", (key,))
                row = cur.fetchone()
                return row["value"] if row else None
            finally:
                conn.close()

    # -- plan fingerprints for anti-repeat --

    def record_plan_fingerprint(self, fingerprint: str, task_id: str) -> None:
        self.kv_set(f"plan_fp:{fingerprint}", json.dumps({"task_id": task_id, "created_at": _now_iso()}))

    def has_plan_fingerprint_within_days(self, fingerprint: str, days: int = 7) -> bool:
        raw = self.kv_get(f"plan_fp:{fingerprint}")
        if not raw:
            return False
        try:
            data = json.loads(raw)
            created_at = data.get("created_at", "")
            dt = datetime.fromisoformat(created_at)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            cutoff = _utc_now() - timedelta(days=days)
            return dt >= cutoff
        except Exception:
            return False

    # -- overall stats --

    def get_history(self, limit: int = 200) -> dict:
        return {
            "queries": self.list_queries(limit=limit),
            "urls": self.list_urls(limit=limit),
            "tasks": self.list_tasks(limit=limit),
        }

    def clear_all(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                for tbl in ["tasks", "actions", "queries", "urls", "findings", "errors", "reports", "kv", "active_session"]:
                    conn.execute(f"DELETE FROM {tbl}")
                conn.commit()
            finally:
                conn.close()
