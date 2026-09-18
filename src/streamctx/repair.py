"""Verified Repair Engine for StreamCtx.

Orchestrates AttributionEngine + CounterfactualReplayer into a single
``verify_fix()`` pipeline: attribute a failure, generate a signal-based
fix candidate, replay with the fix injected, and attest whether the
original failure condition is gone.

Layer 3 is MIT-licensed core SDK logic. Nothing in this module is gated
on a paid tier, license check, or hosted-only flag. ``classify_failure()``
lives in ``failure.py`` (below Layer 2 and Layer 3) and is re-exported
here so existing ``from streamctx.repair import classify_failure``
imports keep working. The binary ``infra_error`` / ``content_error``
contract is unchanged.

``verify_fix()`` is counterfactual: it never writes checkpoints or call
rows. A live session is not mutated, even when ``resolved=True``. Shadow
repair is dry-run only. Applying a candidate is out of band (human /
caller), not automatic.

This is the v1 verified-repair loop described in the StreamCtx design:
attribution finds *where* and *why*, replay proves whether a candidate
fix actually removes the failure.  Dry-run mode reconstructs the
injection and attestation without calling an LLM.

Core API::

    from streamctx.repair import VerifiedRepairEngine

    engine = VerifiedRepairEngine()

    # Dry run — attribute + generate a fix, no LLM calls
    result = engine.verify_fix(session_id=8, failed_call_id=42)
    print(result.fix_candidate)
    print(result.proof["before_after_diff"])

    # Live verification — real LLM calls
    result = engine.verify_fix(
        session_id=8,
        failed_call_id=42,
        llm_fn=lambda messages: client.chat.completions.create(
            model="openrouter/free",
            messages=messages,
        ),
        dry_run=False,
    )
    print(result.resolved, result.confidence_delta)

Or via the factory::

    from streamctx.repair import get_repair_engine
    result = get_repair_engine().verify_fix(session_id, failed_call_id)
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from .attribution import AttributionEngine, AttributionResult, DEFAULT_LOOKBACK
from .compressor import _message_text, compress_messages
from .failure import classify_failure
from .replay import CounterfactualReplayer, ReplayResult
from .storage import get_storage

# Dominant attribution signals we know how to turn into a fix.
_SIGNAL_KEYS = ("drift", "compression", "recency")

INFRA_NOT_REPAIRABLE = (
    "Not repairable via context injection — root cause is an "
    "infrastructure/API error, not a context-quality issue."
)

UNFIXABLE_CONTENT = (
    "Not repairable via context injection — the question is "
    "ambiguous/unanswerable; a different reply is not a verified fix."
)

_AMBIGUOUS_QUESTION_RE = re.compile(
    r"unnamed|unspecified|ambiguous|unanswerable|"
    r"which of the (?:three|two|several)|"
    r"\bsomeone\b|\bsomebody\b",
    re.IGNORECASE,
)
_UNCERTAIN_REPLY_RE = re.compile(
    r"don'?t have enough information|do not have enough|"
    r"not enough information|insufficient (?:information|context)|"
    r"\bambiguous\b|i don'?t know|i do not know|"
    r"can(?:not|'t) (?:answer|determine|tell)|i'?m not sure|"
    r"without (?:more|additional) (?:information|context|detail)|"
    r"does(?:n'?t| not) specify",
    re.IGNORECASE,
)


def is_unfixable_content_failure(messages: list[dict[str, Any]]) -> bool:
    """True when the failure is an unanswerable question, not a context artifact.

    Context injection cannot invent missing referents.  If the last user
    turn is ambiguous/unspecified and the model already answered with
    reasonable uncertainty, a differently worded replay is not a fix.
    """
    last_user = ""
    last_assistant = ""
    for msg in messages:
        role = msg.get("role")
        content = str(msg.get("content") or "")
        if role == "user" and content.strip():
            last_user = content
        elif role == "assistant" and content.strip():
            last_assistant = content
    if not last_user or not last_assistant:
        return False
    return bool(
        _AMBIGUOUS_QUESTION_RE.search(last_user)
        and _UNCERTAIN_REPLY_RE.search(last_assistant)
    )

# Human-readable strategy name stored on the attestation.
_SIGNAL_STRATEGY = {
    "compression": "dedupe",
    "drift": "reanchor",
    "recency": "resurface",
}

# One Layer-2 lookback window of auto-repairs. More than that in a session
# is the same cause recurring, not a new one — give up to human review.
MAX_REPAIR_ATTEMPTS_PER_SESSION = DEFAULT_LOOKBACK
# Live llm_fn hang must not block the caller indefinitely. 30s is above a
# slow free-tier completion and below a stuck socket.
REPAIR_LLM_TIMEOUT_S = 30.0

INVENTED_CORRECT_VALUE = (
    "Rejected: correct_value is not present in session evidence "
    "(circular or invented verification)."
)
ECHOED_INJECTION = (
    "Rejected: replay echoed the repair injection rather than answering "
    "from restored context."
)
NEEDS_HUMAN_REVIEW_LOOP = (
    "Repair attempt cap reached for this session; routing to human review."
)

# Dollar amounts, decimals, years/long ids, and stable IDs. Lone filler
# indexes ("0", "10") are not restore-worthy facts.
_REPAIR_FACT_RE = re.compile(
    r"\$\d+(?:\.\d+)?|\d+\.\d+|\d{4,}|[A-Z]{2,}[-_][A-Z0-9]{2,}"
)


@dataclass
class RepairResult:
    """Result of a verified-repair run.

    Attributes
    ----------
    session_id:
        The session that was repaired.
    failed_call_id:
        The call that originally failed.
    root_cause_call_id:
        The attributed root-cause call, if one was found.
    dominant_signal:
        The strongest attribution signal (``drift``, ``compression``,
        or ``recency``).
    fix_candidate:
        The generated fix that was (or would be) injected — a single
        message dict or a list of message dicts.
    resolved:
        Whether the original correct value is present in the replayed
        response.  Always ``False`` for dry runs, or when no
        ``correct_value`` was supplied to check against.
    confidence_delta:
        Change in confidence after verification.  ``0.0`` on dry runs;
        ``1.0 - attribution.confidence`` if resolved; ``-attribution.confidence``
        if the correct value was not restored.
    proof:
        Attestation dict with a UTC timestamp and a before/after
        ``diff_replay`` payload.
    dry_run:
        Whether this was a dry run (no LLM calls).
    reason:
        Human-readable outcome, including why an infra error was skipped.
    correct_value:
        The known-good fact that must appear in the new reply for
        ``resolved=True`` (string or list of required substrings).
    applied:
        Always ``False`` from ``verify_fix()``. Layer 3 never writes the
        candidate into the live session.
    needs_human_review:
        True when auto-repair must stop (unfixable, unverifiable, timeout,
        missing root cause, empty compression candidate).
    """

    session_id: int
    failed_call_id: int
    root_cause_call_id: Optional[int]
    dominant_signal: Optional[str]
    fix_candidate: dict[str, Any] | list[dict[str, Any]]
    resolved: bool
    confidence_delta: float
    proof: dict[str, Any] = field(default_factory=dict)
    dry_run: bool = True
    reason: str = ""
    correct_value: str | list[str] | None = None
    applied: bool = False
    needs_human_review: bool = False


class VerifiedRepairEngine:
    """Verified Repair Engine.

    Wraps AttributionEngine and CounterfactualReplayer (real SQLite
    storage by default) and exposes ``verify_fix()`` — attribute a
    failure, generate a signal-based fix, replay it, and return a
    signed-style attestation of whether the failure cleared.
    """

    def __init__(
        self,
        storage: Any = None,
        attribution_engine: Any = None,
        replayer: Any = None,
        evidence: Any = None,
    ) -> None:
        self.storage = storage or get_storage()
        self.evidence = evidence
        self.llm_timeout_s = REPAIR_LLM_TIMEOUT_S
        self.attribution = attribution_engine or AttributionEngine(
            storage=self.storage, evidence=evidence
        )
        self.replayer = replayer or CounterfactualReplayer(storage=self.storage)

    def _finish(self, result: RepairResult) -> RepairResult:
        """Best-effort Layer 4 attestation; never breaks repair."""
        from .evidence import safe_append_evidence

        safe_append_evidence(
            "repair",
            result.failed_call_id,
            asdict(result),
            ledger=self.evidence,
        )
        return result

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def verify_fix(
        self,
        session_id: int,
        failed_call_id: int,
        llm_fn: Optional[Callable[[list[dict[str, Any]]], Any]] = None,
        dry_run: bool = True,
        correct_value: str | list[str] | None = None,
    ) -> RepairResult:
        """Attribute a failure, inject a candidate fix, and verify it.

        Parameters
        ----------
        session_id:
            ID of the session that contains the failed call.
        failed_call_id:
            ID of the call row to repair.
        llm_fn:
            Required when ``dry_run=False``.  A callable that takes a
            ``list[dict]`` of messages and returns an LLM response
            object — same contract as ``CounterfactualReplayer.replay``.
        dry_run:
            If ``True`` (default), attribute + generate a fix and build
            the attestation via ``diff_replay`` — no LLM calls, no API
            cost.  ``resolved`` stays ``False`` because the failure
            condition was not live-checked.
            If ``False``, calls ``llm_fn`` through a live replay and
            checks whether ``correct_value`` is present in the new
            response.  Absence of the old failure string is not enough.
        correct_value:
            Known-good fact(s) that must appear in the replayed reply.
            A string is matched case-insensitively; a list requires
            every item to be present.  If omitted, ``resolved`` stays
            ``False`` — we cannot attest that the right answer returned.

        Returns
        -------
        RepairResult
        """
        failed_call = self._load_call(session_id, failed_call_id)
        error_message = None if failed_call is None else failed_call.get("error_message")
        failure_class = classify_failure(error_message)

        if failure_class == "infra_error":
            return self._infra_unresolved(
                session_id=session_id,
                failed_call_id=failed_call_id,
                error_message=str(error_message or ""),
                dry_run=dry_run,
            )

        if not dry_run and llm_fn is None:
            raise ValueError(
                "llm_fn is required for live repair (dry_run=False). "
                "Pass a callable: llm_fn=lambda msgs: client.chat.completions.create(...)"
            )

        attribution = self.attribution.attribute_failure(session_id, failed_call_id)
        dominant = self._dominant_signal(attribution.signal_breakdown)
        failure_condition = self._failure_condition(failed_call)

        if attribution.root_cause_call_id is None:
            return self._unresolved(
                session_id=session_id,
                failed_call_id=failed_call_id,
                attribution=attribution,
                dominant=dominant,
                fix_candidate={},
                failure_condition=failure_condition,
                dry_run=dry_run,
                extra_proof={"reason": attribution.reason},
                needs_human_review=True,
            )

        root_messages = self._call_messages(session_id, attribution.root_cause_call_id)
        if not root_messages and failed_call is not None:
            root_messages = self._parse_messages(failed_call.get("messages_json"))

        fix_candidate = self._generate_fix_candidate(
            dominant, root_messages, session_id=session_id
        )
        from_step = self._step_for_call(session_id, attribution.root_cause_call_id)

        before_after_diff = self.replayer.diff_replay(
            session_id=session_id,
            from_step=from_step,
            with_context=fix_candidate,
        )
        pre_repair = self._checkpoint_snapshot(
            from_step, before_after_diff.get("original_messages") or []
        )

        live_fn = llm_fn
        if not dry_run and llm_fn is not None:
            live_fn = _llm_fn_with_timeout(llm_fn, self.llm_timeout_s)

        replay_result = self.replayer.replay(
            session_id=session_id,
            from_step=from_step,
            with_context=fix_candidate,
            dry_run=dry_run,
            llm_fn=live_fn,
        )

        unfixable = is_unfixable_content_failure(
            self._parse_messages(failed_call.get("messages_json") if failed_call else None)
        )
        replay_error = _replay_had_error(replay_result)
        empty_compression = (
            dominant == "compression" and not self._candidate_text(fix_candidate).strip()
        )

        if dry_run:
            resolved = False
            confidence_delta = 0.0
            if unfixable:
                reason = UNFIXABLE_CONTENT
            elif empty_compression:
                reason = "No dropped facts to restore — compression candidate empty."
            else:
                reason = attribution.reason
            needs_human = unfixable or empty_compression or replay_error
        else:
            if unfixable:
                resolved = False
                reason = UNFIXABLE_CONTENT
                needs_human = True
            elif replay_error:
                resolved = False
                reason = "Repair replay failed or timed out; original session left untouched."
                needs_human = True
            elif empty_compression:
                resolved = False
                reason = "No dropped facts to restore — compression candidate empty."
                needs_human = True
            else:
                resolved, verify_reason = self._independent_verification(
                    correct_value=correct_value,
                    replay_result=replay_result,
                    fix_candidate=fix_candidate,
                    session_id=session_id,
                )
                reason = verify_reason or attribution.reason
                needs_human = (not resolved) and bool(verify_reason)
            confidence_delta = self._confidence_delta(attribution.confidence, resolved)

        proof = self._attestation(
            session_id=session_id,
            failed_call_id=failed_call_id,
            attribution=attribution,
            dominant=dominant,
            from_step=from_step,
            failure_condition=failure_condition,
            before_after_diff=before_after_diff,
            replay_result=replay_result,
            resolved=resolved,
            dry_run=dry_run,
            failure_class=failure_class,
            unfixable_content=unfixable,
            correct_value=correct_value,
            applied=False,
            needs_human_review=needs_human,
            pre_repair_checkpoint=pre_repair,
        )

        return self._finish(
            RepairResult(
                session_id=session_id,
                failed_call_id=failed_call_id,
                root_cause_call_id=attribution.root_cause_call_id,
                dominant_signal=dominant,
                fix_candidate=fix_candidate,
                resolved=resolved,
                confidence_delta=confidence_delta,
                proof=proof,
                dry_run=dry_run,
                reason=reason,
                correct_value=correct_value,
                applied=False,
                needs_human_review=needs_human,
            )
        )

    # ------------------------------------------------------------------
    # Fix generation
    # ------------------------------------------------------------------

    def _generate_fix_candidate(
        self,
        signal: Optional[str],
        messages: list[dict[str, Any]],
        session_id: Optional[int] = None,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        """Build an injectable fix from the dominant attribution signal.

        compression → re-inject facts Layer 1 ``compress_messages()`` would
                     drop from the *attributed* uncompressed request
        drift       → re-anchor to the original task framing (not the drifted step)
        recency     → re-surface the earlier assigned task (not the latest tangent)

        Compression must not use the earliest call: that re-injects stale
        framing (Lyon) that a later uncompressed window had already replaced
        (Phoenix / $12.4). Unknown signals do not silently fall through to
        a compression candidate.
        """
        earliest = (
            self._earliest_call_messages(session_id) if session_id is not None else []
        )

        if signal == "compression":
            return self._fix_compression_dedupe(messages)
        if signal == "drift":
            return self._fix_drift_reanchor(earliest or messages)
        if signal == "recency":
            return self._fix_recency_resurface(earliest or messages)
        return {}

    def _earliest_call_messages(self, session_id: int) -> list[dict[str, Any]]:
        """Known-good framing: the first call in the session, not the failure."""
        rows = self.storage.get_calls_for_session(session_id)
        if not rows:
            return []
        return self._parse_messages(rows[0].get("messages_json"))

    @staticmethod
    def _first_content(messages: list[dict[str, Any]], role: str) -> str:
        for msg in messages:
            content = str(msg.get("content") or "").strip()
            if msg.get("role") == role and content:
                return content
        return ""

    @staticmethod
    def _fact_terms(text: str) -> set[str]:
        return {m.group(0) for m in _REPAIR_FACT_RE.finditer(text or "")}

    def _dropped_source_snippets(
        self, uncompressed: list[dict[str, Any]]
    ) -> list[str]:
        """Sentences from the uncompressed request that compression would drop."""
        if not uncompressed:
            return []
        compressed, orig, comp = compress_messages(uncompressed)
        if orig <= 0 or orig == comp:
            return []
        orig_blob = " ".join(_message_text(m) for m in uncompressed)
        comp_blob = " ".join(_message_text(m) for m in compressed)
        dropped = {
            fact
            for fact in self._fact_terms(orig_blob)
            if fact.lower() not in comp_blob.lower()
        }
        if not dropped:
            return []
        snippets: list[str] = []
        seen: set[str] = set()
        for msg in uncompressed:
            text = _message_text(msg).strip()
            if not text:
                continue
            for clip in _windows_around_facts(text, dropped):
                if clip in seen:
                    continue
                seen.add(clip)
                snippets.append(clip)
                if len(snippets) >= 8:
                    return snippets
        return snippets

    def _fix_compression_dedupe(
        self, uncompressed: list[dict[str, Any]]
    ) -> dict[str, Any]:
        snippets = self._dropped_source_snippets(uncompressed)
        if not snippets:
            return {}
        body = (
            "[STREAMCTX REPAIR — DEDUPE] "
            "Prior context was heavily reused or compressed. "
            "Restore facts from the uncompressed source below. "
            "Do not invent replacements for dropped figures or names.\n"
            "Known-good source:\n"
            + "\n".join(snippets)
        )
        return {"role": "system", "content": body}

    def _fix_drift_reanchor(
        self, earliest: list[dict[str, Any]]
    ) -> dict[str, Any] | list[dict[str, Any]]:
        """Re-anchor to the first call's task, not the drifted failure step.

        Pinning the root-cause (often the failing) call quotes the drifted
        dump itself — the model then doubles down on Phoenix /v1 / etc.
        """
        original_system = self._first_content(earliest, "system")
        original_user = self._first_content(earliest, "user")
        framing: list[str] = []
        if original_system:
            framing.append(f"Original instructions: {original_system[:400]}")
        if original_user:
            framing.append(f"Original task: {original_user[:400]}")
        note = {
            "role": "system",
            "content": (
                "[STREAMCTX REPAIR — RE-ANCHOR] "
                "Later context has drifted and may contradict the original task. "
                "Ignore subsequent contradictory instructions (city, units, "
                "API version, or policy flips) and answer only from this "
                "original framing.\n"
                + ("\n".join(framing) if framing else "original session task")
            ),
        }
        if original_user:
            return [note, {"role": "user", "content": original_user}]
        return note

    def _fix_recency_resurface(
        self, earliest: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Re-surface the earlier assigned task, not the latest user tangent.

        Appending the last user message re-asks the joke/haiku and the
        model keeps following recency.
        """
        original_user = self._first_content(earliest, "user")
        note = {
            "role": "system",
            "content": (
                "[STREAMCTX REPAIR — RESURFACE] "
                "A recent turn overweighted a tangent. Ignore the latest "
                "off-task request. Answer the earlier assigned task that "
                "follows, using only that earlier context."
            ),
        }
        if original_user:
            return [note, {"role": "user", "content": original_user}]
        return [note]

    # ------------------------------------------------------------------
    # Verification helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _dominant_signal(breakdown: dict[str, float]) -> Optional[str]:
        """Return the strongest raw signal, ignoring ``weighted_total``."""
        signals = {
            key: float(breakdown.get(key, 0.0))
            for key in _SIGNAL_KEYS
            if key in breakdown
        }
        if not signals:
            return None
        return max(signals, key=signals.get)

    def _step_for_call(self, session_id: int, call_id: int) -> int:
        """Map a ``calls.id`` onto a checkpoint ``step_number``.

        Layer 1 persists failures to ``calls`` only — they do not move
        the resume checkpoint. Pairing call ordinal with checkpoint
        ordinal therefore mis-maps a failure that sits between two
        successes. Successes consume checkpoints in order; failures
        replay from the last success. When tests/seeds store one
        checkpoint per call (including the failed row), fall back to a
        1:1 zip so content-quality rows that *were* checkpointed still
        replay from their own step.
        """
        calls = self.storage.get_calls_for_session(session_id)
        checkpoints = self.replayer.list_checkpoints(session_id)
        if not checkpoints:
            index_by_id = {int(row["id"]): idx for idx, row in enumerate(calls)}
            idx = index_by_id.get(int(call_id))
            return 0 if idx is None else idx + 1

        if len(checkpoints) == len(calls):
            index_by_id = {int(row["id"]): idx for idx, row in enumerate(calls)}
            idx = index_by_id.get(int(call_id))
            if idx is None:
                return int(checkpoints[-1]["step_number"])
            return int(checkpoints[idx]["step_number"])

        last_success_step = int(checkpoints[0]["step_number"])
        ckpt_i = 0
        mapped: dict[int, int] = {}
        for row in calls:
            cid = int(row["id"])
            if not row.get("failed"):
                if ckpt_i < len(checkpoints):
                    last_success_step = int(checkpoints[ckpt_i]["step_number"])
                    ckpt_i += 1
                mapped[cid] = last_success_step
            else:
                mapped[cid] = last_success_step
        return mapped.get(int(call_id), last_success_step)

    def _load_call(
        self, session_id: int, call_id: int
    ) -> Optional[dict[str, Any]]:
        for row in self.storage.get_calls_for_session(session_id):
            if int(row["id"]) == int(call_id):
                return row
        return None

    def _call_messages(
        self, session_id: int, call_id: int
    ) -> list[dict[str, Any]]:
        row = self._load_call(session_id, call_id)
        if row is None:
            return []
        return self._parse_messages(row.get("messages_json"))

    @staticmethod
    def _parse_messages(raw: Any) -> list[dict[str, Any]]:
        if isinstance(raw, list):
            return raw
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return []
        return parsed if isinstance(parsed, list) else []

    def _failure_condition(self, failed_call: Optional[dict[str, Any]]) -> str:
        if not failed_call:
            return ""
        error = failed_call.get("error_message")
        if error:
            return str(error)
        # Flagged bad response with no API error — use the last assistant
        # turn so live replay can check whether that quality failure repeats.
        for msg in reversed(self._parse_messages(failed_call.get("messages_json"))):
            if msg.get("role") == "assistant" and msg.get("content"):
                return str(msg["content"])
        return ""

    @staticmethod
    def _fold_text(text: str) -> str:
        table = str.maketrans("àâäáéèêëïîíôöóùûüúç", "aaaaeeeeiiiooouuuuc")
        folded = text.lower().translate(table)
        return re.sub(r"\s+", " ", folded)

    def _replay_text(self, replay_result: ReplayResult) -> str:
        texts: list[str] = []
        for response in replay_result.counterfactual_responses:
            if isinstance(response, dict) and response.get("error"):
                return ""
            texts.append(CounterfactualReplayer._extract_response_text(response))
        return "\n".join(texts)

    def _correct_value_restored(
        self,
        correct_value: str | list[str] | None,
        replay_result: ReplayResult,
    ) -> bool:
        """True only when every required correct value appears in the replay."""
        if not replay_result.counterfactual_responses:
            return False
        values = (
            [correct_value]
            if isinstance(correct_value, str)
            else list(correct_value or [])
        )
        values = [str(v).strip() for v in values if str(v).strip()]
        if not values:
            return False

        combined = self._replay_text(replay_result)
        if not combined.strip():
            return False
        haystack = self._fold_text(combined)
        return all(self._fold_text(v) in haystack for v in values)

    def _session_evidence_text(self, session_id: int) -> str:
        parts: list[str] = []
        for row in self.storage.get_calls_for_session(session_id):
            for msg in self._parse_messages(row.get("messages_json")):
                text = str(msg.get("content") or "")
                if text.strip():
                    parts.append(text)
        return "\n".join(parts)

    @staticmethod
    def _candidate_text(fix_candidate: dict[str, Any] | list[dict[str, Any]]) -> str:
        if isinstance(fix_candidate, dict):
            items = [fix_candidate]
        else:
            items = list(fix_candidate or [])
        return "\n".join(str(m.get("content") or "") for m in items if isinstance(m, dict))

    def _is_injection_echo(
        self,
        replay_result: ReplayResult,
        fix_candidate: dict[str, Any] | list[dict[str, Any]],
    ) -> bool:
        reply = self._fold_text(self._replay_text(replay_result))
        if "[streamctx repair" in reply:
            return True
        inj = self._fold_text(self._candidate_text(fix_candidate))
        if not reply or not inj or len(reply) < 80:
            return False
        reply_words = set(reply.split())
        inj_words = set(inj.split())
        if not reply_words:
            return False
        return (len(reply_words & inj_words) / len(reply_words)) >= 0.85

    def _independent_verification(
        self,
        correct_value: str | list[str] | None,
        replay_result: ReplayResult,
        fix_candidate: dict[str, Any] | list[dict[str, Any]],
        session_id: int,
    ) -> tuple[bool, str]:
        """Verify the reply against session evidence, not the attribution signal.

        Presence of ``correct_value`` in the replay is necessary but not
        sufficient: the value must already exist in stored session messages
        (so an LLM echo of an invented string cannot pass), and the reply
        must not be a copy of the injection note.
        """
        if not self._correct_value_restored(correct_value, replay_result):
            return False, ""
        values = (
            [correct_value]
            if isinstance(correct_value, str)
            else list(correct_value or [])
        )
        values = [str(v).strip() for v in values if str(v).strip()]
        evidence = self._fold_text(self._session_evidence_text(session_id))
        for value in values:
            if self._fold_text(value) not in evidence:
                return False, INVENTED_CORRECT_VALUE
        if self._is_injection_echo(replay_result, fix_candidate):
            return False, ECHOED_INJECTION
        return True, ""

    @staticmethod
    def _checkpoint_snapshot(
        from_step: int, original_messages: list[dict[str, Any]]
    ) -> dict[str, Any]:
        encoded = json.dumps(original_messages, sort_keys=True, default=str).encode()
        return {
            "from_step": from_step,
            "message_count": len(original_messages),
            "fingerprint": hashlib.sha256(encoded).hexdigest()[:16],
        }

    @staticmethod
    def _confidence_delta(before: float, resolved: bool) -> float:
        if resolved:
            return round(1.0 - before, 4)
        return round(-before, 4)

    def _unresolved(
        self,
        session_id: int,
        failed_call_id: int,
        attribution: AttributionResult,
        dominant: Optional[str],
        fix_candidate: dict[str, Any] | list[dict[str, Any]],
        failure_condition: str,
        dry_run: bool,
        extra_proof: Optional[dict[str, Any]] = None,
        needs_human_review: bool = True,
    ) -> RepairResult:
        proof = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "attestation": "streamctx.repair.v1",
            "session_id": session_id,
            "failed_call_id": failed_call_id,
            "root_cause_call_id": attribution.root_cause_call_id,
            "from_step": None,
            "dominant_signal": dominant,
            "fix_strategy": _SIGNAL_STRATEGY.get(dominant or "", None),
            "failure_condition": failure_condition,
            "resolved": False,
            "dry_run": dry_run,
            "before_after_diff": {},
            "attribution_confidence": attribution.confidence,
            "attribution_reason": attribution.reason,
            "failure_class": "content_error",
            "applied": False,
            "needs_human_review": needs_human_review,
        }
        if extra_proof:
            proof.update(extra_proof)
        return self._finish(
            RepairResult(
                session_id=session_id,
                failed_call_id=failed_call_id,
                root_cause_call_id=attribution.root_cause_call_id,
                dominant_signal=dominant,
                fix_candidate=fix_candidate,
                resolved=False,
                confidence_delta=0.0,
                proof=proof,
                dry_run=dry_run,
                reason=str((extra_proof or {}).get("reason") or attribution.reason),
                applied=False,
                needs_human_review=needs_human_review,
            )
        )

    def _infra_unresolved(
        self,
        session_id: int,
        failed_call_id: int,
        error_message: str,
        dry_run: bool,
    ) -> RepairResult:
        proof = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "attestation": "streamctx.repair.v1",
            "session_id": session_id,
            "failed_call_id": failed_call_id,
            "root_cause_call_id": None,
            "from_step": None,
            "dominant_signal": None,
            "fix_strategy": None,
            "failure_condition": error_message,
            "failure_class": "infra_error",
            "resolved": False,
            "dry_run": dry_run,
            "before_after_diff": {},
            "reason": INFRA_NOT_REPAIRABLE,
            "applied": False,
            "needs_human_review": False,
        }
        return self._finish(
            RepairResult(
                session_id=session_id,
                failed_call_id=failed_call_id,
                root_cause_call_id=None,
                dominant_signal=None,
                fix_candidate={},
                resolved=False,
                confidence_delta=0.0,
                proof=proof,
                dry_run=dry_run,
                reason=INFRA_NOT_REPAIRABLE,
                applied=False,
                needs_human_review=False,
            )
        )

    @staticmethod
    def _attestation(
        session_id: int,
        failed_call_id: int,
        attribution: AttributionResult,
        dominant: Optional[str],
        from_step: int,
        failure_condition: str,
        before_after_diff: dict[str, Any],
        replay_result: ReplayResult,
        resolved: bool,
        dry_run: bool,
        failure_class: str = "content_error",
        unfixable_content: bool = False,
        correct_value: str | list[str] | None = None,
        applied: bool = False,
        needs_human_review: bool = False,
        pre_repair_checkpoint: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        replay_texts: list[str] = []
        for response in replay_result.counterfactual_responses:
            if isinstance(response, dict) and response.get("error"):
                replay_texts.append(f"error: {response.get('error')}")
            else:
                replay_texts.append(
                    CounterfactualReplayer._extract_response_text(response)
                )
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "attestation": "streamctx.repair.v1",
            "session_id": session_id,
            "failed_call_id": failed_call_id,
            "root_cause_call_id": attribution.root_cause_call_id,
            "from_step": from_step,
            "dominant_signal": dominant,
            "fix_strategy": _SIGNAL_STRATEGY.get(dominant or "", None),
            "failure_condition": failure_condition,
            "failure_class": failure_class,
            "unfixable_content": unfixable_content,
            "correct_value": correct_value,
            "resolved": resolved,
            "dry_run": dry_run,
            "applied": applied,
            "needs_human_review": needs_human_review,
            "pre_repair_checkpoint": pre_repair_checkpoint or {},
            "before_after_diff": before_after_diff,
            "attribution_confidence": attribution.confidence,
            "attribution_reason": attribution.reason,
            "injection_summary": replay_result.injection_summary,
            "replay_texts": replay_texts,
        }


def _windows_around_facts(
    text: str, dropped: set[str], radius: int = 180
) -> list[str]:
    """Clip around dropped facts so a 400-char prefix cannot hide them."""
    lowered = text.lower()
    spans: list[tuple[int, int]] = []
    for fact in dropped:
        needle = fact.lower()
        start = 0
        while True:
            idx = lowered.find(needle, start)
            if idx < 0:
                break
            spans.append(
                (max(0, idx - radius), min(len(text), idx + len(fact) + radius))
            )
            start = idx + max(len(fact), 1)
    if not spans:
        return []
    spans.sort()
    merged = [spans[0]]
    for left, right in spans[1:]:
        prev_l, prev_r = merged[-1]
        if left <= prev_r:
            merged[-1] = (prev_l, max(prev_r, right))
        else:
            merged.append((left, right))
    return [text[left:right].strip() for left, right in merged]


def _replay_had_error(replay_result: ReplayResult) -> bool:
    for response in replay_result.counterfactual_responses:
        if isinstance(response, dict) and response.get("error"):
            return True
    return False


def _llm_fn_with_timeout(
    llm_fn: Callable[[list[dict[str, Any]]], Any],
    timeout_s: float,
) -> Callable[[list[dict[str, Any]]], Any]:
    def wrapped(messages: list[dict[str, Any]]) -> Any:
        if timeout_s is None or timeout_s <= 0:
            return llm_fn(messages)
        box: dict[str, Any] = {}
        errors: dict[str, BaseException] = {}

        def run() -> None:
            try:
                box["result"] = llm_fn(messages)
            except BaseException as exc:  # noqa: BLE001 — surface to caller
                errors["e"] = exc

        thread = threading.Thread(target=run, name="streamctx-repair-llm", daemon=True)
        thread.start()
        thread.join(timeout=timeout_s)
        if thread.is_alive():
            raise TimeoutError(f"repair llm_fn exceeded {timeout_s}s")
        if "e" in errors:
            raise errors["e"]
        return box.get("result")

    return wrapped


def get_repair_engine() -> VerifiedRepairEngine:
    """Convenience factory, mirrors get_storage() in storage.py."""
    return VerifiedRepairEngine()
