"""Layer 4 — Compliance Evidence ledger.

Tamper-evident, log-independent attestation of every attribution and
repair record.  Lives in a separate SQLite file (``evidence_ledger.db``),
never in ``sessions.db``.

The ledger is append-only: UPDATE/DELETE are blocked by triggers and
this module exposes no mutating code paths besides INSERT.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

GENESIS_HASH = "0" * 64
RECORD_TYPES = frozenset({"attribution", "repair"})

NO_UPDATE_TRIGGER = "evidence_ledger_no_update"
NO_DELETE_TRIGGER = "evidence_ledger_no_delete"

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS evidence_ledger (
    entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_type TEXT NOT NULL CHECK (record_type IN ('attribution', 'repair')),
    record_ref_id INTEGER NOT NULL,
    record_payload_hash TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    entry_hash TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    signature TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_evidence_ledger_ref
    ON evidence_ledger(record_ref_id);

CREATE TABLE IF NOT EXISTS evidence_payloads (
    entry_id INTEGER PRIMARY KEY,
    session_id INTEGER,
    payload_json TEXT NOT NULL,
    FOREIGN KEY (entry_id) REFERENCES evidence_ledger(entry_id)
);

CREATE INDEX IF NOT EXISTS idx_evidence_payloads_session
    ON evidence_payloads(session_id);

CREATE TRIGGER IF NOT EXISTS {NO_UPDATE_TRIGGER}
BEFORE UPDATE ON evidence_ledger
BEGIN
    SELECT RAISE(ABORT, 'evidence_ledger is append-only');
END;

CREATE TRIGGER IF NOT EXISTS {NO_DELETE_TRIGGER}
BEFORE DELETE ON evidence_ledger
BEGIN
    SELECT RAISE(ABORT, 'evidence_ledger is append-only');
END;
"""


def _default_evidence_db_path() -> Path:
    base = Path(os.environ.get("STREAMCTX_HOME", Path.home() / ".streamctx"))
    base.mkdir(parents=True, exist_ok=True)
    return base / "evidence_ledger.db"


def canonical_json(obj: Any) -> str:
    """Stable JSON used for payload hashes and signed export bundles."""
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def payload_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def compute_entry_hash(
    entry_id: int,
    record_type: str,
    record_ref_id: int,
    record_payload_hash: str,
    prev_hash: str,
    timestamp: str,
) -> str:
    """sha256(entry_id + record_type + record_ref_id + payload_hash + prev_hash + timestamp)."""
    preimage = (
        f"{entry_id}{record_type}{record_ref_id}"
        f"{record_payload_hash}{prev_hash}{timestamp}"
    )
    return hashlib.sha256(preimage.encode("utf-8")).hexdigest()


def sign_entry_hash(entry_hash: str, key: bytes) -> str:
    return hmac.new(key, entry_hash.encode("utf-8"), hashlib.sha256).hexdigest()


def _payload_session_id(payload: dict[str, Any]) -> Optional[int]:
    raw = payload.get("session_id")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


class EvidenceLedger:
    """Append-only hash-chained evidence store."""

    def __init__(
        self,
        db_path: Optional[Path] = None,
        key: Optional[str] = None,
    ) -> None:
        self.db_path = Path(db_path) if db_path is not None else _default_evidence_db_path()
        env_key = os.environ.get("STREAMCTX_EVIDENCE_KEY")
        self._key = key if key is not None else env_key
        self._write_lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None

    def _key_bytes(self) -> bytes:
        if not self._key:
            raise RuntimeError(
                "STREAMCTX_EVIDENCE_KEY is not set; cannot sign evidence"
            )
        if isinstance(self._key, bytes):
            return self._key
        return str(self._key).encode("utf-8")

    def _connect(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(self.db_path), check_same_thread=False, timeout=30.0
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass
        conn.executescript(_SCHEMA)
        conn.commit()
        self._conn = conn
        return conn

    def close(self) -> None:
        with self._write_lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def append_evidence(
        self,
        record_type: str,
        record_ref_id: int,
        payload: dict[str, Any],
    ) -> int:
        """Append one signed ledger entry. Returns the new ``entry_id``."""
        if record_type not in RECORD_TYPES:
            raise ValueError(
                f"record_type must be one of {sorted(RECORD_TYPES)}, got {record_type!r}"
            )
        if not isinstance(payload, dict):
            raise TypeError("payload must be a dict")

        key = self._key_bytes()
        record_payload_hash = payload_hash(payload)

        with self._write_lock:
            conn = self._connect()
            last = conn.execute(
                "SELECT entry_id, entry_hash FROM evidence_ledger "
                "ORDER BY entry_id DESC LIMIT 1"
            ).fetchone()
            if last is None:
                entry_id = 1
                prev_hash = GENESIS_HASH
            else:
                entry_id = int(last["entry_id"]) + 1
                prev_hash = str(last["entry_hash"])

            timestamp = datetime.now(timezone.utc).isoformat()
            entry_hash = compute_entry_hash(
                entry_id,
                record_type,
                int(record_ref_id),
                record_payload_hash,
                prev_hash,
                timestamp,
            )
            signature = sign_entry_hash(entry_hash, key)

            conn.execute(
                """
                INSERT INTO evidence_ledger (
                    entry_id, record_type, record_ref_id, record_payload_hash,
                    prev_hash, entry_hash, timestamp, signature
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry_id,
                    record_type,
                    int(record_ref_id),
                    record_payload_hash,
                    prev_hash,
                    entry_hash,
                    timestamp,
                    signature,
                ),
            )
            conn.execute(
                """
                INSERT INTO evidence_payloads (entry_id, session_id, payload_json)
                VALUES (?, ?, ?)
                """,
                (
                    entry_id,
                    _payload_session_id(payload),
                    canonical_json(payload),
                ),
            )
            conn.commit()
            return entry_id

    def _all_rows(self) -> list[sqlite3.Row]:
        conn = self._connect()
        return list(
            conn.execute(
                """
                SELECT entry_id, record_type, record_ref_id, record_payload_hash,
                       prev_hash, entry_hash, timestamp, signature
                FROM evidence_ledger
                ORDER BY entry_id ASC
                """
            ).fetchall()
        )

    def verify_chain(
        self, record_ref_id: Optional[int] = None
    ) -> dict[str, Any]:
        """Walk the ledger (or a filtered slice) and check hash/signature/links.

        Returns
        -------
        dict
            ``valid``, ``broken_at_entry_id``, ``total_checked``.
        """
        key = self._key_bytes()
        rows = self._all_rows()
        by_id = {int(row["entry_id"]): row for row in rows}

        if record_ref_id is None:
            targets = rows
        else:
            targets = [
                row for row in rows if int(row["record_ref_id"]) == int(record_ref_id)
            ]

        checked = 0
        for row in targets:
            entry_id = int(row["entry_id"])
            recomputed = compute_entry_hash(
                entry_id,
                str(row["record_type"]),
                int(row["record_ref_id"]),
                str(row["record_payload_hash"]),
                str(row["prev_hash"]),
                str(row["timestamp"]),
            )
            if recomputed != str(row["entry_hash"]):
                return {
                    "valid": False,
                    "broken_at_entry_id": entry_id,
                    "total_checked": checked,
                }

            expected_sig = sign_entry_hash(str(row["entry_hash"]), key)
            if not hmac.compare_digest(expected_sig, str(row["signature"])):
                return {
                    "valid": False,
                    "broken_at_entry_id": entry_id,
                    "total_checked": checked,
                }

            prior = None
            for prior_id in range(entry_id - 1, 0, -1):
                if prior_id in by_id:
                    prior = by_id[prior_id]
                    break
            expected_prev = GENESIS_HASH if prior is None else str(prior["entry_hash"])
            if str(row["prev_hash"]) != expected_prev:
                return {
                    "valid": False,
                    "broken_at_entry_id": entry_id,
                    "total_checked": checked,
                }

            checked += 1

        return {
            "valid": True,
            "broken_at_entry_id": None,
            "total_checked": checked,
        }

    def export_attestation(self, session_id: int) -> dict[str, Any]:
        """Bundle ledger rows + payloads for ``session_id``, then sign the bundle."""
        key = self._key_bytes()
        conn = self._connect()
        rows = conn.execute(
            """
            SELECT
                e.entry_id, e.record_type, e.record_ref_id, e.record_payload_hash,
                e.prev_hash, e.entry_hash, e.timestamp, e.signature,
                p.payload_json
            FROM evidence_ledger e
            JOIN evidence_payloads p ON p.entry_id = e.entry_id
            WHERE p.session_id = ?
            ORDER BY e.entry_id ASC
            """,
            (int(session_id),),
        ).fetchall()

        entries: list[dict[str, Any]] = []
        payloads: list[dict[str, Any]] = []
        for row in rows:
            entries.append(
                {
                    "entry_id": int(row["entry_id"]),
                    "record_type": str(row["record_type"]),
                    "record_ref_id": int(row["record_ref_id"]),
                    "record_payload_hash": str(row["record_payload_hash"]),
                    "prev_hash": str(row["prev_hash"]),
                    "entry_hash": str(row["entry_hash"]),
                    "timestamp": str(row["timestamp"]),
                    "signature": str(row["signature"]),
                }
            )
            payloads.append(
                {
                    "entry_id": int(row["entry_id"]),
                    "record_type": str(row["record_type"]),
                    "record_ref_id": int(row["record_ref_id"]),
                    "payload": json.loads(row["payload_json"]),
                }
            )

        unsigned = {
            "format": "streamctx.evidence.v1",
            "session_id": int(session_id),
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "entries": entries,
            "payloads": payloads,
        }
        bundle_hash = hashlib.sha256(
            canonical_json(unsigned).encode("utf-8")
        ).hexdigest()
        return {
            **unsigned,
            "bundle_hash": bundle_hash,
            "signature": sign_entry_hash(bundle_hash, key),
        }


_ledger_cache: dict[str, EvidenceLedger] = {}
_ledger_cache_lock = threading.Lock()


def get_evidence_ledger() -> EvidenceLedger:
    """Process-wide ledger for ``STREAMCTX_HOME/evidence_ledger.db``."""
    db_path = str(_default_evidence_db_path())
    with _ledger_cache_lock:
        if db_path not in _ledger_cache:
            _ledger_cache[db_path] = EvidenceLedger()
        return _ledger_cache[db_path]


def append_evidence(
    record_type: str,
    record_ref_id: int,
    payload: dict[str, Any],
    ledger: Optional[EvidenceLedger] = None,
) -> int:
    return (ledger or get_evidence_ledger()).append_evidence(
        record_type, record_ref_id, payload
    )


def verify_chain(
    record_ref_id: Optional[int] = None,
    ledger: Optional[EvidenceLedger] = None,
) -> dict[str, Any]:
    return (ledger or get_evidence_ledger()).verify_chain(record_ref_id)


def export_attestation(
    session_id: int,
    ledger: Optional[EvidenceLedger] = None,
) -> dict[str, Any]:
    return (ledger or get_evidence_ledger()).export_attestation(session_id)


def safe_append_evidence(
    record_type: str,
    record_ref_id: int,
    payload: dict[str, Any],
    ledger: Any = None,
) -> None:
    """Best-effort append. Failures are logged and never raised."""
    try:
        if ledger is None:
            if not os.environ.get("STREAMCTX_EVIDENCE_KEY"):
                return
            ledger = get_evidence_ledger()
        ledger.append_evidence(record_type, int(record_ref_id), payload)
    except Exception:
        logger.exception(
            "evidence logging failed for %s ref=%s; continuing",
            record_type,
            record_ref_id,
        )
