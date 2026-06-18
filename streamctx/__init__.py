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

__version__ = "0.2.0"
__all__ = [
    "start", "wrap", "report", "stop",
    "checkpoint", "resume", "get_session_id",
    "compress", "compression_stats",
    "healing_stats",
    "scan",
    "__version__",
]

_detector = PoisonDetector()


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

    Detects:
    - Repeated errors (agent stuck in loop)
    - Contradictory facts
    - Error accumulation
    - Repetitive responses

    Usage::

        result = streamctx.scan(messages)
        print(result["health_score"])   # 0-100
        print(result["warnings"])       # list of issues
        print(result["recommendation"]) # what to do

    Returns:
        {
            "health_score": int,
            "is_poisoned": bool,
            "warnings": list[str],
            "recommendation": str
        }
    """
    return _detector.scan(messages)


