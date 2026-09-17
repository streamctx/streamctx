"""Layer 4 — Compliance Evidence ledger.

Tamper-evident, log-independent attestation of every attribution and
repair record.  Lives in a separate SQLite file (``evidence_ledger.db``),
never in ``sessions.db``.

Entries are hash-chained and signed with Ed25519.  The private key stays
server-side (``STREAMCTX_EVIDENCE_PRIVATE_KEY`` PEM path).  Customers
receive only the public key, so they can verify a bundle but cannot forge
entries.

The ledger is append-only: UPDATE/DELETE are blocked by triggers and
this module exposes no mutating code paths besides INSERT.

HMAC-SHA256 ledgers from the pre-production dogfood window are wiped on
open and replaced with an empty Ed25519 ledger (no production data to
migrate).
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
    load_pem_private_key,
    load_pem_public_key,
)

logger = logging.getLogger(__name__)

GENESIS_HASH = "0" * 64
RECORD_TYPES = frozenset({"attribution", "repair"})
SIGNING_ALGORITHM = "ed25519"
# Frozen. Do not change the 1.0 export shape in place — add or rename
# fields only under schema_version "1.1" or "2.0", and teach
# scripts/verify_attestation.py about the new version. Silent edits to
# the 1.0 object (field names, types, or required keys) break offline
# verifiers already in customer hands.
SCHEMA_VERSION = "1.0"
ISSUER = "streamctx"

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

CREATE TABLE IF NOT EXISTS ledger_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

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
    """Stable JSON used for payload hashes."""
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
    record_ref_id: int | str,
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


def generate_keypair(private_key_path: Path, public_key_path: Path) -> None:
    """Write a new Ed25519 PEM keypair. Private file is created with mode 0600."""
    private_key = Ed25519PrivateKey.generate()
    private_pem = private_key.private_bytes(
        Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
    )
    public_pem = private_key.public_key().public_bytes(
        Encoding.PEM, PublicFormat.SubjectPublicKeyInfo
    )
    private_key_path = Path(private_key_path)
    public_key_path = Path(public_key_path)
    private_key_path.parent.mkdir(parents=True, exist_ok=True)
    public_key_path.parent.mkdir(parents=True, exist_ok=True)
    private_key_path.write_bytes(private_pem)
    try:
        os.chmod(private_key_path, 0o600)
    except OSError:
        pass
    public_key_path.write_bytes(public_pem)


def load_private_key(path: Path) -> Ed25519PrivateKey:
    pem = Path(path).read_bytes()
    key = load_pem_private_key(pem, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise TypeError(
            f"{path} is not an Ed25519 private key PEM "
            "(STREAMCTX_EVIDENCE_PRIVATE_KEY)"
        )
    return key


def load_public_key(path: Path) -> Ed25519PublicKey:
    pem = Path(path).read_bytes()
    key = load_pem_public_key(pem)
    if not isinstance(key, Ed25519PublicKey):
        raise TypeError(
            f"{path} is not an Ed25519 public key PEM "
            "(STREAMCTX_EVIDENCE_PUBLIC_KEY)"
        )
    return key


def public_key_pem(public_key: Ed25519PublicKey) -> str:
    return public_key.public_bytes(
        Encoding.PEM, PublicFormat.SubjectPublicKeyInfo
    ).decode("ascii")


def sign_entry_hash(entry_hash: str, private_key: Ed25519PrivateKey) -> str:
    """Ed25519-sign the raw SHA-256 digest; return standard base64."""
    signature = private_key.sign(bytes.fromhex(entry_hash))
    return base64.b64encode(signature).decode("ascii")


def verify_entry_signature(
    entry_hash: str, signature_b64: str, public_key: Ed25519PublicKey
) -> bool:
    try:
        public_key.verify(base64.b64decode(signature_b64), bytes.fromhex(entry_hash))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def _payload_session_id(payload: dict[str, Any]) -> Optional[int]:
    raw = payload.get("session_id")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


class EvidenceLedger:
    """Append-only hash-chained evidence store, signed with Ed25519."""

    def __init__(
        self,
        db_path: Optional[Path] = None,
        private_key_path: Optional[Path] = None,
        public_key_path: Optional[Path] = None,
    ) -> None:
        self.db_path = Path(db_path) if db_path is not None else _default_evidence_db_path()
        priv = private_key_path or os.environ.get("STREAMCTX_EVIDENCE_PRIVATE_KEY")
        pub = public_key_path or os.environ.get("STREAMCTX_EVIDENCE_PUBLIC_KEY")
        self.private_key_path = Path(priv) if priv else None
        self.public_key_path = Path(pub) if pub else None
        self._write_lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        self._private_key: Optional[Ed25519PrivateKey] = None
        self._public_key: Optional[Ed25519PublicKey] = None

    def _require_private_key(self) -> Ed25519PrivateKey:
        if self._private_key is not None:
            return self._private_key
        if self.private_key_path is None:
            raise RuntimeError(
                "STREAMCTX_EVIDENCE_PRIVATE_KEY is not set; cannot sign evidence"
            )
        self._private_key = load_private_key(self.private_key_path)
        return self._private_key

    def _require_public_key(self) -> Ed25519PublicKey:
        if self._public_key is not None:
            return self._public_key
        if self.public_key_path is not None:
            self._public_key = load_public_key(self.public_key_path)
            return self._public_key
        # Internal verify: derive from the server-held private key.
        self._public_key = self._require_private_key().public_key()
        return self._public_key

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
        self._migrate_hmac_ledger(conn)
        conn.commit()
        self._conn = conn
        return conn

    def _migrate_hmac_ledger(self, conn: sqlite3.Connection) -> None:
        """Wipe dogfood HMAC ledgers; Ed25519 is a clean break."""
        row = conn.execute(
            "SELECT value FROM ledger_meta WHERE key = 'signing_algorithm'"
        ).fetchone()
        if row is not None and str(row["value"]) == SIGNING_ALGORITHM:
            return
        existing = conn.execute("SELECT COUNT(*) AS n FROM evidence_ledger").fetchone()
        count = int(existing["n"]) if existing is not None else 0
        if count > 0:
            logger.info(
                "Wiping pre-Ed25519 evidence ledger (%s entries) for clean re-init",
                count,
            )
            conn.executescript(
                f"""
                DROP TRIGGER IF EXISTS {NO_UPDATE_TRIGGER};
                DROP TRIGGER IF EXISTS {NO_DELETE_TRIGGER};
                DROP TABLE IF EXISTS evidence_payloads;
                DROP TABLE IF EXISTS evidence_ledger;
                DROP TABLE IF EXISTS ledger_meta;
                """
            )
            conn.executescript(_SCHEMA)
        conn.execute(
            "INSERT OR REPLACE INTO ledger_meta (key, value) VALUES (?, ?)",
            ("signing_algorithm", SIGNING_ALGORITHM),
        )
        conn.execute(
            "INSERT OR REPLACE INTO ledger_meta (key, value) VALUES (?, ?)",
            ("schema_version", SCHEMA_VERSION),
        )

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

        private_key = self._require_private_key()
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
            signature = sign_entry_hash(entry_hash, private_key)

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
        public_key = self._require_public_key()
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

            if not verify_entry_signature(
                str(row["entry_hash"]), str(row["signature"]), public_key
            ):
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
        """Export a schema 1.0 attestation bundle for ``session_id``.

        Hashes and signatures only — no raw attribution/repair payloads.
        The issuer public key is embedded so verification is offline.
        """
        public_key = self._require_public_key()
        conn = self._connect()
        rows = conn.execute(
            """
            SELECT
                e.entry_id, e.record_type, e.record_ref_id, e.record_payload_hash,
                e.prev_hash, e.entry_hash, e.timestamp, e.signature
            FROM evidence_ledger e
            JOIN evidence_payloads p ON p.entry_id = e.entry_id
            WHERE p.session_id = ?
            ORDER BY e.entry_id ASC
            """,
            (int(session_id),),
        ).fetchall()

        entries: list[dict[str, Any]] = []
        for row in rows:
            entries.append(
                {
                    "entry_id": int(row["entry_id"]),
                    "record_type": str(row["record_type"]),
                    "record_ref_id": str(row["record_ref_id"]),
                    "record_payload_hash": str(row["record_payload_hash"]),
                    "prev_hash": str(row["prev_hash"]),
                    "entry_hash": str(row["entry_hash"]),
                    "timestamp": str(row["timestamp"]),
                    "signature": str(row["signature"]),
                }
            )

        # schema_version 1.0 is frozen — see SCHEMA_VERSION above.
        return {
            "schema_version": SCHEMA_VERSION,
            "issuer": ISSUER,
            "session_id": str(session_id),
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "public_key_pem": public_key_pem(public_key),
            "entries": entries,
            "chain_root_hash": entries[0]["entry_hash"] if entries else None,
            "chain_tip_hash": entries[-1]["entry_hash"] if entries else None,
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
            if not os.environ.get("STREAMCTX_EVIDENCE_PRIVATE_KEY"):
                return
            ledger = get_evidence_ledger()
        ledger.append_evidence(record_type, int(record_ref_id), payload)
    except Exception:
        logger.exception(
            "evidence logging failed for %s ref=%s; continuing",
            record_type,
            record_ref_id,
        )
