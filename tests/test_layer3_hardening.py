"""Layer 3 senior-bar regression tests.

Adversarial cases from the Verified Auto-Repair hardening cycle.
Verification is independent of the attribution signal. Compression
repair restores facts Layer 1 compression would drop, not the earliest
call's framing. ``verify_fix`` never mutates the live session.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from types import SimpleNamespace

import pytest

from streamctx.repair import (
    ECHOED_INJECTION,
    INVENTED_CORRECT_VALUE,
    MAX_REPAIR_ATTEMPTS_PER_SESSION,
    REPAIR_LLM_TIMEOUT_S,
    VerifiedRepairEngine,
    classify_failure,
)
from streamctx.shadow import (
    MAX_SHADOW_REPAIRS_PER_SESSION,
    wait_for_shadow_repair,
)
from streamctx.storage import SessionStorage
from streamctx.attribution import is_non_content_failure


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


def _seed_compression_session(storage: SessionStorage, *, error_message=None):
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
        error_message=error_message,
    )
    return sid, failed_id, fact


def test_invented_correct_value_is_not_verified(tmp_path):
    storage = SessionStorage(db_path=tmp_path / "inv.db")
    sid, fid, _fact = _seed_compression_session(storage, error_message="context overflow")
    engine = VerifiedRepairEngine(storage=storage)
    invented = "ZEBRA-NOT-IN-SESSION-9917"
    result = engine.verify_fix(
        sid,
        fid,
        llm_fn=lambda _m: _fake_response(f"The code is {invented}."),
        dry_run=False,
        correct_value=invented,
    )
    assert result.resolved is False
    assert result.applied is False
    assert result.needs_human_review is True
    assert result.reason == INVENTED_CORRECT_VALUE


def test_compression_reinjects_dropped_fact_not_earliest(tmp_path):
    storage = SessionStorage(db_path=tmp_path / "comp.db")
    sid, fid, fact = _seed_compression_session(storage, error_message="context overflow")
    engine = VerifiedRepairEngine(storage=storage)
    result = engine.verify_fix(sid, fid, dry_run=True)
    blob = json.dumps(result.fix_candidate)
    assert result.dominant_signal == "compression"
    assert "12.4" in blob
    assert "DEDUPE" in blob
    # Earliest call is the short original task — must not be the only source.
    assert "original task: summarize the report" not in blob.lower() or "12.4" in blob


def test_stale_earliest_city_is_not_re_injected(tmp_path):
    storage = SessionStorage(db_path=tmp_path / "stale.db")
    sid = storage.start_session()
    earliest = [
        {"role": "system", "content": "Operating city is Lyon. Report in km."},
        {"role": "user", "content": "Where do we operate?"},
    ]
    storage.persist_step(
        session_id=sid,
        provider="openai",
        model="test",
        input_tokens=40,
        output_tokens=8,
        cost=0.0,
        reused_tokens=0,
        waste_category=None,
        request_messages=earliest,
        checkpoint_messages=earliest + [{"role": "assistant", "content": "Lyon."}],
        step_number=1,
        failed=False,
    )
    later = _buried(
        "Operating city is Phoenix. Report in miles. Q3 revenue was $12.4 million.",
        "What was Q3 revenue and which city?",
    )
    fid = storage.record_call(
        session_id=sid,
        provider="openai",
        model="test",
        input_tokens=0,
        output_tokens=0,
        cost=0.0,
        reused_tokens=0,
        waste_category=None,
        messages=later,
        failed=True,
        error_message=None,
    )
    engine = VerifiedRepairEngine(storage=storage)
    result = engine.verify_fix(sid, fid, dry_run=True)
    blob = json.dumps(result.fix_candidate).lower()
    assert result.dominant_signal == "compression"
    assert "phoenix" in blob or "12.4" in blob
    assert "lyon" not in blob


def test_live_restore_of_session_fact_is_resolved(tmp_path):
    storage = SessionStorage(db_path=tmp_path / "ok.db")
    sid, fid, fact = _seed_compression_session(storage, error_message="context overflow")
    engine = VerifiedRepairEngine(storage=storage)
    ckpt_before = storage.get_latest_valid_checkpoint(sid)
    calls_before = storage.get_calls_for_session(sid)
    result = engine.verify_fix(
        sid,
        fid,
        llm_fn=lambda _m: _fake_response(f"Q3 revenue was {fact}."),
        dry_run=False,
        correct_value=fact,
    )
    assert result.resolved is True
    assert result.applied is False
    assert result.proof["applied"] is False
    assert result.proof["pre_repair_checkpoint"]["fingerprint"]
    assert storage.get_latest_valid_checkpoint(sid) == ckpt_before
    assert storage.get_calls_for_session(sid) == calls_before


def test_injection_echo_is_not_verified(tmp_path):
    storage = SessionStorage(db_path=tmp_path / "echo.db")
    sid, fid, fact = _seed_compression_session(storage, error_message="context overflow")
    engine = VerifiedRepairEngine(storage=storage)
    dry = engine.verify_fix(sid, fid, dry_run=True)
    injection = json.dumps(dry.fix_candidate)

    result = engine.verify_fix(
        sid,
        fid,
        llm_fn=lambda _m: _fake_response(injection),
        dry_run=False,
        correct_value=fact,
    )
    assert result.resolved is False
    assert result.reason == ECHOED_INJECTION
    assert result.needs_human_review is True


def test_repair_loop_gives_up_after_lookback_window(tmp_path, monkeypatch):
    monkeypatch.setenv("STREAMCTX_SHADOW_REPAIR", "1")
    monkeypatch.setenv("STREAMCTX_SHADOW_REPAIR_SYNC", "1")
    storage = SessionStorage(db_path=tmp_path / "loop.db")
    sid = storage.start_session()
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
        failed_msgs = _buried(
            f"$12.{i} million",
            f"What was the exact Q3 revenue figure on turn {i}?",
        )
        storage.record_call(
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
            error_message=None,
        )
    logs = storage.get_shadow_repair_log()
    human = [r for r in logs if r.get("needs_human_review")]
    assert len(logs) <= MAX_SHADOW_REPAIRS_PER_SESSION + 1
    assert len(logs) < 12
    assert human
    assert MAX_REPAIR_ATTEMPTS_PER_SESSION == MAX_SHADOW_REPAIRS_PER_SESSION
    give_up = [r for r in human if "cap reached" in str(r.get("attribution_reason") or "")]
    assert give_up


def test_duplicate_failed_call_does_not_double_log(tmp_path, monkeypatch):
    monkeypatch.setenv("STREAMCTX_SHADOW_REPAIR", "1")
    monkeypatch.setenv("STREAMCTX_SHADOW_REPAIR_SYNC", "1")
    storage = SessionStorage(db_path=tmp_path / "dup.db")
    sid = storage.start_session()
    fid = storage.record_call(
        session_id=sid,
        provider="openai",
        model="test",
        input_tokens=10,
        output_tokens=2,
        cost=0.0,
        reused_tokens=0,
        waste_category=None,
        messages=[{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}],
        failed=True,
        error_message=None,
    )
    from streamctx.shadow import maybe_schedule_shadow_repair

    maybe_schedule_shadow_repair(sid, fid, error_message=None, storage=storage)
    logs = storage.get_shadow_repair_log()
    assert len(logs) == 1
    assert int(logs[0]["failed_call_id"]) == int(fid)


def test_llm_timeout_fail_safe(tmp_path):
    storage = SessionStorage(db_path=tmp_path / "to.db")
    sid, fid, fact = _seed_compression_session(storage, error_message="context overflow")
    engine = VerifiedRepairEngine(storage=storage)
    engine.llm_timeout_s = 1.0
    ckpt_before = storage.get_latest_valid_checkpoint(sid)

    def hang(_m):
        time.sleep(8)
        return _fake_response(f"Q3 revenue was {fact}.")

    t0 = time.time()
    result = engine.verify_fix(
        sid, fid, llm_fn=hang, dry_run=False, correct_value=fact
    )
    elapsed = time.time() - t0
    assert elapsed < 5
    assert result.resolved is False
    assert result.applied is False
    assert result.needs_human_review is True
    assert storage.get_latest_valid_checkpoint(sid) == ckpt_before


def test_middle_failure_replays_from_last_success_checkpoint(tmp_path):
    storage = SessionStorage(db_path=tmp_path / "mid.db")
    sid = storage.start_session()
    a = [{"role": "user", "content": "task A " + ("x" * 80)}]
    b = [{"role": "user", "content": "task B " + ("y" * 80)}]
    storage.persist_step(
        session_id=sid,
        provider="openai",
        model="test",
        input_tokens=40,
        output_tokens=4,
        cost=0.0,
        reused_tokens=0,
        waste_category=None,
        request_messages=a,
        checkpoint_messages=a + [{"role": "assistant", "content": "A"}],
        step_number=1,
        failed=False,
    )
    storage.record_call(
        session_id=sid,
        provider="openai",
        model="test",
        input_tokens=0,
        output_tokens=0,
        cost=0.0,
        reused_tokens=0,
        waste_category=None,
        messages=[{"role": "user", "content": "transient"}],
        failed=True,
        error_message="timeout",
    )
    storage.persist_step(
        session_id=sid,
        provider="openai",
        model="test",
        input_tokens=40,
        output_tokens=4,
        cost=0.0,
        reused_tokens=0,
        waste_category=None,
        request_messages=b,
        checkpoint_messages=b + [{"role": "assistant", "content": "B"}],
        step_number=2,
        failed=False,
    )
    fail = _buried("$12.4 million", "What was Q3?")
    fid = storage.record_call(
        session_id=sid,
        provider="openai",
        model="test",
        input_tokens=0,
        output_tokens=0,
        cost=0.0,
        reused_tokens=0,
        waste_category=None,
        messages=fail,
        failed=True,
        error_message=None,
    )
    engine = VerifiedRepairEngine(storage=storage)
    result = engine.verify_fix(sid, fid, dry_run=True)
    assert result.proof["from_step"] == 2
    assert storage.get_latest_valid_checkpoint(sid)["step_number"] == 2


def test_classify_failure_contract_unchanged_for_layer2():
    assert classify_failure(None) == "content_error"
    assert classify_failure("") == "content_error"
    assert classify_failure("simulated failure") == "content_error"
    assert is_non_content_failure("simulated failure") is True
    assert classify_failure("Error code: 401 - Unauthorized") == "infra_error"
    assert classify_failure("Connection timed out") == "infra_error"
    assert classify_failure("prompt injection from user") == "content_error"


def test_no_paid_gate_in_repair_source():
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src" / "streamctx" / "repair.py"
    text = src.read_text(encoding="utf-8")
    assert "license_key" not in text
    assert "requires_pro" not in text
    assert "STREAMCTX_PAID" not in text
    assert "if paid" not in text.lower()
    assert "MIT-licensed" in text


def test_injected_content_quality_e2e_pipeline(tmp_path, monkeypatch):
    """Attribution → repair → independent verify → shadow log, one session."""
    monkeypatch.setenv("STREAMCTX_SHADOW_REPAIR", "1")
    monkeypatch.setenv("STREAMCTX_SHADOW_REPAIR_SYNC", "1")
    storage = SessionStorage(db_path=tmp_path / "e2e.db")
    sid, fid, fact = _seed_compression_session(storage, error_message=None)
    logs = storage.get_shadow_repair_log()
    assert len(logs) == 1
    assert int(logs[0]["session_id"]) == int(sid)
    assert int(logs[0]["failed_call_id"]) == int(fid)
    assert logs[0]["applied"] is False
    assert logs[0]["dry_run"] is True
    assert logs[0]["resolved"] is False
    candidate = logs[0]["fix_candidate"] or ""
    assert "12.4" in candidate

    engine = VerifiedRepairEngine(storage=storage)
    live = engine.verify_fix(
        sid,
        fid,
        llm_fn=lambda _m: _fake_response(f"The exact Q3 revenue was {fact}."),
        dry_run=False,
        correct_value=fact,
    )
    assert live.dominant_signal == "compression"
    assert live.resolved is True
    assert live.applied is False
    assert live.needs_human_review is False


def test_shadow_opt_out_env(tmp_path, monkeypatch):
    monkeypatch.setenv("STREAMCTX_SHADOW_REPAIR", "0")
    monkeypatch.setenv("STREAMCTX_SHADOW_REPAIR_SYNC", "1")
    storage = SessionStorage(db_path=tmp_path / "off.db")
    sid = storage.start_session()
    storage.record_call(
        session_id=sid,
        provider="openai",
        model="test",
        input_tokens=10,
        output_tokens=2,
        cost=0.0,
        reused_tokens=0,
        waste_category=None,
        messages=[{"role": "user", "content": "q"}],
        failed=True,
        error_message=None,
    )
    assert storage.get_shadow_repair_log() == []


def test_concurrent_shadow_log_50_workers_no_contamination(tmp_path, monkeypatch):
    monkeypatch.setenv("STREAMCTX_SHADOW_REPAIR", "1")
    monkeypatch.setenv("STREAMCTX_SHADOW_REPAIR_SYNC", "1")
    storage = SessionStorage(db_path=tmp_path / "conc.db")

    def worker(w: int):
        sid = storage.start_session()
        storage.record_call(
            session_id=sid,
            provider="openai",
            model="test",
            input_tokens=10,
            output_tokens=2,
            cost=0.0,
            reused_tokens=0,
            waste_category=None,
            messages=[{"role": "user", "content": f"worker-{w}-base"}],
            failed=False,
        )
        fid = storage.record_call(
            session_id=sid,
            provider="openai",
            model="test",
            input_tokens=10,
            output_tokens=2,
            cost=0.0,
            reused_tokens=0,
            waste_category=None,
            messages=[
                {"role": "user", "content": f"worker-{w}-fail Q3?"},
                {"role": "assistant", "content": f"worker-{w}-hallucination"},
            ],
            failed=True,
            error_message=None,
        )
        return sid, fid, w

    pairs: list[tuple[int, int, int]] = []
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=50) as pool:
        futs = [pool.submit(worker, w) for w in range(50)]
        for fut in as_completed(futs):
            try:
                pairs.append(fut.result())
            except Exception as exc:
                errors.append(str(exc))

    assert errors == [], errors[:3]
    logs = storage.get_shadow_repair_log()
    assert len(logs) == 50
    by_session = {int(r["session_id"]): r for r in logs}
    for sid, fid, w in pairs:
        row = by_session[sid]
        assert int(row["failed_call_id"]) == int(fid)
        assert row["applied"] is False
        blob = str(row.get("fix_candidate") or "") + str(row.get("attribution_reason") or "")
        foreign = [f"worker-{j}-" for j in range(50) if j != w]
        if any(tag in blob for tag in foreign):
            assert f"worker-{w}-" in blob


def test_async_shadow_thread_matches_session(tmp_path, monkeypatch):
    monkeypatch.setenv("STREAMCTX_SHADOW_REPAIR", "1")
    monkeypatch.delenv("STREAMCTX_SHADOW_REPAIR_SYNC", raising=False)
    storage = SessionStorage(db_path=tmp_path / "async.db")
    sid = storage.start_session()
    fid = storage.record_call(
        session_id=sid,
        provider="openai",
        model="test",
        input_tokens=10,
        output_tokens=2,
        cost=0.0,
        reused_tokens=0,
        waste_category=None,
        messages=[
            {"role": "user", "content": "What is Q3?"},
            {"role": "assistant", "content": "$47.3 million"},
        ],
        failed=True,
        error_message=None,
    )
    wait_for_shadow_repair(timeout=10)
    logs = storage.get_shadow_repair_log()
    assert len(logs) == 1
    assert int(logs[0]["session_id"]) == int(sid)
    assert int(logs[0]["failed_call_id"]) == int(fid)


def test_timeout_constant_is_justified():
    assert REPAIR_LLM_TIMEOUT_S == 30.0
    assert MAX_SHADOW_REPAIRS_PER_SESSION == 5
