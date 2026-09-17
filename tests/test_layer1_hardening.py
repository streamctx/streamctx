"""Layer 1 senior-bar regression tests.

These are the four real-world failure cases from the Core SDK hardening
cycle — not happy-path demos. Kill tests use multiprocessing.Process.kill()
(Windows equivalent of SIGKILL / kill -9).
"""
from __future__ import annotations

import json
import multiprocessing
import sqlite3
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from streamctx.compressor import compress_messages, get_compression_stats
from streamctx.healer import SelfHealingEngine
from streamctx.storage import SessionStorage
from streamctx.tracker import LLMTracker


def _fake_response(text: str = "ok") -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        usage=SimpleNamespace(prompt_tokens=4, completion_tokens=2),
    )


class _FakeCompletions:
    def __init__(self, on_create):
        self._on_create = on_create

    def create(self, **kwargs):
        return self._on_create(**kwargs)


class _FakeClient:
    def __init__(self, on_create):
        self.chat = SimpleNamespace(completions=_FakeCompletions(on_create))


def _tracker(tmp_path, agent_id: str) -> LLMTracker:
    tracker = LLMTracker(agent_id=agent_id)
    tracker.state.storage = SessionStorage(db_path=tmp_path / f"{agent_id}.db")
    return tracker


# ---------------------------------------------------------------------------
# Kill -9 writer (module-level for Windows spawn)
# ---------------------------------------------------------------------------

def _kill9_persist_step_writer(db_path: str) -> None:
    storage = SessionStorage(db_path=Path(db_path))
    sid = storage.start_session()
    blob = "x" * 40_000
    for i in range(800):
        storage.persist_step(
            session_id=sid,
            provider="openai",
            model="gpt-4",
            input_tokens=1,
            output_tokens=1,
            cost=0.0,
            reused_tokens=0,
            waste_category=None,
            request_messages=[{"role": "user", "content": f"{i}:{blob}"}],
            checkpoint_messages=[{"role": "user", "content": f"{i}:{blob}"}],
            step_number=i,
            message_fingerprint=f"fp-{i}",
            response_text="ok",
        )


def test_kill9_mid_write_leaves_consistent_rows():
    tmp = tempfile.mkdtemp(prefix="streamctx-kill9-")
    db_path = str(Path(tmp) / "sessions.db")
    proc = multiprocessing.Process(target=_kill9_persist_step_writer, args=(db_path,))
    proc.start()
    deadline = time.time() + 8.0
    while time.time() < deadline:
        if Path(db_path).exists():
            try:
                probe = sqlite3.connect(db_path, timeout=1.0)
                n = probe.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE name='calls'"
                ).fetchone()[0]
                rows = probe.execute("SELECT COUNT(*) FROM calls").fetchone()[0] if n else 0
                probe.close()
                if rows >= 6:
                    break
            except sqlite3.OperationalError:
                pass
        time.sleep(0.04)
    proc.kill()
    proc.join(timeout=5)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    assert integrity == "ok"
    call_rows = conn.execute("SELECT id, messages_json FROM calls").fetchall()
    ckpt_rows = conn.execute("SELECT id, messages_json FROM checkpoints").fetchall()
    parse_failures = 0
    for row in list(call_rows) + list(ckpt_rows):
        try:
            json.loads(row["messages_json"])
        except Exception:
            parse_failures += 1
    conn.close()

    assert parse_failures == 0
    # Atomic persist_step: a kill cannot commit one side of the pair.
    assert len(call_rows) == len(ckpt_rows)
    assert len(call_rows) >= 1


def test_resume_does_not_rerun_file_write_side_effect(tmp_path):
    writes: list[str] = []

    def on_create(**kwargs):
        writes.append("file.txt")
        (tmp_path / "file.txt").write_text("hello", encoding="utf-8")
        return _fake_response("WRITE file.txt")

    tracker = _tracker(tmp_path, "side-a")
    storage = tracker.state.storage
    tracker.start()
    client = tracker.wrap(_FakeClient(on_create))
    msgs = [{"role": "user", "content": "write file.txt"}]
    client.chat.completions.create(model="gpt-4o-mini", messages=msgs)
    sid = tracker.get_session_id()
    assert sid is not None
    tracker.stop()

    tracker2 = LLMTracker(agent_id="side-b")
    tracker2.state.storage = storage
    resumed = tracker2.resume(sid)
    assert tracker2.state.step_counter == 1
    assert tracker2.state.session_id == sid
    assert any(m.get("role") == "assistant" for m in resumed)

    tracker2.start()
    client2 = tracker2.wrap(_FakeClient(on_create))
    client2.chat.completions.create(model="gpt-4o-mini", messages=resumed)
    tracker2.stop()

    assert writes == ["file.txt"]
    assert (tmp_path / "file.txt").read_text(encoding="utf-8") == "hello"


def test_identical_live_prompts_are_two_calls_not_deduped(tmp_path):
    hits = {"n": 0}

    def on_create(**kwargs):
        hits["n"] += 1
        return _fake_response("ok")

    tracker = _tracker(tmp_path, "live-dup")
    tracker.start()
    client = tracker.wrap(_FakeClient(on_create))
    msgs = [{"role": "user", "content": "same prompt"}]
    client.chat.completions.create(model="x", messages=msgs)
    client.chat.completions.create(model="x", messages=msgs)
    stats = tracker.get_stats()
    tracker.stop()
    assert hits["n"] == 2
    assert stats["call_count"] == 2


def test_compression_preserves_buried_constraint():
    constraint = (
        "CRITICAL CONSTRAINT id=ACME-9917: NEVER use the production database; "
        "staging only. All writes must go to staging-db-07."
    )
    messages = [{"role": "system", "content": "You are a coding agent."}]
    messages.append({"role": "user", "content": constraint})
    messages.append({"role": "assistant", "content": "Understood."})
    for i in range(12):
        messages.append({
            "role": "user",
            "content": f"Please refactor module {i} and explain every line. " + ("padding " * 40),
        })
        messages.append({
            "role": "assistant",
            "content": f"Refactored module {i}. " + ("ok " * 40),
        })
    messages.append({"role": "user", "content": "Apply the next change."})

    compressed, orig, after = compress_messages(messages, max_tokens=200, keep_last_n=4)
    blob = " ".join(str(m.get("content", "")) for m in compressed)
    assert orig > after
    assert "ACME-9917" in blob
    assert "staging-db-07" in blob
    assert "NEVER use the production" in blob


def test_intercept_sends_compressed_messages_and_keeps_constraint(tmp_path):
    constraint = (
        "CRITICAL CONSTRAINT id=ACME-9917: NEVER use the production database; "
        "all writes must go to staging-db-07."
    )
    messages = [{"role": "system", "content": "You are a coding agent."}]
    messages.append({"role": "user", "content": constraint})
    for i in range(20):
        messages.append({
            "role": "user",
            "content": f"Long chatter block {i}: " + ("word " * 80),
        })
        messages.append({
            "role": "assistant",
            "content": f"Acknowledged block {i}: " + ("ok " * 80),
        })
    messages.append({"role": "user", "content": "Continue."})
    seen: dict = {}

    def on_create(**kwargs):
        seen["messages"] = kwargs.get("messages")
        return _fake_response("ok")

    tracker = _tracker(tmp_path, "comp-apply")
    tracker.start()
    tracker.wrap(_FakeClient(on_create)).chat.completions.create(
        model="x", messages=messages
    )
    tracker.stop()

    outbound = seen["messages"]
    assert len(outbound) < len(messages)
    blob = " ".join(str(m.get("content", "")) for m in outbound)
    assert "ACME-9917" in blob
    assert "staging-db-07" in blob


def test_healer_falls_through_two_corrupt_checkpoints(tmp_path):
    storage = SessionStorage(db_path=tmp_path / "corrupt.db")
    sid = storage.start_session()
    storage.save_checkpoint(sid, 1, [{"role": "user", "content": "valid-step-1"}])
    storage.save_checkpoint(sid, 2, [{"role": "user", "content": "valid-step-2"}])
    storage.save_checkpoint(sid, 3, [{"role": "user", "content": "valid-step-3"}])
    with storage._write_lock:
        storage._write_conn.execute(
            "UPDATE checkpoints SET messages_json='NOT_JSON' WHERE session_id=? AND step_number=3",
            (sid,),
        )
        storage._write_conn.execute(
            "UPDATE checkpoints SET messages_json='{\"role\": \"user\"}' WHERE session_id=? AND step_number=2",
            (sid,),
        )
        storage._write_conn.commit()

    resumed = storage.resume_from_checkpoint(sid)
    assert resumed == [{"role": "user", "content": "valid-step-1"}]

    healer = SelfHealingEngine()
    healer.ingest_valid_context(storage, sid)
    assert healer.can_heal() is True
    recovery = healer.get_recovery_messages([{"role": "user", "content": "next"}])
    assert any("valid-step-1" in str(m.get("content")) for m in recovery)


def test_intercept_retries_with_previous_valid_context(tmp_path):
    calls = {"n": 0}

    def on_create(**kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise ConnectionError("transient")
        return _fake_response("recovered" if calls["n"] > 2 else "first")

    tracker = _tracker(tmp_path, "heal-retry")
    tracker.start()
    client = tracker.wrap(_FakeClient(on_create))
    client.chat.completions.create(
        model="x", messages=[{"role": "user", "content": "hi"}]
    )
    result = client.chat.completions.create(
        model="x", messages=[{"role": "user", "content": "again"}]
    )
    tracker.stop()

    assert result.choices[0].message.content == "recovered"
    assert calls["n"] == 3
    stats = tracker.healing_stats()
    assert stats["failure_count"] >= 1
    assert stats["recovery_count"] >= 1
    rows = tracker.state.storage.get_calls_for_session(tracker.get_session_id())
    assert any(r["failed"] for r in rows)
    assert any(r["healed"] and not r["failed"] for r in rows)


def test_failed_call_does_not_move_resume_checkpoint(tmp_path):
    def on_create(**kwargs):
        content = kwargs["messages"][-1]["content"]
        if content == "boom":
            raise RuntimeError("hard fail")
        return _fake_response("ok")

    tracker = _tracker(tmp_path, "fail-ckpt")
    tracker.start()
    client = tracker.wrap(_FakeClient(on_create))
    client.chat.completions.create(
        model="x", messages=[{"role": "user", "content": "safe"}]
    )
    with pytest.raises(RuntimeError, match="hard fail"):
        client.chat.completions.create(
            model="x", messages=[{"role": "user", "content": "boom"}]
        )
    sid = tracker.get_session_id()
    tracker.stop()

    resumed = tracker.resume(sid)
    blob = " ".join(m.get("content", "") for m in resumed)
    assert "safe" in blob
    assert "boom" not in blob


def test_persist_failure_does_not_drop_provider_response(tmp_path):
    tracker = _tracker(tmp_path, "persist-fail")

    def boom(**_kwargs):
        raise sqlite3.OperationalError("simulated disk full")

    tracker.state.storage.persist_step = boom  # type: ignore[method-assign]
    tracker.state.storage.record_call = boom  # type: ignore[method-assign]
    tracker.start()
    client = tracker.wrap(_FakeClient(lambda **kw: _fake_response("still here")))
    result = client.chat.completions.create(
        model="x", messages=[{"role": "user", "content": "hi"}]
    )
    tracker.stop()
    assert result.choices[0].message.content == "still here"


def _chatter_session() -> list[dict[str, str]]:
    messages = [{"role": "system", "content": "You are a helpful assistant."}]
    for i in range(16):
        messages.append({
            "role": "user",
            "content": f"Please walk through topic {i} in exhaustive detail. " + ("padding " * 50),
        })
        messages.append({
            "role": "assistant",
            "content": f"Here is a long answer about topic {i}. " + ("content " * 50),
        })
    messages.append({"role": "user", "content": "Summarize the last point."})
    return messages


def _tool_heavy_session() -> list[dict[str, str]]:
    messages = [{"role": "system", "content": "You execute tools."}]
    for i in range(10):
        messages.append({"role": "user", "content": f"Run tool batch {i}."})
        messages.append({
            "role": "assistant",
            "content": f"tool result {i}: " + json.dumps({"rows": list(range(80)), "blob": "x" * 120, "i": i}),
        })
    messages.append({"role": "user", "content": "What was the last status?"})
    return messages


def _constraint_session() -> list[dict[str, str]]:
    messages = [{"role": "system", "content": "Follow policy exactly."}]
    messages.append({
        "role": "user",
        "content": "CRITICAL CONSTRAINT id=ACME-9917: NEVER use the production database.",
    })
    messages.append({"role": "assistant", "content": "Acknowledged ACME-9917."})
    for i in range(14):
        messages.append({
            "role": "user",
            "content": f"Filler turn {i}: " + ("lorem " * 60),
        })
        messages.append({
            "role": "assistant",
            "content": f"Filler reply {i}: " + ("ipsum " * 60),
        })
    messages.append({"role": "user", "content": "Proceed."})
    return messages


@pytest.mark.parametrize(
    "name,builder,must_keep",
    [
        ("chatter", _chatter_session, None),
        ("tool_heavy", _tool_heavy_session, None),
        ("buried_constraint", _constraint_session, "ACME-9917"),
    ],
)
def test_compression_ratios_on_multiple_session_shapes(name, builder, must_keep):
    messages = builder()
    compressed, orig, after = compress_messages(messages, max_tokens=800, keep_last_n=4)
    stats = get_compression_stats(orig, after)
    assert orig > 800
    assert stats["saved_tokens"] >= 0
    if must_keep:
        blob = " ".join(str(m.get("content", "")) for m in compressed)
        assert must_keep in blob
    if name == "chatter":
        assert stats["compression_pct"] >= 40


def test_concurrent_persist_step_50_workers(tmp_path):
    storage = SessionStorage(db_path=tmp_path / "load.db")
    errors: list[str] = []

    def worker(wid: int) -> None:
        try:
            sid = storage.start_session()
            for i in range(20):
                storage.persist_step(
                    session_id=sid,
                    provider="openai",
                    model="gpt-4",
                    input_tokens=10,
                    output_tokens=5,
                    cost=0.0,
                    reused_tokens=0,
                    waste_category=None,
                    request_messages=[{"role": "user", "content": f"w{wid}-{i}"}],
                    checkpoint_messages=[{"role": "user", "content": f"w{wid}-{i}"}],
                    step_number=i,
                    message_fingerprint=f"{wid}-{i}",
                    response_text="ok",
                )
            storage.end_session(sid)
        except Exception as exc:
            errors.append(str(exc))

    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=50) as pool:
        list(pool.map(worker, range(50)))

    assert errors == [], errors[:3]
    conn = sqlite3.connect(str(tmp_path / "load.db"))
    n_calls = conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
    n_ckpts = conn.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0]
    conn.close()
    assert n_calls == n_ckpts == 50 * 20
