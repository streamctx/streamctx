"""SQLite storage for streamctx sessions and checkpoints."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def _default_db_path() -> Path:
    base = Path(os.environ.get("STREAMCTX_HOME", Path.home() / ".streamctx"))
    base.mkdir(parents=True, exist_ok=True)
    return base / "sessions.db"


class SessionStorage:
    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db_path = db_path or _default_db_path()
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock:
            with self._connect() as conn:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS sessions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        started_at TEXT NOT NULL,
                        ended_at TEXT
                    );
                    CREATE TABLE IF NOT EXISTS calls (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id INTEGER NOT NULL,
                        timestamp TEXT NOT NULL,
                        provider TEXT NOT NULL,
                        model TEXT,
                        input_tokens INTEGER DEFAULT 0,
                        output_tokens INTEGER DEFAULT 0,
                        cost REAL DEFAULT 0,
                        reused_tokens INTEGER DEFAULT 0,
                        waste_category TEXT,
                        messages_json TEXT,
                        FOREIGN KEY (session_id) REFERENCES sessions(id)
                    );
                    CREATE TABLE IF NOT EXISTS checkpoints (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id INTEGER NOT NULL,
                        step_number INTEGER NOT NULL,
                        messages_json TEXT NOT NULL,
                        timestamp TEXT NOT NULL,
                        FOREIGN KEY (session_id) REFERENCES sessions(id)
                    );
                    """
                )

    def start_session(self) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            with self._connect() as conn:
                cur = conn.execute(
                    "INSERT INTO sessions (started_at) VALUES (?)", (now,),
                )
                conn.commit()
                return int(cur.lastrowid)

    def end_session(self, session_id: int) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE sessions SET ended_at = ? WHERE id = ?",
                    (now, session_id),
                )
                conn.commit()

    def record_call(
        self,
        session_id: int,
        provider: str,
        model: Optional[str],
        input_tokens: int,
        output_tokens: int,
        cost: float,
        reused_tokens: int,
        waste_category: Optional[str],
        messages: list[dict[str, Any]],
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO calls (
                        session_id, timestamp, provider, model,
                        input_tokens, output_tokens, cost,
                        reused_tokens, waste_category, messages_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id, now, provider, model,
                        input_tokens, output_tokens, cost,
                        reused_tokens, waste_category,
                        json.dumps(messages),
                    ),
                )
                conn.commit()

    def save_checkpoint(
        self,
        session_id: int,
        step_number: int,
        messages: list[dict[str, Any]],
    ) -> None:
        """Save current messages as a checkpoint after each LLM call."""
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO checkpoints (session_id, step_number, messages_json, timestamp)
                    VALUES (?, ?, ?, ?)
                    """,
                    (session_id, step_number, json.dumps(messages), now),
                )
                conn.commit()

    def get_latest_checkpoint(self, session_id: int) -> Optional[dict[str, Any]]:
        """Get the most recent checkpoint for a session."""
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT step_number, messages_json, timestamp
                    FROM checkpoints
                    WHERE session_id = ?
                    ORDER BY step_number DESC LIMIT 1
                    """,
                    (session_id,),
                ).fetchone()
        if row is None:
            return None
        return {
            "step_number": row["step_number"],
            "messages": json.loads(row["messages_json"]),
            "timestamp": row["timestamp"],
        }

    def resume_from_checkpoint(self, session_id: int) -> list[dict[str, Any]]:
        """Return messages from the latest checkpoint to resume from."""
        result = self.get_latest_checkpoint(session_id)
        if result is None:
            return []
        return result["messages"]

    def get_session_stats(self, session_id: int) -> dict[str, Any]:
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT
                        COUNT(*) AS call_count,
                        COALESCE(SUM(input_tokens), 0) AS input_tokens,
                        COALESCE(SUM(output_tokens), 0) AS output_tokens,
                        COALESCE(SUM(cost), 0) AS total_cost,
                        COALESCE(SUM(reused_tokens), 0) AS reused_tokens
                    FROM calls WHERE session_id = ?
                    """,
                    (session_id,),
                ).fetchone()
                waste_rows = conn.execute(
                    """
                    SELECT waste_category, COUNT(*) AS cnt
                    FROM calls
                    WHERE session_id = ? AND waste_category IS NOT NULL
                    GROUP BY waste_category
                    ORDER BY cnt DESC LIMIT 1
                    """,
                    (session_id,),
                ).fetchone()
        return {
            "call_count": int(row["call_count"]),
            "input_tokens": int(row["input_tokens"]),
            "output_tokens": int(row["output_tokens"]),
            "total_tokens": int(row["input_tokens"]) + int(row["output_tokens"]),
            "total_cost": float(row["total_cost"]),
            "reused_tokens": int(row["reused_tokens"]),
            "biggest_waste": waste_rows["waste_category"] if waste_rows else None,
        }



def get_storage() -> "SessionStorage":
    """
    Factory function — STREAMCTX_BACKEND env var check kare ane
    sachu storage backend return kare (sqlite athva supabase).
    """
    backend = os.environ.get("STREAMCTX_BACKEND", "sqlite").lower()

    if backend == "supabase":
        from .supabase_storage import SupabaseStorage
        return SupabaseStorage()

    return SessionStorage()