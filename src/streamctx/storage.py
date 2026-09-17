"""SQLite storage for streamctx sessions and checkpoints."""

from __future__ import annotations

import json
import os
import queue
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def is_valid_checkpoint_messages(messages: Any) -> bool:
    """True if ``messages`` is a usable conversation snapshot."""
    if not isinstance(messages, list) or not messages:
        return False
    for item in messages:
        if not isinstance(item, dict):
            return False
        if "role" not in item:
            return False
        if "content" not in item and "tool_calls" not in item:
            return False
    return True


def parse_checkpoint_messages(raw: Any) -> Optional[list[Any]]:
    if raw is None:
        return None
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not is_valid_checkpoint_messages(parsed):
        return None
    return parsed


def _default_db_path() -> Path:
    base = Path(os.environ.get("STREAMCTX_HOME", Path.home() / ".streamctx"))
    base.mkdir(parents=True, exist_ok=True)
    return base / "sessions.db"


class SessionStorage:
    def __init__(self, db_path: Optional[Path] = None, read_pool_size: int = 8) -> None:
        self.db_path = db_path or _default_db_path()
        self._write_lock = threading.Lock()
        self._write_conn = self._connect()
        self._init_db()
        self._read_pool: "queue.Queue[sqlite3.Connection]" = queue.Queue()
        for _ in range(read_pool_size):
            self._read_pool.put(self._connect())

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self.db_path), check_same_thread=False, timeout=30.0
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
        for attempt in range(5):
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                break
            except sqlite3.OperationalError:
                if attempt == 4:
                    raise
                time.sleep(0.2 * (attempt + 1))
        return conn

    def _borrow_read_conn(self) -> sqlite3.Connection:
        return self._read_pool.get()

    def _return_read_conn(self, conn: sqlite3.Connection) -> None:
        self._read_pool.put(conn)

    def close(self) -> None:
        with self._write_lock:
            self._write_conn.close()
        while not self._read_pool.empty():
            self._read_pool.get_nowait().close()

    def _init_db(self) -> None:
        with self._write_lock:
            self._write_conn.executescript(
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
                    failed INTEGER DEFAULT 0,
                    healed INTEGER DEFAULT 0,
                    error_message TEXT,
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

                CREATE INDEX IF NOT EXISTS idx_calls_session_id
                    ON calls(session_id);
                CREATE INDEX IF NOT EXISTS idx_checkpoints_session_id
                    ON checkpoints(session_id);

                CREATE TABLE IF NOT EXISTS shadow_repair_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER NOT NULL,
                    failed_call_id INTEGER NOT NULL,
                    attribution_reason TEXT,
                    dominant_signal TEXT,
                    fix_candidate TEXT,
                    timestamp TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_shadow_repair_session_id
                    ON shadow_repair_log(session_id);

                CREATE TABLE IF NOT EXISTS shadow_attribution_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER NOT NULL,
                    failed_call_id INTEGER NOT NULL,
                    dominant_signal TEXT,
                    confidence REAL,
                    root_cause_call_id INTEGER,
                    reason TEXT,
                    error_message TEXT,
                    failure_kind TEXT,
                    signal_breakdown TEXT,
                    timestamp TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_shadow_attribution_session_id
                    ON shadow_attribution_log(session_id);
                """
            )
            self._write_conn.commit()
            self._migrate_layer1_columns()

    def _table_columns(self, table: str) -> set[str]:
        rows = self._write_conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {str(r[1]) for r in rows}

    def _ensure_column(self, table: str, column: str, decl: str) -> None:
        if column not in self._table_columns(table):
            self._write_conn.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {decl}"
            )

    def _migrate_layer1_columns(self) -> None:
        self._ensure_column("checkpoints", "valid", "INTEGER DEFAULT 1")
        self._ensure_column("calls", "message_fingerprint", "TEXT")
        self._ensure_column("calls", "response_text", "TEXT")
        self._write_conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_calls_fingerprint "
            "ON calls(session_id, message_fingerprint)"
        )
        self._write_conn.commit()

    def start_session(self) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self._write_lock:
            cur = self._write_conn.execute(
                "INSERT INTO sessions (started_at) VALUES (?)", (now,)
            )
            self._write_conn.commit()
            return int(cur.lastrowid)

    def end_session(self, session_id: int) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._write_lock:
            self._write_conn.execute(
                "UPDATE sessions SET ended_at = ? WHERE id = ?",
                (now, session_id),
            )
            self._write_conn.commit()


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
        failed: bool = False,
        healed: bool = False,
        error_message: Optional[str] = None,
        message_fingerprint: Optional[str] = None,
        response_text: Optional[str] = None,
    ) -> Optional[int]:

        now = datetime.now(timezone.utc).isoformat()
        with self._write_lock:
            cur = self._write_conn.execute(
                 """
                 INSERT INTO calls (
                     session_id, timestamp, provider, model,
                     input_tokens, output_tokens, cost,
                     reused_tokens, waste_category, messages_json,
                     failed, healed, error_message,
                     message_fingerprint, response_text
                 ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                 """,
                 (
                     session_id, now, provider, model,
                     input_tokens, output_tokens, cost,
                     reused_tokens, waste_category,
                     json.dumps(messages),
                     int(failed), int(healed), error_message,
                     message_fingerprint, response_text,
                 ),
             )
            self._write_conn.commit()
            call_id = int(cur.lastrowid)
        if failed:
            try:
                from .shadow import maybe_schedule_shadow_repair

                maybe_schedule_shadow_repair(
                    session_id=session_id,
                    failed_call_id=call_id,
                    error_message=error_message,
                    storage=self,
                )
            except Exception:
                pass
        return call_id

    def save_checkpoint(
        self,
        session_id: int,
        step_number: int,
        messages: list[dict[str, Any]],
        valid: bool = True,
    ) -> None:
        """Save current messages as a checkpoint after each LLM call."""
        now = datetime.now(timezone.utc).isoformat()
        with self._write_lock:
            self._write_conn.execute(
                """
                INSERT INTO checkpoints (session_id, step_number, messages_json, timestamp, valid)
                VALUES (?, ?, ?, ?, ?)
                """,
                (session_id, step_number, json.dumps(messages), now, int(valid)),
            )
            self._write_conn.commit()

    def persist_step(
        self,
        session_id: int,
        provider: str,
        model: Optional[str],
        input_tokens: int,
        output_tokens: int,
        cost: float,
        reused_tokens: int,
        waste_category: Optional[str],
        request_messages: list[dict[str, Any]],
        checkpoint_messages: list[dict[str, Any]],
        step_number: int,
        failed: bool = False,
        healed: bool = False,
        error_message: Optional[str] = None,
        message_fingerprint: Optional[str] = None,
        response_text: Optional[str] = None,
        checkpoint_valid: bool = True,
    ) -> int:
        """Atomically persist a call row and its matching checkpoint.

        Process kill mid-write can no longer leave a calls row without the
        checkpoint (or the reverse) for this step — both share one COMMIT.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._write_lock:
            cur = self._write_conn.execute(
                """
                INSERT INTO calls (
                    session_id, timestamp, provider, model,
                    input_tokens, output_tokens, cost,
                    reused_tokens, waste_category, messages_json,
                    failed, healed, error_message,
                    message_fingerprint, response_text
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id, now, provider, model,
                    input_tokens, output_tokens, cost,
                    reused_tokens, waste_category,
                    json.dumps(request_messages),
                    int(failed), int(healed), error_message,
                    message_fingerprint, response_text,
                ),
            )
            call_id = int(cur.lastrowid)
            self._write_conn.execute(
                """
                INSERT INTO checkpoints (session_id, step_number, messages_json, timestamp, valid)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    step_number,
                    json.dumps(checkpoint_messages),
                    now,
                    int(checkpoint_valid),
                ),
            )
            self._write_conn.commit()
        if failed:
            try:
                from .shadow import maybe_schedule_shadow_repair

                maybe_schedule_shadow_repair(
                    session_id=session_id,
                    failed_call_id=call_id,
                    error_message=error_message,
                    storage=self,
                )
            except Exception:
                pass
        return call_id

    def get_latest_checkpoint(self, session_id: int) -> Optional[dict[str, Any]]:
        """Get the most recent *valid* checkpoint for a session."""
        return self.get_latest_valid_checkpoint(session_id)

    def get_latest_valid_checkpoint(self, session_id: int) -> Optional[dict[str, Any]]:
        """Newest checkpoint whose JSON parses as a message list and valid=1.

        Corrupt rows (invalid JSON, non-list payloads, valid=0) are skipped
        so two consecutive bad checkpoints fall through to a third.
        """
        conn = self._borrow_read_conn()
        try:
            rows = conn.execute(
                """
                SELECT step_number, messages_json, timestamp, valid
                FROM checkpoints
                WHERE session_id = ?
                ORDER BY step_number DESC, id DESC
                """,
                (session_id,),
            ).fetchall()
        finally:
            self._return_read_conn(conn)
        for row in rows:
            if row["valid"] is not None and int(row["valid"]) == 0:
                continue
            messages = parse_checkpoint_messages(row["messages_json"])
            if messages is None:
                continue
            return {
                "step_number": row["step_number"],
                "messages": messages,
                "timestamp": row["timestamp"],
            }
        return None

    def find_successful_call_by_fingerprint(
        self, session_id: int, fingerprint: str
    ) -> Optional[dict[str, Any]]:
        if not fingerprint:
            return None
        conn = self._borrow_read_conn()
        try:
            row = conn.execute(
                """
                SELECT id, session_id, input_tokens, output_tokens,
                       messages_json, response_text, message_fingerprint
                FROM calls
                WHERE session_id = ? AND message_fingerprint = ? AND failed = 0
                ORDER BY id DESC LIMIT 1
                """,
                (session_id, fingerprint),
            ).fetchone()
        finally:
            self._return_read_conn(conn)
        return dict(row) if row is not None else None

    def get_last_successful_call(self, session_id: int) -> Optional[dict[str, Any]]:
        conn = self._borrow_read_conn()
        try:
            row = conn.execute(
                """
                SELECT id, session_id, input_tokens, output_tokens,
                       messages_json, response_text, message_fingerprint
                FROM calls
                WHERE session_id = ? AND failed = 0
                ORDER BY id DESC LIMIT 1
                """,
                (session_id,),
            ).fetchone()
        finally:
            self._return_read_conn(conn)
        return dict(row) if row is not None else None

    def resume_from_checkpoint(self, session_id: int) -> list[dict[str, Any]]:
        """Return messages from the latest checkpoint to resume from."""
        result = self.get_latest_checkpoint(session_id)
        if result is None:
            return []
        return result["messages"]

    def get_session_stats(self, session_id: int) -> dict[str, Any]:
        conn = self._borrow_read_conn()
        try:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS call_count,
                    COALESCE(SUM(input_tokens), 0) AS input_tokens,
                    COALESCE(SUM(output_tokens), 0) AS output_tokens,
                    COALESCE(SUM(cost), 0) AS total_cost,
                    COALESCE(SUM(reused_tokens), 0) AS reused_tokens, (SELECT model FROM calls WHERE session_id = ? ORDER BY rowid DESC LIMIT 1) AS model
                FROM calls WHERE session_id = ?
                """,
                (session_id, session_id),
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
        finally:
            self._return_read_conn(conn)
        return {
            "call_count": int(row["call_count"]),
            "input_tokens": int(row["input_tokens"]),
            "output_tokens": int(row["output_tokens"]),
            "total_tokens": int(row["input_tokens"]) + int(row["output_tokens"]),
            "total_cost": float(row["total_cost"]),
            "reused_tokens": int(row["reused_tokens"]),
            "biggest_waste": waste_rows["waste_category"] if waste_rows else None,
        }

    def get_calls_for_session(self, session_id: int) -> list[dict[str, Any]]:
            """Return every call row for a session, oldest first.

            Used by the Causal Failure Attribution Engine to walk through a
            session's calls in chronological order and score candidate root
            causes for any failed call.
            """
            conn = self._borrow_read_conn()
            try:
                rows = conn.execute(
                    """
                    SELECT id, session_id, timestamp, provider, model,
                           input_tokens, output_tokens, cost,
                           reused_tokens, waste_category, messages_json,
                           failed, healed, error_message
                    FROM calls
                    WHERE session_id = ?
                    ORDER BY id ASC
                    """,
                    (session_id,),
                ).fetchall()
            finally:
                self._return_read_conn(conn)
            return [dict(row) for row in rows]

    def insert_shadow_repair_log(
        self,
        session_id: int,
        failed_call_id: int,
        attribution_reason: Optional[str],
        dominant_signal: Optional[str],
        fix_candidate: Any,
        timestamp: Optional[str] = None,
    ) -> None:
        from .shadow import serialize_fix_candidate

        now = timestamp or datetime.now(timezone.utc).isoformat()
        with self._write_lock:
            self._write_conn.execute(
                """
                INSERT INTO shadow_repair_log (
                    session_id, failed_call_id, attribution_reason,
                    dominant_signal, fix_candidate, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    failed_call_id,
                    attribution_reason,
                    dominant_signal,
                    serialize_fix_candidate(fix_candidate),
                    now,
                ),
            )
            self._write_conn.commit()

    def get_shadow_repair_log(self, limit: Optional[int] = None) -> list[dict[str, Any]]:
        conn = self._borrow_read_conn()
        try:
            sql = """
                SELECT id, session_id, failed_call_id, attribution_reason,
                       dominant_signal, fix_candidate, timestamp
                FROM shadow_repair_log
                ORDER BY id ASC
            """
            if limit is not None:
                rows = conn.execute(sql + " LIMIT ?", (int(limit),)).fetchall()
            else:
                rows = conn.execute(sql).fetchall()
        finally:
            self._return_read_conn(conn)
        return [dict(row) for row in rows]

    def insert_shadow_attribution_log(
        self,
        session_id: int,
        failed_call_id: int,
        dominant_signal: Optional[str],
        confidence: float,
        root_cause_call_id: Optional[int] = None,
        reason: Optional[str] = None,
        error_message: Optional[str] = None,
        failure_kind: Optional[str] = None,
        signal_breakdown: Optional[dict[str, Any]] = None,
        timestamp: Optional[str] = None,
    ) -> None:
        now = timestamp or datetime.now(timezone.utc).isoformat()
        if signal_breakdown is None:
            breakdown_json = None
        else:
            try:
                breakdown_json = json.dumps(signal_breakdown, ensure_ascii=False)
            except (TypeError, ValueError):
                breakdown_json = str(signal_breakdown)
        with self._write_lock:
            self._write_conn.execute(
                """
                INSERT INTO shadow_attribution_log (
                    session_id, failed_call_id, dominant_signal, confidence,
                    root_cause_call_id, reason, error_message, failure_kind,
                    signal_breakdown, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    failed_call_id,
                    dominant_signal,
                    float(confidence),
                    root_cause_call_id,
                    reason,
                    error_message,
                    failure_kind,
                    breakdown_json,
                    now,
                ),
            )
            self._write_conn.commit()

    def get_shadow_attribution_log(
        self, limit: Optional[int] = None
    ) -> list[dict[str, Any]]:
        conn = self._borrow_read_conn()
        try:
            sql = """
                SELECT id, session_id, failed_call_id, dominant_signal,
                       confidence, root_cause_call_id, reason, error_message,
                       failure_kind, signal_breakdown, timestamp
                FROM shadow_attribution_log
                ORDER BY id ASC
            """
            if limit is not None:
                rows = conn.execute(sql + " LIMIT ?", (int(limit),)).fetchall()
            else:
                rows = conn.execute(sql).fetchall()
        finally:
            self._return_read_conn(conn)
        return [dict(row) for row in rows]

    def clear_shadow_attribution_log(self) -> None:
        with self._write_lock:
            self._write_conn.execute("DELETE FROM shadow_attribution_log")
            self._write_conn.commit()


_storage_cache: dict[str, "SessionStorage"] = {}
_storage_cache_lock = threading.Lock()


def get_storage() -> "SessionStorage":
    """
    Factory function - STREAMCTX_BACKEND env var check kare ane
    sachu storage backend return kare (sqlite athva supabase).
    """
    backend = os.environ.get("STREAMCTX_BACKEND", "sqlite").lower()

    if backend == "supabase":
        from streamctx.supabase_storage import SupabaseStorage
        return SupabaseStorage()

    db_path = str(_default_db_path())
    with _storage_cache_lock:
        if db_path not in _storage_cache:
            _storage_cache[db_path] = SessionStorage()
        return _storage_cache[db_path]

