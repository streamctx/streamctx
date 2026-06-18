"""Context Poison Detector for StreamCtx.

Detects when AI agent context becomes corrupted:
- Repeated failed attempts (agent stuck in loop)
- Contradictory facts across messages
- Accumulating errors in context
- Context health score 0-100
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any, Optional


# Common error/failure patterns
ERROR_PATTERNS = [
    r"error",
    r"failed",
    r"exception",
    r"invalid",
    r"not found",
    r"undefined",
    r"cannot",
    r"unable to",
    r"does not exist",
    r"no such",
    r"404",
    r"400",
    r"500",
]

# Contradiction indicator pairs
CONTRADICTION_PAIRS = [
    ("yes", "no"),
    ("true", "false"),
    ("success", "failed"),
    ("exists", "not found"),
    ("available", "unavailable"),
    ("enabled", "disabled"),
    ("correct", "incorrect"),
    ("valid", "invalid"),
]


class PoisonDetector:
    """
    Analyzes conversation messages for context poisoning signals.

    Poisoning happens when:
    1. Agent repeats same failed action 3+ times
    2. Context contains contradictory facts
    3. Error messages accumulate without resolution
    4. Agent references non-existent/stale data
    """

    def __init__(self) -> None:
        self._scan_history: list[dict[str, Any]] = []

    def scan(
        self,
        messages: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """
        Scan messages for context poisoning signals.

        Returns:
            {
                "health_score": int (0-100),
                "is_poisoned": bool,
                "warnings": list[str],
                "details": dict
            }
        """
        if not messages:
            return self._clean_result()

        warnings = []
        penalties = 0

        # Check 1: Repeated error patterns
        error_result = self._check_repeated_errors(messages)
        if error_result["found"]:
            warnings.append(
                f"⚠️  Repeated errors detected: '{error_result['pattern']}' "
                f"appears {error_result['count']}x — agent may be stuck in loop"
            )
            penalties += min(error_result["count"] * 10, 40)

        # Check 2: Contradictory facts
        contradiction_result = self._check_contradictions(messages)
        if contradiction_result["found"]:
            warnings.append(
                f"⚠️  Contradictory context: '{contradiction_result['pair'][0]}' "
                f"and '{contradiction_result['pair'][1]}' both present"
            )
            penalties += 20

        # Check 3: Error accumulation
        error_accumulation = self._check_error_accumulation(messages)
        if error_accumulation["found"]:
            warnings.append(
                f"⚠️  Error accumulation: {error_accumulation['count']} error "
                f"messages in last {error_accumulation['window']} messages"
            )
            penalties += 15

        # Check 4: Repetitive assistant responses
        repetition_result = self._check_repetitive_responses(messages)
        if repetition_result["found"]:
            warnings.append(
                f"⚠️  Repetitive responses: assistant repeating same content "
                f"{repetition_result['count']}x — possible hallucination loop"
            )
            penalties += 25

        # Calculate health score
        health_score = max(0, 100 - penalties)
        is_poisoned = health_score < 60

        result = {
            "health_score": health_score,
            "is_poisoned": is_poisoned,
            "warnings": warnings,
            "details": {
                "repeated_errors": error_result,
                "contradictions": contradiction_result,
                "error_accumulation": error_accumulation,
                "repetitive_responses": repetition_result,
            },
            "recommendation": self._get_recommendation(health_score),
        }

        self._scan_history.append(result)
        return result

    def _check_repeated_errors(
        self, messages: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Detect same error repeating 3+ times."""
        content_lower = [
            m.get("content", "").lower() for m in messages
        ]

        for pattern in ERROR_PATTERNS:
            matches = []
            for content in content_lower:
                if re.search(pattern, content):
                    matches.append(pattern)

            if len(matches) >= 3:
                return {
                    "found": True,
                    "pattern": pattern,
                    "count": len(matches),
                }

        return {"found": False, "pattern": None, "count": 0}

    def _check_contradictions(
        self, messages: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Detect contradictory facts in context."""
        all_content = " ".join(
            m.get("content", "").lower() for m in messages
        )

        for word1, word2 in CONTRADICTION_PAIRS:
            if word1 in all_content and word2 in all_content:
                return {
                    "found": True,
                    "pair": (word1, word2),
                }

        return {"found": False, "pair": None}

    def _check_error_accumulation(
        self, messages: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Check if errors are piling up in recent messages."""
        window = min(6, len(messages))
        recent = messages[-window:]

        error_count = 0
        for msg in recent:
            content = msg.get("content", "").lower()
            for pattern in ERROR_PATTERNS[:5]:  # Top 5 patterns
                if re.search(pattern, content):
                    error_count += 1
                    break

        if error_count >= 3:
            return {
                "found": True,
                "count": error_count,
                "window": window,
            }

        return {"found": False, "count": 0, "window": window}

    def _check_repetitive_responses(
        self, messages: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Detect assistant giving same response repeatedly."""
        assistant_msgs = [
            m.get("content", "")[:100]
            for m in messages
            if m.get("role") == "assistant"
        ]

        if len(assistant_msgs) < 2:
            return {"found": False, "count": 0}

        counter = Counter(assistant_msgs)
        most_common, count = counter.most_common(1)[0]

        if count >= 2:
            return {"found": True, "count": count}

        return {"found": False, "count": 0}

    def _get_recommendation(self, health_score: int) -> str:
        if health_score >= 80:
            return "✅ Context is healthy"
        elif health_score >= 60:
            return "⚡ Context has minor issues — monitor closely"
        elif health_score >= 40:
            return "🔧 Context needs cleaning — use streamctx.compress()"
        else:
            return "🚨 Context severely poisoned — reset or resume from checkpoint"

    def _clean_result(self) -> dict[str, Any]:
        return {
            "health_score": 100,
            "is_poisoned": False,
            "warnings": [],
            "details": {},
            "recommendation": "✅ Context is healthy",
        }

    def get_history(self) -> list[dict[str, Any]]:
        return self._scan_history


