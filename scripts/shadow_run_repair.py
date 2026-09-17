"""Dry-run verify_fix() over real (non-seeded) failed calls.

Diagnostic only: no live LLM calls.  Classifies every failed row, skips
sessions inserted by the seed_* scripts, then runs verify_fix(dry_run=True)
on content_error cases and prints distributions plus a sample of
attribution reasons.

Usage::

    python scripts/shadow_run_repair.py
    python scripts/shadow_run_repair.py --sample 10
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
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from streamctx.repair import classify_failure, get_repair_engine  # noqa: E402
from streamctx.storage import _default_db_path  # noqa: E402

# Distinctive strings that only appear in seed_* synthetic sessions.
# A session is excluded if any of its failed-call messages_json/error
# fields contain one of these fingerprints.
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


def _db_path() -> Path:
    return Path(os.environ.get("STREAMCTX_HOME", Path.home() / ".streamctx")) / "sessions.db"


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
    """Sessions whose stored text matches a seed-script fingerprint."""
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


def _error_key(rec: dict[str, Any]) -> str:
    raw = (rec.get("error_message") or "").strip() or "<empty>"
    return raw[:80]


def _stratified_sample(
    records: list[dict[str, Any]], n: int
) -> list[dict[str, Any]]:
    """Up to n reasons: one per error-message type, then by signal/confidence."""
    by_error: dict[str, list[dict[str, Any]]] = {}
    for rec in records:
        by_error.setdefault(_error_key(rec), []).append(rec)
    for group in by_error.values():
        group.sort(key=lambda r: float(r.get("confidence") or 0.0), reverse=True)

    sample: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for _key, group in sorted(by_error.items(), key=lambda kv: -len(kv[1])):
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
                pick = by_signal[key].pop(0)
                sample.append(pick)
    return sample


def shadow_run(sample_size: int = 10) -> dict[str, Any]:
    db_path = _db_path()
    if not db_path.exists() and not Path(_default_db_path()).exists():
        raise SystemExit(f"sessions.db not found at {db_path}")
    if not db_path.exists():
        db_path = Path(_default_db_path())

    rows = _load_failed_rows(db_path)
    seeded_ids = seeded_session_ids(rows)
    real_rows = [r for r in rows if int(r["session_id"]) not in seeded_ids]

    class_counts: Counter[str] = Counter()
    error_counts: Counter[str] = Counter()
    content_rows: list[dict[str, Any]] = []
    for row in real_rows:
        klass = classify_failure(row.get("error_message"))
        class_counts[klass] += 1
        if klass == "content_error":
            content_rows.append(row)
            key = (row.get("error_message") or "").strip() or "<empty>"
            error_counts[key] += 1

    engine = get_repair_engine()
    dry_results: list[dict[str, Any]] = []
    errors: list[str] = []
    for row in content_rows:
        try:
            result = engine.verify_fix(
                int(row["session_id"]),
                int(row["id"]),
                dry_run=True,
            )
            dry_results.append(
                {
                    "session_id": result.session_id,
                    "failed_call_id": result.failed_call_id,
                    "dominant_signal": result.dominant_signal,
                    "confidence": float(
                        (result.proof or {}).get("attribution_confidence") or 0.0
                    ),
                    "reason": result.reason
                    or (result.proof or {}).get("attribution_reason")
                    or "",
                    "error_message": row.get("error_message"),
                    "fix_strategy": (result.proof or {}).get("fix_strategy"),
                    "unfixable_content": bool(
                        (result.proof or {}).get("unfixable_content")
                    ),
                }
            )
        except Exception as exc:
            errors.append(f"session={row['session_id']} call={row['id']}: {exc}")

    confidences = [r["confidence"] for r in dry_results]
    signals = Counter(str(r["dominant_signal"] or "none") for r in dry_results)
    sample = _stratified_sample(dry_results, sample_size)

    return {
        "db_path": str(db_path),
        "failed_total": len(rows),
        "seeded_sessions_excluded": sorted(seeded_ids),
        "seeded_failed_rows_excluded": len(rows) - len(real_rows),
        "real_failed_rows": len(real_rows),
        "class_counts": dict(class_counts),
        "content_error_total": len(content_rows),
        "content_error_message_top": error_counts.most_common(8),
        "dry_run_ok": len(dry_results),
        "dry_run_errors": errors[:20],
        "dry_run_error_count": len(errors),
        "confidence_summary": _summarize(confidences),
        "confidence_buckets": _confidence_buckets(confidences),
        "dominant_signal_counts": dict(signals),
        "unfixable_content_count": sum(
            1 for r in dry_results if r["unfixable_content"]
        ),
        "sample_reasons": sample,
    }


def _print_report(report: dict[str, Any]) -> None:
    print(f"db: {report['db_path']}")
    print(
        f"failed rows: {report['failed_total']}  "
        f"excluded seeded sessions {report['seeded_sessions_excluded']} "
        f"({report['seeded_failed_rows_excluded']} rows)  "
        f"real failed rows: {report['real_failed_rows']}"
    )
    print(f"classify_failure: {report['class_counts']}")
    print(f"content_error cases: {report['content_error_total']}")
    print("top content_error messages:")
    for msg, n in report["content_error_message_top"]:
        preview = msg if len(msg) <= 90 else msg[:87] + "..."
        print(f"  n={n:<5} {preview!r}")
    print()
    print(
        f"dry_run verify_fix: ok={report['dry_run_ok']}  "
        f"errors={report['dry_run_error_count']}"
    )
    if report["dry_run_errors"]:
        for line in report["dry_run_errors"]:
            print(f"  {line}")
    print()
    print("attribution confidence")
    print(f"  summary: {report['confidence_summary']}")
    print(f"  buckets: {report['confidence_buckets']}")
    print()
    print("dominant_signal distribution")
    for signal, n in sorted(
        report["dominant_signal_counts"].items(), key=lambda kv: (-kv[1], kv[0])
    ):
        print(f"  {signal}: {n}")
    print(f"unfixable_content: {report['unfixable_content_count']}")
    print()
    print(f"sample attribution reasons ({len(report['sample_reasons'])})")
    print("-" * 72)
    for i, rec in enumerate(report["sample_reasons"], start=1):
        print(
            f"{i}. session={rec['session_id']} call={rec['failed_call_id']}  "
            f"signal={rec['dominant_signal']}  conf={rec['confidence']:.4f}  "
            f"strategy={rec['fix_strategy']}"
        )
        err = rec.get("error_message")
        if err:
            preview = err if len(err) <= 140 else err[:137] + "..."
            print(f"   error_message: {preview}")
        print(f"   reason: {rec['reason']}")
        print()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", type=int, default=10, help="Reason sample size.")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Also print the full report as JSON after the text summary.",
    )
    args = parser.parse_args()
    report = shadow_run(sample_size=args.sample)
    _print_report(report)
    if args.json:
        print("json")
        print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
