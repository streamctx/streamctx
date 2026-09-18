"""Layer 4 senior-bar regression tests.

Adversarial cases from the Compliance Evidence hardening cycle.
The ledger must not imply a Layer 3 repair was applied when it was only
shadow-verified, must detect the four tamper classes with a precise
``broken_at_entry_id``, and must survive concurrent and kill-9 writes.
"""

from __future__ import annotations

import json
import multiprocessing
import sqlite3
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from types import SimpleNamespace

import pytest

from streamctx.evidence import (
    NO_DELETE_PAYLOAD_TRIGGER,
    NO_DELETE_TRIGGER,
    NO_UPDATE_PAYLOAD_TRIGGER,
    NO_UPDATE_TRIGGER,
    SCHEMA_VERSION,
    EvidenceLedger,
    generate_keypair,
)
from streamctx.repair import VerifiedRepairEngine
from streamctx.storage import SessionStorage

VERIFY_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verify_attestation.py"


def _fake_response(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))]
    )


def _buried(fact: str, question: str, fillers: int = 16) -> list[dict]:
    msgs = [
        {"role": "system", "content": "Answer from the report only."},
        {"role": "user", "content": question},
        {"role": "assistant", "content": "I will look at the report."},
        {
            "role": "user",
            "content": (
                "We discussed many operational topics. "
                + ("chatter " * 80)
                + f" Buried fact: {fact}"
            ),
        },
        {"role": "assistant", "content": "Noted the operational discussion."},
    ]
    for i in range(fillers):
        msgs.append(
            {"role": "user", "content": f"Filler discussion {i} " + ("padding " * 40)}
        )
        msgs.append(
            {"role": "assistant", "content": f"Filler reply {i} " + ("content " * 40)}
        )
    msgs.append({"role": "user", "content": question})
    return msgs


def _seed_compression_session(storage: SessionStorage):
    sid = storage.start_session()
    first = [{"role": "user", "content": "original task: summarize the report"}]
    storage.persist_step(
        session_id=sid,
        provider="openai",
        model="test",
        input_tokens=40,
        output_tokens=8,
        cost=0.0,
        reused_tokens=0,
        waste_category=None,
        request_messages=first,
        checkpoint_messages=first + [{"role": "assistant", "content": "ok"}],
        step_number=1,
        failed=False,
    )
    fact = "$12.4 million"
    failed_msgs = _buried(fact, "What was the exact Q3 revenue figure?")
    failed_id = storage.record_call(
        session_id=sid,
        provider="openai",
        model="test",
        input_tokens=0,
        output_tokens=0,
        cost=0.0,
        reused_tokens=0,
        waste_category=None,
        messages=failed_msgs + [{"role": "assistant", "content": "$47.3 million"}],
        failed=True,
        error_message=None,
    )
    return sid, failed_id, fact


@pytest.fixture
def keypair(tmp_path):
    priv = tmp_path / "evidence_private.pem"
    pub = tmp_path / "evidence_public.pem"
    generate_keypair(priv, pub)
    return priv, pub


@pytest.fixture
def ledger(tmp_path, keypair):
    priv, pub = keypair
    return EvidenceLedger(
        db_path=tmp_path / "evidence_ledger.db",
        private_key_path=priv,
        public_key_path=pub,
    )


def _drop_triggers(ledger: EvidenceLedger) -> sqlite3.Connection:
    conn = ledger._connect()
    conn.execute(f"DROP TRIGGER IF EXISTS {NO_UPDATE_TRIGGER}")
    conn.execute(f"DROP TRIGGER IF EXISTS {NO_DELETE_TRIGGER}")
    conn.execute(f"DROP TRIGGER IF EXISTS {NO_UPDATE_PAYLOAD_TRIGGER}")
    conn.execute(f"DROP TRIGGER IF EXISTS {NO_DELETE_PAYLOAD_TRIGGER}")
    conn.commit()
    return conn


def _append_n(ledger: EvidenceLedger, n: int, session_id: int = 1) -> None:
    for i in range(n):
        record_type = "attribution" if i % 2 == 0 else "repair"
        payload: dict = {"session_id": session_id, "i": i, "reason": f"r-{i}"}
        if record_type == "repair":
            payload.update(applied=False, resolved=False, dry_run=True)
        ledger.append_evidence(record_type, 100 + i, payload)


def _run_verify(bundle_path: Path, *extra: str):
    import subprocess
    import sys

    return subprocess.run(
        [sys.executable, str(VERIFY_SCRIPT), str(bundle_path), *extra],
        capture_output=True,
        text=True,
        check=False,
    )


def test_schema_version_is_1_1():
    assert SCHEMA_VERSION == "1.1"


def test_no_paid_gate_in_evidence_source():
    src = Path(__file__).resolve().parents[1] / "src" / "streamctx" / "evidence.py"
    text = src.read_text(encoding="utf-8")
    assert "license_key" not in text
    assert "requires_pro" not in text
    assert "STREAMCTX_PAID" not in text
    assert "if paid" not in text.lower()
    assert "MIT-licensed" in text


def test_tamper_field_modify_broken_at(ledger):
    _append_n(ledger, 5)
    conn = _drop_triggers(ledger)
    conn.execute(
        "UPDATE evidence_ledger SET record_payload_hash = ? WHERE entry_id = 3",
        ("cd" * 32,),
    )
    conn.commit()
    result = ledger.verify_chain()
    assert result["valid"] is False
    assert result["broken_at_entry_id"] == 3
    assert result["reason"] in ("payload_hash_mismatch", "hash_mismatch")


def test_tamper_delete_broken_at_missing_id(ledger):
    _append_n(ledger, 5)
    conn = _drop_triggers(ledger)
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("DELETE FROM evidence_payloads WHERE entry_id = 3")
    conn.execute("DELETE FROM evidence_ledger WHERE entry_id = 3")
    conn.commit()
    result = ledger.verify_chain()
    assert result["valid"] is False
    assert result["broken_at_entry_id"] == 3
    assert result["reason"] == "entry_id_gap"
    assert result["found_entry_id"] == 4
    assert result["gap_after_entry_id"] == 2


def test_tamper_reorder_broken_at(ledger):
    _append_n(ledger, 5)
    conn = _drop_triggers(ledger)
    rows = list(conn.execute("SELECT * FROM evidence_ledger ORDER BY entry_id").fetchall())
    a, b = rows[1], rows[2]
    fields = [
        "record_type",
        "record_ref_id",
        "record_payload_hash",
        "prev_hash",
        "session_prev_hash",
        "entry_hash",
        "timestamp",
        "signature",
        "hash_version",
        "key_id",
        "repair_disposition",
        "applied",
        "resolved",
        "dry_run",
    ]
    for field in fields:
        conn.execute(
            f"UPDATE evidence_ledger SET {field}=? WHERE entry_id=?",
            (b[field], a["entry_id"]),
        )
        conn.execute(
            f"UPDATE evidence_ledger SET {field}=? WHERE entry_id=?",
            (a[field], b["entry_id"]),
        )
    conn.commit()
    result = ledger.verify_chain()
    assert result["valid"] is False
    assert result["broken_at_entry_id"] in (2, 3)


def test_tamper_foreign_splice_broken_at(ledger):
    _append_n(ledger, 3)
    conn = _drop_triggers(ledger)
    last = conn.execute(
        "SELECT entry_hash FROM evidence_ledger ORDER BY entry_id DESC LIMIT 1"
    ).fetchone()
    conn.execute(
        """
        INSERT INTO evidence_ledger (
            entry_id, record_type, record_ref_id, record_payload_hash,
            prev_hash, session_prev_hash, entry_hash, timestamp, signature,
            hash_version, key_id, repair_disposition, applied, resolved, dry_run
        ) VALUES (99, 'repair', 999, ?, ?, ?, ?, ?, ?, 2, 'deadbeef', 'applied', 1, 1, 0)
        """,
        (
            "ee" * 32,
            last["entry_hash"],
            last["entry_hash"],
            "ffff" * 16,
            "2020-01-01T00:00:00+00:00",
            "A" * 88,
        ),
    )
    conn.commit()
    result = ledger.verify_chain()
    assert result["valid"] is False
    assert result["reason"] == "entry_id_gap"
    assert result["broken_at_entry_id"] == 4
    assert result["found_entry_id"] == 99


def test_payload_rewrite_is_detected(ledger):
    ledger.append_evidence(
        "repair",
        7,
        {
            "session_id": 1,
            "applied": False,
            "resolved": True,
            "dry_run": False,
        },
    )
    conn = _drop_triggers(ledger)
    conn.execute(
        "UPDATE evidence_payloads SET payload_json=? WHERE entry_id=1",
        (
            json.dumps(
                {"session_id": 1, "applied": True, "resolved": True, "dry_run": False}
            ),
        ),
    )
    conn.commit()
    result = ledger.verify_chain()
    assert result["valid"] is False
    assert result["broken_at_entry_id"] == 1
    assert result["reason"] == "payload_hash_mismatch"


def test_interleaved_session_export_verifies(tmp_path, ledger, keypair):
    ledger.append_evidence("attribution", 1, {"session_id": 1, "n": 1})
    ledger.append_evidence("attribution", 2, {"session_id": 2, "n": 2})
    ledger.append_evidence(
        "repair",
        1,
        {"session_id": 1, "applied": False, "resolved": True, "dry_run": False, "n": 3},
    )
    bundle = ledger.export_attestation(1)
    assert len(bundle["entries"]) == 2
    assert bundle["entries"][0]["session_prev_hash"] == "0" * 64
    assert bundle["entries"][1]["session_prev_hash"] == bundle["entries"][0]["entry_hash"]
    assert bundle["repair_summary"]["applied_count"] == 0
    assert bundle["repair_summary"]["verified_not_applied_count"] == 1
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(bundle), encoding="utf-8")
    _, pub = keypair
    result = _run_verify(path, "--public-key", str(pub), "--verbose")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "VERDICT: PASS" in result.stdout
    assert "AUTHENTICITY: PINNED" in result.stdout
    assert "verified_not_applied" in result.stdout


def test_shadow_verified_not_applied_is_unambiguous(tmp_path, ledger, keypair):
    storage = SessionStorage(db_path=tmp_path / "sessions.db")
    sid, fid, fact = _seed_compression_session(storage)
    engine = VerifiedRepairEngine(storage=storage, evidence=ledger)
    result = engine.verify_fix(
        sid,
        fid,
        llm_fn=lambda _m: _fake_response(f"The exact Q3 revenue was {fact}."),
        dry_run=False,
        correct_value=fact,
    )
    assert result.resolved is True
    assert result.applied is False

    bundle = ledger.export_attestation(sid)
    repair_entries = [e for e in bundle["entries"] if e["record_type"] == "repair"]
    assert repair_entries, bundle
    for entry in repair_entries:
        assert entry["applied"] is False
        assert entry["repair_disposition"] == "verified_not_applied"
        assert entry["resolved"] is True
    assert bundle["repair_summary"]["applied_count"] == 0
    assert bundle["repair_summary"]["verified_not_applied_count"] >= 1

    path = tmp_path / "attestation.json"
    path.write_text(json.dumps(bundle), encoding="utf-8")
    _, pub = keypair
    verified = _run_verify(path, "--public-key", str(pub), "--verbose")
    assert verified.returncode == 0, verified.stdout
    assert "verified_not_applied" in verified.stdout
    assert "applied=False" in verified.stdout or "applied=false" in verified.stdout.lower()
    assert "no bundled repair was applied" in verified.stdout


def test_foreign_keypair_bundle_rejected_when_pinned(tmp_path, keypair):
    honest_priv, honest_pub = keypair
    honest = EvidenceLedger(
        db_path=tmp_path / "honest.db",
        private_key_path=honest_priv,
        public_key_path=honest_pub,
    )
    honest.append_evidence("attribution", 1, {"session_id": 1})

    attacker_priv = tmp_path / "atk_priv.pem"
    attacker_pub = tmp_path / "atk_pub.pem"
    generate_keypair(attacker_priv, attacker_pub)
    attacker = EvidenceLedger(
        db_path=tmp_path / "atk.db",
        private_key_path=attacker_priv,
        public_key_path=attacker_pub,
    )
    attacker.append_evidence(
        "repair",
        1,
        {"session_id": 9, "applied": True, "resolved": True, "dry_run": False},
    )
    forged = attacker.export_attestation(9)
    path = tmp_path / "forged.json"
    path.write_text(json.dumps(forged), encoding="utf-8")

    unpinned = _run_verify(path)
    assert unpinned.returncode == 0
    assert "AUTHENTICITY: UNPINNED" in unpinned.stdout

    pinned = _run_verify(path, "--public-key", str(honest_pub))
    assert pinned.returncode == 1
    assert "VERDICT: FAIL" in pinned.stdout


def test_key_rotation_keeps_historic_entries_verifiable(tmp_path):
    priv1, pub1 = tmp_path / "p1.pem", tmp_path / "u1.pem"
    priv2, pub2 = tmp_path / "p2.pem", tmp_path / "u2.pem"
    generate_keypair(priv1, pub1)
    generate_keypair(priv2, pub2)
    db = tmp_path / "e.db"
    first = EvidenceLedger(db_path=db, private_key_path=priv1, public_key_path=pub1)
    first.append_evidence("attribution", 1, {"session_id": 1, "k": 1})
    first.close()
    second = EvidenceLedger(db_path=db, private_key_path=priv2, public_key_path=pub2)
    second.append_evidence("attribution", 2, {"session_id": 1, "k": 2})
    result = second.verify_chain()
    assert result["valid"] is True, result
    assert result["total_checked"] == 2
    bundle = second.export_attestation(1)
    assert len(bundle["keys"]) == 2


def test_concurrent_append_50_separate_ledger_objects(tmp_path, keypair):
    priv, pub = keypair
    db = tmp_path / "conc.db"
    errors: list[str] = []

    def worker(wid: int) -> int:
        led = EvidenceLedger(db_path=db, private_key_path=priv, public_key_path=pub)
        try:
            return led.append_evidence(
                "attribution",
                wid,
                {"session_id": wid, "worker": wid},
            )
        except Exception as exc:
            errors.append(str(exc))
            raise
        finally:
            led.close()

    ids: list[int] = []
    with ThreadPoolExecutor(max_workers=50) as pool:
        futs = [pool.submit(worker, w) for w in range(50)]
        for fut in as_completed(futs):
            ids.append(fut.result())

    assert errors == [], errors[:3]
    assert len(ids) == 50
    assert len(set(ids)) == 50
    led = EvidenceLedger(db_path=db, private_key_path=priv, public_key_path=pub)
    chain = led.verify_chain()
    assert chain["valid"] is True, chain
    assert chain["total_checked"] == 50
    n_ledger = led._connect().execute("SELECT COUNT(*) FROM evidence_ledger").fetchone()[0]
    n_payload = led._connect().execute("SELECT COUNT(*) FROM evidence_payloads").fetchone()[0]
    assert n_ledger == n_payload == 50


def test_silent_omission_is_detectable_against_shadow_log(tmp_path, ledger, monkeypatch):
    monkeypatch.setenv("STREAMCTX_SHADOW_REPAIR", "1")
    monkeypatch.setenv("STREAMCTX_SHADOW_REPAIR_SYNC", "1")
    storage = SessionStorage(db_path=tmp_path / "sessions.db")
    sid, fid, _fact = _seed_compression_session(storage)
    logs = storage.get_shadow_repair_log()
    assert len(logs) == 1
    # Shadow ran, but we never passed this storage's evidence ledger into it.
    recon = ledger.reconcile_shadow_log(storage, session_id=sid)
    assert recon["complete"] is False
    assert (int(sid), int(fid)) in recon["missing_from_ledger"]


def test_torn_intent_file_is_unreadable(tmp_path, ledger):
    ledger.append_evidence("attribution", 1, {"session_id": 1, "ok": True})
    intent = Path(str(ledger.db_path) + ".intent")
    intent.write_bytes(b"{")
    result = ledger.verify_chain()
    assert result["valid"] is False
    assert result["reason"] == "unreadable_intent"


def test_uncommitted_intent_fails_verify(tmp_path, ledger):
    ledger.append_evidence("attribution", 1, {"session_id": 1, "ok": True})
    intent = Path(str(ledger.db_path) + ".intent")
    intent.write_text(
        json.dumps({"entry_id": 2, "entry_hash": "ab" * 32, "timestamp": "x"}),
        encoding="utf-8",
    )
    result = ledger.verify_chain()
    assert result["valid"] is False
    assert result["reason"] == "uncommitted_intent"
    assert result["broken_at_entry_id"] == 2
    assert result["incomplete_write"]["entry_id"] == 2


# Windows spawn requires module-level target.
def _kill9_evidence_writer(db_path: str, priv: str, pub: str) -> None:
    led = EvidenceLedger(
        db_path=Path(db_path),
        private_key_path=Path(priv),
        public_key_path=Path(pub),
    )
    blob = "x" * 20_000
    for i in range(400):
        led.append_evidence(
            "attribution",
            i,
            {"session_id": 1, "i": i, "blob": blob},
        )


def test_kill9_mid_write_fail_safe():
    tmp = tempfile.mkdtemp(prefix="streamctx-l4-kill9-")
    db_path = str(Path(tmp) / "evidence_ledger.db")
    priv = str(Path(tmp) / "priv.pem")
    pub = str(Path(tmp) / "pub.pem")
    generate_keypair(Path(priv), Path(pub))
    proc = multiprocessing.Process(
        target=_kill9_evidence_writer, args=(db_path, priv, pub)
    )
    proc.start()
    deadline = time.time() + 8.0
    while time.time() < deadline:
        if Path(db_path).exists():
            try:
                probe = sqlite3.connect(db_path, timeout=1.0)
                n = probe.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE name='evidence_ledger'"
                ).fetchone()[0]
                rows = (
                    probe.execute("SELECT COUNT(*) FROM evidence_ledger").fetchone()[0]
                    if n
                    else 0
                )
                probe.close()
                if rows >= 4:
                    break
            except sqlite3.OperationalError:
                pass
        time.sleep(0.04)
    proc.kill()
    proc.join(timeout=5)

    conn = sqlite3.connect(db_path)
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    n_ledger = conn.execute("SELECT COUNT(*) FROM evidence_ledger").fetchone()[0]
    n_payload = conn.execute("SELECT COUNT(*) FROM evidence_payloads").fetchone()[0]
    conn.close()
    assert integrity == "ok"
    assert n_ledger == n_payload
    assert n_ledger >= 1

    led = EvidenceLedger(
        db_path=Path(db_path), private_key_path=Path(priv), public_key_path=Path(pub)
    )
    result = led.verify_chain()
    assert result.get("reason") != "unreadable_intent"
    if result["incomplete_write"]:
        assert result["valid"] is False
        assert result["reason"] == "uncommitted_intent"
    else:
        assert result["valid"] is True
        assert result["total_checked"] == n_ledger
