
"""
Pytest version of StreamCtx checkpoint system test.
"""


from src.streamctx.tracker import get_tracker



def test_checkpoint_and_resume():
    """Checkpoint should save state, and resume should restore exact messages."""
    tracker = get_tracker()
    tracker.start()
    session_id = tracker.state.session_id
    assert session_id is not None

    tracker = get_tracker()

    tracker.state._last_messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "What is Python?"},
    ]
    tracker.state.step_counter = 1
    tracker.checkpoint()

    tracker.state._last_messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "What is Python?"},
        {"role": "assistant", "content": "Python is a programming language."},
        {"role": "user", "content": "Tell me more."},
    ]
    tracker.state.step_counter = 2
    tracker.checkpoint()

    messages = tracker.resume(session_id)

    assert len(messages) == 4
    assert messages[0]["role"] == "system"
    assert messages[1]["content"] == "What is Python?"
    assert messages[2]["content"] == "Python is a programming language."
    assert messages[3]["content"] == "Tell me more."

    tracker.stop()

def test_resume_unknown_session_returns_empty_or_raises():

    """"Resuming a session that never checkpointed should not silently fabricate data."""
    tracker = get_tracker()
    result = tracker.resume(999999999)
    assert result == [] or result is None


