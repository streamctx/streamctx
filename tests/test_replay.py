"""Tests for streamctx.replay (CounterfactualReplayer)."""

import json
import sqlite3

import pytest

from streamctx.replay import CounterfactualReplayer


class _FakeStorage:
    """Minimal storage stand-in backed by a real SQLite file.

    Mirrors the subset of the real storage API that replay.py relies on:
    ``self.storage._connect()`` must return something usable as a context
    manager whose ``.execute(sql, params).fetchall()`` rows support
    ``dict(row)``.
    """

    def __init__(self, db_path):
        self._db_path = str(db_path)
        conn = sqlite3.connect(self._db_path)
        conn.execute(
            """
            CREATE TABLE checkpoints (
                session_id INTEGER,
                step_number INTEGER,
                messages_json TEXT,
                timestamp TEXT
            )
            """
        )
        conn.commit()
        conn.close()

    def _connect(self):
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def seed(self, session_id, step_number, messages, timestamp="2026-06-27T09:00:00"):
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO checkpoints (session_id, step_number, messages_json, timestamp) "
                "VALUES (?, ?, ?, ?)",
                (session_id, step_number, json.dumps(messages), timestamp),
            )


@pytest.fixture
def storage(tmp_path):
    return _FakeStorage(tmp_path / "test_replay.db")


@pytest.fixture
def replayer(storage):
    return CounterfactualReplayer(storage=storage)


def _msgs(*pairs):
    """Helper: _msgs(("user", "hi"), ("assistant", "hello")) -> list[dict]."""
    return [{"role": r, "content": c} for r, c in pairs]


# ---------------------------------------------------------------------
# list_checkpoints
# ---------------------------------------------------------------------

def test_list_checkpoints_returns_all_steps(storage, replayer):
    storage.seed(8, 5, _msgs(("user", "a"), ("assistant", "b")))
    storage.seed(8, 7, _msgs(("user", "a"), ("assistant", "b"), ("user", "c")))

    checkpoints = replayer.list_checkpoints(8)

    assert [c["step_number"] for c in checkpoints] == [5, 7]
    assert checkpoints[0]["message_count"] == 2
    assert checkpoints[1]["message_count"] == 3


def test_list_checkpoints_empty_session(replayer):
    assert replayer.list_checkpoints(999) == []


# ---------------------------------------------------------------------
# replay() - no checkpoint found
# ---------------------------------------------------------------------

def test_replay_no_checkpoint_found(replayer):
    result = replayer.replay(session_id=8, from_step=7, dry_run=True)

    assert result.original_messages == []
    assert result.counterfactual_messages == []
    assert "No checkpoint found" in result.injection_summary


# ---------------------------------------------------------------------
# replay() - dry run, no injection
# ---------------------------------------------------------------------

def test_replay_dry_run_no_injection_returns_checkpoint_as_is(storage, replayer):
    original = _msgs(("user", "hi"), ("assistant", "hello"))
    storage.seed(8, 7, original)

    result = replayer.replay(session_id=8, from_step=7, with_context=None, dry_run=True)

    assert result.dry_run is True
    assert result.original_messages == original
    assert result.counterfactual_messages == original
    assert "No injection" in result.injection_summary
    assert result.counterfactual_responses == []


# ---------------------------------------------------------------------
# replay() - dry run, append injection (replace_step=False)
# ---------------------------------------------------------------------

def test_replay_dry_run_append_injection(storage, replayer):
    original = _msgs(("user", "hi"), ("assistant", "hello"))
    storage.seed(8, 7, original)

    result = replayer.replay(
        session_id=8,
        from_step=7,
        with_context={"role": "user", "content": "different input"},
        dry_run=True,
        replace_step=False,
    )

    assert result.dry_run is True
    assert len(result.original_messages) == 2
    assert len(result.counterfactual_messages) == 3
    assert result.counterfactual_messages[-1] == {
        "role": "user",
        "content": "different input",
    }
    assert "Appended" in result.injection_summary


# ---------------------------------------------------------------------
# replay() - dry run, replace injection (replace_step=True)
# ---------------------------------------------------------------------

def test_replay_dry_run_replace_last_user_message(storage, replayer):
    original = _msgs(("user", "original input"), ("assistant", "reply"))
    storage.seed(8, 7, original)

    result = replayer.replay(
        session_id=8,
        from_step=7,
        with_context={"role": "user", "content": "different input"},
        dry_run=True,
        replace_step=True,
    )

    assert len(result.counterfactual_messages) == 2
    assert result.counterfactual_messages[0] == {
        "role": "user",
        "content": "different input",
    }
    assert "Replaced last user message" in result.injection_summary


def test_replay_dry_run_replace_no_user_message_appends(storage, replayer):
    original = _msgs(("system", "setup"), ("assistant", "reply"))
    storage.seed(8, 7, original)

    result = replayer.replay(
        session_id=8,
        from_step=7,
        with_context={"role": "user", "content": "different input"},
        dry_run=True,
        replace_step=True,
    )

    assert len(result.counterfactual_messages) == 3
    assert "No user message to replace" in result.injection_summary


# ---------------------------------------------------------------------
# replay() - dry run, list of messages injected
# ---------------------------------------------------------------------

def test_replay_dry_run_injects_list_of_messages(storage, replayer):
    original = _msgs(("user", "hi"))
    storage.seed(8, 7, original)

    injection = _msgs(("user", "alt 1"), ("assistant", "alt reply"))
    result = replayer.replay(
        session_id=8,
        from_step=7,
        with_context=injection,
        dry_run=True,
        replace_step=False,
    )

    assert len(result.counterfactual_messages) == 3
    assert result.counterfactual_messages[1:] == injection


# ---------------------------------------------------------------------
# replay() - closest-step fallback (exact step not found)
# ---------------------------------------------------------------------

def test_replay_falls_back_to_closest_earlier_step(storage, replayer):
    step5_msgs = _msgs(("user", "a"))
    storage.seed(8, 5, step5_msgs)

    result = replayer.replay(session_id=8, from_step=7, dry_run=True)

    assert result.original_messages == step5_msgs


# ---------------------------------------------------------------------
# replay() - live replay requires llm_fn
# ---------------------------------------------------------------------

def test_replay_live_without_llm_fn_raises(storage, replayer):
    storage.seed(8, 7, _msgs(("user", "hi")))

    with pytest.raises(ValueError, match="llm_fn is required"):
        replayer.replay(session_id=8, from_step=7, dry_run=False)


# ---------------------------------------------------------------------
# replay() - live replay, single injected step, no further steps recorded
# ---------------------------------------------------------------------

def test_replay_live_single_step_no_further_history(storage, replayer):
    storage.seed(8, 7, _msgs(("user", "hi")))

    class FakeResponse:
        class _Choice:
            class _Message:
                content = "counterfactual reply"

            message = _Message()

        choices = [_Choice()]

    calls = []

    def fake_llm_fn(messages):
        calls.append(list(messages))
        return FakeResponse()

    result = replayer.replay(
        session_id=8,
        from_step=7,
        with_context={"role": "user", "content": "different input"},
        dry_run=False,
        llm_fn=fake_llm_fn,
    )

    assert result.dry_run is False
    assert result.steps_replayed == 1
    assert len(result.counterfactual_responses) == 1
    assert len(calls) == 1
    # conversation grew by one assistant turn after the injected message
    assert result.counterfactual_messages[-1]["role"] == "assistant"
    assert result.counterfactual_messages[-1]["content"] == "counterfactual reply"


# ---------------------------------------------------------------------
# replay() - live replay across multiple recorded steps (session 8 / step 7
# scenario: 9 original messages -> 10 after injection, replayed forward)
# ---------------------------------------------------------------------

def test_replay_live_multiple_steps(storage, replayer):
    step7_msgs = _msgs(
        ("user", "m1"), ("assistant", "m2"), ("user", "m3"),
        ("assistant", "m4"), ("user", "m5"), ("assistant", "m6"),
        ("user", "m7"), ("assistant", "m8"), ("user", "m9"),
    )
    storage.seed(8, 7, step7_msgs)
    storage.seed(8, 9, step7_msgs + _msgs(("assistant", "m10")))

    class FakeResponse:
        class _Choice:
            class _Message:
                content = "alt response"

            message = _Message()

        choices = [_Choice()]

    call_count = {"n": 0}

    def fake_llm_fn(messages):
        call_count["n"] += 1
        return FakeResponse()

    result = replayer.replay(
        session_id=8,
        from_step=7,
        with_context={"role": "user", "content": "alt input"},
        dry_run=False,
        llm_fn=fake_llm_fn,
    )

    assert len(step7_msgs) == 9
    assert len(result.counterfactual_messages) == 10  # 9 + injected message
    assert result.steps_replayed == 1  # only step 9 is > from_step 7
    assert call_count["n"] == 1


# ---------------------------------------------------------------------
# replay() - live replay stops and records error on llm_fn exception
# ---------------------------------------------------------------------

def test_replay_live_records_error_on_exception(storage, replayer):
    storage.seed(8, 7, _msgs(("user", "hi")))

    def failing_llm_fn(messages):
        raise RuntimeError("API down")

    result = replayer.replay(
        session_id=8,
        from_step=7,
        with_context={"role": "user", "content": "alt"},
        dry_run=False,
        llm_fn=failing_llm_fn,
    )

    assert len(result.counterfactual_responses) == 1
    assert "error" in result.counterfactual_responses[0]
    assert "API down" in result.counterfactual_responses[0]["error"]


# ---------------------------------------------------------------------
# diff_replay()
# ---------------------------------------------------------------------

def test_diff_replay_reports_added_and_unchanged(storage, replayer):
    original = _msgs(("user", "hi"), ("assistant", "hello"))
    storage.seed(8, 7, original)

    diff = replayer.diff_replay(
        session_id=8,
        from_step=7,
        with_context={"role": "user", "content": "different input"},
    )

    assert diff["session_id"] == 8
    assert diff["from_step"] == 7
    assert diff["unchanged_count"] == 2
    assert len(diff["added_messages"]) == 1
    assert diff["added_messages"][0]["content"] == "different input"
    assert diff["removed_messages"] == []

