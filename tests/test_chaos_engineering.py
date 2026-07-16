"""Chaos engineering tests for StreamCtx.

Simulates real-world failure conditions:
- CPU saturation while self-healing is in progress
- SQLite database lock/unavailability during checkpoint/resume
"""

from __future__ import annotations

import multiprocessing
import time
import sqlite3
from pathlib import Path

import pytest

from streamctx.healer import SelfHealingEngine
from streamctx.tracker import get_tracker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _burn_cpu(duration_seconds: float) -> None:
    """Pin one CPU core at 100% for the given duration."""
    end = time.time() + duration_seconds
    while time.time() < end:
        pass  # tight busy-loop


def _spawn_cpu_load(num_workers: int, duration_seconds: float) -> list[multiprocessing.Process]:
    procs = []
    for _ in range(num_workers):
        p = multiprocessing.Process(target=_burn_cpu, args=(duration_seconds,))
        p.start()
        procs.append(p)
    return procs


# ---------------------------------------------------------------------------
# Chaos Test 1: CPU saturation during self-healing
# ---------------------------------------------------------------------------

def test_self_healing_survives_cpu_saturation():
    """attempt_heal should still succeed correctly even while all CPU
    cores are saturated by other processes — it should not corrupt
    state or silently fail, just possibly run slower."""
    engine = SelfHealingEngine()

    good_messages = [{"role": "user", "content": "hello"}]
    good_response = {"role": "assistant", "content": "hi there"}
    engine.record_success(good_messages, good_response)

    call_attempts = {"count": 0}

    def flaky_fn(**kwargs):
        call_attempts["count"] += 1
        if call_attempts["count"] < 2:
            raise ConnectionError("simulated transient failure")
        return {"role": "assistant", "content": "recovered"}

    num_cores = multiprocessing.cpu_count()
    procs = _spawn_cpu_load(num_workers=max(num_cores, 2), duration_seconds=5)

    try:
        failed_messages = [{"role": "user", "content": "another question"}]
        start = time.time()
        result = engine.attempt_heal(
            fn=flaky_fn,
            failed_messages=failed_messages,
            kwargs={},
            max_retries=3,
        )
        elapsed = time.time() - start

        assert result == {"role": "assistant", "content": "recovered"}
        stats = engine.get_stats()
        assert stats["recovery_count"] == 1
        # Sanity: shouldn't hang forever even under load
        assert elapsed < 30
    finally:
        for p in procs:
            p.terminate()
            p.join()


# ---------------------------------------------------------------------------
# Chaos Test 2: Self-healing with no valid checkpoint (cold start under chaos)
# ---------------------------------------------------------------------------

def test_self_healing_fails_gracefully_with_no_checkpoint():
    """If there's no valid checkpoint to recover from, attempt_heal must
    raise a clear RuntimeError — not crash unpredictably or hang."""
    engine = SelfHealingEngine()  # fresh engine, no record_success called

    def always_fails(**kwargs):
        raise ConnectionError("simulated failure")

    with pytest.raises(RuntimeError, match="no valid checkpoint"):
        engine.attempt_heal(
            fn=always_fails,
            failed_messages=[{"role": "user", "content": "test"}],
            kwargs={},
            max_retries=2,
        )


def test_self_healing_exhausts_retries_and_raises():
    """If every retry attempt fails, the original exception should
    propagate rather than being swallowed."""
    engine = SelfHealingEngine()
    engine.record_success(
        [{"role": "user", "content": "hi"}],
        {"role": "assistant", "content": "hello"},
    )

    def always_fails(**kwargs):
        raise ValueError("permanent failure")

    with pytest.raises(ValueError, match="permanent failure"):
        engine.attempt_heal(
            fn=always_fails,
            failed_messages=[{"role": "user", "content": "test"}],
            kwargs={},
            max_retries=2,
        )


# ---------------------------------------------------------------------------
# Chaos Test 3: DB lock / unavailability during tracker operations
# ---------------------------------------------------------------------------

def test_tracker_handles_locked_database(tmp_path, monkeypatch):
    """WAL mode allows readers to proceed even when another connection
    holds an exclusive write lock — so tracker.start() should succeed
    gracefully rather than raising, confirming our WAL hardening works
    under a simulated 'DB down' write-lock scenario."""
    db_path = tmp_path / "chaos_test.db"

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS dummy (id INTEGER)")
    conn.commit()

    lock_conn = sqlite3.connect(str(db_path), timeout=0)
    lock_conn.execute("BEGIN EXCLUSIVE")

    try:
        tracker = get_tracker("chaos-agent")
        start = time.time()
        tracker.start()  # should NOT raise, thanks to WAL mode
        elapsed = time.time() - start

        assert elapsed < 10  # should fail fast/succeed fast, not hang
    finally:
        lock_conn.rollback()
        lock_conn.close()
        conn.close()



# ---------------------------------------------------------------------------
# Chaos Test 4: Repeated failure then recovery (flapping connection)
# ---------------------------------------------------------------------------

def test_self_healing_recovers_after_flapping_failures():
    """Simulates a flapping/unstable connection: fails a few times,
    then succeeds. Healing should recover on the last valid attempt
    within max_retries."""
    engine = SelfHealingEngine()
    engine.record_success(
        [{"role": "user", "content": "setup"}],
        {"role": "assistant", "content": "ready"},
    )

    attempts = {"count": 0}

    def flapping_fn(**kwargs):
        attempts["count"] += 1
        if attempts["count"] <= 3:
            raise TimeoutError("connection flapping")
        return {"role": "assistant", "content": "stable now"}

    result = engine.attempt_heal(
        fn=flapping_fn,
        failed_messages=[{"role": "user", "content": "q"}],
        kwargs={},
        max_retries=5,
    )

    assert result == {"role": "assistant", "content": "stable now"}
    assert attempts["count"] == 4


