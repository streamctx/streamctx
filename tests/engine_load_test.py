"""
Concurrent load test for the Attribution Engine and Counterfactual Replay
Engine - simulates many "developers" reading/replaying sessions at the
same time, on top of the same SQLite storage singleton used by the
concurrent_load_test.py write-path test.

Two phases:
  1. Seed: create N sessions, each with `--steps` calls + checkpoints
     (a mix of normal and failed calls so attribution has something to
     find).
  2. Load: spawn `--workers` threads; each thread repeatedly picks a
     random seeded session and calls attribute_session() and a dry-run
     replay() concurrently with everyone else.
"""
import argparse
import random
import threading
import time
import traceback

from streamctx.storage import get_storage
from streamctx.attribution import get_attribution_engine
from streamctx.replay import get_replayer


def seed_sessions(n_sessions: int, steps: int) -> list[int]:
    storage = get_storage()
    session_ids = []
    for s in range(n_sessions):
        session_id = storage.start_session()
        for i in range(steps):
            failed = (i == steps // 2)  # inject one failure mid-session
            storage.record_call(
                session_id=session_id,
                provider="openrouter",
                model="test-model",
                input_tokens=100 + i,
                output_tokens=50,
                cost=0.001,
                reused_tokens=10,
                waste_category="drift" if failed else None,
                messages=[{"role": "user", "content": f"step {i}"}],
                failed=failed,
                error_message="simulated failure" if failed else None,
            )
            storage.save_checkpoint(
                session_id=session_id,
                step_number=i,
                messages=[{"role": "user", "content": f"checkpoint {i}"}],
            )
        session_ids.append(session_id)
    return session_ids


def worker(worker_id, session_ids, iterations, results, lock):
    start = time.time()
    errors = []
    attribution_engine = get_attribution_engine()
    replayer = get_replayer()
    for _ in range(iterations):
        session_id = random.choice(session_ids)
        try:
            attribution_engine.attribute_session(session_id)
        except Exception as e:
            errors.append(f"attribution: {e}")
        try:
            replayer.replay(session_id=session_id, from_step=0, dry_run=True)
        except Exception as e:
            errors.append(f"replay: {e}")
    elapsed = (time.time() - start) * 1000
    with lock:
        results[worker_id] = {"elapsed_ms": elapsed, "errors": errors}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sessions", type=int, default=20)
    parser.add_argument("--steps", type=int, default=10)
    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument("--iterations-per-worker", type=int, default=20)
    args = parser.parse_args()

    print(f"Seeding {args.sessions} sessions x {args.steps} steps...")
    session_ids = seed_sessions(args.sessions, args.steps)
    print("Seed complete.")
    print("-" * 60)

    print(f"Spawning {args.workers} concurrent workers, {args.iterations_per_worker} "
          f"attribute+replay iterations each...")
    print("-" * 60)

    results = {}
    lock = threading.Lock()
    threads = []
    wall_start = time.time()
    for w in range(args.workers):
        t = threading.Thread(
            target=worker,
            args=(w, session_ids, args.iterations_per_worker, results, lock),
        )
        threads.append(t)
        t.start()
    for t in threads:
        t.join()
    wall_elapsed = time.time() - wall_start

    total_errors = 0
    times = []
    for w, r in results.items():
        times.append(r["elapsed_ms"])
        for e in r["errors"]:
            total_errors += 1
            print(f"Worker {w}: ERROR - {e}")

    print(f"All {args.workers} workers completed in {wall_elapsed:.2f}s (wall clock)")
    if times:
        print(f"Avg per-worker time: {sum(times)/len(times):.1f}ms")
        print(f"Slowest worker: {max(times):.1f}ms")
        print(f"Fastest worker: {min(times):.1f}ms")
    print("-" * 60)

    if total_errors == 0:
        print("PASS: No errors under concurrent attribution + replay load.")
    else:
        print(f"FAIL: {total_errors} total errors.")


if __name__ == "__main__":
    main()
