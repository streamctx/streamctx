"""Failure classification shared by Layer 2 and Layer 3.

Lives below both layers so attribution does not import repair.
Binary contract is ``infra_error`` vs ``content_error`` only.
"""

from __future__ import annotations

import re
from typing import Optional

# Context injection cannot repair these — they are API/config failures.
_INFRA_PATTERNS = (
    r"\b401\b",
    r"\b403\b",
    r"\b404\b",
    r"\b429\b",
    r"\b502\b",
    r"\b503\b",
    r"error code:\s*400",
    r"status(?:\s+code)?:\s*400",
    r"unauthorized",
    r"forbidden",
    r"invalid api key",
    r"authentication",
    r"auth(?:entication)? fail",
    r"rate[- ]?limit",
    r"too many requests",
    r"timeout",
    r"timed out",
    r"connection (?:refused|reset|error|aborted|timed)",
    r"network (?:error|unreachable|timeout)",
    r"malformed request",
    r"invalid request",
    r"bad request",
    r"invalid model",
    r"model[_ ]not[_ ]found",
    r"is not a valid model",
    r"model .+ (?:does not exist|not found|not available)",
    r"does-not-exist",
)

_INFRA_RE = re.compile("|".join(_INFRA_PATTERNS), re.IGNORECASE)


def classify_failure(error_message: Optional[str]) -> str:
    """Classify a failed call as ``infra_error`` or ``content_error``.

    ``infra_error``
        API/config failures that context injection cannot fix:
        invalid model IDs, auth (401/403), rate limits (429),
        network timeouts, malformed requests.

    ``content_error``
        The model responded (or would have) but the result was
        low-quality, incomplete, hallucinated, or drifted — including
        calls with no ``error_message`` but a flagged bad response.
    """
    if error_message is None:
        return "content_error"
    text = str(error_message).strip()
    if not text:
        return "content_error"
    if _INFRA_RE.search(text):
        return "infra_error"
    return "content_error"
