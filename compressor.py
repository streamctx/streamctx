"""Context compression engine for StreamCtx."""

from __future__ import annotations

from typing import Any, Optional


def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, len(text) // 4)


def _total_tokens(messages: list[dict[str, Any]]) -> int:
    return sum(_estimate_tokens(m.get("content", "")) for m in messages)


def compress_messages(
    messages: list[dict[str, Any]],
    max_tokens: int = 2000,
    keep_system: bool = True,
    keep_last_n: int = 4,
) -> tuple[list[dict[str, Any]], int, int]:
    """
    Compress messages to fit within max_tokens.

    Strategy:
    1. Always keep system prompt
    2. Always keep last N messages (recent context)
    3. Summarize/truncate middle messages

    Returns:
        (compressed_messages, original_tokens, compressed_tokens)
    """
    if not messages:
        return messages, 0, 0

    original_tokens = _total_tokens(messages)

    # If already within limit, no compression needed
    if original_tokens <= max_tokens:
        return messages, original_tokens, original_tokens

    # Separate system, middle, and recent messages
    system_msgs = [m for m in messages if m.get("role") == "system"]
    non_system = [m for m in messages if m.get("role") != "system"]

    # Always keep last N messages
    if len(non_system) <= keep_last_n:
        recent = non_system
        middle = []
    else:
        recent = non_system[-keep_last_n:]
        middle = non_system[:-keep_last_n]

    # Compress middle messages by summarizing content
    compressed_middle = _compress_middle(middle, max_tokens // 4)

    # Build final message list
    result = system_msgs + compressed_middle + recent

    compressed_tokens = _total_tokens(result)

    return result, original_tokens, compressed_tokens


def _compress_middle(
    messages: list[dict[str, Any]],
    target_tokens: int,
) -> list[dict[str, Any]]:
    """Summarize middle messages into a compact context summary."""
    if not messages:
        return []

    # Build a summary of the conversation so far
    summary_parts = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if not content:
            continue
        # Truncate each message to first 100 chars for summary
        short = content[:100].replace("\n", " ")
        if len(content) > 100:
            short += "..."
        summary_parts.append(f"{role}: {short}")

    if not summary_parts:
        return []

    summary = "Previous conversation summary:\n" + "\n".join(summary_parts)

    return [{"role": "system", "content": summary}]


def get_compression_stats(
    original_tokens: int,
    compressed_tokens: int,
) -> dict[str, Any]:
    """Calculate compression statistics."""
    if original_tokens == 0:
        return {
            "original_tokens": 0,
            "compressed_tokens": 0,
            "saved_tokens": 0,
            "compression_pct": 0,
        }

    saved = original_tokens - compressed_tokens
    pct = int(round(100 * saved / original_tokens))

    return {
        "original_tokens": original_tokens,
        "compressed_tokens": compressed_tokens,
        "saved_tokens": saved,
        "compression_pct": pct,
    }


