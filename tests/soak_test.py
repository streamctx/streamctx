
"""Soak test for StreamCtx — sustained load over several hours.

Runs continuous session start/checkpoint/resume cycles and monitors
memory usage over time to catch leaks that short-duration tests miss.
"""

from __future__ import annotations

import gc
import os
import time
import tracemalloc
from datetime import datetime, timedelta

import psutil

from streamctx.tracker import get_tracker

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DURATION_HOURS = 3          # change to 2 or 4 as needed
CYCLE_SLEEP_SECONDS = 2     # pause between cycles (avoid hammering disk)
LOG_EVERY_N_CYCLES = 50     # print/log memory every N cycles
LOG_FILE = "soak_test_results.log"

process = psutil.Process(os.getpid())


def log(msg: str) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def run_cycle(cycle_num: int) -> None:
    """One realistic unit of work: start tracker, checkpoint, resume."""
    agent_id = f"soak-agent-{cycle_num % 10}"  # rotate across 10 agents
    tracker = get_tracker(agent_id)
    tracker.start()
    tracker.checkpoint()

    session_id = tracker.get_stats().get("session_id")
    if session_id:
        tracker.resume(session_id)


def main() -> None:
    tracemalloc.start()
    start_time = datetime.now()
    end_time = start_time + timedelta(hours=DURATION_HOURS)

    log(f"=== Soak test started: {DURATION_HOURS}h duration ===")
    log(f"Baseline memory: {process.memory_info().rss / 1024 / 1024:.2f} MB")

    cycle = 0
    errors = 0

    try:
        while datetime.now() < end_time:
            cycle += 1
            try:
                run_cycle(cycle)
            except Exception as e:
                errors += 1
                log(f"ERROR at cycle {cycle}: {type(e).__name__}: {e}")

            if cycle % LOG_EVERY_N_CYCLES == 0:
                gc.collect()
                mem_mb = process.memory_info().rss / 1024 / 1024
                current, peak = tracemalloc.get_traced_memory()
                remaining = end_time - datetime.now()
                log(
                    f"Cycle {cycle} | RSS: {mem_mb:.2f} MB | "
                    f"Traced: {current / 1024 / 1024:.2f} MB "
                    f"(peak {peak / 1024 / 1024:.2f} MB) | "
                    f"Errors: {errors} | Remaining: {remaining}"
                )

            time.sleep(CYCLE_SLEEP_SECONDS)

    except KeyboardInterrupt:
        log("Soak test interrupted by user.")

    finally:
        gc.collect()
        final_mem = process.memory_info().rss / 1024 / 1024
        log(f"=== Soak test ended ===")
        log(f"Total cycles: {cycle} | Total errors: {errors}")
        log(f"Final memory: {final_mem:.2f} MB")
        tracemalloc.stop()


if __name__ == "__main__":
    main()
