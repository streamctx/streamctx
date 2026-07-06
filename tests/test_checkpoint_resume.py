"""
Test: Checkpoint + Resume
==========================
Goal: Verify that StreamCtx correctly checkpoints session state after
each step, and that resume() restores EXACTLY where the session left
off — not from scratch, not skipping steps.

How this test works:
1. Run a 5-step session, checkpointing after each step.
2. After step 3, simulate a "crash" (just stop the script — no special
   cleanup, no graceful shutdown).
3. In a SEPARATE run, call streamctx's resume() with the same session_id
   and verify:
   a. The returned messages match what was saved after step 3 (not
      step 1, not step 5 — exactly step 3).
   b. The step_counter is correctly at 3, so the next real call would
      be step 4.

Run this file TWICE:
  python test_checkpoint_resume.py --phase 1   (runs steps 1-3, then exits)
  python test_checkpoint_resume.py --phase 2 --session-id <ID>   (resumes)

The session ID from phase 1 will be printed — pass it into phase 2.
"""

import sys
import argparse
import streamctx


def run_phase_1():
    """Run steps 1-3, then simulate a crash (just stop)."""
    print("=" * 60)
    print("PHASE 1: Running steps 1-3, then simulating a crash")
    print("=" * 60)

    streamctx.start()
    tracker = streamctx.get_tracker()
    session_id = tracker.get_session_id()
    print(f"\n✅ Session started: {session_id}")
    print(f"   SAVE THIS ID for phase 2: {session_id}\n")

    conversation = [
        {"role": "system", "content": "You are a test assistant for checkpoint verification."}
    ]

    fake_steps = [
        "Step 1: This is the first message in the conversation.",
        "Step 2: This is the second message, building on the first.",
        "Step 3: This is the third message — the LAST one before the crash.",
    ]

    for i, step_text in enumerate(fake_steps, start=1):
        conversation.append({"role": "user", "content": step_text})
        conversation.append({"role": "assistant", "content": f"Acknowledged step {i}."})

        # Manually trigger a checkpoint (simulating what _intercept_call does
        # after every real LLM call) — must increment step_counter ourselves
        # since we're bypassing the real _intercept_call flow.
        with tracker.state._lock:
            tracker.state.step_counter += 1
            tracker.state._last_messages = list(conversation)
        tracker.checkpoint()

        print(f"✅ Step {i} checkpointed. Step counter: {tracker.state.step_counter}")

    print("\n💥 Simulating crash now — process will exit without cleanup.")
    print(f"   Run phase 2 with: python test_checkpoint_resume.py --phase 2 --session-id {session_id}")

    # Deliberately do NOT call tracker.stop() — simulating an ungraceful crash
    sys.exit(0)


def run_phase_2(session_id: int):
    """Resume from the given session_id and verify state."""
    print("=" * 60)
    print(f"PHASE 2: Resuming session {session_id}")
    print("=" * 60)

    tracker = streamctx.get_tracker()
    resumed_messages = tracker.resume(session_id)

    print(f"\n📋 Resumed {len(resumed_messages)} messages:")
    for m in resumed_messages:
        preview = m["content"][:60].replace("\n", " ")
        print(f"   [{m['role']}] {preview}")

    # --- Verification checks ---
    print("\n" + "-" * 60)
    print("VERIFICATION")
    print("-" * 60)

    checks_passed = 0
    checks_total = 3

    # Check 1: Should have system + 3 steps * 2 messages (user+assistant) = 7 messages
    expected_count = 1 + (3 * 2)
    if len(resumed_messages) == expected_count:
        print(f"✅ Message count correct: {len(resumed_messages)} (expected {expected_count})")
        checks_passed += 1
    else:
        print(f"❌ Message count WRONG: got {len(resumed_messages)}, expected {expected_count}")

    # Check 2: Last message should mention "Step 3" (not step 1, not missing)
    last_user_msgs = [m for m in resumed_messages if m["role"] == "user"]
    if last_user_msgs and "Step 3" in last_user_msgs[-1]["content"]:
        print(f"✅ Last checkpoint is from Step 3 (correct resume point)")
        checks_passed += 1
    else:
        print(f"❌ Last checkpoint is NOT from Step 3 — resume point is wrong!")
        if last_user_msgs:
            print(f"   Last user message was: {last_user_msgs[-1]['content'][:80]}")

    # Check 3: step_counter should be 3
    if tracker.state.step_counter == 3:
        print(f"✅ step_counter correctly at 3")
        checks_passed += 1
    else:
        print(f"❌ step_counter is {tracker.state.step_counter}, expected 3")

    print("-" * 60)
    print(f"RESULT: {checks_passed}/{checks_total} checks passed")
    print("-" * 60)

    if checks_passed == checks_total:
        print("\n🎉 Checkpoint + Resume test PASSED")
    else:
        print("\n⚠️  Checkpoint + Resume test FAILED — see ❌ above")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", type=int, required=True, choices=[1, 2])
    parser.add_argument("--session-id", type=int, default=None)
    args = parser.parse_args()

    if args.phase == 1:
        run_phase_1()
    else:
        if args.session_id is None:
            print("❌ --session-id is required for phase 2")
            sys.exit(1)
        run_phase_2(args.session_id)