"""Self-healing engine for StreamCtx."""

from __future__ import annotations

from typing import Any, Optional


class SelfHealingEngine:
    """
    Detects failed LLM calls and recovers using last valid checkpoint.

    When a call fails:
    1. Catches the exception
    2. Loads last valid checkpoint messages
    3. Injects recovery context
    4. Retries the call automatically
    """

    def __init__(self) -> None:
        self._last_valid_messages: list[dict[str, Any]] = []
        self._last_valid_response: Any = None
        self._failure_count: int = 0
        self._recovery_count: int = 0

    def record_success(
        self,
        messages: list[dict[str, Any]],
        response: Any,
    ) -> None:
        """Save last successful call for recovery."""
        self._last_valid_messages = list(messages)
        self._last_valid_response = response

    def record_failure(self) -> None:
        """Track failure count."""
        self._failure_count += 1

    def can_heal(self) -> bool:
        """Check if we have valid context to recover from."""
        return len(self._last_valid_messages) > 0

    def get_recovery_messages(
        self,
        failed_messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """
        Build recovery message list by injecting last valid context.

        Strategy:
        - Take last valid messages as base
        - Add recovery notice
        - Append the last user message from failed call
        """
        if not self._last_valid_messages:
            return failed_messages

        # Get last user message from failed call
        last_user_msg = None
        for msg in reversed(failed_messages):
            if msg.get("role") == "user":
                last_user_msg = msg
                break

        # Build recovery context
        recovery = list(self._last_valid_messages)

        # Add recovery notice as system message
        recovery_notice = {
            "role": "system",
            "content": (
                "Note: Previous response was interrupted. "
                "Continuing from last valid context."
            ),
        }
        recovery.append(recovery_notice)

        # Re-add the last user message
        if last_user_msg:
            recovery.append(last_user_msg)

        self._recovery_count += 1
        return recovery

    def attempt_heal(
        self,
        fn: Any,
        failed_messages: list[dict[str, Any]],
        kwargs: dict[str, Any],
        max_retries: int = 2,
    ) -> Any:
        """
        Attempt to recover from a failed LLM call.

        Retries with last valid context injected.
        """
        if not self.can_heal():
            raise RuntimeError("Self-healing failed: no valid checkpoint available.")

        recovery_messages = self.get_recovery_messages(failed_messages)

        for attempt in range(max_retries):
            try:
                # Update messages with recovery context
                kwargs_copy = dict(kwargs)
                kwargs_copy["messages"] = recovery_messages
                response = fn(**kwargs_copy)
                self.record_success(recovery_messages, response)
                return response
            except Exception:
                if attempt == max_retries - 1:
                    raise
                continue

        raise RuntimeError("Self-healing exhausted all retries.")

    def get_stats(self) -> dict[str, Any]:
        """Return healing statistics."""
        return {
            "failure_count": self._failure_count,
            "recovery_count": self._recovery_count,
            "has_valid_context": self.can_heal(),
        }


