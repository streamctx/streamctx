"""Pytest suite to verify Context Diff works."""

import streamctx


# Step 3 messages (earlier)
step3 = [
    {"role": "system", "content": "You are a helpful assistant. Use Claude only."},
    {"role": "user", "content": "What is Python?"},
    {"role": "assistant", "content": "Python is a programming language."},
]

# Step 7 messages (later - things changed!)
step7 = [
    {"role": "system", "content": "You are a helpful assistant. Use GPT-4 only."},
    {"role": "user", "content": "What is Python?"},
    {"role": "assistant", "content": "Python is a programming language."},
    {"role": "user", "content": "Now explain JavaScript too."},
    {"role": "assistant", "content": "JavaScript runs in the browser."},
]


def test_diff_minor_change_has_low_drift():
    """Comparing identical message sets should show minimal/no drift."""
    diff = streamctx.context_diff(step3, step3, step_a=3, step_b=3)

    assert "summary" in diff
    assert "drift_score" in diff
    assert diff["drift_score"] == 0


def test_diff_significant_drift_detected():
    """Comparing step3 vs step7 should detect drift, added messages, and removed messages."""
    diff = streamctx.context_diff(step3, step7, step_a=3, step_b=7)

    assert "summary" in diff
    assert "drift_score" in diff
    assert "added" in diff
    assert "removed" in diff
    assert "token_delta" in diff

    # step7 has 2 more messages than step3
    assert len(diff["added"]) >= 1
    # drift score should be greater than the identical-message case
    assert diff["drift_score"] > 0


def test_diff_system_prompt_removed_triggers_warning():
    """Removing the system prompt between steps should raise a warning."""
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

    assert "summary" in diff
    assert "warnings" in diff
    assert len(diff["warnings"]) >= 1
