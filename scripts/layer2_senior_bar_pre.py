"""Layer 2 senior-engineer-bar cases against CURRENT attribution code (pre-fix).

Run: python scripts/layer2_senior_bar_pre.py
Prints honest PASS / WEAK / PARTIAL / FAIL per case. Does not patch anything.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from streamctx.attribution import (  # noqa: E402
    INFRA_NON_CONTENT_REASON,
    UNATTRIBUTABLE_REASON,
    AttributionEngine,
    is_non_content_failure,
)
from streamctx.compressor import compress_messages  # noqa: E402
from streamctx.repair import classify_failure  # noqa: E402
from streamctx.storage import SessionStorage  # noqa: E402
from streamctx.tracker import LLMTracker  # noqa: E402


def _header(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def _dominant(result) -> str | None:
    breakdown = result.signal_breakdown or {}
    signals = {
        k: float(breakdown[k])
        for k in ("drift", "compression", "recency")
        if k in breakdown
    }
    if not signals:
        return None
    return max(signals, key=signals.get)


class _MemStore:
    def __init__(self, db_path: Path):
        self._db_path = str(db_path)
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
        conn.commit()
        conn.close()

    def seed(
        self,
        session_id,
        messages,
        *,
        input_tokens=100,
        reused_tokens=0,
        waste_category=None,
        failed=False,
        error_message=None,
    ):
        conn = sqlite3.connect(self._db_path)
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
                session_id,
                "2026-09-17T12:00:00",
                "openai",
                "test",
                input_tokens,
                10,
                0.0,
                reused_tokens,
                waste_category,
                json.dumps(messages),
                int(failed),
                0,
                error_message,
            ),
        )
        conn.commit()
        rowid = int(cur.lastrowid)
        conn.close()
        return rowid

    def get_calls_for_session(self, session_id):
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
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
        conn.close()
        return [dict(r) for r in rows]


def _engine(tmp: Path) -> tuple[AttributionEngine, _MemStore]:
    store = _MemStore(tmp / "attr.db")
    return AttributionEngine(storage=store), store


def case_determinism() -> str:
    tmp = Path(tempfile.mkdtemp(prefix="l2-det-"))
    engine, store = _engine(tmp)
    sid = 1
    store.seed(sid, [{"role": "user", "content": "a" * 200}], input_tokens=50, waste_category="ok")
    failed = store.seed(
        sid,
        [{"role": "user", "content": "b" * 2000}],
        input_tokens=500,
        waste_category="drift",
        failed=True,
        error_message="context overflow",
    )
    r1 = engine.attribute_failure(sid, failed)
    r2 = engine.attribute_failure(sid, failed)
    same = (
        r1.reason == r2.reason
        and r1.confidence == r2.confidence
        and r1.root_cause_call_id == r2.root_cause_call_id
        and r1.signal_breakdown == r2.signal_breakdown
    )
    print(f"  run1 conf={r1.confidence} breakdown={r1.signal_breakdown} reason={r1.reason[:80]}")
    print(f"  run2 conf={r2.confidence} identical={same}")
    return "PASS" if same else "FAIL"


def case_infra_after_drift() -> str:
    """Timeout after a genuinely drifted context was sent.

    The *error* is infra. Token shape drift is coincidental. Correct:
    abstain as infra/non-content, do not emit DRIFT for Layer 3 to repair.
    """
    tmp = Path(tempfile.mkdtemp(prefix="l2-infra-"))
    engine, store = _engine(tmp)
    sid = 2
    store.seed(
        sid,
        [{"role": "user", "content": "city is Lyon. metric units only."}],
        input_tokens=50,
        waste_category="ok",
    )
    failed = store.seed(
        sid,
        [{"role": "user", "content": "STANDARD TERMS " * 40 + "city is Phoenix. miles."}],
        input_tokens=500,
        waste_category="drift",
        failed=True,
        error_message="Connection timed out talking to OpenRouter",
    )
    result = engine.attribute_failure(sid, failed)
    print(
        f"  reason={result.reason!r} conf={result.confidence} "
        f"root={result.root_cause_call_id} dominant={_dominant(result)} "
        f"breakdown={result.signal_breakdown}"
    )
    if result.reason == INFRA_NON_CONTENT_REASON and result.root_cause_call_id is None:
        return "PASS"
    if _dominant(result) == "drift":
        return "FAIL"
    return "PARTIAL"


def case_simultaneous_causes() -> str:
    """Over-budget reuse AND old context. Must be deterministic, not flip on re-run."""
    tmp = Path(tempfile.mkdtemp(prefix="l2-both-"))
    engine, store = _engine(tmp)
    sid = 3
    store.seed(sid, [{"role": "user", "content": "baseline"}], input_tokens=800, reused_tokens=0)
    failed = store.seed(
        sid,
        [{"role": "user", "content": "same shape, fully reused, session is old"}],
        input_tokens=800,
        reused_tokens=800,
        failed=True,
        error_message="incomplete/hallucinated response after compression",
    )
    results = [engine.attribute_failure(sid, failed) for _ in range(20)]
    keys = {(r.reason, r.confidence, _dominant(r), r.root_cause_call_id) for r in results}
    r = results[0]
    print(
        f"  unique_outcomes={len(keys)} conf={r.confidence} dominant={_dominant(r)} "
        f"breakdown={r.signal_breakdown}"
    )
    print(f"  reason={r.reason}")
    if len(keys) != 1:
        return "FAIL"
    # Recency at offset 0 is always 1.0; compression also 1.0. Raw-max insertion
    # order awards compression if tied at 1.0? dict insertion: drift, compression, recency
    # max() on equal values returns the first max in iteration order... actually
    # max(dict, key=dict.get) is first key with the max value among equals.
    return "PASS"


def case_zero_signal() -> str:
    tmp = Path(tempfile.mkdtemp(prefix="l2-zero-"))
    engine, store = _engine(tmp)
    sid = 4
    msgs = [{"role": "user", "content": "continue the same task"}]
    for _ in range(3):
        store.seed(sid, msgs, input_tokens=120, reused_tokens=0, waste_category=None)
    failed = store.seed(
        sid, msgs, input_tokens=120, reused_tokens=0, waste_category=None,
        failed=True, error_message="context overflow",
    )
    result = engine.attribute_failure(sid, failed)
    print(
        f"  reason={result.reason!r} conf={result.confidence} "
        f"root={result.root_cause_call_id} breakdown={result.signal_breakdown}"
    )
    if (
        result.reason == UNATTRIBUTABLE_REASON
        and result.confidence == 0.0
        and result.root_cause_call_id is None
    ):
        return "PASS"
    if result.root_cause_call_id is not None:
        return "FAIL"
    return "PARTIAL"


def case_confidence_gaming() -> str:
    """Inflate reused_tokens on an under-budget prompt. Compression never fired."""
    tmp = Path(tempfile.mkdtemp(prefix="l2-game-"))
    engine, store = _engine(tmp)
    sid = 5
    short = [{"role": "user", "content": "tiny prompt"}]
    store.seed(sid, short, input_tokens=200, reused_tokens=0)
    failed = store.seed(
        sid,
        short,
        input_tokens=200,
        reused_tokens=180,
        failed=True,
        error_message="context overflow",
    )
    result = engine.attribute_failure(sid, failed)
    compressed, orig, comp = compress_messages(short)
    print(
        f"  Layer1 compress: orig={orig} after={comp} fired={orig > 2000} "
        f"attr dominant={_dominant(result)} conf={result.confidence} "
        f"breakdown={result.signal_breakdown} reason={result.reason!r}"
    )
    if orig <= 2000 and _dominant(result) == "compression" and result.confidence >= 0.4:
        return "FAIL"
    if orig <= 2000 and _dominant(result) == "recency":
        # recency floor stole the label from a fake compression signal
        return "FAIL"
    if orig <= 2000 and result.root_cause_call_id is None:
        return "PASS"
    return "PARTIAL"


def case_tracker_zero_tokens_looks_like_drift() -> str:
    """Real tracker persist_failure zeros tokens. Same-size content error
    must not become DRIFT=1.0 just because input_tokens went  N -> 0.
    """

    def _fake_response(text: str = "ok") -> SimpleNamespace:
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
            usage=SimpleNamespace(prompt_tokens=40, completion_tokens=8),
        )

    class _FakeCompletions:
        def __init__(self, on_create):
            self._on_create = on_create

        def create(self, **kwargs):
            return self._on_create(**kwargs)

    class _FakeClient:
        def __init__(self, on_create):
            self.chat = SimpleNamespace(completions=_FakeCompletions(on_create))

    tmp = Path(tempfile.mkdtemp(prefix="l2-tracker-"))
    tracker = LLMTracker(agent_id="l2-zero")
    tracker.state.storage = SessionStorage(db_path=tmp / "sessions.db")

    def on_create(**kwargs):
        last = kwargs["messages"][-1]["content"]
        if last == "same-size followup":
            raise RuntimeError("context overflow")
        return _fake_response("ok")

    tracker.start()
    client = tracker.wrap(_FakeClient(on_create))
    first = [{"role": "user", "content": "same-size original task about Lyon metrics"}]
    second = [{"role": "user", "content": "same-size followup"}]
    client.chat.completions.create(model="x", messages=first)
    try:
        client.chat.completions.create(model="x", messages=second)
    except RuntimeError:
        pass
    sid = tracker.get_session_id()
    tracker.stop()

    rows = tracker.state.storage.get_calls_for_session(sid)
    failed_rows = [r for r in rows if r["failed"]]
    print("  persisted rows:")
    for r in rows:
        print(
            f"    id={r['id']} failed={r['failed']} tokens={r['input_tokens']} "
            f"reused={r['reused_tokens']} waste={r['waste_category']!r} "
            f"err={r.get('error_message')!r}"
        )
    if not failed_rows:
        print("  no failed row persisted")
        return "FAIL"

    engine = AttributionEngine(storage=tracker.state.storage)
    result = engine.attribute_failure(sid, failed_rows[-1]["id"])
    print(
        f"  attr reason={result.reason!r} conf={result.confidence} "
        f"dominant={_dominant(result)} breakdown={result.signal_breakdown}"
    )
    failed = failed_rows[-1]
    if failed["input_tokens"] == 0 and failed["reused_tokens"] == 0:
        if _dominant(result) == "drift" and result.signal_breakdown.get("drift", 0) >= 0.99:
            return "FAIL"
        if result.reason == UNATTRIBUTABLE_REASON:
            return "PASS"
        return "PARTIAL"
    return "WEAK"


def case_compression_did_not_fire() -> str:
    """Genuine under-budget conversation. reused_tokens from prefix overlap
    must not be blamed as COMPRESSION.
    """
    tmp = Path(tempfile.mkdtemp(prefix="l2-nofire-"))
    engine, store = _engine(tmp)
    sid = 6
    msgs = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "What is 2+2?"},
    ]
    orig = compress_messages(msgs)[1]
    store.seed(sid, msgs, input_tokens=max(orig, 1), reused_tokens=0)
    failed = store.seed(
        sid,
        msgs,
        input_tokens=max(orig, 1),
        reused_tokens=max(orig, 1),
        failed=True,
        error_message="incomplete/hallucinated response after compression",
    )
    result = engine.attribute_failure(sid, failed)
    print(
        f"  orig_tokens={orig} fired={orig > 2000} dominant={_dominant(result)} "
        f"conf={result.confidence} breakdown={result.signal_breakdown} "
        f"reason={result.reason!r}"
    )
    if orig <= 2000 and _dominant(result) == "compression":
        return "FAIL"
    if orig <= 2000 and result.root_cause_call_id is None:
        return "PASS"
    return "PARTIAL"


def case_classify_failure_location() -> str:
    import streamctx.attribution as amod
    import streamctx.repair as rmod

    in_attr = hasattr(amod, "classify_failure")
    in_repair = hasattr(rmod, "classify_failure")
    print(f"  attribution.classify_failure exists={in_attr}")
    print(f"  repair.classify_failure exists={in_repair}")
    print(f"  attribution.is_non_content_failure exists={hasattr(amod, 'is_non_content_failure')}")
    print(f"  UNATTRIBUTABLE={getattr(amod, 'UNATTRIBUTABLE_REASON', None)!r}")
    print(f"  INFRA={getattr(amod, 'INFRA_NON_CONTENT_REASON', None)!r}")
    # settle the conflicting prior audits
    if in_repair and not in_attr and hasattr(amod, "is_non_content_failure"):
        return "PASS"
    return "WEAK"


def case_concurrency() -> str:
    tmp = Path(tempfile.mkdtemp(prefix="l2-conc-"))
    storage = SessionStorage(db_path=tmp / "conc.db")
    engine = AttributionEngine(storage=storage)

    # 50 sessions, each with a distinctive token profile so contamination
    # would change the winner.
    session_failed: list[tuple[int, int, str]] = []
    for w in range(50):
        sid = storage.start_session()
        storage.record_call(
            session_id=sid,
            provider="openai",
            model="gpt-4",
            input_tokens=50 + w,
            output_tokens=5,
            cost=0.0,
            reused_tokens=0,
            waste_category="ok",
            messages=[{"role": "user", "content": f"worker-{w}-base {'x' * (20 + w)}"}],
            failed=False,
        )
        err = "context overflow" if w % 3 else "Connection timed out"
        fid = storage.record_call(
            session_id=sid,
            provider="openai",
            model="gpt-4",
            input_tokens=500 + w * 3,
            output_tokens=5,
            cost=0.0,
            reused_tokens=0 if w % 2 else 400,
            waste_category="drift",
            messages=[{"role": "user", "content": f"worker-{w}-fail {'y' * (200 + w)}"}],
            failed=True,
            error_message=err,
        )
        session_failed.append((sid, fid, err))
        storage.end_session(sid)

    serial = {
        sid: engine.attribute_failure(sid, fid)
        for sid, fid, _err in session_failed
    }

    errors: list[str] = []
    results: dict[int, object] = {}

    def worker(item):
        sid, fid, _err = item
        return sid, engine.attribute_failure(sid, fid)

    with ThreadPoolExecutor(max_workers=50) as pool:
        futs = [pool.submit(worker, item) for item in session_failed]
        for fut in as_completed(futs):
            try:
                sid, result = fut.result()
                results[sid] = result
            except Exception as exc:
                errors.append(str(exc))

    mismatches = 0
    for sid, fid, err in session_failed:
        a = serial[sid]
        b = results[sid]
        if (
            a.reason != b.reason
            or a.confidence != b.confidence
            or a.root_cause_call_id != b.root_cause_call_id
            or a.signal_breakdown != b.signal_breakdown
        ):
            mismatches += 1
            if mismatches <= 3:
                print(f"  mismatch sid={sid}: serial={a} concurrent={b}")

    print(f"  errors={len(errors)} mismatches={mismatches} n={len(session_failed)}")
    if errors:
        print(f"  first error: {errors[0]}")
        return "FAIL"
    if mismatches:
        return "FAIL"
    return "PASS"


def main() -> None:
    cases = [
        ("determinism (same input twice)", case_determinism),
        ("infra timeout after real drift", case_infra_after_drift),
        ("simultaneous compression+recency", case_simultaneous_causes),
        ("zero signal abstention", case_zero_signal),
        ("adversarial confidence gaming", case_confidence_gaming),
        ("tracker persist_failure zero tokens", case_tracker_zero_tokens_looks_like_drift),
        ("compression did not fire (under budget)", case_compression_did_not_fire),
        ("classify_failure location", case_classify_failure_location),
        ("50-worker concurrent attribution", case_concurrency),
    ]
    verdicts = {}
    for title, fn in cases:
        _header(title)
        try:
            verdicts[title] = fn()
        except Exception as exc:
            print(f"  exception: {type(exc).__name__}: {exc}")
            verdicts[title] = "FAIL"
        print(f"  VERDICT: {verdicts[title]}")

    _header("SUMMARY")
    for title, v in verdicts.items():
        print(f"  {v:<8} {title}")


if __name__ == "__main__":
    main()
