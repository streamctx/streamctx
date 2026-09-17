"""Adversarial senior-bar probes for Layer 3 (VerifiedRepairEngine).

Judges live code, not docs. Same bar as Layers 1-2: PARTIAL is not PASS.
Run from repo root::

    python scripts/layer3_senior_bar_pre.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from streamctx.repair import (  # noqa: E402
    VerifiedRepairEngine,
    classify_failure,
)
from streamctx.shadow import (  # noqa: E402
    maybe_schedule_shadow_repair,
    wait_for_shadow_repair,
)
from streamctx.storage import SessionStorage  # noqa: E402
from streamctx.attribution import is_non_content_failure  # noqa: E402


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


class _Mem:
    def __init__(self, path):
        self._db_path = str(path)
        conn = sqlite3.connect(self._db_path)
        conn.execute(
            """
            CREATE TABLE calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER,
                timestamp TEXT,
                provider TEXT,
                model TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER,
                cost REAL,
                reused_tokens INTEGER,
                waste_category TEXT,
                messages_json TEXT,
                failed INTEGER,
                healed INTEGER,
                error_message TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE checkpoints (
                session_id INTEGER,
                step_number INTEGER,
                messages_json TEXT,
                timestamp TEXT
            )
            """
        )
        conn.commit()
        conn.close()

    def _connect(self):
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def seed_call(self, session_id, messages, *, failed=False, error_message=None,
                  timestamp="2026-09-17T12:00:00"):
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO calls (
                    session_id, timestamp, provider, model,
                    input_tokens, output_tokens, cost,
                    reused_tokens, waste_category, messages_json,
                    failed, healed, error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id, timestamp, "openai", "test",
                    100, 10, 0.0, 0, None, json.dumps(messages),
                    int(failed), 0, error_message,
                ),
            )
            conn.commit()
            return int(cur.lastrowid)

    def seed_checkpoint(self, session_id, step, messages, timestamp="2026-09-17T12:00:00"):
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO checkpoints (session_id, step_number, messages_json, timestamp) "
                "VALUES (?, ?, ?, ?)",
                (session_id, step, json.dumps(messages), timestamp),
            )
            conn.commit()

    def get_calls_for_session(self, session_id):
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, session_id, timestamp, provider, model,
                       input_tokens, output_tokens, cost,
                       reused_tokens, waste_category, messages_json,
                       failed, healed, error_message
                FROM calls WHERE session_id=? ORDER BY id ASC
                """,
                (session_id,),
            ).fetchall()
        return [dict(r) for r in rows]


def case_circular_verification() -> str:
    """Invented correct_value echoed by the LLM — is that treated as verified?"""
    tmp = Path(tempfile.mkdtemp()) / "circ.db"
    store = _Mem(tmp)
    sid = 1
    first = [{"role": "user", "content": "original task: summarize the report"}]
    second = _buried("$12.4 million", "What was Q3 revenue?")
    store.seed_call(sid, first)
    fid = store.seed_call(sid, second, failed=True, error_message=None)
    store.seed_checkpoint(sid, 1, first)
    store.seed_checkpoint(sid, 2, second)
    engine = VerifiedRepairEngine(storage=store)
    invented = "ZEBRA-NOT-IN-SESSION-9917"
    result = engine.verify_fix(
        sid,
        fid,
        llm_fn=lambda _m: _fake_response(f"The code is {invented}."),
        dry_run=False,
        correct_value=invented,
    )
    print(f"  invented value resolved={result.resolved}")
    print(f"  applied={getattr(result, 'applied', '<missing>')}")
    # If resolved, verification accepted a fact that was never in the session.
    if result.resolved:
        return "FAIL"
    return "PASS"


def case_compression_reinjects_dropped_fact() -> str:
    tmp = Path(tempfile.mkdtemp()) / "comp.db"
    store = _Mem(tmp)
    sid = 1
    first = [{"role": "user", "content": "original task: summarize the report"}]
    fact = "$12.4 million"
    second = _buried(fact, "What was the exact Q3 revenue figure?")
    store.seed_call(sid, first)
    fid = store.seed_call(sid, second, failed=True, error_message="context overflow")
    store.seed_checkpoint(sid, 1, first)
    store.seed_checkpoint(sid, 2, second)
    engine = VerifiedRepairEngine(storage=store)
    result = engine.verify_fix(sid, fid, dry_run=True)
    blob = json.dumps(result.fix_candidate)
    print(f"  dominant={result.dominant_signal}")
    print(f"  candidate has 12.4={'12.4' in blob}")
    print(f"  candidate has original-task-only={'original task' in blob.lower()}")
    print(f"  candidate preview={blob[:240]!r}")
    if result.dominant_signal != "compression":
        return "PARTIAL"
    if "12.4" not in blob:
        return "FAIL"
    return "PASS"


def case_stale_earliest_beats_later_fact() -> str:
    """Compression attributed; earliest call has Lyon, later uncompressed has Phoenix.

    Re-injecting earliest would restore the city compression correctly dropped
    as superseded chatter — worse than leaving the compressed window alone.
    """
    tmp = Path(tempfile.mkdtemp()) / "stale.db"
    store = _Mem(tmp)
    sid = 1
    earliest = [
        {"role": "system", "content": "Operating city is Lyon. Report in km."},
        {"role": "user", "content": "Where do we operate?"},
    ]
    later = _buried(
        "Operating city is Phoenix. Report in miles. Q3 revenue was $12.4 million.",
        "What was Q3 revenue and which city?",
    )
    store.seed_call(sid, earliest)
    fid = store.seed_call(sid, later, failed=True, error_message=None)
    store.seed_checkpoint(sid, 1, earliest)
    store.seed_checkpoint(sid, 2, later)
    engine = VerifiedRepairEngine(storage=store)
    result = engine.verify_fix(sid, fid, dry_run=True)
    blob = json.dumps(result.fix_candidate).lower()
    print(f"  dominant={result.dominant_signal}")
    print(f"  injects Lyon={'lyon' in blob}")
    print(f"  injects Phoenix={'phoenix' in blob}")
    print(f"  injects 12.4={'12.4' in blob}")
    if result.dominant_signal != "compression":
        return "PARTIAL"
    if "lyon" in blob and "phoenix" not in blob:
        return "FAIL"
    if "12.4" not in blob:
        return "FAIL"
    return "PASS"


def case_repair_loop_backoff() -> str:
    tmp = Path(tempfile.mkdtemp()) / "loop.db"
    storage = SessionStorage(db_path=tmp)
    sid = storage.start_session()
    os.environ["STREAMCTX_SHADOW_REPAIR"] = "1"
    os.environ["STREAMCTX_SHADOW_REPAIR_SYNC"] = "1"
    first = [{"role": "user", "content": "original task: summarize the report"}]
    storage.persist_step(
        session_id=sid,
        provider="openai",
        model="test",
        input_tokens=40,
        output_tokens=4,
        cost=0.0,
        reused_tokens=0,
        waste_category=None,
        request_messages=first,
        checkpoint_messages=first + [{"role": "assistant", "content": "ok"}],
        step_number=1,
        failed=False,
    )
    for i in range(12):
        storage.record_call(
            session_id=sid,
            provider="openai",
            model="gpt-4",
            input_tokens=20,
            output_tokens=4,
            cost=0.0,
            reused_tokens=0,
            waste_category=None,
            messages=_buried(f"$12.{i} million", f"What was Q3 on turn {i}?"),
            failed=True,
            error_message=None,
        )
    logs = storage.get_shadow_repair_log()
    human = [r for r in logs if r.get("needs_human_review")]
    print(f"  shadow rows for 12 failures: {len(logs)}")
    print(f"  needs_human_review rows: {len(human)}")
    print(f"  columns: {sorted(logs[0].keys()) if logs else []}")
    if len(logs) >= 12:
        return "FAIL"
    if "needs_human_review" not in (logs[0] if logs else {}):
        return "FAIL"
    if not any("cap reached" in str(r.get("attribution_reason") or "") for r in logs):
        return "PARTIAL"
    return "PASS"


def case_partial_repair_resume_intact() -> str:
    tmp = Path(tempfile.mkdtemp()) / "resume.db"
    storage = SessionStorage(db_path=tmp)
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
    failed_msgs = _buried("$12.4 million", "What was Q3 revenue?")
    fid = storage.record_call(
        session_id=sid,
        provider="openai",
        model="test",
        input_tokens=0,
        output_tokens=0,
        cost=0.0,
        reused_tokens=0,
        waste_category=None,
        messages=failed_msgs,
        failed=True,
        error_message="context overflow",
    )
    ckpt_before = storage.get_latest_valid_checkpoint(sid)
    engine = VerifiedRepairEngine(storage=storage)
    result = engine.verify_fix(
        sid,
        fid,
        llm_fn=lambda _m: _fake_response("Q3 revenue was $12.4 million."),
        dry_run=False,
        correct_value="$12.4 million",
    )
    ckpt_after = storage.get_latest_valid_checkpoint(sid)
    calls_n = len(storage.get_calls_for_session(sid))
    print(f"  resolved={result.resolved} applied={getattr(result, 'applied', None)}")
    print(f"  checkpoint unchanged={ckpt_before == ckpt_after}")
    print(f"  calls still 2={calls_n == 2}")
    mutated = ckpt_before != ckpt_after
    if mutated:
        return "FAIL"
    if getattr(result, "applied", None) is True:
        return "FAIL"
    return "PASS"


def case_failed_call_step_mapping() -> str:
    """Layer 1: failures have no checkpoint. Ordinal zip is wrong."""
    tmp = Path(tempfile.mkdtemp()) / "step.db"
    store = _Mem(tmp)
    sid = 1
    a = [{"role": "user", "content": "task A " + ("x" * 80)}]
    b = [{"role": "user", "content": "task B " + ("y" * 80)}]
    fail = _buried("$12.4 million", "What was Q3?")
    store.seed_call(sid, a, timestamp="2026-09-17T12:00:01")
    store.seed_call(sid, b, timestamp="2026-09-17T12:00:02")
    store.seed_call(
        sid, [{"role": "user", "content": "transient fail"}],
        failed=True, error_message="timeout", timestamp="2026-09-17T12:00:03",
    )
    fid = store.seed_call(
        sid, fail, failed=True, error_message=None, timestamp="2026-09-17T12:00:04",
    )
    store.seed_checkpoint(sid, 1, a, timestamp="2026-09-17T12:00:01")
    store.seed_checkpoint(sid, 2, b, timestamp="2026-09-17T12:00:02")
    engine = VerifiedRepairEngine(storage=store)
    result = engine.verify_fix(sid, fid, dry_run=True)
    from_step = result.proof.get("from_step")
    print(f"  from_step={from_step} (expect last success step=2, not a phantom 3/4)")
    print(f"  dominant={result.dominant_signal}")
    if from_step not in (1, 2):
        return "FAIL"
    if from_step != 2:
        return "PARTIAL"
    return "PASS"


def case_timeout_fail_safe() -> str:
    tmp = Path(tempfile.mkdtemp()) / "to.db"
    store = _Mem(tmp)
    sid = 1
    first = [{"role": "user", "content": "original task: summarize"}]
    second = _buried("$12.4 million", "What was Q3?")
    store.seed_call(sid, first)
    fid = store.seed_call(sid, second, failed=True, error_message=None)
    store.seed_checkpoint(sid, 1, first)
    store.seed_checkpoint(sid, 2, second)
    engine = VerifiedRepairEngine(storage=store)
    engine.llm_timeout_s = 1.0

    def hang(_m):
        import time
        time.sleep(6)
        return _fake_response("should not get here")

    import time as _t
    t0 = _t.time()
    try:
        result = engine.verify_fix(
            sid, fid, llm_fn=hang, dry_run=False, correct_value="$12.4 million",
        )
    except Exception as exc:
        print(f"  raised {type(exc).__name__}: {exc}")
        elapsed = _t.time() - t0
        print(f"  elapsed={elapsed:.2f}s")
        return "FAIL" if elapsed > 3 else "PARTIAL"
    elapsed = _t.time() - t0
    print(f"  elapsed={elapsed:.2f}s resolved={result.resolved} "
          f"needs_human={getattr(result, 'needs_human_review', None)}")
    if elapsed > 3:
        return "FAIL"
    if result.resolved:
        return "FAIL"
    return "PASS"


def case_classify_failure_contract() -> str:
    samples = {
        None: "content_error",
        "": "content_error",
        "simulated failure": "content_error",
        "Error code: 401 - Unauthorized": "infra_error",
        "Connection timed out": "infra_error",
    }
    bad = []
    for raw, expected in samples.items():
        got = classify_failure(raw)
        if got != expected:
            bad.append((raw, expected, got))
        extra = is_non_content_failure(raw)
        print(f"  classify({raw!r})={got}  layer2_non_content={extra}")
    # No abstention token exists.
    third = classify_failure("prompt injection from user")
    print(f"  classify(prompt injection)={third} (no abstain token)")
    if bad:
        return "FAIL"
    if third != "content_error":
        return "PARTIAL"
    return "PASS"


def case_never_auto_applied() -> str:
    tmp = Path(tempfile.mkdtemp()) / "apply.db"
    storage = SessionStorage(db_path=tmp)
    os.environ["STREAMCTX_SHADOW_REPAIR"] = "1"
    os.environ["STREAMCTX_SHADOW_REPAIR_SYNC"] = "1"
    sid = storage.start_session()
    storage.record_call(
        session_id=sid, provider="openai", model="x",
        input_tokens=10, output_tokens=2, cost=0.0, reused_tokens=0,
        waste_category=None,
        messages=[{"role": "user", "content": "hi"}],
        failed=False,
    )
    storage.record_call(
        session_id=sid, provider="openai", model="x",
        input_tokens=10, output_tokens=2, cost=0.0, reused_tokens=0,
        waste_category=None,
        messages=[
            {"role": "user", "content": "What is Q3?"},
            {"role": "assistant", "content": "$47.3 million"},
        ],
        failed=True,
        error_message=None,
    )
    logs = storage.get_shadow_repair_log()
    ckpts = storage.get_latest_valid_checkpoint(sid)
    print(f"  shadow rows={len(logs)} latest_checkpoint={ckpts}")
    applied_col = logs[0].get("applied") if logs else None
    print(f"  shadow applied column={applied_col}")
    if ckpts is not None:
        return "FAIL"
    return "PASS"


def case_shadow_concurrency() -> str:
    tmp = Path(tempfile.mkdtemp()) / "conc.db"
    storage = SessionStorage(db_path=tmp)
    os.environ["STREAMCTX_SHADOW_REPAIR"] = "1"
    os.environ["STREAMCTX_SHADOW_REPAIR_SYNC"] = "1"
    pairs = []
    for w in range(50):
        sid = storage.start_session()
        storage.record_call(
            session_id=sid, provider="openai", model="x",
            input_tokens=10, output_tokens=2, cost=0.0, reused_tokens=0,
            waste_category=None,
            messages=[{"role": "user", "content": f"worker-{w}-base"}],
            failed=False,
        )
        fid = storage.record_call(
            session_id=sid, provider="openai", model="x",
            input_tokens=10, output_tokens=2, cost=0.0, reused_tokens=0,
            waste_category=None,
            messages=[
                {"role": "user", "content": f"worker-{w}-fail Q3?"},
                {"role": "assistant", "content": f"worker-{w}-hallucination"},
            ],
            failed=True,
            error_message=None,
        )
        pairs.append((sid, fid, w))
    logs = storage.get_shadow_repair_log()
    by_session = {int(r["session_id"]): r for r in logs}
    mismatches = 0
    for sid, fid, w in pairs:
        row = by_session.get(sid)
        if row is None or int(row["failed_call_id"]) != int(fid):
            mismatches += 1
            continue
        blob = str(row.get("fix_candidate") or "") + str(row.get("attribution_reason") or "")
        # Contamination: another worker's id in this row's candidate without this worker.
        others = [f"worker-{j}-" for j in range(50) if j != w]
        if any(tag in blob for tag in others) and f"worker-{w}-" not in blob:
            mismatches += 1
    print(f"  logs={len(logs)} expected=50 mismatches={mismatches}")
    if len(logs) != 50:
        return "FAIL"
    if mismatches:
        return "FAIL"
    return "PASS"


def main() -> int:
    cases = [
        ("Circular verification (invented correct_value)", case_circular_verification),
        ("Compression re-injects dropped fact (not earliest framing)", case_compression_reinjects_dropped_fact),
        ("Stale earliest fact vs later Phoenix/$12.4", case_stale_earliest_beats_later_fact),
        ("Repair-loop backoff / give-up", case_repair_loop_backoff),
        ("Partial repair leaves Layer 1 resume intact", case_partial_repair_resume_intact),
        ("Failure-without-checkpoint step mapping", case_failed_call_step_mapping),
        ("LLM timeout fail-safe", case_timeout_fail_safe),
        ("classify_failure contract vs Layer 2", case_classify_failure_contract),
        ("Repairs never auto-applied to live session", case_never_auto_applied),
        ("50-worker shadow_repair_log fidelity", case_shadow_concurrency),
    ]
    print("Layer 3 senior-bar (live code)\n")
    results = []
    for name, fn in cases:
        print(f"== {name}")
        try:
            verdict = fn()
        except Exception as exc:
            verdict = "FAIL"
            print(f"  CRASH {type(exc).__name__}: {exc}")
        print(f"  => {verdict}\n")
        results.append((name, verdict))
    print("-" * 60)
    for name, verdict in results:
        print(f"{verdict:<8} {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
