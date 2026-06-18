"""
streamctx — zero-config LLM call tracing with context reuse insights.

Usage::

    import streamctx
    streamctx.start()
"""

from __future__ import annotations

from typing import Any

from .reporter import print_auto_summary, print_report
from .tracker import get_tracker

__version__ = "0.1.0"
__all__ = [
    "start", "wrap", "report", "stop",
    "checkpoint", "resume", "get_session_id",
    "__version__",
]


def start() -> None:
    """Enable tracing, monkeypatch OpenAI/Anthropic SDKs, and start token tracking."""
    get_tracker().start()


def wrap(client: Any) -> Any:
    """Manually wrap an OpenAI or Anthropic client for token tracking."""
    return get_tracker().wrap(client)


def report() -> None:
    """Print a terminal summary with token counts, cost, and context reuse."""
    print_report(get_tracker())


def stop() -> None:
    """Stop tracking, restore SDK patches, and close the session."""
    get_tracker().stop()


def checkpoint() -> None:
    """Manually save a checkpoint of the current conversation state."""
    get_tracker().checkpoint()


def resume(session_id: int) -> list:
    """Resume from the latest checkpoint of a given session.

    Returns the list of messages from the last checkpoint.

    Usage::
        messages = streamctx.resume(session_id)
    """
    return get_tracker().resume(session_id)


def get_session_id() -> int | None:
    """Get the current active session ID."""
    return get_tracker().get_session_id()


