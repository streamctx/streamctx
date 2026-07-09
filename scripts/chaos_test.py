"""
Chaos test: randomly injects concurrency, malformed data, and simulated
crashes against SessionStorage to verify graceful degradation and DB
integrity — no real API keys required.

Usage:
    python scripts/chaos_test.py
"""

from __future__ import annotations

import random
import sqlite3
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from streamctx.storage import SessionStorage  # noqa: E402
from streamctx.poison_detector import PoisonDetector  # noqa: E402


def check_db_integrity(db_path: Path) -> bool:
    """Run SQLite's built-in integrity check."""
    conn = sqlite3.connect(str(db_path))
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()
        return result[0] == "ok"
    finally:
        conn.close()


def scenario_concurrent_same_session(storage: SessionStorage, session_id: int, errors: list) -> None:
    """Many threads hammering the SAME session's checkpoints simultaneously."""
    def worker(step: int) -> None:
        try:
            messages = [{"role": "user", "content": f"concurrent step {step}"}]
            storage.save_checkpoint(session_id, step, messages)
        except Exception as e:
            errors.append(("concurrent_same_session", step, e))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def scenario_concurrent_many_sessions(storage: SessionStorage, errors: list) -> None:
    """Many threads each starting/using/ending their own session concurrently."""
    def worker(idx: int) -> None:
        try:
            sid = storage.start_session()
            storage.save_checkpoint(sid, 1, [{"role": "user", "content": f"session {idx}"}])
            storage.end_session(sid)
        except Exception as e:
            errors.append(("concurrent_many_sessions", idx, e))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(30)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def scenario_malformed_messages(storage: SessionStorage, session_id: int, errors: list) -> None:
    """Adversarial / malformed message payloads."""
    malformed_cases = [
        [],  # empty list
        [{"role": "user"}],  # missing content
        [{"content": "no role"}],  # missing role
        [{"role": "user", "content": "x" * 100_000}],  # huge single message
        [{"role": "user", "content": "null byte: \x00 embedded"}],
        [{"role": "user", "content": "unicode chaos: 🚀💥ગુજરાતી日本語"}],
        [{"role": None, "content": None}],  # None values
    ]
    for i, messages in enumerate(malformed_cases):
        try:
            storage.save_checkpoint(session_id, 9000 + i, messages)
        except Exception as e:
            errors.append(("malformed_messages", i, e))


def scenario_random_poison_scans(errors: list) -> None:
    """Feed the Poison Detector random adversarial conversation shapes."""
    detector = PoisonDetector()
    roles = ["user", "assistant", "system", None, "weird_role"]
    contents = [
        "Error: not found.",
        "",
        None,
        "x" * 50_000,
        "ERROR FAILED EXCEPTION invalid cannot",
        "🚀" * 500,
    ]
    for _ in range(100):
        n = random.randint(0, 10)
        messages = [
            {"role": random.choice(roles), "content": random.choice(contents)}
            for _ in range(n)
        ]
        try:
            detector.scan(messages)
        except Exception as e:
            errors.append(("random_poison_scan", messages, e))


def scenario_rapid_session_cycling(storage: SessionStorage, errors: list) -> None:
    """Rapidly start and end many sessions back-to-back."""
    for i in range(100):
        try:
            sid = storage.start_session()
            if i % 3 == 0:
                storage.save_checkpoint(sid, 1, [{"role": "user", "content": "quick"}])
            storage.end_session(sid)
        except Exception as e:
            errors.append(("rapid_session_cycling", i, e))


def run_chaos(db_path: Path) -> None:
    storage = SessionStorage(db_path=db_path)
    session_id = storage.start_session()
    errors: list = []

    scenarios = [
        ("Concurrent writes to same session", lambda: scenario_concurrent_same_session(storage, session_id, errors)),
        ("Concurrent writes across many sessions", lambda: scenario_concurrent_many_sessions(storage, errors)),
        ("Malformed/adversarial message payloads", lambda: scenario_malformed_messages(storage, session_id, errors)),
        ("Random adversarial Poison Detector scans", lambda: scenario_random_poison_scans(errors)),
        ("Rapid session start/end cycling", lambda: scenario_rapid_session_cycling(storage, errors)),
    ]

    print(f"Chaos test starting — db={db_path}")
    print("-" * 60)

    for name, fn in scenarios:
        t0 = time.monotonic()
        fn()
        elapsed = time.monotonic() - t0
        print(f"[{elapsed:6.2f}s] {name}")

    storage.end_session(session_id)

    print("-" * 60)
    integrity_ok = check_db_integrity(db_path)
    print(f"DB integrity check: {'OK' if integrity_ok else 'FAILED'}")
    print(f"Unhandled errors encountered: {len(errors)}")

    if errors:
        print("\nFirst 10 errors:")
        for scenario_name, context, exc in errors[:10]:
            print(f"  [{scenario_name}] context={context!r} -> {type(exc).__name__}: {exc}")

    if not integrity_ok or errors:
        print("\n RESULT: CHAOS TEST FOUND ISSUES — review above.")
        sys.exit(1)
    else:
        print("\n RESULT: PASSED — system degraded gracefully under chaos.")


if __name__ == "__main__":
    db_path = Path("chaos_test.db")
    if db_path.exists():
        db_path.unlink()
    run_chaos(db_path)
