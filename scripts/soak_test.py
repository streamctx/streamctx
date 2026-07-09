"""
Soak test: sustained load against SessionStorage to catch memory leaks,
DB bloat, and latency degradation over many checkpoint saves.

Usage:
    python scripts/soak_test.py --iterations 2000
    python scripts/soak_test.py --duration 300   # run for 300 seconds instead
"""

from __future__ import annotations

import argparse
import gc
import sys
import time
import tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from streamctx.storage import SessionStorage  # noqa: E402


def run_soak(iterations: int | None, duration: float | None, db_path: Path) -> None:
    storage = SessionStorage(db_path=db_path)
    session_id = storage.start_session()

    tracemalloc.start()
    start_time = time.monotonic()
    latencies: list[float] = []
    i = 0

    print(f"Soak test starting — session_id={session_id}, db={db_path}")
    print("-" * 60)

    while True:
        if iterations is not None and i >= iterations:
            break
        if duration is not None and (time.monotonic() - start_time) >= duration:
            break

        messages = [
            {"role": "user", "content": f"Message {j} in iteration {i}"}
            for j in range(5)
        ]

        t0 = time.monotonic()
        storage.save_checkpoint(session_id, i, messages)
        latencies.append(time.monotonic() - t0)

        if i > 0 and i % 200 == 0:
            gc.collect()
            current, peak = tracemalloc.get_traced_memory()
            db_size_kb = db_path.stat().st_size / 1024
            avg_latency_ms = (sum(latencies[-200:]) / 200) * 1000
            print(
                f"[{i:>6}] avg_latency={avg_latency_ms:.2f}ms "
                f"mem_current={current / 1024:.1f}KB mem_peak={peak / 1024:.1f}KB "
                f"db_size={db_size_kb:.1f}KB"
            )

        i += 1

    storage.end_session(session_id)
    tracemalloc.stop()

    print("-" * 60)
    print(f"Soak test complete: {i} checkpoints saved")
    print(f"First-100 avg latency:  {(sum(latencies[:100]) / min(100, len(latencies))) * 1000:.2f}ms")
    print(f"Last-100  avg latency:  {(sum(latencies[-100:]) / min(100, len(latencies))) * 1000:.2f}ms")
    print("If last-100 latency is significantly higher than first-100, investigate DB/index growth.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="StreamCtx soak test")
    parser.add_argument("--iterations", type=int, default=None, help="Number of checkpoint saves")
    parser.add_argument("--duration", type=float, default=None, help="Run for N seconds instead")
    parser.add_argument("--db", type=str, default="soak_test.db", help="Path to test DB file")
    args = parser.parse_args()

    if args.iterations is None and args.duration is None:
        args.iterations = 1000  # sensible default

    run_soak(args.iterations, args.duration, Path(args.db))


