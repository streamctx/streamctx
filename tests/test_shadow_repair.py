"""Tests for the passive shadow-repair monitor."""

from __future__ import annotations

import json

import pytest

from streamctx.healer import SelfHealingEngine
from streamctx.shadow import should_shadow_repair, wait_for_shadow_repair
from streamctx.storage import SessionStorage


@pytest.fixture
def storage(tmp_path, monkeypatch):
    monkeypatch.setenv("STREAMCTX_SHADOW_REPAIR_SYNC", "1")
    monkeypatch.setenv("STREAMCTX_SHADOW_REPAIR", "1")
    return SessionStorage(db_path=tmp_path / "shadow.db")


def _record(storage, session_id, *, failed=False, error_message=None, content="ok"):
    storage.record_call(
        session_id=session_id,
        provider="openai",
        model="gpt-4o-mini",
        input_tokens=20,
        output_tokens=8,
        cost=0.0,
        reused_tokens=0,
        waste_category=None,
        messages=[
            {"role": "system", "content": "Answer using the briefing only."},
            {"role": "user", "content": "What is Q3 revenue?"},
            {"role": "assistant", "content": content},
        ],
        failed=failed,
        error_message=error_message,
    )


def test_should_shadow_only_empty_content_errors():
    assert should_shadow_repair(None) is True
    assert should_shadow_repair("") is True
    assert should_shadow_repair("   ") is True
    assert should_shadow_repair("simulated failure") is False
    assert should_shadow_repair("Error code: 401 - Unauthorized") is False
    assert should_shadow_repair("maximum recursion depth exceeded") is False


def test_record_call_empty_error_writes_shadow_log(storage):
    session_id = storage.start_session()
    _record(storage, session_id, content="$12.4 million")
    _record(storage, session_id, failed=True, error_message=None, content="$47.3 million")

    logs = storage.get_shadow_repair_log()
    assert len(logs) == 1
    row = logs[0]
    assert row["session_id"] == session_id
    assert row["failed_call_id"] > 0
    assert row["timestamp"]
    assert row["dominant_signal"] in {"drift", "compression", "recency", None}
    assert row["attribution_reason"]
    candidate = json.loads(row["fix_candidate"]) if row["fix_candidate"] else {}
    assert candidate == {} or isinstance(candidate, (dict, list))


def test_record_call_blank_error_writes_shadow_log(storage):
    session_id = storage.start_session()
    _record(storage, session_id, failed=True, error_message="  ")
    assert len(storage.get_shadow_repair_log()) == 1


def test_exception_style_failure_does_not_shadow(storage):
    session_id = storage.start_session()
    _record(
        storage,
        session_id,
        failed=True,
        error_message="maximum recursion depth exceeded",
    )
    _record(
        storage,
        session_id,
        failed=True,
        error_message="create() takes 1 argument but 2 were given",
    )
    _record(
        storage,
        session_id,
        failed=True,
        error_message="Error code: 429 - Too Many Requests",
    )
    assert storage.get_shadow_repair_log() == []


def test_success_record_does_not_shadow(storage):
    session_id = storage.start_session()
    _record(storage, session_id, failed=False, error_message=None)
    assert storage.get_shadow_repair_log() == []


def test_healer_record_failure_without_ids_is_noop(storage):
    healer = SelfHealingEngine()
    healer.record_failure()
    healer.record_failure(error_message=None)
    assert healer.get_stats()["failure_count"] == 2
    assert storage.get_shadow_repair_log() == []


def test_healer_record_failure_with_ids_shadows(storage):
    session_id = storage.start_session()
    _record(storage, session_id, content="briefing fact")
    storage.record_call(
        session_id=session_id,
        provider="openai",
        model="gpt-4o-mini",
        input_tokens=10,
        output_tokens=4,
        cost=0.0,
        reused_tokens=0,
        waste_category=None,
        messages=[{"role": "user", "content": "repeat the briefing fact"}],
        failed=True,
        error_message="simulated failure",
    )
    calls = storage.get_calls_for_session(session_id)
    failed_id = calls[-1]["id"]
    assert storage.get_shadow_repair_log() == []

    healer = SelfHealingEngine()
    healer.record_failure(
        error_message=None,
        session_id=session_id,
        failed_call_id=failed_id,
        storage=storage,
    )
    logs = storage.get_shadow_repair_log()
    assert len(logs) == 1
    assert logs[0]["failed_call_id"] == failed_id


def test_shadow_disabled_env(tmp_path, monkeypatch):
    monkeypatch.setenv("STREAMCTX_SHADOW_REPAIR", "0")
    monkeypatch.setenv("STREAMCTX_SHADOW_REPAIR_SYNC", "1")
    storage = SessionStorage(db_path=tmp_path / "disabled.db")
    session_id = storage.start_session()
    _record(storage, session_id, failed=True, error_message=None)
    assert storage.get_shadow_repair_log() == []


def test_background_thread_does_not_raise(tmp_path, monkeypatch):
    monkeypatch.setenv("STREAMCTX_SHADOW_REPAIR", "1")
    monkeypatch.delenv("STREAMCTX_SHADOW_REPAIR_SYNC", raising=False)
    storage = SessionStorage(db_path=tmp_path / "async.db")
    session_id = storage.start_session()
    _record(storage, session_id, failed=True, error_message=None)
    wait_for_shadow_repair(timeout=10)
    assert len(storage.get_shadow_repair_log()) == 1
