"""Edge-case tests for Checkpoint/Resume — simulating network cuts and crash recovery."""

import threading
import pytest
from streamctx.storage import SessionStorage


@pytest.fixture
def storage(tmp_path):
    """Fresh SQLite storage per test, using tmp_path (avoids Windows file-lock issues)."""
    db_path = tmp_path / "test_sessions.db"
    return SessionStorage(db_path=db_path)


def test_checkpoint_survives_simulated_network_cut(storage):
    """
    Simulate: checkpoint saved successfully, then the NEXT call
    fails due to a network cut. The already-saved checkpoint must
    remain intact and resumable.
    """
    session_id = storage.start_session()
    messages_step1 = [
        {"role": "user", "content": "Step 1 message"},
    ]
    storage.save_checkpoint(session_id, 1, messages_step1)

    # Simulate network cut: the call that would produce step 2 never completes,
    # so no save_checkpoint(step=2) ever happens.
    try:
        raise ConnectionError("Simulated network cut")
    except ConnectionError:
        pass  # tracker would catch this, record failure, and re-raise

    checkpoint = storage.get_latest_checkpoint(session_id)
    assert checkpoint is not None
    assert checkpoint["step_number"] == 1
    assert checkpoint["messages"] == messages_step1


def test_resume_after_simulated_crash_returns_last_checkpoint(storage):
    """Simulate a full process crash: new storage instance, same DB file, should still resume."""
    session_id = storage.start_session()
    messages = [{"role": "user", "content": "Before crash"}]
    storage.save_checkpoint(session_id, 1, messages)

    # Simulate process restart: brand new SessionStorage instance, same db_path
    storage_after_restart = SessionStorage(db_path=storage.db_path)
    resumed = storage_after_restart.resume_from_checkpoint(session_id)

    assert resumed == messages


def test_multiple_checkpoints_resume_returns_most_recent(storage):
    """When multiple checkpoints exist, resume must return the LATEST, not the first."""
    session_id = storage.start_session()
    storage.save_checkpoint(session_id, 1, [{"role": "user", "content": "old"}])
    storage.save_checkpoint(session_id, 2, [{"role": "user", "content": "newer"}])
    storage.save_checkpoint(session_id, 3, [{"role": "user", "content": "newest"}])

    resumed = storage.resume_from_checkpoint(session_id)
    assert resumed == [{"role": "user", "content": "newest"}]


def test_resume_with_no_checkpoints_returns_empty_list(storage):
    """A session with zero checkpoints should resume to an empty list, not crash."""
    session_id = storage.start_session()
    resumed = storage.resume_from_checkpoint(session_id)
    assert resumed == []


def test_resume_with_nonexistent_session_id_does_not_crash(storage):
    """Resuming a session_id that was never created should not raise."""
    resumed = storage.resume_from_checkpoint(999999)
    assert resumed == []


def test_checkpoint_persists_after_session_end(storage):
    """Ending a session should not delete or invalidate its checkpoints."""
    session_id = storage.start_session()
    messages = [{"role": "user", "content": "Final message"}]
    storage.save_checkpoint(session_id, 1, messages)
    storage.end_session(session_id)

    resumed = storage.resume_from_checkpoint(session_id)
    assert resumed == messages


def test_checkpoint_survives_unicode_and_special_characters(storage):
    """Checkpoint messages with unicode, quotes, and newlines must survive JSON roundtrip."""
    session_id = storage.start_session()
    messages = [
        {"role": "user", "content": 'Special chars: "quotes", \n newline, emoji 🚀, gujarati ગુજરાતી'},
    ]
    storage.save_checkpoint(session_id, 1, messages)

    resumed = storage.resume_from_checkpoint(session_id)
    assert resumed == messages


def test_concurrent_checkpoint_writes_do_not_crash(storage):
    """Multiple threads saving checkpoints concurrently should not crash
    (SessionStorage uses an internal lock)."""
    session_id = storage.start_session()
    errors = []

    def save_checkpoint_worker(step: int):
        try:
            storage.save_checkpoint(
                session_id, step, [{"role": "user", "content": f"step {step}"}]
            )
        except Exception as e:
            errors.append(e)

    threads = [
        threading.Thread(target=save_checkpoint_worker, args=(i,))
        for i in range(1, 11)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    # After all writes, resume should return SOME valid checkpoint (highest step_number wins)
    resumed = storage.resume_from_checkpoint(session_id)
    assert resumed is not None
    assert len(resumed) == 1


def test_different_sessions_checkpoints_are_isolated(storage):
    """Checkpoints from one session must never leak into another session's resume."""
    session_a = storage.start_session()
    session_b = storage.start_session()

    storage.save_checkpoint(session_a, 1, [{"role": "user", "content": "session A data"}])
    storage.save_checkpoint(session_b, 1, [{"role": "user", "content": "session B data"}])

    resumed_a = storage.resume_from_checkpoint(session_a)
    resumed_b = storage.resume_from_checkpoint(session_b)

    assert resumed_a == [{"role": "user", "content": "session A data"}]
    assert resumed_b == [{"role": "user", "content": "session B data"}]


def test_get_latest_checkpoint_returns_none_for_empty_session(storage):
    """get_latest_checkpoint (lower-level than resume_from_checkpoint) should return None, not crash."""
    session_id = storage.start_session()
    result = storage.get_latest_checkpoint(session_id)
    assert result is None


