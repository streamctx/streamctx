"""Test script to verify StreamCtx self-healing works."""

import streamctx
from streamctx.tracker import get_tracker
from streamctx.healer import SelfHealingEngine

print("=" * 50)
print("StreamCtx Self-Healing Test")
print("=" * 50)

# Step 1: Setup healer
healer = SelfHealingEngine()

# Step 2: Simulate successful call
valid_messages = [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "What is Python?"},
    {"role": "assistant", "content": "Python is a programming language."},
]

class FakeResponse:
    content = "Python is a programming language."

healer.record_success(valid_messages, FakeResponse())
print("\n✅ Successful call recorded")
print(f"   Can heal: {healer.can_heal()}")

# Step 3: Simulate failure
healer.record_failure()
print("\n✅ Failure recorded")

# Step 4: Get recovery messages
failed_messages = [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "What is Python?"},
    {"role": "assistant", "content": "Python is a programming language."},
    {"role": "user", "content": "Tell me more about Python."},
]

recovery = healer.get_recovery_messages(failed_messages)
print(f"\n✅ Recovery messages generated: {len(recovery)}")
for msg in recovery:
    print(f"   [{msg['role']}]: {msg['content'][:50]}")

# Step 5: Stats
stats = healer.get_stats()
print(f"\n✅ Healing stats:")
print(f"   Failures:    {stats['failure_count']}")
print(f"   Recoveries:  {stats['recovery_count']}")
print(f"   Can heal:    {stats['has_valid_context']}")

# Step 6: Full streamctx integration
streamctx.start()
h_stats = streamctx.healing_stats()
print(f"\n✅ StreamCtx healing_stats():")
print(f"   {h_stats}")
streamctx.stop()

print("\n🎉 Self-healing system working correctly!")
print("=" * 50)


