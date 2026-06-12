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
    """Enable tracing and auto-detect OpenAI / Anthropic SDK calls."""
    get_tracker().start()


def wrap(client: Any) -> Any:
    """Manually instrument an OpenAI or Anthropic client instance."""
    return get_tracker().wrap(client)


def report() -> None:
    """Print a Rich terminal summary of the current session."""
    print_report(get_tracker())


def stop() -> None:
    """Disable tracking and close the current session."""
    get_tracker().stop()
