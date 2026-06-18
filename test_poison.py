"""Test script to verify Context Poison Detector works."""

import streamctx

print("=" * 55)
print("StreamCtx Context Poison Detector Test")
print("=" * 55)

# Test 1: Healthy context
print("\n--- Test 1: Healthy Context ---")
healthy_messages = [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "What is Python?"},
    {"role": "assistant", "content": "Python is a programming language."},
    {"role": "user", "content": "Tell me more."},
    {"role": "assistant", "content": "Python is used for web, data science, and AI."},
]
result = streamctx.scan(healthy_messages)
print(f"Health Score: {result['health_score']}/100")
print(f"Is Poisoned: {result['is_poisoned']}")
print(f"Recommendation: {result['recommendation']}")

# Test 2: Agent stuck in loop (repeated errors)
print("\n--- Test 2: Agent Stuck in Loop ---")
poisoned_messages = [
    {"role": "system", "content": "You are an API assistant."},
    {"role": "user", "content": "Call the payment API."},
    {"role": "assistant", "content": "Error: endpoint not found. Failed to connect."},
    {"role": "user", "content": "Try again."},
    {"role": "assistant", "content": "Error: endpoint not found. Failed again."},
    {"role": "user", "content": "Try once more."},
    {"role": "assistant", "content": "Error: invalid request. Failed to process."},
    {"role": "user", "content": "One more time."},
    {"role": "assistant", "content": "Error: cannot connect. Failed."},
]
result = streamctx.scan(poisoned_messages)
print(f"Health Score: {result['health_score']}/100")
print(f"Is Poisoned: {result['is_poisoned']}")
for w in result['warnings']:
    print(f"{w}")
print(f"Recommendation: {result['recommendation']}")

# Test 3: Contradictory context
print("\n--- Test 3: Contradictory Facts ---")
contradiction_messages = [
    {"role": "system", "content": "The payment feature is enabled and available."},
    {"role": "user", "content": "Process my payment."},
    {"role": "assistant", "content": "Payment processed successfully."},
    {"role": "user", "content": "Check status."},
    {"role": "assistant", "content": "Payment is unavailable. Feature is disabled."},
]
result = streamctx.scan(contradiction_messages)
print(f"Health Score: {result['health_score']}/100")
print(f"Is Poisoned: {result['is_poisoned']}")
for w in result['warnings']:
    print(f"{w}")
print(f"Recommendation: {result['recommendation']}")

print("\n🎉 Context Poison Detector working correctly!")
print("=" * 55)


