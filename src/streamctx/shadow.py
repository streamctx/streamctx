"""Passive shadow-repair monitor for dogfooding.

When a content-quality failure is persisted (model replied, but the
response was flagged bad — ``error_message`` empty/None), schedule a
``verify_fix(dry_run=True)`` in a background thread and append the
result to ``shadow_repair_log``.  Exceptions are swallowed so agents
never see this path.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from typing import Any, Optional

_shadow_lock = threading.Lock()
_shadow_threads: list[threading.Thread] = []


def _env_flag(name: str, default: str = "1") -> bool:
    raw = os.environ.get(name, default)
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


def should_shadow_repair(error_message: Optional[str]) -> bool:
    """True for content-quality failures with no exception text."""
    from .repair import classify_failure

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
    engine = VerifiedRepairEngine(storage=store)
    result = engine.verify_fix(
        session_id=session_id,
        failed_call_id=failed_call_id,
        dry_run=True,
    )
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
    )


def serialize_fix_candidate(fix_candidate: Any) -> str:
    if isinstance(fix_candidate, str):
        return fix_candidate
    try:
        return json.dumps(fix_candidate, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(fix_candidate)
