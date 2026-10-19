"""Durable plugin session records in SQLite."""

from __future__ import annotations

import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  id                 TEXT PRIMARY KEY,
  hermes_session_id  TEXT,
  omo_session_id     TEXT,
  durable_session_id TEXT,
  session_path       TEXT,
  cwd                TEXT NOT NULL,
  model              TEXT,
  permission         TEXT NOT NULL,
  status             TEXT NOT NULL,
  pending_request_id TEXT,
  pending_question   TEXT,
  last_text          TEXT,
  created_at         TEXT NOT NULL,
  updated_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_hermes
  ON sessions (hermes_session_id, created_at DESC);
"""

UPDATABLE = {
    "omo_session_id", "durable_session_id", "session_path", "cwd", "model",
    "permission", "status", "pending_request_id", "pending_question", "last_text",
}

ACTIVE_STATUSES = ("starting", "running", "question")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_session_id() -> str:
    return f"omo-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"


class SessionStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def create(self, *, id: str, hermes_session_id: str, omo_session_id: str | None,
               durable_session_id: str | None, session_path: str | None, cwd: str,
               model: str | None, permission: str, status: str = "running") -> None:
        ts = now_iso()
        with self._lock:
            self.conn.execute(
                """INSERT INTO sessions
                   (id, hermes_session_id, omo_session_id, durable_session_id, session_path,
                    cwd, model, permission, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (id, hermes_session_id, omo_session_id, durable_session_id, session_path,
                 cwd, model, permission, status, ts, ts),
            )
            self.conn.commit()

    def get(self, session_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()

    def update(self, session_id: str, **fields) -> None:
        unknown = set(fields) - UPDATABLE
        if unknown:
            raise KeyError(sorted(unknown))
        fields["updated_at"] = now_iso()
        assignments = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [session_id]
        with self._lock:
            self.conn.execute(f"UPDATE sessions SET {assignments} WHERE id = ?", values)
            self.conn.commit()

    def active_for(self, hermes_session_id: str) -> sqlite3.Row | None:
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        return self.conn.execute(
            f"""SELECT * FROM sessions
                WHERE hermes_session_id = ? AND status IN ({placeholders})
                ORDER BY created_at DESC LIMIT 1""",
            (hermes_session_id, *ACTIVE_STATUSES),
        ).fetchone()

    def resolve(self, hermes_session_id: str, session_id: str | None = None) -> sqlite3.Row | None:
        if session_id:
            return self.get(session_id)
        row = self.conn.execute(
            """SELECT * FROM sessions
               WHERE hermes_session_id = ?
                 AND status IN ('starting','idle','running','question')
               ORDER BY created_at DESC LIMIT 1""",
            (hermes_session_id,),
        ).fetchone()
        if row:
            return row
        return self.conn.execute(
            """SELECT * FROM sessions WHERE hermes_session_id = ?
               ORDER BY created_at DESC LIMIT 1""",
            (hermes_session_id,),
        ).fetchone()

    def list_recent(self, hermes_session_id: str, limit: int = 10) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT * FROM sessions WHERE hermes_session_id = ?
               ORDER BY created_at DESC LIMIT ?""",
            (hermes_session_id, limit),
        ).fetchall()
