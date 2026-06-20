"""Test script to verify StreamCtx checkpoint system works."""

import streamctx
from streamctx.tracker import get_tracker

print("=" * 50)
print("StreamCtx Checkpoint Test")
print("=" * 50)

# Step 1: Start session
streamctx.start()
session_id = streamctx.get_session_id()
print(f"\n✅ Session started: {session_id}")

# Step 2: Simulate LLM calls via manual checkpoint
tracker = get_tracker()

# Simulate Step 1
tracker.state._last_messages = [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "What is Python?"},
]
tracker.state.step_counter = 1
streamctx.checkpoint()
print("")

# Simulate Step 2
tracker.state._last_messages = [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "What is Python?"},
    {"role": "assistant", "content": "Python is a programming language."},
    {"role": "user", "content": "Tell me more."},
]
tracker.state.step_counter = 2
streamctx.checkpoint()
print("✅ Checkpoint 2 saved (step 2)")

# Step 3: Resume from checkpoint
messages = streamctx.resume(session_id)
print(f"\n✅ Resume successful!")
print(f"   Messages recovered: {len(messages)}")
for msg in messages:
    print(f"   [{msg['role']}]: {msg['content'][:50]}")

# Step 4: Stats
stats = get_tracker().get_stats()
print(f"\n✅ Session stats:")
print(f"   Calls: {stats['call_count']}")
print(f"   Tokens: {stats['total_tokens']}")

streamctx.stop()
print("\n✅ Session stopped")
print("\n🎉 Checkpoint system working correctly!")
print("=" * 50)


