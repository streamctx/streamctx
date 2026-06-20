"""Pytest suite to verify real-time step streaming (SessionStorage + LLMTracker)."""

import pytest

from streamctx.storage import SessionStorage
from streamctx.tracker import get_tracker


@pytest.fixture
def storage(tmp_path):
    """Fresh SessionStorage backed by a temp SQLite file for each test.

    Uses pytest's built-in tmp_path fixture, which handles Windows-safe
    cleanup automatically (avoids PermissionError on file handle release).
    """
    db_path = tmp_path / "test_sessions.db"
    return SessionStorage(db_path=db_path)



def test_start_and_end_session(storage):
    session_id = storage.start_session()

    assert isinstance(session_id, int)
    assert session_id > 0

    # Should not raise
    storage.end_session(session_id)


def test_record_call_streams_to_db(storage):
    """Every LLM call should be written to the calls table immediately (real-time streaming)."""
    session_id = storage.start_session()

    storage.record_call(
        session_id=session_id,
        provider="openai",
        model="gpt-4",
        input_tokens=100,
        output_tokens=50,
        cost=0.01,
        reused_tokens=10,
        waste_category=None,
        messages=[{"role": "user", "content": "Hello"}],
    )

    stats = storage.get_session_stats(session_id)

    assert stats["call_count"] == 1
    assert stats["input_tokens"] == 100
    assert stats["output_tokens"] == 50
    assert stats["total_tokens"] == 150
    assert stats["reused_tokens"] == 10


def test_record_call_accumulates_across_multiple_calls(storage):
    session_id = storage.start_session()

    for _ in range(3):
        storage.record_call(
            session_id=session_id,
            provider="openai",
            model="gpt-4",
            input_tokens=100,
            output_tokens=50,
            cost=0.01,
            reused_tokens=0,
            waste_category=None,
            messages=[{"role": "user", "content": "Hi"}],
        )

    stats = storage.get_session_stats(session_id)

    assert stats["call_count"] == 3
    assert stats["input_tokens"] == 300
    assert stats["output_tokens"] == 150
    assert abs(stats["total_cost"] - 0.03) < 1e-9


def test_save_checkpoint_per_step(storage):
    """Auto-checkpoint per step: every checkpoint write should be retrievable by step number."""
    session_id = storage.start_session()
    messages_step1 = [{"role": "user", "content": "step 1"}]
    messages_step2 = [
        {"role": "user", "content": "step 1"},
        {"role": "assistant", "content": "response 1"},
        {"role": "user", "content": "step 2"},
    ]

    storage.save_checkpoint(session_id, step_number=1, messages=messages_step1)
    storage.save_checkpoint(session_id, step_number=2, messages=messages_step2)

    latest = storage.get_latest_checkpoint(session_id)

    assert latest is not None
    assert latest["step_number"] == 2
    assert latest["messages"] == messages_step2


def test_resume_from_checkpoint_returns_latest_messages(storage):
    session_id = storage.start_session()
    messages = [{"role": "user", "content": "resume me"}]

    storage.save_checkpoint(session_id, step_number=1, messages=messages)
    resumed = storage.resume_from_checkpoint(session_id)

    assert resumed == messages


def test_resume_from_checkpoint_no_checkpoint_returns_empty(storage):
    session_id = storage.start_session()

    resumed = storage.resume_from_checkpoint(session_id)

    assert resumed == []


def test_get_session_stats_tracks_biggest_waste(storage):
    session_id = storage.start_session()

    storage.record_call(
        session_id=session_id,
        provider="openai",
        model="gpt-4",
        input_tokens=50,
        output_tokens=20,
        cost=0.005,
        reused_tokens=5,
        waste_category="repeated system prompt",
        messages=[{"role": "system", "content": "You are helpful."}],
    )
    storage.record_call(
        session_id=session_id,
        provider="openai",
        model="gpt-4",
        input_tokens=50,
        output_tokens=20,
        cost=0.005,
        reused_tokens=5,
        waste_category="repeated system prompt",
        messages=[{"role": "system", "content": "You are helpful."}],
    )

    stats = storage.get_session_stats(session_id)

    assert stats["biggest_waste"] == "repeated system prompt"


def test_tracker_start_stop_creates_and_ends_session():
    """Full integration: LLMTracker.start()/stop() should create a real streamed session."""
    tracker = get_tracker()

    tracker.start()
    session_id = tracker.get_session_id()

    assert session_id is not None

    tracker.stop()
    # After stop, the active session should be cleared/ended without error
    assert tracker.state.active is False
