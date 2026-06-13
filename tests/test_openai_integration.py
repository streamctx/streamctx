"""Integration tests for streamctx with the OpenAI Python SDK."""

from __future__ import annotations

import os
import unittest

import httpx
import streamctx
from openai import OpenAI
from streamctx.tracker import get_tracker


def _mock_openai_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 0,
            "model": "gpt-4o-mini",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "Hello from streamctx test!",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 18,
                "completion_tokens": 6,
                "total_tokens": 24,
            },
        },
    )


def _mock_client() -> OpenAI:
    return OpenAI(
        api_key="sk-test",
        http_client=httpx.Client(transport=httpx.MockTransport(_mock_openai_handler)),
    )


class TestOpenAIIntegration(unittest.TestCase):
    def tearDown(self) -> None:
        streamctx.stop()
        tracker = get_tracker()
        tracker.state.auto_reported = False
        tracker.state.call_count = 0
        tracker.state.active = False
        tracker.state.session_id = None
        tracker.state._wrapped_clients.clear()
        tracker.diff = tracker.diff.__class__()

    def test_start_tracks_openai_calls(self) -> None:
        streamctx.start()
        client = _mock_client()
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are helpful."},
                {"role": "user", "content": "Say hello"},
            ],
        )

        self.assertEqual(response.choices[0].message.content, "Hello from streamctx test!")
        stats = get_tracker().get_stats()
        self.assertEqual(stats["call_count"], 1)
        self.assertEqual(stats["total_tokens"], 24)
        self.assertGreater(stats["total_cost"], 0)

    def test_wrap_without_double_counting(self) -> None:
        streamctx.start()
        client = streamctx.wrap(_mock_client())
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hi"},
        ]
        client.chat.completions.create(model="gpt-4o-mini", messages=messages)
        client.chat.completions.create(model="gpt-4o-mini", messages=messages)

        stats = get_tracker().get_stats()
        self.assertEqual(stats["call_count"], 2)
        self.assertEqual(stats["total_tokens"], 48)
        self.assertGreater(stats["reused_tokens"], 0)

    def test_wrap_only_tracks_client(self) -> None:
        client = streamctx.wrap(_mock_client())
        client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "Hello"}],
        )

        stats = get_tracker().get_stats()
        self.assertEqual(stats["call_count"], 1)
        self.assertEqual(stats["total_tokens"], 24)

    def test_report_and_stop(self) -> None:
        streamctx.start()
        _mock_client().chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "Hello"}],
        )
        streamctx.report()
        streamctx.stop()

        self.assertFalse(get_tracker().state.active)


@unittest.skipUnless(os.environ.get("OPENAI_API_KEY"), "OPENAI_API_KEY not set")
class TestOpenAILive(unittest.TestCase):
    def tearDown(self) -> None:
        streamctx.stop()

    def test_real_openai_call(self) -> None:
        streamctx.start()
        client = OpenAI()
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "Reply with exactly: pong"}],
            max_tokens=5,
        )
        content = response.choices[0].message.content or ""
        self.assertIn("pong", content.lower())

        stats = get_tracker().get_stats()
        self.assertEqual(stats["call_count"], 1)
        self.assertGreater(stats["total_tokens"], 0)
        self.assertGreater(stats["total_cost"], 0)
        streamctx.report()


if __name__ == "__main__":
    unittest.main(verbosity=2)
