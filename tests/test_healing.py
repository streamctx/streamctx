"""Pytest suite to verify StreamCtx self-healing works."""

import streamctx
from streamctx.healer import SelfHealingEngine


valid_messages = [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "What is Python?"},
    {"role": "assistant", "content": "Python is a programming language."},
]

failed_messages = [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "What is Python?"},
    {"role": "assistant", "content": "Python is a programming language."},
    {"role": "user", "content": "Tell me more about Python."},
]


class FakeResponse:
    content = "Python is a programming language."


def test_healer_records_success_and_can_heal():
    healer = SelfHealingEngine()
    healer.record_success(valid_messages, FakeResponse())

    assert healer.can_heal() is True


def test_healer_records_failure():
    healer = SelfHealingEngine()
    healer.record_success(valid_messages, FakeResponse())
    healer.record_failure()

    stats = healer.get_stats()
    assert stats["failure_count"] >= 1


def test_healer_generates_recovery_messages():
    healer = SelfHealingEngine()
    healer.record_success(valid_messages, FakeResponse())
    healer.record_failure()

    recovery = healer.get_recovery_messages(failed_messages)

    assert isinstance(recovery, list)
    assert len(recovery) > 0
    for msg in recovery:
        assert "role" in msg
        assert "content" in msg


def test_healer_stats_structure():
    healer = SelfHealingEngine()
    healer.record_success(valid_messages, FakeResponse())
    healer.record_failure()
    stats = healer.get_stats()

    assert "failure_count" in stats
    assert "recovery_count" in stats
    assert "has_valid_context" in stats


def test_streamctx_healing_stats_integration():
    """Full integration: streamctx.start()/stop() should expose healing_stats()."""
    streamctx.start()
    h_stats = streamctx.healing_stats()
    streamctx.stop()

    assert h_stats is not None


