"""Causal Failure Attribution Engine.

Reads real call + checkpoint data from SessionStorage and identifies
*which step* in a multi-step agent session most likely caused a failure,
and *why* (drift, compression-loss, or recency-related context decay).

This is a hybrid causal-graph + lightweight-feature approach (no LLM calls):
for every failed call in a session, we walk backwards through the calls
that preceded it and score each one as a candidate root cause using three
signals:

    DRIFT_WEIGHT       - message/context *shape* change between consecutive
                         calls (token estimate from stored messages, not
                         persist-zeroed usage columns)
    COMPRESSION_WEIGHT - information loss from replaying Layer 1
                         ``compress_messages()`` on the stored (uncompressed)
                         request; 0 when compression would not have fired
    RECENCY_WEIGHT     - (ranking) how close the candidate is to the failure.
                         The *why*-signal stored as ``recency`` is topic
                         shift with the original task still buried in context,
                         not the offset-0 structural prior of 1.0.

The weights are intentionally simple and tunable - this is the v1
heuristic baseline described in the StreamCtx design doc. It is meant to
be replaced/augmented later, not to be the final word in accuracy.

Layer 2 is MIT-licensed core detection logic. Nothing in this module is
gated on a paid tier, license check, or hosted-only flag.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from .compressor import _message_text, _total_tokens, compress_messages
from .failure import classify_failure
from .storage import get_storage

# --- Tunable weights for the v1 heuristic (see design doc) ---
DRIFT_WEIGHT = 0.5
COMPRESSION_WEIGHT = 0.3
RECENCY_WEIGHT = 0.2

# How many calls *before* the failing call we're willing to consider as
# candidate root causes. Keeps the engine from blaming something that
# happened too long ago to plausibly be related.
DEFAULT_LOOKBACK = 5

# Token estimator is ``len(text) // 4``. Relative change on a 10-token
# prompt is mostly noise (one short sentence). Floor the denominator at
# 50 tokens (~200 characters) so a 6-token jitter cannot look like 60%
# drift. A 21% move against that floor is 0.7 * 0.21 ≈ 0.15 — the
# abstention gate. Sub-20% shape jitter is tokenizer/usage disagreement,
# not a cause. Waste-category flips (0.3) clear the floor on their own
# when *both* sides are labeled. Compression must show ~15% combined
# savings+loss; a constraint-preserving compress that drops nothing
# important stays unattributable.
SHAPE_TOKEN_FLOOR = 50
CONTENT_SIGNAL_FLOOR = 0.15
# Back-compat alias; the gate is CONTENT_SIGNAL_FLOOR, not this epsilon.
CONTENT_SIGNAL_EPS = CONTENT_SIGNAL_FLOOR

UNATTRIBUTABLE_REASON = "unattributable"
INFRA_NON_CONTENT_REASON = "infra/non-content"

# Significant terms for recency Jaccard: 3+ letter words, numbers, IDs.
_TERM_RE = re.compile(
    r"[A-Za-z]{3,}|\d+(?:\.\d+)?|[A-Z]{2,}[-_][A-Z0-9]{2,}"
)
# Compression "information loss" is factual, not chatter. Filler words
# dropped by extractive summary are not a cause; dropped numbers/IDs are.
_FACT_RE = re.compile(r"\d+(?:\.\d+)?|[A-Z]{2,}[-_][A-Z0-9]{2,}")

# Extra non-content needles beyond classify_failure() (401/429/
# invalid model/timeout/…).  Kept here so classify_failure() can
# still treat "simulated failure" as a content_error placeholder in
# Layer 3 tests, while attribution refuses to blame a heuristic for it.
_NON_CONTENT_EXTRA_RE = re.compile(
    r"recursion depth|"
    r"simulated failure|"
    r"takes 1 argument|"
    r"Completions\.create|"
    r"missing 1 required positional argument",
    re.IGNORECASE,
)

# Content failures whose error_message already names a cause outside
# {drift, compression, recency}. Forcing a heuristic would send Layer 3
# after the wrong repair. Abstain as unattributable, not infra.
_OUT_OF_TAXONOMY_RE = re.compile(
    r"prompt injection|\bjailbreak\b|"
    r"poisoned?\s+(?:context|prompt|input|message)",
    re.IGNORECASE,
)


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
    failure_kind: Optional[str] = None


def _safe_div(numerator: float, denominator: float) -> float:
    if denominator == 0:
        return 0.0
    return numerator / denominator


def _shape_tokens(call: CallSnapshot) -> int:
    """Token count for drift. Prefer stored messages over usage columns.

    Layer 1 ``_persist_failure`` writes ``input_tokens=0`` /
    ``reused_tokens=0`` (tracker.py). Treating that as a 100% token drop
    is a measurement artifact, not drift. The uncompressed request is
    still in ``messages_json``.
    """
    if call.messages:
        try:
            n = _total_tokens(call.messages)
        except Exception:
            n = 0
        if n > 0:
            return n
    return max(0, int(call.input_tokens or 0))


def _drift_score(prev: CallSnapshot, curr: CallSnapshot) -> float:
    """Estimate how much context 'drifted' between two consecutive calls.

    Relative token-shape change against SHAPE_TOKEN_FLOOR, plus a waste
    flip only when *both* sides have a non-null waste_category. A failed
    row with waste=None is missing data, not a pattern change.
    """
    prev_n = _shape_tokens(prev)
    curr_n = _shape_tokens(curr)
    denom = max(prev_n, curr_n, SHAPE_TOKEN_FLOOR)
    token_drift = min(1.0, abs(curr_n - prev_n) / denom)

    if prev.waste_category and curr.waste_category:
        waste_changed = 1.0 if prev.waste_category != curr.waste_category else 0.0
    else:
        waste_changed = 0.0

    return min(1.0, 0.7 * token_drift + 0.3 * waste_changed)


def _significant_terms(text: str) -> set[str]:
    return {m.group(0).lower() for m in _TERM_RE.finditer(text or "")}


def _messages_blob(messages: list[dict[str, Any]]) -> str:
    return " ".join(_message_text(m) for m in messages)


def _fact_terms(text: str) -> set[str]:
    return {m.group(0).lower() for m in _FACT_RE.finditer(text or "")}


def _content_loss(original: list[dict[str, Any]], compressed: list[dict[str, Any]]) -> float:
    orig = _fact_terms(_messages_blob(original))
    if not orig:
        return 0.0
    kept = _fact_terms(_messages_blob(compressed))
    return len(orig - kept) / len(orig)


def _compression_score(call: CallSnapshot) -> float:
    """Replay Layer 1 compression on the stored uncompressed request.

    ``reused_tokens`` is *not* a compression signal: tracker sums
    ContextDiffer prefix-reuse with compression savings, and failed
    rows zero it. If ``compress_messages()`` would not fire (under
    default ``max_tokens=2000``) or saves nothing, score is 0.
    """
    msgs = call.messages
    if not msgs:
        # No messages to replay. Do not treat reused_tokens as compression
        # unless the usage column itself says we were over budget.
        total = int(call.input_tokens or 0)
        if total <= 2000:
            return 0.0
        savings = min(1.0, _safe_div(call.reused_tokens, total))
        return savings

    compressed, orig, comp = compress_messages(msgs)
    if orig <= 0 or orig == comp:
        return 0.0
    loss = _content_loss(msgs, compressed)
    if loss <= 0.0:
        return 0.0
    savings = min(1.0, (orig - comp) / orig)
    return min(1.0, 0.5 * savings + 0.5 * loss)


def _user_texts(messages: list[dict[str, Any]]) -> list[str]:
    texts: list[str] = []
    for msg in messages:
        if msg.get("role") != "user":
            continue
        text = _message_text(msg).strip()
        if text:
            texts.append(text)
    return texts


def _recency_why_score(call: CallSnapshot) -> float:
    """Topic shift with the original task still present in this call.

    Offset-0 recency is a ranking prior, not a why. Recency-as-cause is
    "the assigned task is still in context but the latest user turn
    abandoned it". A single-turn prompt change is drift or nothing, not
    recency. If the original terms are gone from the blob, the task was
    replaced (drift), not buried.
    """
    users = _user_texts(call.messages)
    if len(users) < 2:
        return 0.0
    orig = _significant_terms(users[0])
    last = _significant_terms(users[-1])
    if not orig or not last:
        return 0.0
    blob = _significant_terms(_messages_blob(call.messages))
    buried = orig & blob
    if len(buried) / len(orig) < 0.3:
        return 0.0
    union = orig | last
    overlap = len(orig & last) / len(union) if union else 1.0
    return min(1.0, max(0.0, 1.0 - overlap))


def _recency_score(offset: int, lookback: int) -> float:
    """Closer candidates (smaller offset) score higher. Linear decay.

    Ranking prior only — not written into signal_breakdown['recency'].
    """
    if lookback <= 0:
        return 0.0
    return max(0.0, 1.0 - (offset / (lookback + 1)))


def is_non_content_failure(error_message: Optional[str]) -> bool:
    """True for infra / SDK / load-test errors that are not content quality.

    Reuses ``classify_failure()`` for API/config patterns, then adds
    recursion-depth, simulated-failure, and SDK-signature needles that
    classify_failure() still treats as content_error.
    """
    if classify_failure(error_message) == "infra_error":
        return True
    if error_message is None:
        return False
    return bool(_NON_CONTENT_EXTRA_RE.search(str(error_message)))


def _has_content_signal(breakdown: dict[str, float]) -> bool:
    drift = float(breakdown.get("drift") or 0.0)
    compression = float(breakdown.get("compression") or 0.0)
    recency_why = float(breakdown.get("recency") or 0.0)
    return max(drift, compression, recency_why) >= CONTENT_SIGNAL_FLOOR


def _abstain(
    session_id: int, failed_call_id: int, reason: str
) -> AttributionResult:
    return AttributionResult(
        session_id=session_id,
        failed_call_id=failed_call_id,
        root_cause_call_id=None,
        root_cause_step_offset=None,
        confidence=0.0,
        reason=reason,
        signal_breakdown={},
    )


def _parse_stored_messages(raw_messages: Any) -> list[dict[str, Any]]:
    if isinstance(raw_messages, list):
        return [m for m in raw_messages if isinstance(m, dict)]
    try:
        messages = json.loads(raw_messages) if raw_messages else []
    except (TypeError, ValueError):
        messages = []
    if not isinstance(messages, list):
        return []
    return [m for m in messages if isinstance(m, dict)]


def _row_to_snapshot(row: dict[str, Any]) -> CallSnapshot:
    messages = _parse_stored_messages(row.get("messages_json"))

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

    def __init__(
        self,
        storage: Any = None,
        lookback: int = DEFAULT_LOOKBACK,
        evidence: Any = None,
    ) -> None:
        self.storage = storage or get_storage()
        self.lookback = lookback
        self.evidence = evidence

    def _finish(self, result: AttributionResult) -> AttributionResult:
        """Best-effort independent log + Layer 4 attestation; never breaks attribution."""
        from .evidence import safe_append_evidence

        try:
            insert = getattr(self.storage, "insert_shadow_attribution_log", None)
            if insert is not None:
                breakdown = result.signal_breakdown or {}
                keys = [k for k in breakdown if k != "weighted_total"]
                dominant = (
                    max(keys, key=lambda k: float(breakdown.get(k) or 0.0))
                    if keys
                    else None
                )
                insert(
                    session_id=int(result.session_id),
                    failed_call_id=int(result.failed_call_id),
                    dominant_signal=dominant,
                    confidence=float(result.confidence),
                    root_cause_call_id=result.root_cause_call_id,
                    reason=result.reason,
                    error_message=None,
                    failure_kind=result.failure_kind or "attribution",
                    signal_breakdown=breakdown,
                )
        except Exception:
            pass

        safe_append_evidence(
            "attribution",
            result.failed_call_id,
            asdict(result),
            ledger=self.evidence,
        )
        return result

    def _load_session_calls(self, session_id: int) -> list[CallSnapshot]:
        """Load all calls for a session, ordered chronologically.

        Requires `SessionStorage.get_calls_for_session()` - see the
        storage.py patch that ships alongside this module.
        """
        rows = self.storage.get_calls_for_session(session_id)
        return [_row_to_snapshot(r) for r in rows]

    def attribute_failure(
        self,
        session_id: int,
        failed_call_id: int,
        calls: Optional[list[CallSnapshot]] = None,
        ) -> AttributionResult:
        """Attribute a single failed call to its most likely root-cause step.

        Walks backwards from the failed call (inclusive) up to `self.lookback`
        prior calls in the same session, scores each as a candidate, and
        returns the highest-scoring one.

        Abstains (confidence 0, no root cause) when:
          - the failed call's error_message is infra / SDK / load-test
            (reason ``infra/non-content``) — this gate runs *before*
            content heuristics, so a timeout after a drifted prompt is
            still infra, not DRIFT; or
          - the error_message names a cause outside the three-bucket
            taxonomy (prompt injection, jailbreak, poison) — abstain as
            ``unattributable`` rather than force DRIFT/COMPRESSION/RECENCY; or
          - the winning candidate's why-signals (drift / compression /
            recency-as-topic-shift) are all below CONTENT_SIGNAL_FLOOR
            (reason ``unattributable``). Offset recency is a ranking
            prior and is not evidence.

        If `calls` is provided (pre-loaded session calls), it's reused instead
        of re-querying storage — used by `attribute_session()` to avoid an
        N+1 query pattern when attributing multiple failures in one session.
        """

        if calls is None:
            calls = self._load_session_calls(session_id)
        index_by_id = {c.id: i for i, c in enumerate(calls)}

        if failed_call_id not in index_by_id:
            return self._finish(
                AttributionResult(
                    session_id=session_id,
                    failed_call_id=failed_call_id,
                    root_cause_call_id=None,
                    root_cause_step_offset=None,
                    confidence=0.0,
                    reason="failed_call_id not found in session",
                    signal_breakdown={},
                )
            )

        fail_idx = index_by_id[failed_call_id]
        failed_call = calls[fail_idx]
        if is_non_content_failure(failed_call.error_message):
            return self._finish(
                _abstain(session_id, failed_call_id, INFRA_NON_CONTENT_REASON)
            )
        if _OUT_OF_TAXONOMY_RE.search(str(failed_call.error_message or "")):
            return self._finish(
                _abstain(session_id, failed_call_id, UNATTRIBUTABLE_REASON)
            )

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
            recency_why = _recency_why_score(candidate)
            offset_recency = _recency_score(offset, self.lookback)

            score = (
                DRIFT_WEIGHT * drift
                + COMPRESSION_WEIGHT * compression
                + RECENCY_WEIGHT * offset_recency
            )

            if score > best_score:
                best_score = score
                best_call = candidate
                best_offset = offset
                best_breakdown = {
                    "drift": drift,
                    "compression": compression,
                    "recency": recency_why,
                    "weighted_total": score,
                }

        if best_call is None or not _has_content_signal(best_breakdown):
            return self._finish(
                _abstain(session_id, failed_call_id, UNATTRIBUTABLE_REASON)
            )

        reason = self._explain(best_call, best_offset, best_breakdown)
        content_confidence = (
            DRIFT_WEIGHT * float(best_breakdown.get("drift") or 0.0)
            + COMPRESSION_WEIGHT * float(best_breakdown.get("compression") or 0.0)
            + RECENCY_WEIGHT * float(best_breakdown.get("recency") or 0.0)
        )

        return self._finish(
            AttributionResult(
                session_id=session_id,
                failed_call_id=failed_call_id,
                root_cause_call_id=best_call.id if best_call else None,
                root_cause_step_offset=best_offset,
                confidence=round(min(1.0, max(0.0, content_confidence)), 4),
                reason=reason,
                signal_breakdown=best_breakdown,
            )
        )

    def attribute_session(self, session_id: int) -> list[AttributionResult]:
        """Attribute every failed call in a session.

        Returns one AttributionResult per call where `failed=True`.
        """
        calls = self._load_session_calls(session_id)
        results: list[AttributionResult] = []
        for call in calls:
            if call.failed:
                results.append(self.attribute_failure(session_id, call.id, calls=calls))
        return results

    def review_success_reply(
        self,
        session_id: int,
        call_id: int,
    ) -> Optional[AttributionResult]:
        """Flag a successful reply that contradicts stored session facts.

        This is a review signal, not a failure. ``failed`` on the call row
        is left unchanged. Findings reuse Layer 1's stable-ID regex and
        Layer 3's dollar-amount tokens; they do not judge real-world
        correctness. No row is written when the reply is clean.
        """
        from .facts import (
            FAILURE_KIND,
            KIND_CONTRADICTION,
            KIND_MISSING_CONTEXT,
            compressed_view,
            fact_review_enabled,
            find_reply_contradictions,
            format_findings,
        )

        if not fact_review_enabled():
            return None

        rows = self.storage.get_calls_for_session(session_id)
        current = next((r for r in rows if int(r["id"]) == int(call_id)), None)
        if current is None:
            return None
        reply = str(current.get("response_text") or "")
        if not reply.strip():
            return None

        history: list[dict[str, Any]] = []
        for row in rows:
            if int(row["id"]) > int(call_id):
                break
            for msg in _parse_stored_messages(row.get("messages_json")):
                item = dict(msg)
                item["call_id"] = int(row["id"])
                history.append(item)

        request = _parse_stored_messages(current.get("messages_json"))
        outbound = compressed_view(request) if request else None
        findings = find_reply_contradictions(
            history, reply, compressed_outbound=outbound
        )
        if not findings:
            return None

        has_contradiction = any(f.kind == KIND_CONTRADICTION for f in findings)
        if has_contradiction and any(f.fact_type == "stable_id" for f in findings):
            confidence = 0.85
        elif has_contradiction:
            confidence = 0.70
        else:
            confidence = 0.55

        source_ids = [f.source_call_id for f in findings if f.source_call_id]
        root_cause = source_ids[-1] if source_ids else int(call_id)
        breakdown = {
            KIND_CONTRADICTION: 1.0 if has_contradiction else 0.0,
            KIND_MISSING_CONTEXT: 0.0 if has_contradiction else 1.0,
        }
        result = AttributionResult(
            session_id=int(session_id),
            failed_call_id=int(call_id),
            root_cause_call_id=int(root_cause),
            root_cause_step_offset=None,
            confidence=confidence,
            reason=format_findings(findings),
            signal_breakdown=breakdown,
            failure_kind=FAILURE_KIND,
        )
        return self._finish(result)
    

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
