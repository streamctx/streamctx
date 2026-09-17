"""Adversarial senior-bar probes for Layer 4 (Compliance Evidence).

Judges live code, not docs. Same bar as Layers 1-3: PARTIAL is not PASS.
Run from repo root::

    python scripts/layer4_senior_bar_pre.py
"""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from streamctx.evidence import (  # noqa: E402
    NO_DELETE_TRIGGER,
    NO_UPDATE_TRIGGER,
    EvidenceLedger,
    generate_keypair,
)
from streamctx.repair import VerifiedRepairEngine  # noqa: E402
from streamctx.storage import SessionStorage  # noqa: E402

VERIFY_SCRIPT = ROOT / "scripts" / "verify_attestation.py"


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
            {
                "role": "user",
                "content": f"Filler discussion {i} " + ("padding " * 40),
            }
        )
        msgs.append(
            {
                "role": "assistant",
                "content": f"Filler reply {i} " + ("content " * 40),
            }
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


def _ledger(tmp: Path) -> EvidenceLedger:
    tmp.mkdir(parents=True, exist_ok=True)
    priv, pub = tmp / "priv.pem", tmp / "pub.pem"
    generate_keypair(priv, pub)
    return EvidenceLedger(
        db_path=tmp / "evidence_ledger.db",
        private_key_path=priv,
        public_key_path=pub,
    )


def _drop_triggers(ledger: EvidenceLedger) -> sqlite3.Connection:
    conn = ledger._connect()
    conn.execute(f"DROP TRIGGER IF EXISTS {NO_UPDATE_TRIGGER}")
    conn.execute(f"DROP TRIGGER IF EXISTS {NO_DELETE_TRIGGER}")
    conn.commit()
    return conn


def _run_verify(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(VERIFY_SCRIPT), str(path)],
        capture_output=True,
        text=True,
        check=False,
    )


def verdict(name: str, grade: str, detail: str) -> None:
    print(f"[{grade:<8}] {name}")
    print(f"           {detail}")


def probe_signing_coverage(ledger: EvidenceLedger) -> None:
    ledger.append_evidence(
        "attribution", 1, {"session_id": 1, "failed_call_id": 1, "reason": "x"}
    )
    row = ledger._connect().execute(
        "SELECT * FROM evidence_ledger WHERE entry_id = 1"
    ).fetchone()
    cols = set(row.keys())
    signed_via_hash = (
        "entry_id",
        "record_type",
        "record_ref_id",
        "record_payload_hash",
        "prev_hash",
        "timestamp",
    )
    missing_from_schema = []
    for needed in ("applied", "resolved", "dry_run", "repair_disposition", "session_prev_hash", "key_id"):
        if needed not in cols:
            missing_from_schema.append(needed)
    verdict(
        "signing coverage",
        "PARTIAL" if missing_from_schema else "PASS",
        (
            f"every ledger row has signature={bool(row['signature'])}; "
            f"entry_hash covers {list(signed_via_hash)}; "
            f"first-class status/key/session-link columns missing: {missing_from_schema or 'none'}"
        ),
    )


def probe_tamper(ledger: EvidenceLedger) -> None:
    for i in range(5):
        ledger.append_evidence(
            "attribution" if i % 2 == 0 else "repair",
            10 + i,
            {"session_id": 1, "i": i, "applied": False},
        )
    conn = _drop_triggers(ledger)

    # field modify
    conn.execute(
        "UPDATE evidence_ledger SET record_payload_hash = ? WHERE entry_id = 2",
        ("ab" * 32,),
    )
    conn.commit()
    r1 = ledger.verify_chain()
    conn.execute(
        "UPDATE evidence_ledger SET record_payload_hash = ? WHERE entry_id = 2",
        (ledger._connect()
         .execute("SELECT record_payload_hash FROM evidence_ledger WHERE entry_id=2")
         .fetchone()["record_payload_hash"],),
    )
    # restore by rebuilding is messy; use a fresh ledger per case below
    verdict(
        "tamper field-modify",
        "PASS" if r1["valid"] is False and r1["broken_at_entry_id"] == 2 else "FAIL",
        f"verify_chain={r1}",
    )


def probe_tamper_cases(tmp: Path) -> None:
    tmp.mkdir(parents=True, exist_ok=True)

    def fresh():
        d = Path(tempfile.mkdtemp(dir=str(tmp)))
        return _ledger(d)

    # modify
    led = fresh()
    for i in range(5):
        led.append_evidence("attribution", 10 + i, {"session_id": 1, "i": i})
    conn = _drop_triggers(led)
    conn.execute(
        "UPDATE evidence_ledger SET record_payload_hash=? WHERE entry_id=3",
        ("cd" * 32,),
    )
    conn.commit()
    r = led.verify_chain()
    verdict(
        "tamper field-modify",
        "PASS" if (not r["valid"] and r["broken_at_entry_id"] == 3) else "FAIL",
        f"{r}",
    )

    # delete
    led = fresh()
    for i in range(5):
        led.append_evidence("attribution", 10 + i, {"session_id": 1, "i": i})
    conn = _drop_triggers(led)
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("DELETE FROM evidence_payloads WHERE entry_id=3")
    conn.execute("DELETE FROM evidence_ledger WHERE entry_id=3")
    conn.commit()
    conn.execute("PRAGMA foreign_keys=ON")
    r = led.verify_chain()
    grade = "PARTIAL"
    if not r["valid"] and r["broken_at_entry_id"] in (3, 4):
        grade = "PARTIAL" if r["broken_at_entry_id"] == 4 else "PASS"
    if r["valid"]:
        grade = "FAIL"
    verdict(
        "tamper delete",
        grade,
        f"{r} (id 3 gone; successor prev_hash still names 3)",
    )

    # reorder by swapping non-pk payload hashes + hashes of 2 and 4
    led = fresh()
    for i in range(5):
        led.append_evidence("attribution", 10 + i, {"session_id": 1, "i": i})
    conn = _drop_triggers(led)
    rows = list(conn.execute("SELECT * FROM evidence_ledger ORDER BY entry_id").fetchall())
    a, b = rows[1], rows[2]
    # swap every field except entry_id
    fields = [
        "record_type",
        "record_ref_id",
        "record_payload_hash",
        "prev_hash",
        "entry_hash",
        "timestamp",
        "signature",
    ]
    for f in fields:
        conn.execute(
            f"UPDATE evidence_ledger SET {f}=? WHERE entry_id=?",
            (b[f], a["entry_id"]),
        )
        conn.execute(
            f"UPDATE evidence_ledger SET {f}=? WHERE entry_id=?",
            (a[f], b["entry_id"]),
        )
    conn.commit()
    r = led.verify_chain()
    verdict(
        "tamper reorder",
        "PASS" if (not r["valid"] and r["broken_at_entry_id"] in (2, 3)) else "FAIL",
        f"{r}",
    )

    # foreign splice: insert a row with a valid-looking hash but garbage sig
    led = fresh()
    for i in range(3):
        led.append_evidence("attribution", 10 + i, {"session_id": 1, "i": i})
    conn = _drop_triggers(led)
    last = conn.execute(
        "SELECT entry_hash FROM evidence_ledger ORDER BY entry_id DESC LIMIT 1"
    ).fetchone()
    bad_sig = base64.b64encode(b"\x11" * 64).decode("ascii")
    conn.execute(
        """
        INSERT INTO evidence_ledger (
            entry_id, record_type, record_ref_id, record_payload_hash,
            prev_hash, entry_hash, timestamp, signature
        ) VALUES (99, 'repair', 999, ?, ?, ?, ?, ?)
        """,
        ("ee" * 32, last["entry_hash"], "ffff" * 16, "2020-01-01T00:00:00+00:00", bad_sig),
    )
    conn.commit()
    r = led.verify_chain()
    verdict(
        "tamper foreign-splice",
        "PASS" if (not r["valid"] and r["broken_at_entry_id"] == 99) else "FAIL",
        f"{r}",
    )


def probe_payload_mutate(ledger: EvidenceLedger) -> None:
    ledger.append_evidence(
        "repair",
        7,
        {"session_id": 1, "applied": False, "resolved": True, "dry_run": False},
    )
    conn = ledger._connect()
    conn.execute(
        "UPDATE evidence_payloads SET payload_json=? WHERE entry_id=1",
        (json.dumps({"session_id": 1, "applied": True, "resolved": True}),),
    )
    conn.commit()
    r = ledger.verify_chain()
    payload = json.loads(
        conn.execute("SELECT payload_json FROM evidence_payloads WHERE entry_id=1").fetchone()[0]
    )
    grade = "FAIL" if r["valid"] and payload.get("applied") is True else "PASS"
    verdict(
        "payload mutate vs verify_chain",
        grade,
        f"chain valid={r['valid']} after flipping stored payload applied→True "
        f"(payloads table has no append-only trigger)",
    )


def probe_interleaved_export(tmp: Path) -> None:
    led = _ledger(tmp / "interleave")
    led.append_evidence("attribution", 1, {"session_id": 1, "n": 1})
    led.append_evidence("attribution", 2, {"session_id": 2, "n": 2})
    led.append_evidence("repair", 1, {"session_id": 1, "applied": False, "n": 3})
    bundle = led.export_attestation(1)
    path = tmp / "interleave" / "bundle.json"
    path.write_text(json.dumps(bundle), encoding="utf-8")
    result = _run_verify(path)
    keys = sorted(bundle["entries"][0].keys())
    has_applied = any("applied" in e or "repair_disposition" in e for e in bundle["entries"])
    grade = "FAIL" if result.returncode != 0 else "PASS"
    verdict(
        "interleaved session export + offline verify",
        grade,
        f"exit={result.returncode} stdout={result.stdout.strip()!r:.400} "
        f"entry_keys={keys} applied_in_bundle={has_applied}",
    )


def probe_applied_vs_verified(tmp: Path) -> None:
    d = tmp / "applied"
    d.mkdir()
    led = _ledger(d)
    storage = SessionStorage(db_path=d / "sessions.db")
    sid, fid, fact = _seed_compression_session(storage)
    engine = VerifiedRepairEngine(storage=storage, evidence=led)
    result = engine.verify_fix(
        sid,
        fid,
        llm_fn=lambda _m: _fake_response(f"The exact Q3 revenue was {fact}."),
        dry_run=False,
        correct_value=fact,
    )
    bundle = led.export_attestation(sid)
    stored = led._connect().execute(
        "SELECT payload_json, record_type FROM evidence_payloads p "
        "JOIN evidence_ledger e ON e.entry_id=p.entry_id "
        "WHERE e.record_type='repair'"
    ).fetchone()
    payload = json.loads(stored["payload_json"]) if stored else {}
    entry_text = json.dumps(bundle)
    auditor_sees_applied = "applied" in entry_text.lower()
    grade = "FAIL"
    if result.applied is False and result.resolved is True and auditor_sees_applied:
        grade = "PARTIAL"
    if result.applied is False and result.resolved is True and not auditor_sees_applied:
        grade = "FAIL"
    verdict(
        "applied vs verified in exported bundle",
        grade,
        (
            f"Layer3 resolved={result.resolved} applied={result.applied} "
            f"dry_run={result.dry_run}; payload.applied={payload.get('applied')} "
            f"payload.resolved={payload.get('resolved')}; "
            f"bundle mentions applied={auditor_sees_applied}; "
            f"bundle record_types={[e['record_type'] for e in bundle['entries']]}; "
            f"auditor reading only JSON sees record_type=repair with no applied field"
        ),
    )


def probe_unpinned_key(tmp: Path) -> None:
    """Forge a self-consistent bundle with a throwaway keypair."""
    d = tmp / "forge"
    d.mkdir()
    led = _ledger(d)
    led.append_evidence("repair", 1, {"session_id": 9, "applied": True, "resolved": True})
    bundle = led.export_attestation(9)
    path = d / "forged.json"
    path.write_text(json.dumps(bundle), encoding="utf-8")
    result = _run_verify(path)
    # This ledger WAS signed with its own key — that's a 'legit' bundle.
    # Now mint a second keypair and a second ledger claiming applied=True.
    d2 = tmp / "forge2"
    d2.mkdir()
    led2 = _ledger(d2)
    led2.append_evidence(
        "repair", 1, {"session_id": 9, "applied": True, "resolved": True, "lie": True}
    )
    bundle2 = led2.export_attestation(9)
    path2 = d2 / "forged.json"
    path2.write_text(json.dumps(bundle2), encoding="utf-8")
    result2 = _run_verify(path2)
    pinned = "--public-key" in (VERIFY_SCRIPT.read_text(encoding="utf-8"))
    grade = "FAIL" if result2.returncode == 0 and not pinned else "PASS"
    verdict(
        "unpinned embedded public key",
        grade,
        f"attacker-generated keypair bundle exit={result2.returncode} "
        f"(PASS means verifier accepted a foreign issuer). "
        f"verify_attestation.py has --public-key pin: {pinned}. "
        f"honest bundle exit={result.returncode}",
    )


def probe_key_rotation(tmp: Path) -> None:
    d = tmp / "rotate"
    d.mkdir()
    priv1, pub1 = d / "p1.pem", d / "u1.pem"
    priv2, pub2 = d / "p2.pem", d / "u2.pem"
    generate_keypair(priv1, pub1)
    generate_keypair(priv2, pub2)
    led = EvidenceLedger(db_path=d / "e.db", private_key_path=priv1, public_key_path=pub1)
    led.append_evidence("attribution", 1, {"session_id": 1})
    led.close()
    led2 = EvidenceLedger(db_path=d / "e.db", private_key_path=priv2, public_key_path=pub2)
    led2.append_evidence("attribution", 2, {"session_id": 1})
    r = led2.verify_chain()
    grade = "FAIL" if r["valid"] else "PARTIAL"
    # valid=True after rotation means old sigs were checked with the NEW key
    # (should fail) OR keys are stored per-entry (would be PASS if valid with mixed keys)
    if r["valid"]:
        grade = "FAIL"
        detail = "verify_chain=valid after rotation — historic sigs cannot match new public key unless per-entry keys exist"
    else:
        grade = "PARTIAL"
        detail = (
            f"verify_chain broke after rotation ({r}); old entries are no longer "
            "verifiable. No key_id on entries."
        )
    verdict("key rotation", grade, detail)


def probe_concurrent(tmp: Path) -> None:
    d = tmp / "conc"
    d.mkdir()
    # 50 separate EvidenceLedger instances = 50 locks, same DB (the race)
    priv, pub = d / "p.pem", d / "u.pem"
    generate_keypair(priv, pub)
    db = d / "e.db"

    errors = []
    ids = []

    def worker(w: int):
        led = EvidenceLedger(db_path=db, private_key_path=priv, public_key_path=pub)
        try:
            eid = led.append_evidence(
                "attribution",
                w,
                {"session_id": w, "worker": w},
            )
            return eid
        except Exception as exc:
            errors.append(str(exc))
            return None
        finally:
            led.close()

    with ThreadPoolExecutor(max_workers=50) as pool:
        futs = [pool.submit(worker, w) for w in range(50)]
        for fut in as_completed(futs):
            ids.append(fut.result())

    led = EvidenceLedger(db_path=db, private_key_path=priv, public_key_path=pub)
    chain = led.verify_chain()
    n = led._connect().execute("SELECT COUNT(*) FROM evidence_ledger").fetchone()[0]
    ok_ids = [i for i in ids if i is not None]
    grade = "PASS"
    if errors or n != 50 or not chain["valid"] or len(set(ok_ids)) != 50:
        grade = "FAIL"
    verdict(
        "50-worker concurrent append (separate ledger objects)",
        grade,
        f"rows={n} unique_ids={len(set(ok_ids))} errors={len(errors)} "
        f"chain={chain} sample_errors={errors[:2]}",
    )


def probe_silent_gap(ledger: EvidenceLedger) -> None:
    ledger.append_evidence("attribution", 1, {"session_id": 1})
    ledger.append_evidence("attribution", 2, {"session_id": 1})
    # never write entry for event 3
    r = ledger.verify_chain()
    src = Path(ROOT / "src" / "streamctx" / "evidence.py").read_text(encoding="utf-8")
    has_reconcile = "reconcil" in src.lower()
    grade = "WEAK" if r["valid"] and not has_reconcile else "PASS"
    verdict(
        "silent omitted event",
        grade,
        f"never-written entry is undetectable by verify_chain (valid={r['valid']}, "
        f"checked={r['total_checked']}). reconcile helper present={has_reconcile}. "
        "Hash chains prove integrity of what was logged, not completeness of what should have been.",
    )


def probe_kill_intent(tmp: Path) -> None:
    src = Path(ROOT / "src" / "streamctx" / "evidence.py").read_text(encoding="utf-8")
    has_intent = "intent" in src.lower() and "fsync" in src.lower()
    has_immediate = "BEGIN IMMEDIATE" in src or "IMMEDIATE" in src
    verdict(
        "kill-9 / torn write discipline",
        "WEAK" if not has_intent else "PASS",
        f"BEGIN IMMEDIATE={has_immediate}; intent-log/fsync={has_intent}. "
        "Two INSERTs share one COMMIT (atomic pair) but an uncommitted kill "
        "looks like a valid shorter chain.",
    )


def probe_paid_gate() -> None:
    src = Path(ROOT / "src" / "streamctx" / "evidence.py").read_text(encoding="utf-8")
    hits = []
    for needle in ("license_key", "requires_pro", "STREAMCTX_PAID", "if paid"):
        if needle.lower() in src.lower() and needle != "if paid":
            hits.append(needle)
        if needle == "if paid" and "if paid" in src.lower():
            hits.append(needle)
    grade = "FAIL" if hits else "PASS"
    verdict("MIT / no paid gate", grade, f"needles={hits or 'none'}")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="layer4-pre-"))
    print(f"probe tmp={tmp}")
    print("=" * 72)
    probe_signing_coverage(_ledger(tmp / "sign"))
    probe_tamper_cases(tmp / "tamper")
    probe_payload_mutate(_ledger(tmp / "payload"))
    probe_interleaved_export(tmp)
    probe_applied_vs_verified(tmp)
    probe_unpinned_key(tmp)
    probe_key_rotation(tmp)
    probe_concurrent(tmp)
    probe_silent_gap(_ledger(tmp / "gap"))
    probe_kill_intent(tmp)
    probe_paid_gate()
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
