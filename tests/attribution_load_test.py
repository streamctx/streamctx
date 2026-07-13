
"""Load test for the Causal Failure Attribution Engine.

Simulates a session with many calls and several failures, then measures
how long attribute_session() takes to attribute all failures.

This specifically targets the N+1 query pattern: before the fix,
attribute_failure() re-queried storage.get_calls_for_session() on every
single failed call. After the fix, attribute_session() loads the calls
once and reuses them.

Usage:
    python attribution_load_test.py --calls 500 --failure-rate 0.1
    python attribution_load_test.py --calls 2000 --failure-rate 0.05
"""

from __future__ import annotations

import argparse
import random
import time

from streamctx.attribution import get_attribution_engine
from streamctx.storage import get_storage


def build_session(storage, num_calls: int, failure_rate: float) -> int:
    """Create a session with num_calls calls, some marked as failed."""
    session_id = storage.start_session()

    for i in range(num_calls):
        failed = random.random() < failure_rate
        storage.record_call(
            session_id=session_id,
            provider="openai",
            model="gpt-4",
            input_tokens=random.randint(50, 500),
            output_tokens=random.randint(20, 200),
            cost=0.01,
            reused_tokens=random.randint(0, 50),
            waste_category=random.choice([None, "repeated system prompt", "repeated user message"]),
            messages=[{"role": "user", "content": f"step {i}"}],
            failed=failed,
            healed=False,
            error_message="simulated failure" if failed else None,
        )

    storage.end_session(session_id)
    return session_id


def main() -> None:
    parser = argparse.ArgumentParser(description="Attribution engine load test")
    parser.add_argument("--calls", type=int, default=500, help="Number of calls in the session")
    parser.add_argument("--failure-rate", type=float, default=0.1, help="Fraction of calls that fail (0-1)")
    args = parser.parse_args()

    storage = get_storage()
    engine = get_attribution_engine()

    print(f"Building session with {args.calls} calls (~{args.failure_rate:.0%} failure rate)...")
    session_id = build_session(storage, args.calls, args.failure_rate)

    stats = storage.get_session_stats(session_id)
    failed_count = sum(
        1 for c in storage.get_calls_for_session(session_id) if c["failed"]
    )
    print(f"Session {session_id} built: {stats['call_count']} calls, {failed_count} failed")
    print("-" * 60)

    start = time.perf_counter()
    results = engine.attribute_session(session_id)
    elapsed = time.perf_counter() - start

    print(f"attribute_session() completed in {elapsed * 1000:.2f}ms")
    print(f"Attributed {len(results)} failures")
    if failed_count > 0:
        print(f"Avg time per failure: {(elapsed * 1000) / failed_count:.3f}ms")
    print("-" * 60)
    print("If this scales roughly linearly with --calls and --failure-rate,")
    print("the fix is working. Quadratic blowup (old N+1 bug) would show")
    print("avg-time-per-failure growing sharply as --calls increases.")


if __name__ == "__main__":
    main()