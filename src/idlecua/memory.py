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
"""


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
            finally:
                conn.close()

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
                for tbl in ["tasks", "actions", "queries", "urls", "findings", "errors", "reports", "kv"]:
                    conn.execute(f"DELETE FROM {tbl}")
                conn.commit()
            finally:
                conn.close()
