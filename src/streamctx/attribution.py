"""Causal Failure Attribution Engine.

Reads real call + checkpoint data from SessionStorage and identifies
*which step* in a multi-step agent session most likely caused a failure,
and *why* (drift, compression-loss, or recency-related context decay).

This is a hybrid causal-graph + lightweight-feature approach (no LLM calls):
for every failed call in a session, we walk backwards through the calls
that preceded it and score each one as a candidate root cause using three
signals:

    DRIFT_WEIGHT       - how much the message/context shape changed
                         between consecutive calls (proxy for "something
                         shifted here")
    COMPRESSION_WEIGHT - how much context was reused/compressed right
                         before the failure (proxy for "information loss")
    RECENCY_WEIGHT     - how close the candidate call is to the failure
                         (closer = more likely the proximate cause)

The weights are intentionally simple and tunable - this is the v1
heuristic baseline described in the StreamCtx design doc. It is meant to
be replaced/augmented later, not to be the final word in accuracy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .storage import get_storage

# --- Tunable weights for the v1 heuristic (see design doc) ---
DRIFT_WEIGHT = 0.5
COMPRESSION_WEIGHT = 0.3
RECENCY_WEIGHT = 0.2

# How many calls *before* the failing call we're willing to consider as
# candidate root causes. Keeps the engine from blaming something that
# happened too long ago to plausibly be related.
DEFAULT_LOOKBACK = 5


@dataclass
class CallSnapshot:
    """Lightweight view of a single `calls` row, used for scoring."""

    id: int
    session_id: int
    timestamp: str
    provider: str
    model: Optional[str]
    input_tokens: int
    output_tokens: int
    cost: float
    reused_tokens: int
    waste_category: Optional[str]
    failed: bool
    healed: bool
    error_message: Optional[str]
    messages: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class AttributionResult:
    """Result of attributing one failure to a candidate root-cause step."""

    session_id: int
    failed_call_id: int
    root_cause_call_id: Optional[int]
    root_cause_step_offset: Optional[int]  # 0 = the failing call itself, 1 = previous call, etc.
    confidence: float  # 0.0-1.0, the combined weighted score, normalized
    reason: str
    signal_breakdown: dict[str, float]


def _safe_div(numerator: float, denominator: float) -> float:
    if denominator == 0:
        return 0.0
    return numerator / denominator


def _drift_score(prev: CallSnapshot, curr: CallSnapshot) -> float:
    """Estimate how much context 'drifted' between two consecutive calls.

    Proxy signal: relative change in input token count, plus whether the
    waste_category changed (a change in waste pattern often co-occurs with
    a shift in what's actually in the context window).
    """
    if prev.input_tokens == 0:
        token_drift = 1.0 if curr.input_tokens > 0 else 0.0
    else:
        token_drift = abs(curr.input_tokens - prev.input_tokens) / prev.input_tokens
        token_drift = min(token_drift, 1.0)

    waste_changed = 1.0 if prev.waste_category != curr.waste_category else 0.0

    return min(1.0, 0.7 * token_drift + 0.3 * waste_changed)


def _compression_score(call: CallSnapshot) -> float:
    """Estimate how much of this call's context was reused/compressed.

    High reuse right before a failure is a proxy for "stale or truncated
    context fed into the model" - a classic failure precursor.
    """
    total = call.input_tokens
    if total == 0:
        return 0.0
    return min(1.0, _safe_div(call.reused_tokens, total))


def _recency_score(offset: int, lookback: int) -> float:
    """Closer candidates (smaller offset) score higher. Linear decay."""
    if lookback <= 0:
        return 0.0
    return max(0.0, 1.0 - (offset / (lookback + 1)))


def _row_to_snapshot(row: dict[str, Any]) -> CallSnapshot:
    import json

    raw_messages = row.get("messages_json")
    try:
        messages = json.loads(raw_messages) if raw_messages else []
    except (TypeError, ValueError):
        messages = []

    return CallSnapshot(
        id=int(row["id"]),
        session_id=int(row["session_id"]),
        timestamp=str(row["timestamp"]),
        provider=str(row["provider"]),
        model=row.get("model"),
        input_tokens=int(row.get("input_tokens") or 0),
        output_tokens=int(row.get("output_tokens") or 0),
        cost=float(row.get("cost") or 0.0),
        reused_tokens=int(row.get("reused_tokens") or 0),
        waste_category=row.get("waste_category"),
        failed=bool(row.get("failed") or False),
        healed=bool(row.get("healed") or False),
        error_message=row.get("error_message"),
        messages=messages,
    )


class AttributionEngine:
    """Causal Failure Attribution Engine.

    Wraps a SessionStorage instance (real SQLite by default) and exposes
    methods to attribute failures within a session to a likely root-cause
    step, using the weighted heuristic described at module level.
    """

    def __init__(self, storage: Any = None, lookback: int = DEFAULT_LOOKBACK) -> None:
        self.storage = storage or get_storage()
        self.lookback = lookback

    def _load_session_calls(self, session_id: int) -> list[CallSnapshot]:
        """Load all calls for a session, ordered chronologically.

        Requires `SessionStorage.get_calls_for_session()` - see the
        storage.py patch that ships alongside this module.
        """
        rows = self.storage.get_calls_for_session(session_id)
        return [_row_to_snapshot(r) for r in rows]

    def attribute_failure(self, session_id: int, failed_call_id: int) -> AttributionResult:
        """Attribute a single failed call to its most likely root-cause step.

        Walks backwards from the failed call (inclusive) up to `self.lookback`
        prior calls in the same session, scores each as a candidate, and
        returns the highest-scoring one.
        """
        calls = self._load_session_calls(session_id)
        index_by_id = {c.id: i for i, c in enumerate(calls)}

        if failed_call_id not in index_by_id:
            return AttributionResult(
                session_id=session_id,
                failed_call_id=failed_call_id,
                root_cause_call_id=None,
                root_cause_step_offset=None,
                confidence=0.0,
                reason="failed_call_id not found in session",
                signal_breakdown={},
            )

        fail_idx = index_by_id[failed_call_id]
        lookback_start = max(0, fail_idx - self.lookback)

        best_score = -1.0
        best_call: Optional[CallSnapshot] = None
        best_offset = 0
        best_breakdown: dict[str, float] = {}

        for candidate_idx in range(fail_idx, lookback_start - 1, -1):
            candidate = calls[candidate_idx]
            offset = fail_idx - candidate_idx

            prev = calls[candidate_idx - 1] if candidate_idx > 0 else candidate
            drift = _drift_score(prev, candidate)
            compression = _compression_score(candidate)
            recency = _recency_score(offset, self.lookback)

            score = (
                DRIFT_WEIGHT * drift
                + COMPRESSION_WEIGHT * compression
                + RECENCY_WEIGHT * recency
            )

            if score > best_score:
                best_score = score
                best_call = candidate
                best_offset = offset
                best_breakdown = {
                    "drift": drift,
                    "compression": compression,
                    "recency": recency,
                    "weighted_total": score,
                }

        reason = self._explain(best_call, best_offset, best_breakdown)

        return AttributionResult(
            session_id=session_id,
            failed_call_id=failed_call_id,
            root_cause_call_id=best_call.id if best_call else None,
            root_cause_step_offset=best_offset,
            confidence=round(min(1.0, max(0.0, best_score)), 4),
            reason=reason,
            signal_breakdown=best_breakdown,
        )

    def attribute_session(self, session_id: int) -> list[AttributionResult]:
        """Attribute every failed call in a session.

        Returns one AttributionResult per call where `failed=True`.
        """
        calls = self._load_session_calls(session_id)
        results: list[AttributionResult] = []
        for call in calls:
            if call.failed:
                results.append(self.attribute_failure(session_id, call.id))
        return results

    @staticmethod
    def _explain(
        call: Optional[CallSnapshot],
        offset: int,
        breakdown: dict[str, float],
    ) -> str:
        if call is None:
            return "No candidate calls available to attribute against."

        location = "the failing call itself" if offset == 0 else f"{offset} step(s) before the failure"
        dominant = max(breakdown, key=lambda k: breakdown.get(k, 0.0) if k != "weighted_total" else -1)

        signal_label = {
            "drift": "a context/token-shape drift",
            "compression": "heavy reliance on reused/compressed context",
            "recency": "proximity to the failure point",
        }.get(dominant, "a combination of signals")

        waste_note = f" (waste_category: {call.waste_category})" if call.waste_category else ""
        return (
            f"Most likely root cause is the call at {location} "
            f"(call_id={call.id}), driven mainly by {signal_label}{waste_note}."
        )


def get_attribution_engine() -> AttributionEngine:
    """Convenience factory, mirrors get_storage() in storage.py."""
    return AttributionEngine()
