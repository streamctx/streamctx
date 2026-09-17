"""Print the accumulated shadow_repair_log table.

Passive dogfood monitor: content-quality failures (empty error_message)
trigger verify_fix(dry_run=True) and the result is stored locally.

Usage::

    python scripts/review_shadow_log.py
    python scripts/review_shadow_log.py --limit 20
    python scripts/review_shadow_log.py --json
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any


def _db_path() -> Path:
    return Path(os.environ.get("STREAMCTX_HOME", Path.home() / ".streamctx")) / "sessions.db"


def load_shadow_log(db_path: Path, limit: int | None = None) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "shadow_repair_log" not in tables:
            return []
        sql = """
            SELECT id, session_id, failed_call_id, attribution_reason,
                   dominant_signal, fix_candidate, timestamp
            FROM shadow_repair_log
            ORDER BY id ASC
        """
        if limit is not None:
            rows = conn.execute(sql + " LIMIT ?", (int(limit),)).fetchall()
        else:
            rows = conn.execute(sql).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _clip(text: Any, width: int) -> str:
    value = "" if text is None else str(text).replace("\n", " ")
    if len(value) <= width:
        return value
    if width <= 3:
        return value[:width]
    return value[: width - 3] + "..."


def _print_table(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print("shadow_repair_log: (empty)")
        return

    headers = (
        "id",
        "session",
        "call",
        "signal",
        "attribution_reason",
        "fix_candidate",
        "timestamp",
    )
    widths = {
        "id": 4,
        "session": 8,
        "call": 6,
        "signal": 12,
        "attribution_reason": 42,
        "fix_candidate": 36,
        "timestamp": 20,
    }

    def cells(row: dict[str, Any]) -> list[str]:
        return [
            _clip(row.get("id"), widths["id"]),
            _clip(row.get("session_id"), widths["session"]),
            _clip(row.get("failed_call_id"), widths["call"]),
            _clip(row.get("dominant_signal"), widths["signal"]),
            _clip(row.get("attribution_reason"), widths["attribution_reason"]),
            _clip(row.get("fix_candidate"), widths["fix_candidate"]),
            _clip(row.get("timestamp"), widths["timestamp"]),
        ]

    header_line = "  ".join(h.ljust(widths[h]) for h in headers)
    rule = "  ".join("-" * widths[h] for h in headers)
    print(f"shadow_repair_log: {len(rows)} row(s)")
    print(header_line)
    print(rule)
    for row in rows:
        values = cells(row)
        print("  ".join(v.ljust(widths[h]) for v, h in zip(values, headers)))


def main() -> None:
    parser = argparse.ArgumentParser(description="Review shadow_repair_log")
    parser.add_argument("--limit", type=int, default=None, help="Max rows to print")
    parser.add_argument("--json", action="store_true", help="Dump rows as JSON")
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="Override sessions.db path (default: $STREAMCTX_HOME/sessions.db)",
    )
    args = parser.parse_args()
    db_path = args.db or _db_path()
    rows = load_shadow_log(db_path, limit=args.limit)
    if args.json:
        print(json.dumps(rows, indent=2, default=str))
        return
    print(f"db: {db_path}")
    _print_table(rows)


if __name__ == "__main__":
    sys.exit(main())
