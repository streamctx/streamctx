"""Regression tests for stacked start()/stop() SDK patches.

Multiple LLMTracker instances used to each replace Completions.create and
save the previous wrapper as "original". After start/stop cycles that
produced:

- RecursionError: maximum recursion depth exceeded
- KeyError: 'openai.resources.chat.completions.Completions.create'
- TypeError: create() takes 1 argument but 2 were given

These tests replay the dogfood_concurrency start/stop pattern and then
issue a real (mocked) OpenAI create() — the path that polluted sessions.db.
"""

from __future__ import annotations

import concurrent.futures

import httpx
from openai import OpenAI

from streamctx.tracker import get_tracker


def _mock_openai_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-stack",
            "object": "chat.completion",
            "created": 0,
            "model": "gpt-4o-mini",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 8,
                "completion_tokens": 2,
                "total_tokens": 10,
            },
        },
    )


def _mock_client() -> OpenAI:
    return OpenAI(
        api_key="sk-test",
        http_client=httpx.Client(transport=httpx.MockTransport(_mock_openai_handler)),
    )


def _failed_errors(tracker) -> list[str]:
    sid = tracker.get_session_id()
    if sid is None:
        return []
    rows = tracker.state.storage.get_calls_for_session(sid)
    return [r.get("error_message") or "" for r in rows if r.get("failed")]


def _assert_clean_create(tracker, client=None) -> None:
    client = client or _mock_client()
    client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "ping"}],
    )
    errors = _failed_errors(tracker)
    assert errors == [], f"create() recorded wrapper bugs: {errors}"
    stats = tracker.get_stats()
    assert stats["call_count"] >= 1


def test_two_agents_start_stop_start_does_not_recurse():
    a = get_tracker(agent_id="stack-a")
    b = get_tracker(agent_id="stack-b")
    a.start()
    b.start()
    a.stop()
    b.stop()
    a.start()
    try:
        _assert_clean_create(a)
    finally:
        a.stop()


def test_stop_first_tracker_leaves_second_patch_working():
    a = get_tracker(agent_id="stack-keep-a")
    b = get_tracker(agent_id="stack-keep-b")
    a.start()
    b.start()
    a.stop()
    try:
        _assert_clean_create(b)
    finally:
        b.stop()


def test_wrap_after_stacked_start_stop_has_no_arity_error():
    a = get_tracker(agent_id="stack-wrap-a")
    b = get_tracker(agent_id="stack-wrap-b")
    a.start()
    b.start()
    a.stop()
    b.stop()
    a.start()
    try:
        client = a.wrap(_mock_client())
        _assert_clean_create(a, client)
    finally:
        a.stop()


def test_wrap_only_then_other_agent_start_stop():
    """Instance wrap + later class-patch cycling must not smash create() arity."""
    wrapper = get_tracker(agent_id="stack-wrap-only")
    client = wrapper.wrap(_mock_client())
    other = get_tracker(agent_id="stack-other-start")
    other.start()
    other.stop()
    other.start()
    other.stop()
    try:
        _assert_clean_create(wrapper, client)
    finally:
        wrapper.stop()


def test_dogfood_concurrency_then_create_is_clean():
    """Replay dogfood_concurrency.py (50 workers start/stop) then create()."""

    def worker_task(worker_id: int) -> int:
        tracker = get_tracker(agent_id=f"dogfood_worker_{worker_id}")
        tracker.start()
        tracker.checkpoint()
        tracker.stop()
        return worker_id

    errors: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=50) as executor:
        futures = [executor.submit(worker_task, i) for i in range(50)]
        for future in concurrent.futures.as_completed(futures):
            try:
                future.result()
            except Exception as exc:
                errors.append(str(exc))

    assert errors == [], f"start/stop workers failed: {errors[:3]}"

    tracker = get_tracker(agent_id="dogfood_after_concurrency")
    tracker.start()
    try:
        _assert_clean_create(tracker)
        client = tracker.wrap(_mock_client())
        client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "second"}],
        )
        leftover = _failed_errors(tracker)
        assert leftover == [], f"wrap+create after concurrency failed: {leftover}"
        assert tracker.get_stats()["call_count"] == 2
    finally:
        tracker.stop()
