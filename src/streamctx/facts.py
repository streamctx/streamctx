"""Session-grounded stable-fact review.

This is not a hallucination detector. It compares an assistant reply
against facts that are already present verbatim in the session's own
stored history. The SDK has no external ground truth and does not
claim to judge real-world correctness.

In scope
--------
* Same-family stable IDs (``ACME-9917`` vs ``ACME-1234``), using the
  Layer 1 compressor regex.
* ``$`` amounts, using the dollar subset of Layer 3's repair fact regex,
  with scale-variant suppression so ``$12.4`` vs ``$12,400,000`` is
  treated as paraphrase, not contradiction.

Out of scope
------------
* Factual correctness against the world (legal advice, science, etc.).
* Bare decimals, years, or unprefixed numbers (too noisy).
* New ID families the session never used (the assistant may mint IDs).
* Completeness: omitting a known fact is not a finding.

Ground truth is the latest non-question USER/SYSTEM assertion per
family. Assistant text never becomes truth, so a prior wrong reply
cannot launder itself. A later user statement ("reassigned to
ACME-4401") updates the family; restating the new value is not a
contradiction. Messages containing ``?`` do not update ground truth, so
a sycophancy trap ("it's ACME-1234, right?") cannot redefine the fact.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Optional

from .compressor import _STABLE_ID_RE, _message_text, compress_messages

# Layer 3 `_REPAIR_FACT_RE` dollar arm is `\$\d+(?:\.\d+)?`. Commas are
# added so `$12,400,000` is the same class of token, then compared as a
# scale variant of `$12.4` rather than a contradiction.
_DOLLAR_RE = re.compile(r"\$\d+(?:,\d{3})*(?:\.\d+)?")

_USD_FAMILY = "$"
_SCALE_RATIOS = (1_000.0, 1_000_000.0, 1_000_000_000.0)
_SCALE_TOLERANCE = 0.01
_DOLLAR_ABS_EPS = 0.005

KIND_CONTRADICTION = "contradiction"
KIND_MISSING_CONTEXT = "missing_context"
FAILURE_KIND = "stable_fact_review"


def fact_review_enabled() -> bool:
    raw = os.environ.get("STREAMCTX_FACT_REVIEW", "1")
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


@dataclass(frozen=True)
class FactFinding:
    kind: str
    fact_type: str
    family: str
    expected: str
    observed: str
    expected_in_compressed: bool
    source_call_id: Optional[int] = None


@dataclass
class _GroundFact:
    family: str
    value: str
    fact_type: str
    numeric: Optional[float] = None
    source_call_id: Optional[int] = None


def _id_family(token: str) -> str:
    for sep in ("-", "_"):
        if sep in token:
            return token.split(sep, 1)[0]
    return token


def _parse_dollar(token: str) -> float:
    return float(token.replace("$", "").replace(",", ""))


def _is_scale_variant(left: float, right: float) -> bool:
    if left <= 0 or right <= 0:
        return False
    ratio = max(left, right) / min(left, right)
    for scale in _SCALE_RATIOS:
        if abs(ratio - scale) / scale <= _SCALE_TOLERANCE:
            return True
    return False


def _dollars_equivalent(left: float, right: float) -> bool:
    if abs(left - right) <= _DOLLAR_ABS_EPS:
        return True
    denom = max(abs(left), abs(right), 1e-9)
    if abs(left - right) / denom <= 0.001:
        return True
    return _is_scale_variant(left, right)


def extract_stable_ids(text: str) -> list[str]:
    return [m.group(0) for m in _STABLE_ID_RE.finditer(text or "")]


def extract_dollar_amounts(text: str) -> list[str]:
    return [m.group(0) for m in _DOLLAR_RE.finditer(text or "")]


def _is_question(text: str) -> bool:
    return "?" in (text or "")


def _role(msg: dict[str, Any]) -> str:
    return str(msg.get("role") or "").strip().lower()


def extract_ground_truth(messages: list[dict[str, Any]]) -> dict[str, _GroundFact]:
    """Latest non-question user/system assertion per ID family and $ slot."""
    ground: dict[str, _GroundFact] = {}
    for msg in messages:
        if _role(msg) not in {"user", "system"}:
            continue
        text = _message_text(msg)
        if not text.strip() or _is_question(text):
            continue
        call_id = msg.get("call_id")
        try:
            source_call_id = int(call_id) if call_id is not None else None
        except (TypeError, ValueError):
            source_call_id = None
        for token in extract_stable_ids(text):
            family = _id_family(token)
            ground[family] = _GroundFact(
                family=family,
                value=token,
                fact_type="stable_id",
                source_call_id=source_call_id,
            )
        dollars = extract_dollar_amounts(text)
        if dollars:
            token = dollars[-1]
            ground[_USD_FAMILY] = _GroundFact(
                family=_USD_FAMILY,
                value=token,
                fact_type="dollar",
                numeric=_parse_dollar(token),
                source_call_id=source_call_id,
            )
    return ground


def _blob(messages: Optional[list[dict[str, Any]]]) -> str:
    if not messages:
        return ""
    return " ".join(_message_text(m) for m in messages)


def _in_compressed(token: str, compressed_blob: str) -> bool:
    if not token:
        return False
    if not compressed_blob:
        return True
    return token in compressed_blob


def find_reply_contradictions(
    history_messages: list[dict[str, Any]],
    reply_text: str,
    compressed_outbound: Optional[list[dict[str, Any]]] = None,
) -> list[FactFinding]:
    """Return session-grounded findings for one assistant reply.

    A family is flagged only when the reply uses a *different* value and
    does not also contain the ground-truth value (recaps like "was
    ACME-9917, now ACME-4401" therefore do not fire). If the ground-truth
    token is absent from ``compressed_outbound``, the finding is
    ``missing_context`` rather than ``contradiction`` — the SDK dropped
    the fact on purpose. When ``compressed_outbound`` is omitted, the
    check assumes the fact was available.
    """
    reply = reply_text or ""
    if not reply.strip():
        return []

    ground = extract_ground_truth(history_messages)
    if not ground:
        return []

    compressed_blob = _blob(compressed_outbound)
    reply_ids: dict[str, list[str]] = {}
    for token in extract_stable_ids(reply):
        reply_ids.setdefault(_id_family(token), []).append(token)
    reply_dollars = extract_dollar_amounts(reply)

    findings: list[FactFinding] = []

    for family, fact in ground.items():
        if fact.fact_type == "stable_id":
            observed_list = reply_ids.get(family) or []
            if not observed_list:
                continue
            if fact.value in observed_list:
                continue
            available = _in_compressed(fact.value, compressed_blob)
            findings.append(
                FactFinding(
                    kind=KIND_CONTRADICTION if available else KIND_MISSING_CONTEXT,
                    fact_type="stable_id",
                    family=family,
                    expected=fact.value,
                    observed=observed_list[0],
                    expected_in_compressed=available,
                    source_call_id=fact.source_call_id,
                )
            )
            continue

        if fact.fact_type == "dollar":
            if not reply_dollars or fact.numeric is None:
                continue
            if any(
                _dollars_equivalent(fact.numeric, _parse_dollar(token))
                for token in reply_dollars
            ):
                continue
            available = _in_compressed(fact.value, compressed_blob)
            findings.append(
                FactFinding(
                    kind=KIND_CONTRADICTION if available else KIND_MISSING_CONTEXT,
                    fact_type="dollar",
                    family=_USD_FAMILY,
                    expected=fact.value,
                    observed=reply_dollars[0],
                    expected_in_compressed=available,
                    source_call_id=fact.source_call_id,
                )
            )

    return findings


def compressed_view(
    messages: list[dict[str, Any]],
    max_tokens: int = 2000,
    keep_last_n: int = 4,
) -> list[dict[str, Any]]:
    """Replay Layer 1 compression on the stored (uncompressed) request."""
    outbound, _, _ = compress_messages(
        messages, max_tokens=max_tokens, keep_last_n=keep_last_n
    )
    return outbound


def format_findings(findings: list[FactFinding]) -> str:
    parts = []
    for item in findings:
        parts.append(
            f"{item.kind} {item.fact_type}: expected {item.expected}, "
            f"reply used {item.observed}"
        )
    return "; ".join(parts)
