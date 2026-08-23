"""Shadow-run attribute_failure() over real (non-seeded) failed calls.

Diagnostic only: does not heal, repair, or replay.  Writes one row per
failed call into shadow_attribution_log and prints distributions,
including how many known load-test / SDK-bug failures were labeled
drift / compression / recency.

Usage::

    python scripts/shadow_run_attribution.py
    python scripts/shadow_run_attribution.py --sample 12
    python scripts/shadow_run_attribution.py --report-only
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from streamctx.attribution import AttributionEngine  # noqa: E402
from streamctx.repair import classify_failure  # noqa: E402
from streamctx.storage import SessionStorage, _default_db_path  # noqa: E402

# Distinctive strings that only appear in seed_* synthetic sessions.
SEED_FINGERPRINTS = (
    "$47.3 million",
    "Northwind Analytics Q3 2025 internal report",
    "What did the unnamed stakeholder decide about the unspecified",
    "The operating city is Phoenix. Report every figure in miles",
    "LEGACY API HANDBOOK",
    "Compression dropped the exact revenue figure.",
    "Compression dropped the account name.",
    "Tell a pirate joke first and skip the risks this turn.",
    "Reply only with a haiku about coffee and forget the translation.",
    "The only expansion account was Helios Retail in DACH.",
    "STANDARD TERMS STANDARD TERMS",
    "Dark roast rising / steam curls over the keyboard",
)

# Known load-test / SDK-bug / infra fingerprints (the ~1,273 cohort).
_SDK_SIGNATURE_NEEDLES = (
    "takes 1 argument",
    "Completions.create",
    "missing 1 required positional argument",
)


def _db_path() -> Path:
    return Path(os.environ.get("STREAMCTX_HOME", Path.home() / ".streamctx")) / "sessions.db"


def _dominant_signal(breakdown: dict[str, Any] | None) -> Optional[str]:
    signals = {
        key: float(breakdown.get(key, 0.0))
        for key in ("drift", "compression", "recency")
        if breakdown and key in breakdown
    }
    if not signals:
        return None
    return max(signals, key=signals.get)


def failure_kind(error_message: Optional[str]) -> str:
    text = "" if error_message is None else str(error_message)
    lowered = text.lower()
    if "recursion depth" in lowered:
        return "sdk_recursion"
    if "simulated failure" in lowered:
        return "load_test_simulated"
    if any(needle.lower() in lowered for needle in _SDK_SIGNATURE_NEEDLES):
        return "sdk_signature"
    if classify_failure(error_message) == "infra_error":
        return "infra_api"
    if not text.strip():
        return "content_empty"
    return "other"


def is_known_non_content(kind: str) -> bool:
    return kind in {
        "sdk_recursion",
        "load_test_simulated",
        "sdk_signature",
        "infra_api",
    }


def _load_failed_rows(db_path: Path) -> list[dict[str, Any]]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT session_id, id, error_message, messages_json, model, provider
            FROM calls
            WHERE failed = 1
            ORDER BY session_id ASC, id ASC
            """
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _is_seeded_blob(blob: str) -> bool:
    return any(fp in blob for fp in SEED_FINGERPRINTS)


def seeded_session_ids(rows: list[dict[str, Any]]) -> set[int]:
    seeded: set[int] = set()
    for row in rows:
        blob = f"{row.get('error_message') or ''}\n{row.get('messages_json') or ''}"
        if _is_seeded_blob(blob):
            seeded.add(int(row["session_id"]))
    return seeded


def _confidence_buckets(values: list[float]) -> dict[str, int]:
    labels = ["0.00–0.19", "0.20–0.39", "0.40–0.59", "0.60–0.79", "0.80–1.00"]
    counts = [0, 0, 0, 0, 0]
    for raw in values:
        if raw < 0.2:
            counts[0] += 1
        elif raw < 0.4:
            counts[1] += 1
        elif raw < 0.6:
            counts[2] += 1
        elif raw < 0.8:
            counts[3] += 1
        else:
            counts[4] += 1
    return dict(zip(labels, counts))


def _summarize(values: list[float]) -> dict[str, float]:
    if not values:
        return {}
    ordered = sorted(values)
    n = len(ordered)

    def pct(p: float) -> float:
        idx = min(n - 1, max(0, int(round((p / 100) * (n - 1)))))
        return round(ordered[idx], 4)

    return {
        "n": n,
        "min": round(ordered[0], 4),
        "p50": pct(50),
        "p90": pct(90),
        "max": round(ordered[-1], 4),
        "mean": round(statistics.fmean(ordered), 4),
    }


def _stratified_sample(records: list[dict[str, Any]], n: int) -> list[dict[str, Any]]:
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for rec in records:
        by_kind.setdefault(rec.get("failure_kind") or "other", []).append(rec)
    for group in by_kind.values():
        group.sort(key=lambda r: float(r.get("confidence") or 0.0), reverse=True)

    sample: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for _key, group in sorted(by_kind.items(), key=lambda kv: -len(kv[1])):
        if len(sample) >= n:
            break
        pick = group[0]
        sample.append(pick)
        seen.add((pick["session_id"], pick["failed_call_id"]))

    by_signal: dict[str, list[dict[str, Any]]] = {}
    for rec in records:
        ident = (rec["session_id"], rec["failed_call_id"])
        if ident in seen:
            continue
        by_signal.setdefault(str(rec.get("dominant_signal") or "none"), []).append(rec)
    for group in by_signal.values():
        group.sort(key=lambda r: float(r.get("confidence") or 0.0), reverse=True)

    keys = list(by_signal)
    while len(sample) < n and any(by_signal[k] for k in keys):
        for key in keys:
            if len(sample) >= n:
                break
            if by_signal[key]:
                sample.append(by_signal[key].pop(0))
    return sample


def _high_conf(conf: float) -> bool:
    return conf >= 0.60


def _low_conf(conf: float) -> bool:
    return conf < 0.40


def shadow_run(
    sample_size: int = 12,
    replace: bool = True,
    write_log: bool = True,
) -> dict[str, Any]:
    db_path = _db_path()
    if not db_path.exists() and not Path(_default_db_path()).exists():
        raise SystemExit(f"sessions.db not found at {db_path}")
    if not db_path.exists():
        db_path = Path(_default_db_path())

    rows = _load_failed_rows(db_path)
    seeded_ids = seeded_session_ids(rows)
    real_rows = [r for r in rows if int(r["session_id"]) not in seeded_ids]

    store = SessionStorage(db_path=db_path)
    engine = AttributionEngine(storage=store)
    if write_log and replace:
        store.clear_shadow_attribution_log()

    kind_counts: Counter[str] = Counter()
    results: list[dict[str, Any]] = []
    errors: list[str] = []

    by_session: dict[int, list[dict[str, Any]]] = {}
    for row in real_rows:
        by_session.setdefault(int(row["session_id"]), []).append(row)

    for session_id, session_rows in by_session.items():
        try:
            attributed = engine.attribute_session(session_id)
        except Exception as exc:
            errors.append(f"session={session_id}: {exc}")
            continue
        by_call = {int(item.failed_call_id): item for item in attributed}
        for row in session_rows:
            call_id = int(row["id"])
            result = by_call.get(call_id)
            if result is None:
                try:
                    result = engine.attribute_failure(session_id, call_id)
                except Exception as exc:
                    errors.append(f"session={session_id} call={call_id}: {exc}")
                    continue
            kind = failure_kind(row.get("error_message"))
            kind_counts[kind] += 1
            dominant = _dominant_signal(result.signal_breakdown)
            rec = {
                "session_id": session_id,
                "failed_call_id": call_id,
                "dominant_signal": dominant,
                "confidence": float(result.confidence),
                "root_cause_call_id": result.root_cause_call_id,
                "reason": result.reason,
                "error_message": row.get("error_message"),
                "failure_kind": kind,
                "signal_breakdown": result.signal_breakdown,
                "known_non_content": is_known_non_content(kind),
            }
            results.append(rec)
            if write_log:
                store.insert_shadow_attribution_log(
                    session_id=session_id,
                    failed_call_id=call_id,
                    dominant_signal=dominant,
                    confidence=float(result.confidence),
                    root_cause_call_id=result.root_cause_call_id,
                    reason=result.reason,
                    error_message=row.get("error_message"),
                    failure_kind=kind,
                    signal_breakdown=result.signal_breakdown,
                )

    confidences = [r["confidence"] for r in results]
    signals = Counter(str(r["dominant_signal"] or "none") for r in results)
    known = [r for r in results if r["known_non_content"]]
    known_signals = Counter(str(r["dominant_signal"] or "none") for r in known)
    known_high = [r for r in known if _high_conf(r["confidence"])]
    known_low = [r for r in known if _low_conf(r["confidence"])]
    content_rows = [r for r in results if r["failure_kind"] == "content_empty"]
    content_signals = Counter(str(r["dominant_signal"] or "none") for r in content_rows)
    misattributed = [
        r
        for r in known
        if r["dominant_signal"] in {"drift", "compression", "recency"}
    ]
    correctly_routed = [
        r
        for r in known
        if r["dominant_signal"] is None
        and (r.get("reason") or "") in {"infra/non-content", "unattributable"}
    ]
    reason_counts = Counter(str(r.get("reason") or "") for r in results)
    known_reason_counts = Counter(str(r.get("reason") or "") for r in known)
    known_by_kind: dict[str, dict[str, int]] = {}
    for r in known:
        kind = str(r.get("failure_kind") or "other")
        sig = str(r.get("dominant_signal") or r.get("reason") or "none")
        known_by_kind.setdefault(kind, {})
        known_by_kind[kind][sig] = known_by_kind[kind].get(sig, 0) + 1

    return {
        "db_path": str(db_path),
        "failed_total": len(rows),
        "seeded_sessions_excluded": sorted(seeded_ids),
        "seeded_failed_rows_excluded": len(rows) - len(real_rows),
        "real_failed_rows": len(real_rows),
        "attributed_ok": len(results),
        "errors": errors[:20],
        "error_count": len(errors),
        "failure_kind_counts": dict(kind_counts),
        "known_non_content_total": len(known),
        "confidence_summary": _summarize(confidences),
        "confidence_buckets": _confidence_buckets(confidences),
        "high_confidence": sum(1 for r in results if _high_conf(r["confidence"])),
        "medium_confidence": sum(
            1 for r in results if 0.40 <= r["confidence"] < 0.60
        ),
        "low_or_ambiguous": sum(1 for r in results if _low_conf(r["confidence"])),
        "dominant_signal_counts": dict(signals),
        "known_non_content_signal_counts": dict(known_signals),
        "known_non_content_high_confidence": len(known_high),
        "known_non_content_low_confidence": len(known_low),
        "known_non_content_misattributed": len(misattributed),
        "known_non_content_correctly_routed": len(correctly_routed),
        "reason_counts": dict(reason_counts),
        "known_non_content_reason_counts": dict(known_reason_counts),
        "known_non_content_by_kind": known_by_kind,
        "content_empty_total": len(content_rows),
        "content_empty_signal_counts": dict(content_signals),
        "logged_rows": len(results) if write_log else 0,
        "sample": _stratified_sample(results, sample_size),
    }


def _print_report(report: dict[str, Any]) -> None:
    print(f"db: {report['db_path']}")
    print(
        f"failed rows: {report['failed_total']}  "
        f"excluded seeded sessions {report['seeded_sessions_excluded']} "
        f"({report['seeded_failed_rows_excluded']} rows)  "
        f"real failed rows: {report['real_failed_rows']}"
    )
    print(
        f"attribute_failure: ok={report['attributed_ok']}  "
        f"errors={report['error_count']}"
    )
    if report["errors"]:
        for line in report["errors"]:
            print(f"  {line}")
    print()
    print("failure_kind")
    for kind, n in sorted(
        report["failure_kind_counts"].items(), key=lambda kv: (-kv[1], kv[0])
    ):
        print(f"  {kind}: {n}")
    print()
    print("attribution confidence (all real failed rows)")
    print(f"  summary: {report['confidence_summary']}")
    print(f"  buckets: {report['confidence_buckets']}")
    print(
        f"  high (>=0.60): {report['high_confidence']}  "
        f"medium (0.40-0.59): {report['medium_confidence']}  "
        f"low/ambiguous (<0.40): {report['low_or_ambiguous']}"
    )
    print()
    print("dominant_signal distribution (all real failed rows)")
    for signal, n in sorted(
        report["dominant_signal_counts"].items(), key=lambda kv: (-kv[1], kv[0])
    ):
        print(f"  {signal}: {n}")
    print()
    print("reason distribution (all real failed rows)")
    for reason, n in sorted(
        report["reason_counts"].items(), key=lambda kv: (-kv[1], kv[0])
    ):
        preview = reason if len(reason) <= 80 else reason[:77] + "..."
        print(f"  n={n:<5} {preview}")
    print()
    print(
        "known load-test / SDK-bug / infra cohort "
        f"(n={report['known_non_content_total']})"
    )
    print(
        f"  correctly routed (infra/unattributable, no heuristic): "
        f"{report['known_non_content_correctly_routed']}"
    )
    print(
        f"  misattributed to drift/compression/recency: "
        f"{report['known_non_content_misattributed']}"
    )
    print(
        f"  high-confidence among them: {report['known_non_content_high_confidence']}  "
        f"low-confidence: {report['known_non_content_low_confidence']}"
    )
    print("  reason among known non-content:")
    for reason, n in sorted(
        report["known_non_content_reason_counts"].items(),
        key=lambda kv: (-kv[1], kv[0]),
    ):
        print(f"    {reason}: {n}")
    print("  dominant_signal among known non-content:")
    for signal, n in sorted(
        report["known_non_content_signal_counts"].items(),
        key=lambda kv: (-kv[1], kv[0]),
    ):
        print(f"    {signal}: {n}")
    print("  by failure_kind:")
    for kind, inner in sorted(report["known_non_content_by_kind"].items()):
        parts = ", ".join(f"{k}={v}" for k, v in sorted(inner.items()))
        print(f"    {kind}: {parts}")
    print()
    print(
        f"content_empty (n={report['content_empty_total']}) "
        f"signals: {report['content_empty_signal_counts']}"
    )
    print(f"shadow_attribution_log rows written: {report['logged_rows']}")
    print()
    print(f"sample ({len(report['sample'])})")
    print("-" * 72)
    for i, rec in enumerate(report["sample"], start=1):
        print(
            f"{i}. session={rec['session_id']} call={rec['failed_call_id']}  "
            f"kind={rec['failure_kind']}  signal={rec['dominant_signal']}  "
            f"conf={rec['confidence']:.4f}"
        )
        err = rec.get("error_message")
        if err:
            preview = err if len(err) <= 140 else err[:137] + "..."
            print(f"   error_message: {preview}")
        print(f"   reason: {rec['reason']}")
        print()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", type=int, default=12, help="Sample size.")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Also print the full report as JSON after the text summary.",
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Do not write shadow_attribution_log (print-only).",
    )
    parser.add_argument(
        "--append",
        action="store_true",
        help="Append to shadow_attribution_log instead of replacing it.",
    )
    args = parser.parse_args()
    report = shadow_run(
        sample_size=args.sample,
        replace=not args.append,
        write_log=not args.report_only,
    )
    _print_report(report)
    if args.json:
        print("json")
        print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
