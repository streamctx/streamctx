"""Concurrent multi-user load test for StreamCtx's SQLite storage layer.

Simulates multiple "developers" (threads) using the SDK at the same time —
each starting their own session and recording calls/checkpoints concurrently.
This checks whether SQLite's default locking causes "database is locked"
errors under concurrent write load, which WAL mode would normally prevent.

Usage:
    python concurrent_load_test.py --workers 10 --calls-per-worker 50
    python concurrent_load_test.py --workers 25 --calls-per-worker 100
"""

from __future__ import annotations

import argparse
import random
import threading
import time

from streamctx.storage import get_storage


def worker_job(worker_id: int, calls_per_worker: int, results: dict, lock: threading.Lock) -> None:
    """Simulate one 'developer' using StreamCtx concurrently with others."""
    storage = get_storage()  # each thread gets its own SessionStorage instance
    errors = []
    start = time.perf_counter()

    try:
        session_id = storage.start_session()
        for i in range(calls_per_worker):
            storage.record_call(
                session_id=session_id,
                provider="openai",
                model="gpt-4",
                input_tokens=random.randint(50, 300),
                output_tokens=random.randint(20, 150),
                cost=0.01,
                reused_tokens=0,
                waste_category=None,
                messages=[{"role": "user", "content": f"worker {worker_id} step {i}"}],
                failed=False,
                healed=False,
                error_message=None,
            )
            storage.save_checkpoint(
                session_id, i,
                [{"role": "user", "content": f"worker {worker_id} step {i}"}],
            )
        storage.end_session(session_id)
    except Exception as e:
        errors.append(str(e))

    elapsed = time.perf_counter() - start
    with lock:
        results[worker_id] = {"elapsed": elapsed, "errors": errors}


def run_test(num_workers: int, calls_per_worker: int) -> None:
    print(f"Spawning {num_workers} concurrent workers, "
          f"{calls_per_worker} calls each ({num_workers * calls_per_worker} total calls)...")
    print("-" * 60)

    results: dict = {}
    lock = threading.Lock()
    threads = []

    overall_start = time.perf_counter()
    for wid in range(num_workers):
        t = threading.Thread(target=worker_job, args=(wid, calls_per_worker, results, lock))
        threads.append(t)
        t.start()

    for t in threads:
        t.join()
    overall_elapsed = time.perf_counter() - overall_start

    # --- Report ---
    total_errors = 0
    lock_errors = 0
    worker_times = []

    for wid, data in sorted(results.items()):
        worker_times.append(data["elapsed"])
        if data["errors"]:
            total_errors += len(data["errors"])
            for err in data["errors"]:
                if "locked" in err.lower():
                    lock_errors += 1
                print(f"Worker {wid}: ERROR - {err}")

    print(f"All {num_workers} workers completed in {overall_elapsed:.2f}s (wall clock)")
    print(f"Avg per-worker time: {sum(worker_times) / len(worker_times) * 1000:.1f}ms")
    print(f"Slowest worker: {max(worker_times) * 1000:.1f}ms")
    print(f"Fastest worker: {min(worker_times) * 1000:.1f}ms")
    print("-" * 60)

    if total_errors == 0:
        print("PASS: No errors under concurrent load.")
    else:
        print(f"FAIL: {total_errors} total errors, {lock_errors} were 'database is locked' errors.")
        print("This confirms SQLite needs WAL mode or connection pooling for concurrent use.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Concurrent multi-user load test")
    parser.add_argument("--workers", type=int, default=10, help="Number of concurrent workers")
    parser.add_argument("--calls-per-worker", type=int, default=50, help="Calls per worker")
    args = parser.parse_args()
    run_test(args.workers, args.calls_per_worker)


if __name__ == "__main__":
    main()

