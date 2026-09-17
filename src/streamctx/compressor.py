"""Context compression engine for StreamCtx."""

from __future__ import annotations

import re
from typing import Any


def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, len(text) // 4)


def _message_text(msg: dict[str, Any]) -> str:
    content = msg.get("content", "")
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return str(content)


def _total_tokens(messages: list[dict[str, Any]]) -> int:
    return sum(_estimate_tokens(_message_text(m)) for m in messages)


# Constraint / policy language that must survive compression even when it
# appears once, early, and never again. 15-char prefixes used to drop these.
_CONSTRAINT_RE = re.compile(
    r"(?i)\b("
    r"must(?:\s+not)?|never|always|do\s+not|don't|required|constraint|"
    r"forbidden|shall(?:\s+not)?|only\s+use|exactly|critical|"
    r"never\s+use|must\s+go\s+to"
    r")\b"
)
_STABLE_ID_RE = re.compile(r"\b[A-Z]{2,}[-_][A-Z0-9]{2,}\b")


def _is_high_value(msg: dict[str, Any]) -> bool:
    """True if dropping this message could change agent behavior."""
    if msg.get("role") == "system":
        return True
    text = _message_text(msg)
    if not text.strip():
        return False
    if _CONSTRAINT_RE.search(text) or _STABLE_ID_RE.search(text):
        return True
    if msg.get("tool_calls") or msg.get("tool_call_id"):
        return True
    return False


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

    system_msgs = [m for m in messages if m.get("role") == "system"] if keep_system else []
    non_system = [m for m in messages if m.get("role") != "system"]

    if len(non_system) <= keep_last_n:
        recent = non_system
        middle = []
    else:
        recent = non_system[-keep_last_n:]
        middle = non_system[:-keep_last_n]

    pinned, compressed_middle = _compress_middle(middle)
    result = system_msgs + pinned + compressed_middle + recent
    compressed_tokens = _total_tokens(result)

    if compressed_tokens > max_tokens:
        result = _fit_to_budget(system_msgs, pinned, compressed_middle, recent, max_tokens)
        compressed_tokens = _total_tokens(result)

    return result, original_tokens, compressed_tokens


def _first_sentence(text: str, limit: int = 240) -> str:
    cleaned = text.replace("\n", " ").strip()
    if not cleaned:
        return ""
    for sep in (". ", "? ", "! "):
        idx = cleaned.find(sep)
        if 0 < idx < limit:
            return cleaned[: idx + 1]
    return cleaned[:limit]


def _compress_middle(
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not messages:
        return [], []

    pinned: list[dict[str, Any]] = []
    rest: list[dict[str, Any]] = []
    for msg in messages:
        if _is_high_value(msg):
            pinned.append(msg)
        else:
            rest.append(msg)

    if not rest:
        return pinned, []

    topics: list[str] = []
    for msg in rest:
        text = _message_text(msg)
        if not text.strip():
            continue
        short = _first_sentence(text)
        if short:
            topics.append(f"{msg.get('role', 'user')}: {short}")

    if not topics:
        return pinned, []

    summary = {"role": "system", "content": "Earlier context: " + " | ".join(topics)}
    return pinned, [summary]


def _fit_to_budget(
    system_msgs: list[dict[str, Any]],
    pinned: list[dict[str, Any]],
    middle: list[dict[str, Any]],
    recent: list[dict[str, Any]],
    max_tokens: int,
) -> list[dict[str, Any]]:
    """Trim the extractive summary first. Never drop pinned constraints."""
    result = system_msgs + pinned + middle + recent
    if _total_tokens(result) <= max_tokens:
        return result
    # Drop the chatter summary before touching constraints or recent turns.
    result = system_msgs + pinned + recent
    if _total_tokens(result) <= max_tokens:
        return result
    return result


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
