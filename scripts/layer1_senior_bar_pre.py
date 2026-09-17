"""Layer 1 senior-engineer-bar cases against CURRENT code (pre-fix).

Run: python scripts/layer1_senior_bar_pre.py
Prints honest PASS/WEAK/FAIL per differentiator. Does not patch anything.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

# Ensure src/ is importable
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from streamctx.compressor import compress_messages
from streamctx.healer import SelfHealingEngine
from streamctx.storage import SessionStorage
from streamctx.tracker import LLMTracker


def _header(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def _kill9_writer(db_path: str) -> None:
    storage = SessionStorage(db_path=Path(db_path))
    sid = storage.start_session()
    blob = "x" * 80_000
    for i in range(400):
        storage.record_call(
            session_id=sid,
            provider="openai",
            model="gpt-4",
            input_tokens=1,
            output_tokens=1,
            cost=0.0,
            reused_tokens=0,
            waste_category=None,
            messages=[{"role": "user", "content": f"{i}:{blob}"}],
        )
        storage.save_checkpoint(
            sid, i, [{"role": "user", "content": f"{i}:{blob}"}]
        )


def case_kill9_mid_write() -> str:
    """Kill a writer process mid-commit. Can the reader parse every row?"""
    import multiprocessing

    tmp = tempfile.mkdtemp(prefix="streamctx-l1-kill-")
    db_path = str(Path(tmp) / "sessions.db")

    proc = multiprocessing.Process(target=_kill9_writer, args=(db_path,))
    proc.start()
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if Path(db_path).exists():
            try:
                probe = sqlite3.connect(db_path, timeout=1.0)
                n = probe.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE name='calls'"
                ).fetchone()[0]
                rows = 0
                if n:
                    rows = probe.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
                probe.close()
                if rows >= 8:
                    break
            except sqlite3.OperationalError:
                pass
        time.sleep(0.05)
    time.sleep(0.05)
    proc.kill()  # SIGKILL equivalent on Windows
    proc.join(timeout=5)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    tables = {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "calls" not in tables:
        print("  writer died before schema init")
        conn.close()
        return "WEAK — kill landed before first commit; no half-row to inspect"
    call_rows = conn.execute("SELECT id, messages_json FROM calls").fetchall()
    ckpt_rows = conn.execute("SELECT id, messages_json FROM checkpoints").fetchall()

    parse_failures = 0
    for row in list(call_rows) + list(ckpt_rows):
        try:
            json.loads(row["messages_json"])
        except Exception:
            parse_failures += 1

    n_calls = len(call_rows)
    n_ckpts = len(ckpt_rows)
    print(f"  process exit: {proc.exitcode} (killed={proc.exitcode is not None})")
    print(f"  integrity_check: {integrity}")
    print(f"  calls={n_calls} checkpoints={n_ckpts} json_parse_failures={parse_failures}")
    print(f"  split commits: |calls-checkpoints|={abs(n_calls - n_ckpts)}")
    conn.close()

    if integrity != "ok" or parse_failures:
        return "FAIL — corrupt/unreadable row after kill"
    if abs(n_calls - n_ckpts) > 0:
        return (
            "WEAK — SQLite recovered (no half-row) but call+checkpoint are "
            "separate commits; kill can leave a call without a matching checkpoint"
        )
    return "PASS — recovered, counts matched (kill may have landed between loops)"


def case_resume_side_effect() -> str:
    """Checkpoint one step before a file-write tool; resume + re-call create()."""
    tmp = tempfile.mkdtemp(prefix="streamctx-l1-side-")
    db_path = Path(tmp) / "sessions.db"
    storage = SessionStorage(db_path=db_path)
    writes = []

    class FakeCompletions:
        def create(self, **kwargs):
            # Side effect the "agent" performs when the model asks to write.
            writes.append("file.txt")
            Path(tmp, "file.txt").write_text("hello", encoding="utf-8")
            return type("R", (), {
                "choices": [type("C", (), {
                    "message": type("M", (), {"content": "WRITE file.txt"})()
                })()],
                "usage": type("U", (), {"prompt_tokens": 4, "completion_tokens": 2})(),
            })()

    class FakeClient:
        def __init__(self):
            self.chat = type("Chat", (), {"completions": FakeCompletions()})()

    tracker = LLMTracker(agent_id="side-effect")
    tracker.state.storage = storage
    tracker.start()
    client = tracker.wrap(FakeClient())
    msgs = [{"role": "user", "content": "write file.txt"}]
    client.chat.completions.create(model="gpt-4o-mini", messages=msgs)
    sid = tracker.get_session_id()
    tracker.stop()

    # Fresh tracker, resume, then a typical agent loop: create(resumed_messages)
    tracker2 = LLMTracker(agent_id="side-effect-resume")
    tracker2.state.storage = storage
    resumed = tracker2.resume(sid)
    print(f"  resumed messages: {resumed}")
    print(f"  step_counter after resume: {tracker2.state.step_counter} (want last completed step)")
    print(f"  session_id after resume: {tracker2.session_id or tracker2.state.session_id}")

    tracker2.start()
    client2 = tracker2.wrap(FakeClient())
    # Typical loop re-sends whatever resume() returned.
    client2.chat.completions.create(model="gpt-4o-mini", messages=resumed or msgs)
    tracker2.stop()

    print(f"  file-write side effects: {len(writes)} (want 1)")
    calls = storage.get_calls_for_session(sid)
    print(f"  persisted calls in original session: {len(calls)}")
    if tracker2.state.step_counter == 0:
        print("  resume() did not restore step_counter")
    if len(writes) > 1:
        return "FAIL — resume re-ran the file-write side effect"
    return "PASS — side effect ran once"


def case_compression_buried_constraint() -> str:
    constraint = (
        "CRITICAL CONSTRAINT id=ACME-9917: NEVER use the production database; "
        "staging only. All writes must go to staging-db-07."
    )
    messages = [{"role": "system", "content": "You are a coding agent."}]
    messages.append({"role": "user", "content": constraint})
    messages.append({"role": "assistant", "content": "Understood."})
    for i in range(12):
        messages.append({"role": "user", "content": f"Please refactor module {i} and explain every line in detail. " + ("padding " * 40)})
        messages.append({"role": "assistant", "content": f"Refactored module {i}. " + ("ok " * 40)})
    messages.append({"role": "user", "content": "Apply the next change."})

    compressed, orig, after = compress_messages(messages, max_tokens=200, keep_last_n=4)
    blob = " ".join(m.get("content", "") for m in compressed)
    print(f"  original_tokens={orig} compressed_tokens={after} n_messages={len(compressed)}")
    print(f"  ACME-9917 present: {'ACME-9917' in blob}")
    print(f"  staging-db-07 present: {'staging-db-07' in blob}")
    print(f"  'NEVER use the production' present: {'NEVER use the production' in blob}")
    preview = compressed[1]["content"][:80] if len(compressed) > 1 else ""
    print(f"  middle/summary preview: {preview!r}")

    # Does intercept actually send compressed messages?
    from streamctx.tracker import LLMTracker as T
    seen = {}

    class Cap:
        def create(self, **kwargs):
            seen["messages"] = kwargs.get("messages")
            return type("R", (), {
                "choices": [type("C", (), {"message": type("M", (), {"content": "ok"})()})()],
                "usage": type("U", (), {"prompt_tokens": 10, "completion_tokens": 1})(),
            })()

    class Client:
        def __init__(self):
            self.chat = type("Chat", (), {"completions": Cap()})()

    tr = T(agent_id="comp-apply")
    tr.state.storage = SessionStorage(db_path=Path(tempfile.mkdtemp()) / "t.db")
    tr.start()
    tr.wrap(Client()).chat.completions.create(model="x", messages=messages)
    tr.stop()
    outbound = seen.get("messages") or []
    outbound_blob = " ".join(
        (m.get("content") or "") if isinstance(m, dict) else str(m) for m in outbound
    )
    applied = len(outbound) < len(messages)
    print(f"  intercept outbound message count: {len(outbound)} (in={len(messages)}); compressed applied={applied}")

    preserved = "ACME-9917" in blob and "staging-db-07" in blob
    if not preserved:
        return "FAIL — buried constraint dropped by 15-char middle squash"
    if not applied:
        return "WEAK — constraint survived public compress() but intercept does not send compressed messages"
    return "PASS"


def case_dual_corrupt_checkpoints() -> str:
    storage = SessionStorage(db_path=Path(tempfile.mkdtemp()) / "c.db")
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

    print("  corrupted steps 3 (invalid JSON) and 2 (not a message list)")

    resumed = None
    resume_err = None
    try:
        resumed = storage.resume_from_checkpoint(sid)
    except Exception as e:
        resume_err = f"{type(e).__name__}: {e}"
    print(f"  resume_from_checkpoint -> {resumed!r} err={resume_err}")

    healer = SelfHealingEngine()
    healer.record_success([{"role": "user", "content": "in-memory-only"}], {"ok": True})
    # Process "restart": new healer, no in-memory success.
    healer2 = SelfHealingEngine()
    print(f"  healer after restart can_heal={healer2.can_heal()} (checkpoints exist)")

    # Does intercept retry?
    from types import SimpleNamespace

    class Flaky:
        def __init__(self):
            self.n = 0

        def create(self, **kwargs):
            self.n += 1
            if self.n == 1:
                raise ConnectionError("transient")
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="recovered"))],
                usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
            )

    class Client:
        def __init__(self):
            self.chat = type("Chat", (), {"completions": Flaky()})()

    tr = LLMTracker(agent_id="heal-retry")
    tr.state.storage = SessionStorage(db_path=Path(tempfile.mkdtemp()) / "h.db")
    tr.start()
    wrapped = tr.wrap(Client())
    first_ok = False
    second_healed = False
    try:
        wrapped.chat.completions.create(
            model="x", messages=[{"role": "user", "content": "hi"}]
        )
        first_ok = True
        wrapped.chat.completions.create(
            model="x", messages=[{"role": "user", "content": "again"}]
        )
        second_healed = True
        intercept_err = None
    except Exception as e:
        intercept_err = f"{type(e).__name__}: {e}"
    stats = tr.healing_stats()
    rows = tr.state.storage.get_calls_for_session(tr.get_session_id())
    print(f"  first_ok={first_ok} second_healed={second_healed} err={intercept_err}")
    print(f"  healing_stats={stats}")
    print(f"  call rows failed/healed: {[(r['failed'], r['healed'], r['error_message']) for r in rows]}")
    tr.stop()

    parts = []
    if resume_err:
        parts.append("resume raises on corrupt latest checkpoint")
    elif resumed and (not isinstance(resumed, list) or "valid-step-1" not in str(resumed)):
        parts.append(f"resume did not fall through to step 1 (got {resumed!r})")
    if not healer2.can_heal():
        parts.append("healer ignores DB checkpoints (in-memory only)")
    if intercept_err:
        parts.append("intercept records healed then re-raises; no retry")
    if not parts:
        return "PASS"
    return "FAIL — " + "; ".join(parts)


def main() -> None:
    results = {}
    _header("1. Streaming — kill writer mid-write")
    results["streaming"] = case_kill9_mid_write()
    print("  VERDICT:", results["streaming"])

    _header("2. Checkpoint/resume — side-effect tool one step after checkpoint")
    results["resume"] = case_resume_side_effect()
    print("  VERDICT:", results["resume"])

    _header("3. Compression — buried critical constraint")
    results["compression"] = case_compression_buried_constraint()
    print("  VERDICT:", results["compression"])

    _header("4. Self-healing — two consecutive corrupt checkpoints + intercept retry")
    results["healing"] = case_dual_corrupt_checkpoints()
    print("  VERDICT:", results["healing"])

    _header("SUMMARY")
    for k, v in results.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
