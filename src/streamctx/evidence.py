"""Layer 4 — Compliance Evidence ledger.

Tamper-evident, log-independent attestation of every attribution and
repair record.  Lives in a separate SQLite file (``evidence_ledger.db``),
never in ``sessions.db``.

Entries are hash-chained (global + per-session) and signed with Ed25519.
The private key stays server-side (``STREAMCTX_EVIDENCE_PRIVATE_KEY`` PEM
path).  Customers receive the public key out of band so they can pin it;
the copy embedded in an export is a convenience, not the authenticity
root.

The ledger is append-only: UPDATE/DELETE are blocked by triggers on both
``evidence_ledger`` and ``evidence_payloads``.  This module exposes no
mutating code paths besides INSERT.

Repair rows carry a first-class ``repair_disposition`` that is part of
the signed preimage.  Layer 3 ``verify_fix()`` is counterfactual:
``applied`` is always False unless a caller applies a candidate out of
band.  The ledger records that distinction; it never collapses
``resolved`` / ``verified_not_applied`` into ``applied``.

Layer 4 is MIT-licensed core SDK logic. Nothing in this module is gated
on a paid tier, license check, or hosted-only flag.

HMAC-SHA256 ledgers from the pre-production dogfood window are wiped on
open and replaced with an empty Ed25519 ledger (no production data to
migrate).  Ed25519 ledgers are never wiped on schema upgrade.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
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
HASH_VERSION = 2
# Frozen 1.0 export shape is still accepted by scripts/verify_attestation.py.
# 1.1 adds signed repair_disposition / session_prev_hash / key_id. Do not
# silently edit 1.1 field names; bump to 1.2+ and teach the verifier.
SCHEMA_VERSION = "1.1"
ISSUER = "streamctx"

REPAIR_DISPOSITION_APPLIED = "applied"
REPAIR_DISPOSITION_VERIFIED_NOT_APPLIED = "verified_not_applied"
REPAIR_DISPOSITION_UNRESOLVED_NOT_APPLIED = "unresolved_not_applied"
REPAIR_DISPOSITIONS = frozenset(
    {
        REPAIR_DISPOSITION_APPLIED,
        REPAIR_DISPOSITION_VERIFIED_NOT_APPLIED,
        REPAIR_DISPOSITION_UNRESOLVED_NOT_APPLIED,
    }
)

# Canonical JSON key set for hash_version 2. Order does not matter
# (canonical_json sorts keys); membership does. Changing this set is a
# new hash_version, not an in-place edit.
SIGNED_ENTRY_KEYS = (
    "applied",
    "dry_run",
    "entry_id",
    "hash_version",
    "key_id",
    "prev_hash",
    "record_payload_hash",
    "record_ref_id",
    "record_type",
    "repair_disposition",
    "resolved",
    "session_prev_hash",
    "timestamp",
)

NO_UPDATE_TRIGGER = "evidence_ledger_no_update"
NO_DELETE_TRIGGER = "evidence_ledger_no_delete"
NO_UPDATE_PAYLOAD_TRIGGER = "evidence_payloads_no_update"
NO_DELETE_PAYLOAD_TRIGGER = "evidence_payloads_no_delete"
NO_UPDATE_KEYS_TRIGGER = "signing_keys_no_update"
NO_DELETE_KEYS_TRIGGER = "signing_keys_no_delete"

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS evidence_ledger (
    entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_type TEXT NOT NULL CHECK (record_type IN ('attribution', 'repair')),
    record_ref_id INTEGER NOT NULL,
    record_payload_hash TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    session_prev_hash TEXT NOT NULL,
    entry_hash TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    signature TEXT NOT NULL,
    hash_version INTEGER NOT NULL,
    key_id TEXT NOT NULL,
    repair_disposition TEXT,
    applied INTEGER,
    resolved INTEGER,
    dry_run INTEGER
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

CREATE TABLE IF NOT EXISTS signing_keys (
    key_id TEXT PRIMARY KEY,
    public_key_pem TEXT NOT NULL,
    created_at TEXT NOT NULL
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

CREATE TRIGGER IF NOT EXISTS {NO_UPDATE_PAYLOAD_TRIGGER}
BEFORE UPDATE ON evidence_payloads
BEGIN
    SELECT RAISE(ABORT, 'evidence_payloads is append-only');
END;

CREATE TRIGGER IF NOT EXISTS {NO_DELETE_PAYLOAD_TRIGGER}
BEFORE DELETE ON evidence_payloads
BEGIN
    SELECT RAISE(ABORT, 'evidence_payloads is append-only');
END;

CREATE TRIGGER IF NOT EXISTS {NO_UPDATE_KEYS_TRIGGER}
BEFORE UPDATE ON signing_keys
BEGIN
    SELECT RAISE(ABORT, 'signing_keys is append-only');
END;

CREATE TRIGGER IF NOT EXISTS {NO_DELETE_KEYS_TRIGGER}
BEFORE DELETE ON signing_keys
BEGIN
    SELECT RAISE(ABORT, 'signing_keys is append-only');
END;
"""


def _default_evidence_db_path() -> Path:
    base = Path(os.environ.get("STREAMCTX_HOME", Path.home() / ".streamctx"))
    base.mkdir(parents=True, exist_ok=True)
    return base / "evidence_ledger.db"


def canonical_json(obj: Any) -> str:
    """Stable JSON used for payload hashes and v2 entry hashes."""
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def payload_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def compute_entry_hash_v1(
    entry_id: int,
    record_type: str,
    record_ref_id: int | str,
    record_payload_hash: str,
    prev_hash: str,
    timestamp: str,
) -> str:
    """sha256 of concatenated v1 fields (schema 1.0). No delimiters."""
    preimage = (
        f"{entry_id}{record_type}{record_ref_id}"
        f"{record_payload_hash}{prev_hash}{timestamp}"
    )
    return hashlib.sha256(preimage.encode("utf-8")).hexdigest()


def compute_entry_hash(fields: dict[str, Any], hash_version: int = HASH_VERSION) -> str:
    """Compute the signed entry hash for ``hash_version``."""
    if int(hash_version) == 1:
        return compute_entry_hash_v1(
            int(fields["entry_id"]),
            str(fields["record_type"]),
            fields["record_ref_id"],
            str(fields["record_payload_hash"]),
            str(fields["prev_hash"]),
            str(fields["timestamp"]),
        )
    signed = {key: fields.get(key) for key in SIGNED_ENTRY_KEYS}
    signed["hash_version"] = int(hash_version)
    signed["entry_id"] = int(signed["entry_id"])
    signed["record_ref_id"] = int(signed["record_ref_id"])
    return hashlib.sha256(canonical_json(signed).encode("utf-8")).hexdigest()


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


def public_key_id(public_key: Ed25519PublicKey) -> str:
    """SHA-256 of the SubjectPublicKeyInfo PEM (hex). Stable across exports."""
    return hashlib.sha256(public_key_pem(public_key).encode("ascii")).hexdigest()


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


def _json_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    return bool(value)


def disposition_from_payload(
    record_type: str, payload: dict[str, Any]
) -> tuple[Optional[str], Optional[bool], Optional[bool], Optional[bool]]:
    """Map a Layer 2/3 payload onto signed repair status fields.

    ``resolved`` is never treated as ``applied``. Layer 3 shadow
    verification that succeeds is ``verified_not_applied``.
    """
    if record_type != "repair":
        return None, None, None, None
    applied = _json_bool(payload.get("applied"))
    if applied is None:
        applied = False
    resolved = _json_bool(payload.get("resolved"))
    if resolved is None:
        resolved = False
    dry_run = _json_bool(payload.get("dry_run"))
    if applied:
        disposition = REPAIR_DISPOSITION_APPLIED
    elif resolved:
        disposition = REPAIR_DISPOSITION_VERIFIED_NOT_APPLIED
    else:
        disposition = REPAIR_DISPOSITION_UNRESOLVED_NOT_APPLIED
    return disposition, applied, resolved, dry_run


def repair_summary_from_entries(entries: list[dict[str, Any]]) -> dict[str, int]:
    applied = 0
    verified_not = 0
    unresolved = 0
    for entry in entries:
        if str(entry.get("record_type")) != "repair":
            continue
        disp = entry.get("repair_disposition")
        if disp == REPAIR_DISPOSITION_APPLIED:
            applied += 1
        elif disp == REPAIR_DISPOSITION_VERIFIED_NOT_APPLIED:
            verified_not += 1
        elif disp == REPAIR_DISPOSITION_UNRESOLVED_NOT_APPLIED:
            unresolved += 1
    return {
        "repair_entries": applied + verified_not + unresolved,
        "applied_count": applied,
        "verified_not_applied_count": verified_not,
        "unresolved_not_applied_count": unresolved,
    }


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
        self._incomplete_write: Optional[dict[str, Any]] = None

    def _intent_path(self) -> Path:
        return Path(str(self.db_path) + ".intent")

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
        self._public_key = self._require_private_key().public_key()
        return self._public_key

    def _connect(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            timeout=30.0,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=FULL")
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass
        conn.executescript(_SCHEMA)
        self._migrate_hmac_ledger(conn)
        self._ensure_v11_columns(conn)
        self._backfill_session_chain(conn)
        self._load_incomplete_write(conn)
        self._conn = conn
        return conn

    def _migrate_hmac_ledger(self, conn: sqlite3.Connection) -> None:
        """Wipe dogfood HMAC ledgers; never wipe an Ed25519 ledger."""
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "ledger_meta" not in tables or "evidence_ledger" not in tables:
            return
        row = conn.execute(
            "SELECT value FROM ledger_meta WHERE key = 'signing_algorithm'"
        ).fetchone()
        existing = conn.execute("SELECT COUNT(*) AS n FROM evidence_ledger").fetchone()
        count = int(existing["n"]) if existing is not None else 0
        if row is not None and str(row["value"]) == SIGNING_ALGORITHM:
            return
        if row is None and count > 0:
            # Pre-meta Ed25519 dogfood: stamp the algorithm, do not wipe.
            logger.info(
                "Evidence ledger has %s entries but no signing_algorithm meta; "
                "assuming ed25519 and leaving rows intact",
                count,
            )
            conn.execute(
                "INSERT OR REPLACE INTO ledger_meta (key, value) VALUES (?, ?)",
                ("signing_algorithm", SIGNING_ALGORITHM),
            )
            return
        if count > 0:
            logger.info(
                "Wiping pre-Ed25519 evidence ledger (%s entries) for clean re-init",
                count,
            )
            conn.executescript(
                f"""
                DROP TRIGGER IF EXISTS {NO_UPDATE_TRIGGER};
                DROP TRIGGER IF EXISTS {NO_DELETE_TRIGGER};
                DROP TRIGGER IF EXISTS {NO_UPDATE_PAYLOAD_TRIGGER};
                DROP TRIGGER IF EXISTS {NO_DELETE_PAYLOAD_TRIGGER};
                DROP TRIGGER IF EXISTS {NO_UPDATE_KEYS_TRIGGER};
                DROP TRIGGER IF EXISTS {NO_DELETE_KEYS_TRIGGER};
                DROP TABLE IF EXISTS evidence_payloads;
                DROP TABLE IF EXISTS evidence_ledger;
                DROP TABLE IF EXISTS signing_keys;
                DROP TABLE IF EXISTS ledger_meta;
                """
            )
            conn.executescript(_SCHEMA)

    def _ensure_v11_columns(self, conn: sqlite3.Connection) -> None:
        cols = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(evidence_ledger)").fetchall()
        }
        alterations = (
            ("hash_version", "INTEGER NOT NULL DEFAULT 1"),
            ("session_prev_hash", "TEXT"),
            ("key_id", "TEXT"),
            ("repair_disposition", "TEXT"),
            ("applied", "INTEGER"),
            ("resolved", "INTEGER"),
            ("dry_run", "INTEGER"),
        )
        for name, spec in alterations:
            if name not in cols:
                conn.execute(f"ALTER TABLE evidence_ledger ADD COLUMN {name} {spec}")
        conn.execute(
            "INSERT OR REPLACE INTO ledger_meta (key, value) VALUES (?, ?)",
            ("signing_algorithm", SIGNING_ALGORITHM),
        )
        conn.execute(
            "INSERT OR REPLACE INTO ledger_meta (key, value) VALUES (?, ?)",
            ("schema_version", SCHEMA_VERSION),
        )
        conn.execute(
            "INSERT OR REPLACE INTO ledger_meta (key, value) VALUES (?, ?)",
            ("hash_version", str(HASH_VERSION)),
        )

    def _recreate_triggers(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            f"""
            DROP TRIGGER IF EXISTS {NO_UPDATE_TRIGGER};
            DROP TRIGGER IF EXISTS {NO_DELETE_TRIGGER};
            DROP TRIGGER IF EXISTS {NO_UPDATE_PAYLOAD_TRIGGER};
            DROP TRIGGER IF EXISTS {NO_DELETE_PAYLOAD_TRIGGER};
            DROP TRIGGER IF EXISTS {NO_UPDATE_KEYS_TRIGGER};
            DROP TRIGGER IF EXISTS {NO_DELETE_KEYS_TRIGGER};
            """
        )
        conn.executescript(_SCHEMA)

    def _backfill_session_chain(self, conn: sqlite3.Connection) -> None:
        """Fill session_prev_hash for pre-1.1 rows. Not part of the v1 hash."""
        pending = conn.execute(
            """
            SELECT e.entry_id, p.session_id
            FROM evidence_ledger e
            JOIN evidence_payloads p ON p.entry_id = e.entry_id
            WHERE e.session_prev_hash IS NULL
            ORDER BY e.entry_id ASC
            """
        ).fetchall()
        if not pending:
            return
        last_hash: dict[Optional[int], str] = {}
        all_rows = conn.execute(
            """
            SELECT e.entry_id, e.entry_hash, p.session_id, e.session_prev_hash
            FROM evidence_ledger e
            JOIN evidence_payloads p ON p.entry_id = e.entry_id
            ORDER BY e.entry_id ASC
            """
        ).fetchall()
        updates: list[tuple[str, int]] = []
        for row in all_rows:
            session_id = int(row["session_id"]) if row["session_id"] is not None else None
            expected = last_hash.get(session_id, GENESIS_HASH)
            if row["session_prev_hash"] is None:
                updates.append((expected, int(row["entry_id"])))
            last_hash[session_id] = str(row["entry_hash"])
        if not updates:
            return
        conn.execute(f"DROP TRIGGER IF EXISTS {NO_UPDATE_TRIGGER}")
        conn.executemany(
            "UPDATE evidence_ledger SET session_prev_hash = ? WHERE entry_id = ?",
            updates,
        )
        self._recreate_triggers(conn)

    def _write_intent(self, payload: dict[str, Any]) -> None:
        """Atomically replace the intent file.

        Truncating the live ``.intent`` path then writing it made kill-9
        able to leave a partial JSON file (``unreadable_intent``). Write
        to ``.intent.tmp``, fsync, then ``os.replace`` so readers only
        ever see a complete previous intent or a complete new one.
        """
        path = self._intent_path()
        tmp = Path(str(path) + ".tmp")
        data = canonical_json(payload).encode("utf-8")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(str(tmp), str(path))

    def _clear_intent(self) -> None:
        path = self._intent_path()
        tmp = Path(str(path) + ".tmp")
        for target in (path, tmp):
            try:
                target.unlink()
            except FileNotFoundError:
                pass

    def _load_incomplete_write(self, conn: sqlite3.Connection) -> None:
        path = self._intent_path()
        if not path.exists():
            self._incomplete_write = None
            return
        try:
            intent = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._incomplete_write = {
                "reason": "unreadable_intent",
                "path": str(path),
            }
            return
        entry_id = intent.get("entry_id")
        present = None
        if entry_id is not None:
            present = conn.execute(
                "SELECT entry_id FROM evidence_ledger WHERE entry_id = ?",
                (int(entry_id),),
            ).fetchone()
        if present is not None:
            self._clear_intent()
            self._incomplete_write = None
            return
        self._incomplete_write = {
            "reason": "uncommitted_intent",
            "entry_id": entry_id,
            "entry_hash": intent.get("entry_hash"),
        }

    def _register_key(self, conn: sqlite3.Connection, public_key: Ed25519PublicKey) -> str:
        key_id = public_key_id(public_key)
        pem = public_key_pem(public_key)
        conn.execute(
            """
            INSERT OR IGNORE INTO signing_keys (key_id, public_key_pem, created_at)
            VALUES (?, ?, ?)
            """,
            (key_id, pem, datetime.now(timezone.utc).isoformat()),
        )
        return key_id

    def _key_for_id(self, conn: sqlite3.Connection, key_id: Optional[str]) -> Optional[Ed25519PublicKey]:
        if not key_id:
            return None
        row = conn.execute(
            "SELECT public_key_pem FROM signing_keys WHERE key_id = ?",
            (str(key_id),),
        ).fetchone()
        if row is None:
            return None
        key = load_pem_public_key(str(row["public_key_pem"]).encode("ascii"))
        if not isinstance(key, Ed25519PublicKey):
            return None
        return key

    def _all_stored_keys(self, conn: sqlite3.Connection) -> list[Ed25519PublicKey]:
        keys: list[Ed25519PublicKey] = []
        for row in conn.execute("SELECT public_key_pem FROM signing_keys").fetchall():
            try:
                key = load_pem_public_key(str(row["public_key_pem"]).encode("ascii"))
            except (ValueError, TypeError):
                continue
            if isinstance(key, Ed25519PublicKey):
                keys.append(key)
        return keys

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
        public_key = private_key.public_key()
        record_payload_hash = payload_hash(payload)
        session_id = _payload_session_id(payload)
        disposition, applied, resolved, dry_run = disposition_from_payload(
            record_type, payload
        )

        last_error: Optional[Exception] = None
        with self._write_lock:
            for attempt in range(8):
                began = False
                try:
                    conn = self._connect()
                    conn.execute("BEGIN IMMEDIATE")
                    began = True
                    key_id = self._register_key(conn, public_key)
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

                    session_prev_hash = GENESIS_HASH
                    if session_id is not None:
                        prior_session = conn.execute(
                            """
                            SELECT e.entry_hash
                            FROM evidence_ledger e
                            JOIN evidence_payloads p ON p.entry_id = e.entry_id
                            WHERE p.session_id = ?
                            ORDER BY e.entry_id DESC
                            LIMIT 1
                            """,
                            (int(session_id),),
                        ).fetchone()
                        if prior_session is not None:
                            session_prev_hash = str(prior_session["entry_hash"])

                    timestamp = datetime.now(timezone.utc).isoformat()
                    fields = {
                        "applied": applied,
                        "dry_run": dry_run,
                        "entry_id": entry_id,
                        "hash_version": HASH_VERSION,
                        "key_id": key_id,
                        "prev_hash": prev_hash,
                        "record_payload_hash": record_payload_hash,
                        "record_ref_id": int(record_ref_id),
                        "record_type": record_type,
                        "repair_disposition": disposition,
                        "resolved": resolved,
                        "session_prev_hash": session_prev_hash,
                        "timestamp": timestamp,
                    }
                    entry_hash = compute_entry_hash(fields, HASH_VERSION)
                    signature = sign_entry_hash(entry_hash, private_key)

                    self._write_intent(
                        {
                            "entry_id": entry_id,
                            "entry_hash": entry_hash,
                            "timestamp": timestamp,
                        }
                    )
                    conn.execute(
                        """
                        INSERT INTO evidence_ledger (
                            entry_id, record_type, record_ref_id, record_payload_hash,
                            prev_hash, session_prev_hash, entry_hash, timestamp,
                            signature, hash_version, key_id, repair_disposition,
                            applied, resolved, dry_run
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            entry_id,
                            record_type,
                            int(record_ref_id),
                            record_payload_hash,
                            prev_hash,
                            session_prev_hash,
                            entry_hash,
                            timestamp,
                            signature,
                            HASH_VERSION,
                            key_id,
                            disposition,
                            None if applied is None else int(applied),
                            None if resolved is None else int(resolved),
                            None if dry_run is None else int(dry_run),
                        ),
                    )
                    conn.execute(
                        """
                        INSERT INTO evidence_payloads (entry_id, session_id, payload_json)
                        VALUES (?, ?, ?)
                        """,
                        (
                            entry_id,
                            session_id,
                            canonical_json(payload),
                        ),
                    )
                    conn.commit()
                    began = False
                    self._clear_intent()
                    self._incomplete_write = None
                    return entry_id
                except sqlite3.OperationalError as exc:
                    last_error = exc
                    if began:
                        try:
                            conn.rollback()
                        except sqlite3.Error:
                            pass
                    self._clear_intent()
                    time.sleep(0.02 * (attempt + 1))
                except sqlite3.IntegrityError as exc:
                    last_error = exc
                    if began:
                        try:
                            conn.rollback()
                        except sqlite3.Error:
                            pass
                    self._clear_intent()
                    time.sleep(0.02 * (attempt + 1))
                except Exception:
                    if began:
                        try:
                            conn.rollback()
                        except sqlite3.Error:
                            pass
                    raise
            assert last_error is not None
            raise last_error

    def _fail(
        self,
        entry_id: Optional[int],
        checked: int,
        reason: str,
        **extra: Any,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "valid": False,
            "broken_at_entry_id": entry_id,
            "total_checked": checked,
            "reason": reason,
            "incomplete_write": self._incomplete_write,
            "matched_ref": extra.pop("matched_ref", 0),
        }
        result.update(extra)
        return result

    def verify_chain(
        self, record_ref_id: Optional[int] = None
    ) -> dict[str, Any]:
        """Walk the full ledger and check hash/signature/links/payloads.

        ``record_ref_id`` never weakens the walk — the chain is global.
        It only populates ``matched_ref``. A filtered slice that skipped
        predecessor checks would miss a tamper on a non-matching row.
        """
        with self._write_lock:
            conn = self._connect()
            self._load_incomplete_write(conn)
            if self._incomplete_write is not None:
                return self._fail(
                    self._incomplete_write.get("entry_id"),
                    0,
                    str(self._incomplete_write.get("reason") or "uncommitted_intent"),
                )

            rows = list(
                conn.execute(
                    """
                    SELECT
                        e.entry_id, e.record_type, e.record_ref_id,
                        e.record_payload_hash, e.prev_hash, e.session_prev_hash,
                        e.entry_hash, e.timestamp, e.signature, e.hash_version,
                        e.key_id, e.repair_disposition, e.applied, e.resolved,
                        e.dry_run, p.payload_json, p.session_id
                    FROM evidence_ledger e
                    LEFT JOIN evidence_payloads p ON p.entry_id = e.entry_id
                    ORDER BY e.entry_id ASC
                    """
                ).fetchall()
            )
            stored_keys = self._all_stored_keys(conn)
            try:
                current_key = self._require_public_key()
            except RuntimeError:
                current_key = None

            checked = 0
            matched_ref = 0
            expected_id = 1
            last_global_hash = GENESIS_HASH
            last_session_hash: dict[Optional[int], str] = {}

            for row in rows:
                entry_id = int(row["entry_id"])
                if entry_id != expected_id:
                    return self._fail(
                        expected_id,
                        checked,
                        "entry_id_gap",
                        gap_after_entry_id=expected_id - 1 if expected_id > 1 else None,
                        found_entry_id=entry_id,
                        matched_ref=matched_ref,
                    )

                if row["payload_json"] is None:
                    return self._fail(
                        entry_id, checked, "missing_payload", matched_ref=matched_ref
                    )
                payload_obj = json.loads(str(row["payload_json"]))
                if payload_hash(payload_obj) != str(row["record_payload_hash"]):
                    return self._fail(
                        entry_id,
                        checked,
                        "payload_hash_mismatch",
                        matched_ref=matched_ref,
                    )

                hash_version = int(row["hash_version"] or 1)
                applied = _json_bool(row["applied"])
                resolved = _json_bool(row["resolved"])
                dry_run = _json_bool(row["dry_run"])
                fields = {
                    "applied": applied,
                    "dry_run": dry_run,
                    "entry_id": entry_id,
                    "hash_version": hash_version,
                    "key_id": str(row["key_id"]) if row["key_id"] is not None else None,
                    "prev_hash": str(row["prev_hash"]),
                    "record_payload_hash": str(row["record_payload_hash"]),
                    "record_ref_id": int(row["record_ref_id"]),
                    "record_type": str(row["record_type"]),
                    "repair_disposition": (
                        str(row["repair_disposition"])
                        if row["repair_disposition"] is not None
                        else None
                    ),
                    "resolved": resolved,
                    "session_prev_hash": (
                        str(row["session_prev_hash"])
                        if row["session_prev_hash"] is not None
                        else None
                    ),
                    "timestamp": str(row["timestamp"]),
                }
                recomputed = compute_entry_hash(fields, hash_version)
                if recomputed != str(row["entry_hash"]):
                    return self._fail(
                        entry_id, checked, "hash_mismatch", matched_ref=matched_ref
                    )

                key = self._key_for_id(conn, fields["key_id"])
                candidates = []
                if key is not None:
                    candidates.append(key)
                candidates.extend(stored_keys)
                if current_key is not None:
                    candidates.append(current_key)
                sig_ok = False
                seen: set[int] = set()
                for candidate in candidates:
                    ident = id(candidate)
                    if ident in seen:
                        continue
                    seen.add(ident)
                    if verify_entry_signature(
                        str(row["entry_hash"]), str(row["signature"]), candidate
                    ):
                        sig_ok = True
                        break
                if not sig_ok:
                    return self._fail(
                        entry_id, checked, "bad_signature", matched_ref=matched_ref
                    )

                if str(row["prev_hash"]) != last_global_hash:
                    return self._fail(
                        entry_id,
                        checked,
                        "prev_hash_mismatch",
                        matched_ref=matched_ref,
                    )

                session_id = row["session_id"]
                if session_id is not None:
                    session_id = int(session_id)
                expected_session_prev = last_session_hash.get(session_id, GENESIS_HASH)
                stored_session_prev = fields["session_prev_hash"]
                if stored_session_prev is not None and stored_session_prev != expected_session_prev:
                    return self._fail(
                        entry_id,
                        checked,
                        "session_prev_hash_mismatch",
                        matched_ref=matched_ref,
                    )

                if record_ref_id is not None and int(row["record_ref_id"]) == int(
                    record_ref_id
                ):
                    matched_ref += 1

                last_global_hash = str(row["entry_hash"])
                last_session_hash[session_id] = str(row["entry_hash"])
                checked += 1
                expected_id = entry_id + 1

            return {
                "valid": True,
                "broken_at_entry_id": None,
                "total_checked": checked,
                "reason": None,
                "incomplete_write": None,
                "matched_ref": matched_ref,
            }

    def export_attestation(self, session_id: int) -> dict[str, Any]:
        """Export a schema 1.1 attestation bundle for ``session_id``.

        Hashes, signatures, and signed repair disposition — not the raw
        attribution/repair payloads. Session-scoped ``session_prev_hash``
        lets a third party verify a single-session bundle even when other
        sessions interleaved on the global chain.
        """
        with self._write_lock:
            public_key = self._require_public_key()
            conn = self._connect()
            rows = conn.execute(
                """
                SELECT
                    e.entry_id, e.record_type, e.record_ref_id, e.record_payload_hash,
                    e.prev_hash, e.session_prev_hash, e.entry_hash, e.timestamp,
                    e.signature, e.hash_version, e.key_id, e.repair_disposition,
                    e.applied, e.resolved, e.dry_run
                FROM evidence_ledger e
                JOIN evidence_payloads p ON p.entry_id = e.entry_id
                WHERE p.session_id = ?
                ORDER BY e.entry_id ASC
                """,
                (int(session_id),),
            ).fetchall()

            entries: list[dict[str, Any]] = []
            key_ids: list[str] = []
            for row in rows:
                key_id = str(row["key_id"]) if row["key_id"] is not None else public_key_id(public_key)
                if key_id not in key_ids:
                    key_ids.append(key_id)
                entries.append(
                    {
                        "entry_id": int(row["entry_id"]),
                        "record_type": str(row["record_type"]),
                        "record_ref_id": int(row["record_ref_id"]),
                        "record_payload_hash": str(row["record_payload_hash"]),
                        "prev_hash": str(row["prev_hash"]),
                        "session_prev_hash": (
                            str(row["session_prev_hash"])
                            if row["session_prev_hash"] is not None
                            else GENESIS_HASH
                        ),
                        "entry_hash": str(row["entry_hash"]),
                        "timestamp": str(row["timestamp"]),
                        "signature": str(row["signature"]),
                        "hash_version": int(row["hash_version"] or HASH_VERSION),
                        "key_id": key_id,
                        "repair_disposition": (
                            str(row["repair_disposition"])
                            if row["repair_disposition"] is not None
                            else None
                        ),
                        "applied": _json_bool(row["applied"]),
                        "resolved": _json_bool(row["resolved"]),
                        "dry_run": _json_bool(row["dry_run"]),
                    }
                )

            keys: dict[str, str] = {}
            for key_id in key_ids:
                stored = conn.execute(
                    "SELECT public_key_pem FROM signing_keys WHERE key_id = ?",
                    (key_id,),
                ).fetchone()
                if stored is not None:
                    keys[key_id] = str(stored["public_key_pem"])
            if not keys:
                keys[public_key_id(public_key)] = public_key_pem(public_key)

            current_pem = public_key_pem(public_key)
            current_id = public_key_id(public_key)
            summary = repair_summary_from_entries(entries)
            return {
                "schema_version": SCHEMA_VERSION,
                "issuer": ISSUER,
                "session_id": str(session_id),
                "exported_at": datetime.now(timezone.utc).isoformat(),
                "public_key_pem": current_pem,
                "public_key_fingerprint": f"sha256:{current_id}",
                "keys": keys,
                "entries": entries,
                "chain_root_hash": entries[0]["entry_hash"] if entries else None,
                "chain_tip_hash": entries[-1]["entry_hash"] if entries else None,
                "repair_summary": summary,
                "layer3_contract": {
                    "verify_fix_never_applies": True,
                    "applied_means": (
                        "A caller applied a candidate out of band. "
                        "Layer 3 verify_fix() never sets applied=true."
                    ),
                    "resolved_means": (
                        "Shadow verification succeeded. The live session "
                        "was not mutated."
                    ),
                    "verified_not_applied_means": (
                        "resolved=true and applied=false: a candidate was "
                        "built and checked in shadow, then left unapplied."
                    ),
                },
            }

    def reconcile_shadow_log(
        self, storage: Any, session_id: Optional[int] = None
    ) -> dict[str, Any]:
        """Compare repair ledger rows to ``shadow_repair_log``.

        Detects a shadow attempt that was never written to the ledger.
        Never-attempted Layer 2 attributions have no independent table
        and remain undetectable by design.
        """
        with self._write_lock:
            conn = self._connect()
            params: tuple[Any, ...] = ()
            sql = """
                SELECT e.entry_id, e.record_ref_id, p.session_id
                FROM evidence_ledger e
                JOIN evidence_payloads p ON p.entry_id = e.entry_id
                WHERE e.record_type = 'repair'
            """
            if session_id is not None:
                sql += " AND p.session_id = ?"
                params = (int(session_id),)
            evidence_pairs = {
                (int(row["session_id"]), int(row["record_ref_id"]))
                for row in conn.execute(sql, params).fetchall()
                if row["session_id"] is not None
            }

        logs = storage.get_shadow_repair_log()
        shadow_pairs = set()
        for row in logs:
            sid = int(row["session_id"])
            fid = int(row["failed_call_id"])
            if session_id is not None and sid != int(session_id):
                continue
            shadow_pairs.add((sid, fid))

        missing_from_ledger = sorted(shadow_pairs - evidence_pairs)
        extra_in_ledger = sorted(evidence_pairs - shadow_pairs)
        return {
            "matched": len(shadow_pairs & evidence_pairs),
            "missing_from_ledger": missing_from_ledger,
            "extra_in_ledger": extra_in_ledger,
            "complete": not missing_from_ledger,
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
