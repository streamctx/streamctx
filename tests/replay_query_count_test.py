"""Load test / instrumentation for CounterfactualReplayer.replay().

Confirms whether replay() re-queries checkpoint rows from SQLite more than
once per call. It works by monkeypatching _get_all_checkpoint_rows() to
count invocations, then calling replay() in both dry_run and live modes.

Usage:
    python replay_query_count_test.py --checkpoints 500
"""

from __future__ import annotations

import argparse
import time

from streamctx.replay import CounterfactualReplayer, get_replayer
from streamctx.storage import get_storage


def build_session_with_checkpoints(storage, num_checkpoints: int) -> int:
    """Create a session and save num_checkpoints checkpoints to it."""
    session_id = storage.start_session()
    messages = []
    for step in range(1, num_checkpoints + 1):
        messages.append({"role": "user", "content": f"step {step} message"})
        storage.save_checkpoint(session_id, step, messages)
    storage.end_session(session_id)
    return session_id


def fake_llm(messages):
    """Stand-in for a real LLM call — no network, just echoes back."""
    class FakeResponse:
        class Choice:
            class Message:
                content = "fake reply"
            message = Message()
        choices = [Choice()]
    return FakeResponse()


def run_test(num_checkpoints: int) -> None:
    storage = get_storage()
    session_id = build_session_with_checkpoints(storage, num_checkpoints)
    print(f"Session {session_id} built with {num_checkpoints} checkpoints")
    print("-" * 60)

    replayer = get_replayer()

    # Instrument: wrap _get_all_checkpoint_rows to count calls
    call_count = {"n": 0}
    original_method = replayer._get_all_checkpoint_rows

    def counting_wrapper(session_id_arg):
        call_count["n"] += 1
        return original_method(session_id_arg)

    replayer._get_all_checkpoint_rows = counting_wrapper

    # --- Test 1: dry run ---
    call_count["n"] = 0
    start = time.perf_counter()
    result = replayer.replay(
        session_id=session_id,
        from_step=num_checkpoints // 2,
        with_context={"role": "user", "content": "alternate input"},
        dry_run=True,
    )
    elapsed = time.perf_counter() - start
    print(f"DRY RUN:  _get_all_checkpoint_rows() called {call_count['n']}x "
          f"in {elapsed * 1000:.2f}ms")

    # --- Test 2: live replay ---
    call_count["n"] = 0
    start = time.perf_counter()
    result = replayer.replay(
        session_id=session_id,
        from_step=num_checkpoints // 2,
        with_context={"role": "user", "content": "alternate input"},
        dry_run=False,
        llm_fn=fake_llm,
    )
    elapsed = time.perf_counter() - start
    print(f"LIVE RUN: _get_all_checkpoint_rows() called {call_count['n']}x "
          f"in {elapsed * 1000:.2f}ms")
    print("-" * 60)

    if call_count["n"] > 1:
        print(f"CONFIRMED: live replay queries checkpoint rows {call_count['n']}x "
              f"per call — this is the duplicate-query bug.")
    else:
        print("No duplicate query detected in this run.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay engine query-count test")
    parser.add_argument("--checkpoints", type=int, default=500,
                         help="Number of checkpoints in the test session")
    args = parser.parse_args()
    run_test(args.checkpoints)


if __name__ == "__main__":
    main()