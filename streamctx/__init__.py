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
__all__ = ["start", "wrap", "report", "stop", "__version__"]


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
