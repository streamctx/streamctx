
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
from .compressor import compress_messages, get_compression_stats
from .poison_detector import PoisonDetector
from .differ import ContextDiffer

__version__ = "0.3.0"
__all__ = [
    "start", "wrap", "report", "stop",
    "checkpoint", "resume", "get_session_id",
    "compress", "compression_stats",
    "healing_stats",
    "scan",
    "context_diff",
    "__version__",
]

_detector = PoisonDetector()
_differ = ContextDiffer()


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
    """Resume from the latest checkpoint of a given session."""
    return get_tracker().resume(session_id)


def get_session_id() -> int | None:
    """Get the current active session ID."""
    return get_tracker().get_session_id()


def compress(
    messages: list,
    max_tokens: int = 2000,
    keep_last_n: int = 4,
) -> dict:
    """
    Compress messages to reduce token usage by 40-70%.

    Usage::

        result = streamctx.compress(messages, max_tokens=2000)
        compressed = result["messages"]
        print(result["stats"])
    """
    compressed, original, after = compress_messages(
        messages,
        max_tokens=max_tokens,
        keep_last_n=keep_last_n,
    )
    stats = get_compression_stats(original, after)
    return {
        "messages": compressed,
        "stats": stats,
    }


def compression_stats(original_tokens: int, compressed_tokens: int) -> dict:
    """Get compression statistics."""
    return get_compression_stats(original_tokens, compressed_tokens)


def healing_stats() -> dict:
    """Get self-healing statistics."""
    return get_tracker().healing_stats()


def scan(messages: list) -> dict:
    """
    Scan messages for context poisoning.

    Usage::

        result = streamctx.scan(messages)
        print(result["health_score"])
        print(result["warnings"])
    """
    return _detector.scan(messages)


def context_diff(
    messages_a: list,
    messages_b: list,
    step_a: int = 0,
    step_b: int = 0,
) -> dict:
    """
    Compare two sets of messages to see exactly what changed.

    Shows added, removed, unchanged messages and drift score.

    Usage::

        diff = streamctx.context_diff(
            messages_a=step3_messages,
            messages_b=step7_messages,
            step_a=3,
            step_b=7,
        )
        print(diff["summary"])
        print(diff["warnings"])
        print(diff["drift_score"])

    Returns:
        {
            "added": list,
            "removed": list,
            "unchanged": list,
            "drift_score": int 0-100,
            "summary": str,
            "warnings": list,
            "token_delta": int,
        }
    """
    return _differ.diff(messages_a, messages_b, step_a, step_b)


