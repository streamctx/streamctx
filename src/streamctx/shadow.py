"""Passive shadow-repair monitor for dogfooding.

When a content-quality failure is persisted (model replied, but the
response was flagged bad — ``error_message`` empty/None), schedule a
``verify_fix(dry_run=True)`` in a background thread and append the
result to ``shadow_repair_log``.  Exceptions are swallowed so agents
never see this path.  Repairs are never applied to the live session.

Layer 3 is MIT core SDK. No paid flag, license check, or hosted gate.

Cap: at most one log row per failed call, and at most
``MAX_SHADOW_REPAIRS_PER_SESSION`` verify_fix runs per session (equal to
Layer 2 ``DEFAULT_LOOKBACK``). Further failures insert a single
``needs_human_review`` give-up row and stop.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from typing import Any, Optional

from .attribution import DEFAULT_LOOKBACK

_shadow_lock = threading.Lock()
_shadow_threads: list[threading.Thread] = []

# One lookback window of auto-repairs. More is a loop, not a new cause.
MAX_SHADOW_REPAIRS_PER_SESSION = DEFAULT_LOOKBACK
SHADOW_IN_PROGRESS = "in-progress"
SHADOW_GIVE_UP_REASON = (
    "needs_human_review: session repair attempt cap reached "
    f"({MAX_SHADOW_REPAIRS_PER_SESSION})"
)


def _env_flag(name: str, default: str = "1") -> bool:
    raw = os.environ.get(name, default)
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


def should_shadow_repair(error_message: Optional[str]) -> bool:
    """True for content-quality failures with no exception text."""
    from .failure import classify_failure

    if classify_failure(error_message) != "content_error":
        return False
    return error_message is None or not str(error_message).strip()


def maybe_schedule_shadow_repair(
    session_id: Optional[int],
    failed_call_id: Optional[int],
    error_message: Optional[str] = None,
    storage: Any = None,
) -> None:
    """No-op unless this is a content-quality failure with ids present."""
    if session_id is None or failed_call_id is None:
        return
    if not _env_flag("STREAMCTX_SHADOW_REPAIR", "1"):
        return
    try:
        if not should_shadow_repair(error_message):
            return
    except Exception:
        return

    if _env_flag("STREAMCTX_SHADOW_REPAIR_SYNC", "0"):
        _safe_run_shadow_repair(int(session_id), int(failed_call_id), storage)
        return

    thread = threading.Thread(
        target=_safe_run_shadow_repair,
        args=(int(session_id), int(failed_call_id), storage),
        name="streamctx-shadow-repair",
        daemon=True,
    )
    thread.start()
    with _shadow_lock:
        _shadow_threads.append(thread)
        _shadow_threads[:] = [t for t in _shadow_threads if t.is_alive()]


def wait_for_shadow_repair(timeout: float = 10.0) -> None:
    """Join outstanding shadow threads (tests)."""
    with _shadow_lock:
        threads = list(_shadow_threads)
        _shadow_threads.clear()
    for thread in threads:
        thread.join(timeout=timeout)


def _safe_run_shadow_repair(
    session_id: int,
    failed_call_id: int,
    storage: Any,
) -> None:
    try:
        _run_shadow_repair(session_id, failed_call_id, storage)
    except Exception:
        return


def _run_shadow_repair(
    session_id: int,
    failed_call_id: int,
    storage: Any,
) -> None:
    from .repair import VerifiedRepairEngine
    from .storage import get_storage

    store = storage if storage is not None else get_storage()
    begin = getattr(store, "begin_shadow_repair", None)
    row_id = None
    if begin is not None:
        row_id = begin(int(session_id), int(failed_call_id))
        if row_id is None:
            return

    engine = VerifiedRepairEngine(storage=store)
    try:
        result = engine.verify_fix(
            session_id=session_id,
            failed_call_id=failed_call_id,
            dry_run=True,
        )
    except Exception:
        finalize = getattr(store, "finalize_shadow_repair_log", None)
        if finalize is not None and row_id is not None:
            finalize(
                row_id,
                attribution_reason="shadow repair failed; original session left untouched",
                dominant_signal=None,
                fix_candidate={},
                resolved=False,
                dry_run=True,
                applied=False,
                needs_human_review=True,
            )
        elif getattr(store, "insert_shadow_repair_log", None) is not None and row_id is None:
            pass
        return

    finalize = getattr(store, "finalize_shadow_repair_log", None)
    if finalize is not None and row_id is not None:
        finalize(
            row_id,
            attribution_reason=result.reason,
            dominant_signal=result.dominant_signal,
            fix_candidate=result.fix_candidate,
            resolved=result.resolved,
            dry_run=result.dry_run,
            applied=result.applied,
            needs_human_review=result.needs_human_review,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        return

    insert = getattr(store, "insert_shadow_repair_log", None)
    if insert is None:
        return
    insert(
        session_id=result.session_id,
        failed_call_id=result.failed_call_id,
        attribution_reason=result.reason,
        dominant_signal=result.dominant_signal,
        fix_candidate=result.fix_candidate,
        timestamp=datetime.now(timezone.utc).isoformat(),
        resolved=result.resolved,
        dry_run=result.dry_run,
        applied=result.applied,
        needs_human_review=result.needs_human_review,
    )


def serialize_fix_candidate(fix_candidate: Any) -> str:
    if isinstance(fix_candidate, str):
        return fix_candidate
    try:
        return json.dumps(fix_candidate, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(fix_candidate)
