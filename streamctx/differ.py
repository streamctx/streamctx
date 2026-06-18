"""Context Diff Engine for StreamCtx.

Compare context between any two steps to see exactly:
- What was added
- What was removed 
- What changed
- Context drift score 0-100
"""

from __future__ import annotations

from typing import Any, Optional


def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, len(text) // 4)


class ContextDiffer:
    """
    Compare context between two steps in a session.

    Shows exactly when and how context changed —
    helping developers pinpoint where agent went wrong.
    """

    def diff(
        self,
        messages_a: list[dict[str, Any]],
        messages_b: list[dict[str, Any]],
        step_a: int = 0,
        step_b: int = 0,
    ) -> dict[str, Any]:
        """
        Compare two sets of messages and return diff.

        Returns:
            {
                "added": list of new messages in B,
                "removed": list of messages only in A,
                "unchanged": list of common messages,
                "drift_score": int 0-100,
                "summary": str,
                "warnings": list[str],
                "token_delta": int,
            }
        """
        # Convert to comparable format
        set_a = {self._msg_key(m): m for m in messages_a}
        set_b = {self._msg_key(m): m for m in messages_b}

        keys_a = set(set_a.keys())
        keys_b = set(set_b.keys())

        added = [set_b[k] for k in keys_b - keys_a]
        removed = [set_a[k] for k in keys_a - keys_b]
        unchanged = [set_a[k] for k in keys_a & keys_b]

        # Token delta
        tokens_a = sum(_estimate_tokens(m.get("content", "")) for m in messages_a)
        tokens_b = sum(_estimate_tokens(m.get("content", "")) for m in messages_b)
        token_delta = tokens_b - tokens_a

        # Drift score
        drift_score = self._calc_drift(
            added, removed, unchanged, messages_a, messages_b
        )

        # Warnings
        warnings = self._get_warnings(added, removed, drift_score)

        # Summary
        summary = self._build_summary(
            added, removed, unchanged, step_a, step_b, drift_score
        )

        return {
            "step_a": step_a,
            "step_b": step_b,
            "added": added,
            "removed": removed,
            "unchanged": unchanged,
            "drift_score": drift_score,
            "summary": summary,
            "warnings": warnings,
            "token_delta": token_delta,
            "tokens_a": tokens_a,
            "tokens_b": tokens_b,
        }

    def _msg_key(self, msg: dict[str, Any]) -> str:
        """Unique key for a message."""
        role = msg.get("role", "")
        content = msg.get("content", "")
        return f"{role}:{content}"

    def _calc_drift(
        self,
        added: list,
        removed: list,
        unchanged: list,
        messages_a: list,
        messages_b: list,
    ) -> int:
        """
        Calculate drift score 0-100.
        0 = identical, 100 = completely different.
        """
        total = len(messages_a) + len(messages_b)
        if total == 0:
            return 0

        changed = len(added) + len(removed)
        drift = int(round(100 * changed / total))
        return min(100, drift)

    def _get_warnings(
        self,
        added: list,
        removed: list,
        drift_score: int,
    ) -> list[str]:
        warnings = []

        # System prompt removed
        removed_roles = [m.get("role") for m in removed]
        added_roles = [m.get("role") for m in added]

        if "system" in removed_roles:
            warnings.append(
                "⚠️  System prompt was REMOVED — agent may have lost instructions"
            )

        if "system" in added_roles:
            warnings.append(
                "⚠️  New system prompt ADDED — context instructions changed"
            )

        if drift_score >= 70:
            warnings.append(
                f"🚨 High drift ({drift_score}/100) — context changed significantly"
            )
        elif drift_score >= 40:
            warnings.append(
                f"⚡ Medium drift ({drift_score}/100) — monitor context closely"
            )

        # Check for contradictions
        added_content = " ".join(m.get("content", "").lower() for m in added)
        removed_content = " ".join(m.get("content", "").lower() for m in removed)

        contradiction_pairs = [
            ("use gpt", "use claude"),
            ("enabled", "disabled"),
            ("yes", "no"),
            ("true", "false"),
        ]
        for w1, w2 in contradiction_pairs:
            if w1 in added_content and w2 in removed_content:
                warnings.append(
                    f"⚠️  Contradiction: '{w1}' added but '{w2}' removed"
                )
            elif w2 in added_content and w1 in removed_content:
                warnings.append(
                    f"⚠️  Contradiction: '{w2}' added but '{w1}' removed"
                )

        return warnings

    def _build_summary(
        self,
        added: list,
        removed: list,
        unchanged: list,
        step_a: int,
        step_b: int,
        drift_score: int,
    ) -> str:
        if drift_score == 0:
            return f"✅ Step {step_a} → {step_b}: No context change"
        elif drift_score < 40:
            return (
                f"✅ Step {step_a} → {step_b}: Minor changes "
                f"(+{len(added)} -{len(removed)} messages, drift: {drift_score}/100)"
            )
        elif drift_score < 70:
            return (
                f"⚡ Step {step_a} → {step_b}: Moderate drift "
                f"(+{len(added)} -{len(removed)} messages, drift: {drift_score}/100)"
            )
        else:
            return (
                f"🚨 Step {step_a} → {step_b}: HIGH DRIFT "
                f"(+{len(added)} -{len(removed)} messages, drift: {drift_score}/100)"
            )


