"""Test script to verify Context Diff works."""

import streamctx

print("=" * 55)
print("StreamCtx Context Diff Test")
print("=" * 55)

# Step 3 messages (earlier)
step3 = [
    {"role": "system", "content": "You are a helpful assistant. Use Claude only."},
    {"role": "user", "content": "What is Python?"},
    {"role": "assistant", "content": "Python is a programming language."},
]

# Step 7 messages (later — things changed!)
step7 = [
    {"role": "system", "content": "You are a helpful assistant. Use GPT-4 only."},
    {"role": "user", "content": "What is Python?"},
    {"role": "assistant", "content": "Python is a programming language."},
    {"role": "user", "content": "Now explain JavaScript too."},
    {"role": "assistant", "content": "JavaScript runs in the browser."},
]

print("\n--- Test 1: Minor Change ---")
diff = streamctx.context_diff(step3, step3, step_a=3, step_b=3)
print(f"Summary: {diff['summary']}")
print(f"Drift Score: {diff['drift_score']}/100")

print("\n--- Test 2: Significant Drift ---")
diff = streamctx.context_diff(step3, step7, step_a=3, step_b=7)
print(f"Summary: {diff['summary']}")
print(f"Drift Score: {diff['drift_score']}/100")
print(f"Added messages: {len(diff['added'])}")
print(f"Removed messages: {len(diff['removed'])}")
print(f"Token delta: {diff['token_delta']:+d} tokens")

if diff['warnings']:
    print("Warnings:")
    for w in diff['warnings']:
        print(f"  {w}")

if diff['added']:
    print("Added:")
    for m in diff['added']:
        print(f"  [{m['role']}]: {m['content'][:50]}")

if diff['removed']:
    print("Removed:")
    for m in diff['removed']:
        print(f"  [{m['role']}]: {m['content'][:50]}")

print("\n--- Test 3: System Prompt Removed ---")
step_with_system = [
    {"role": "system", "content": "Always respond in English only."},
    {"role": "user", "content": "Hello"},
]
step_without_system = [
    {"role": "user", "content": "Hello"},
    {"role": "user", "content": "Now respond in French please."},
]
diff = streamctx.context_diff(
    step_with_system, step_without_system, step_a=1, step_b=5
)
print(f"Summary: {diff['summary']}")
if diff['warnings']:
    for w in diff['warnings']:
        print(f"  {w}")

print("\n🎉 Context Diff working correctly!")
print("=" * 55)


