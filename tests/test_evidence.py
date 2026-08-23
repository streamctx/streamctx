"""Tests for streamctx.evidence (Layer 4 Compliance Evidence ledger)."""

from __future__ import annotations

import sqlite3

import pytest

from streamctx.attribution import AttributionEngine
from streamctx.evidence import (
    NO_UPDATE_TRIGGER,
    EvidenceLedger,
    payload_hash,
)
from streamctx.repair import VerifiedRepairEngine
from streamctx.storage import SessionStorage


TEST_KEY = "streamctx-test-evidence-key"


@pytest.fixture
def ledger(tmp_path):
    return EvidenceLedger(db_path=tmp_path / "evidence_ledger.db", key=TEST_KEY)


def _payload(i: int, session_id: int = 1) -> dict:
    return {
        "session_id": session_id,
        "failed_call_id": 100 + i,
        "reason": f"record-{i}",
        "confidence": i / 10.0,
    }


def _append_n(ledger: EvidenceLedger, n: int, session_id: int = 1) -> list[int]:
    ids = []
    for i in range(n):
        record_type = "attribution" if i % 2 == 0 else "repair"
        ids.append(
            ledger.append_evidence(record_type, 100 + i, _payload(i, session_id))
        )
    return ids


def _mutate_ledger(ledger: EvidenceLedger, sql: str, params: tuple) -> None:
    """Bypass append-only triggers so tests can simulate a tamper."""
    conn = ledger._connect()
    conn.execute(f"DROP TRIGGER IF EXISTS {NO_UPDATE_TRIGGER}")
    conn.execute(sql, params)
    conn.commit()


class _BoomLedger:
    def append_evidence(self, *args, **kwargs):
        raise RuntimeError("forced evidence failure")


# ---------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------


def test_append_ten_verify_chain_valid(ledger):
    ids = _append_n(ledger, 10)
    assert ids == list(range(1, 11))

    result = ledger.verify_chain()
    assert result["valid"] is True
    assert result["broken_at_entry_id"] is None
    assert result["total_checked"] == 10


def test_verify_chain_filtered_slice(ledger):
    _append_n(ledger, 6)
    result = ledger.verify_chain(record_ref_id=103)
    assert result["valid"] is True
    assert result["total_checked"] == 1


def test_genesis_prev_hash_is_zero(ledger):
    ledger.append_evidence("attribution", 1, _payload(0))
    row = ledger._connect().execute(
        "SELECT prev_hash FROM evidence_ledger WHERE entry_id = 1"
    ).fetchone()
    assert row["prev_hash"] == "0" * 64


def test_update_and_delete_are_rejected(ledger):
    ledger.append_evidence("attribution", 1, _payload(0))
    conn = ledger._connect()
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(
            "UPDATE evidence_ledger SET record_payload_hash = ? WHERE entry_id = 1",
            ("00" * 32,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM evidence_ledger WHERE entry_id = 1")


# ---------------------------------------------------------------------
# Tamper
# ---------------------------------------------------------------------


def test_payload_hash_tamper_reports_broken_entry(ledger):
    _append_n(ledger, 10)
    tampered = "ab" * 32
    _mutate_ledger(
        ledger,
        "UPDATE evidence_ledger SET record_payload_hash = ? WHERE entry_id = ?",
        (tampered, 4),
    )

    result = ledger.verify_chain()
    assert result["valid"] is False
    assert result["broken_at_entry_id"] == 4
    assert result["total_checked"] == 3


def test_signature_tamper_is_caught(ledger):
    _append_n(ledger, 8)
    _mutate_ledger(
        ledger,
        "UPDATE evidence_ledger SET signature = ? WHERE entry_id = ?",
        ("ff" * 32, 6),
    )

    result = ledger.verify_chain()
    assert result["valid"] is False
    assert result["broken_at_entry_id"] == 6
    assert result["total_checked"] == 5


# ---------------------------------------------------------------------
# Non-blocking hooks
# ---------------------------------------------------------------------


def _seed_content_failure(storage, session_id=8):
    """Two-call session that attribution can score (drift)."""
    first = [{"role": "user", "content": "original task: summarize the report"}]
    second = [
        {"role": "user", "content": "original task: summarize the report"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "continue"},
    ]
    storage.record_call(
        session_id,
        provider="test",
        model="test-model",
        input_tokens=50,
        output_tokens=10,
        cost=0.0,
        reused_tokens=0,
        waste_category="ok",
        messages=first,
    )
    storage.record_call(
        session_id,
        provider="test",
        model="test-model",
        input_tokens=500,
        output_tokens=10,
        cost=0.0,
        reused_tokens=0,
        waste_category="drift",
        messages=second,
        failed=True,
        error_message="context overflow",
    )
    calls = storage.get_calls_for_session(session_id)
    failed_id = next(c["id"] for c in calls if c["failed"])
    storage.save_checkpoint(session_id, 1, first)
    storage.save_checkpoint(session_id, 2, second)
    return failed_id


def test_attribute_failure_continues_when_evidence_raises(tmp_path):
    storage = SessionStorage(db_path=tmp_path / "sessions.db")
    session_id = storage.start_session()
    failed_id = _seed_content_failure(storage, session_id)

    engine = AttributionEngine(storage=storage, evidence=_BoomLedger())
    result = engine.attribute_failure(session_id, failed_id)

    assert result.failed_call_id == failed_id
    assert result.session_id == session_id
    assert result.reason


def test_repair_continues_when_evidence_raises(tmp_path):
    storage = SessionStorage(db_path=tmp_path / "sessions.db")
    session_id = storage.start_session()
    failed_id = _seed_content_failure(storage, session_id)

    engine = VerifiedRepairEngine(storage=storage, evidence=_BoomLedger())
    result = engine.verify_fix(session_id, failed_id)

    assert result.failed_call_id == failed_id
    assert result.session_id == session_id
    assert result.dry_run is True
    assert result.proof


def test_hooks_append_on_success(tmp_path, ledger):
    storage = SessionStorage(db_path=tmp_path / "sessions.db")
    session_id = storage.start_session()
    failed_id = _seed_content_failure(storage, session_id)

    attr = AttributionEngine(storage=storage, evidence=ledger)
    attr_result = attr.attribute_failure(session_id, failed_id)
    assert attr_result.root_cause_call_id is not None

    repair = VerifiedRepairEngine(storage=storage, evidence=ledger)
    repair.verify_fix(session_id, failed_id)

    chain = ledger.verify_chain()
    assert chain["valid"] is True
    # attribution hook + (attribution inside verify_fix) + repair result
    assert chain["total_checked"] == 3

    rows = ledger._connect().execute(
        "SELECT record_type FROM evidence_ledger ORDER BY entry_id"
    ).fetchall()
    types = [r["record_type"] for r in rows]
    assert types.count("attribution") == 2
    assert types.count("repair") == 1


# ---------------------------------------------------------------------
# Export (implemented; format still open for review)
# ---------------------------------------------------------------------


def test_export_attestation_includes_payloads_and_signature(ledger):
    _append_n(ledger, 4, session_id=7)
    ledger.append_evidence("attribution", 999, _payload(0, session_id=8))

    bundle = ledger.export_attestation(7)
    assert bundle["format"] == "streamctx.evidence.v1"
    assert bundle["session_id"] == 7
    assert len(bundle["entries"]) == 4
    assert len(bundle["payloads"]) == 4
    assert bundle["bundle_hash"]
    assert bundle["signature"]
    assert bundle["payloads"][0]["payload"]["session_id"] == 7
    assert payload_hash(bundle["payloads"][0]["payload"]) == bundle["entries"][0][
        "record_payload_hash"
    ]
    # other session stayed out
    assert all(p["payload"]["session_id"] == 7 for p in bundle["payloads"])
