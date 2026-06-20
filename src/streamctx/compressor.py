"""Context compression engine for StreamCtx."""

from __future__ import annotations

from typing import Any


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
    if not messages:
        return messages, 0, 0

    original_tokens = _total_tokens(messages)

    if original_tokens <= max_tokens:
        return messages, original_tokens, original_tokens

    # Separate system, middle, recent
    system_msgs = [m for m in messages if m.get("role") == "system"]
    non_system = [m for m in messages if m.get("role") != "system"]

    if len(non_system) <= keep_last_n:
        recent = non_system
        middle = []
    else:
        recent = non_system[-keep_last_n:]
        middle = non_system[:-keep_last_n]

    # Compress middle into one short summary
    compressed_middle = _compress_middle(middle)

    result = system_msgs + compressed_middle + recent
    compressed_tokens = _total_tokens(result)

    return result, original_tokens, compressed_tokens


def _compress_middle(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not messages:
        return []

    # Extract key points only — very aggressive compression
    topics = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if not content:
            continue
        # Take only first 15 chars per message
        short = content[:15].replace("\n", " ").strip()
        topics.append(f"{role}: {short}")

    if not topics:
        return []

    # Single compressed summary message
    summary = "Earlier context: " + " | ".join(topics)

    return [{"role": "system", "content": summary}]


def get_compression_stats(
    original_tokens: int,
    compressed_tokens: int,
) -> dict[str, Any]:
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


