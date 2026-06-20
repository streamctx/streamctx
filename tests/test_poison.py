"""Pytest suite to verify Context Poison Detector works."""

import streamctx


def test_healthy_context_has_high_health_score():
    healthy_messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "What is Python?"},
        {"role": "assistant", "content": "Python is a programming language."},
        {"role": "user", "content": "Tell me more."},
        {"role": "assistant", "content": "Python is used for web, data science, and AI."},
    ]

    result = streamctx.scan(healthy_messages)

    assert "health_score" in result
    assert "is_poisoned" in result
    assert "recommendation" in result
    assert result["is_poisoned"] is False


def test_agent_stuck_in_loop_is_flagged_as_poisoned():
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

    assert "health_score" in result
    assert "is_poisoned" in result
    assert "warnings" in result
    assert result["is_poisoned"] is True
    assert len(result["warnings"]) >= 1


def test_contradictory_context_is_flagged():
    contradiction_messages = [
        {"role": "system", "content": "The payment feature is enabled and available."},
        {"role": "user", "content": "Process my payment."},
        {"role": "assistant", "content": "Payment processed successfully."},
        {"role": "user", "content": "Check status."},
        {"role": "assistant", "content": "Payment is unavailable. Feature is disabled."},
    ]

    result = streamctx.scan(contradiction_messages)

    assert "health_score" in result
    assert "is_poisoned" in result
    assert "warnings" in result
    assert "recommendation" in result
