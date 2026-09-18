"""Live + historical proof that blank-but-billed replies are failed.

Scans ``~/.streamctx/sessions.db`` (and the integration-proof DB if
present) for rows that match the old misclassification, then runs a
real OpenRouter/OpenAI multi-turn session with a tight max_tokens so a
blank billed reply can occur the same way the integration proof saw it.

Usage::

    python scripts/live_blank_reply_proof.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

PROOF_HOME = ROOT / "artifacts" / "blank-reply-proof"
VERIFY_SCRIPT = ROOT / "scripts" / "verify_attestation.py"
HISTORIC_DBS = [
    Path.home() / ".streamctx" / "sessions.db",
    ROOT / "artifacts" / "integration-proof" / "sessions.db",
]

OPENAI_KEY = os.getenv("OPENAI_API_KEY")
OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY")

BLANK_SQL = """
SELECT id, session_id, output_tokens, failed,
       COALESCE(length(response_text), 0) AS rlen
FROM calls
WHERE COALESCE(failed, 0) = 0
  AND COALESCE(output_tokens, 0) > 0
  AND (
        response_text IS NULL
        OR TRIM(response_text) = ''
      )
"""


def scan_db(path: Path) -> dict:
    if not path.exists():
        return {"path": str(path), "exists": False}
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    cols = {r[1] for r in conn.execute("PRAGMA table_info(calls)").fetchall()}
    if "calls" not in {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }:
        conn.close()
        return {"path": str(path), "exists": True, "calls_table": False}
    total = conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
    if "response_text" not in cols:
        conn.close()
        return {
            "path": str(path),
            "exists": True,
            "total_calls": total,
            "blank_billed_successes": None,
            "note": "no response_text column",
        }
    rows = [dict(r) for r in conn.execute(BLANK_SQL).fetchall()]
    conn.close()
    return {
        "path": str(path),
        "exists": True,
        "total_calls": total,
        "blank_billed_successes": len(rows),
        "sample_ids": [r["id"] for r in rows[:20]],
        "sample_tokens": [r["output_tokens"] for r in rows[:20]],
    }


def print_scans() -> None:
    print("=" * 72)
    print("HISTORICAL SCAN (not retroactively fixed)")
    print("=" * 72)
    for path in HISTORIC_DBS:
        info = scan_db(path)
        print(json.dumps(info, indent=2))
        print()


def _client():
    from openai import OpenAI

    if OPENAI_KEY:
        return OpenAI(api_key=OPENAI_KEY), "gpt-4o-mini", "openai"
    if not OPENROUTER_KEY:
        raise SystemExit("No OPENAI_API_KEY or OPENROUTER_API_KEY")
    return (
        OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=OPENROUTER_KEY,
        ),
        "openrouter/free",
        "openrouter",
    )


def _extract(resp) -> str:
    try:
        return (resp.choices[0].message.content or "").strip()
    except Exception:
        return ""


TURNS = [
    "I'm on ticket ACME-9917. Q3 budget is $12.4 million. Write only to staging-db-07.",
    "The DAG is northwind_q3_etl, pandas + psycopg2, CSV from S3 into Postgres.",
    "Standup noise: coffee machine broke, Slack rename, wiki debate. Ignore. Why is revenue NULL?",
    "Transform does df.rename({'rev':'revenue'}) then drop notes/chatter/padding.",
    "CI green, parking lot full, on-call is Maya. Logs show 14203 rows in and out, revenue NULL.",
    "Pandas rename vs copy, SettingWithCopyWarning — short answer.",
    "Schema dump is id, customer, amount, region. Could dest_table be fact_orders_v1?",
    "XCom dest_table=analytics.fact_orders_v1. Quote ticket, DB, and budget from turn 1.",
    "Should Variable be staging-db-07.analytics.fact_orders? Four-bullet incident summary.",
    "One more: exact ticket ID, allowed database, Q3 figure. Values only if they appeared.",
]


def run_live() -> None:
    os.environ["STREAMCTX_HOME"] = str(PROOF_HOME)
    os.environ["STREAMCTX_SHADOW_REPAIR"] = "1"
    os.environ["STREAMCTX_SHADOW_REPAIR_SYNC"] = "1"
    PROOF_HOME.mkdir(parents=True, exist_ok=True)

    from streamctx.evidence import generate_keypair

    priv = PROOF_HOME / "issuer-private.pem"
    pub = PROOF_HOME / "issuer-public.pem"
    if not priv.exists():
        generate_keypair(priv, pub)
    os.environ["STREAMCTX_EVIDENCE_PRIVATE_KEY"] = str(priv)
    os.environ["STREAMCTX_EVIDENCE_PUBLIC_KEY"] = str(pub)

    import streamctx
    from streamctx.evidence import export_attestation
    from streamctx.storage import get_storage

    raw, model, provider = _client()
    print("=" * 72)
    print("LIVE SESSION")
    print("=" * 72)
    print(f"provider={provider} model={model} max_tokens=80")
    print(f"STREAMCTX_HOME={PROOF_HOME}")

    streamctx.start()
    client = streamctx.wrap(raw)
    session_id = streamctx.get_session_id()
    messages = [
        {
            "role": "system",
            "content": (
                "You are a data-engineering assistant. Be concise. "
                "If a figure is missing, say you do not know."
            ),
        }
    ]
    blanks_seen = 0
    for i, turn in enumerate(TURNS, start=1):
        messages.append({"role": "user", "content": turn})
        print(f"--- turn {i}/{len(TURNS)} ---")
        t0 = time.time()
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=0.2,
                max_tokens=80,
            )
            text = _extract(resp)
            print(f"assistant ({time.time()-t0:.1f}s): {text[:180]!r}")
            messages.append({"role": "assistant", "content": text})
            if not text:
                blanks_seen += 1
        except Exception as exc:
            print(f"ERROR {type(exc).__name__}: {exc}")
            break
        time.sleep(0.2)

    storage = get_storage()
    calls = storage.get_calls_for_session(int(session_id))
    failed = [c for c in calls if c.get("failed")]
    blank_failed = [
        c
        for c in failed
        if not (c.get("response_text") or "").strip()
        and int(c.get("output_tokens") or 0) > 0
        and not (c.get("error_message") or "").strip()
    ]
    blank_success = [
        c
        for c in calls
        if not c.get("failed")
        and not (c.get("response_text") or "").strip()
        and int(c.get("output_tokens") or 0) > 0
    ]
    shadow = [
        r
        for r in storage.get_shadow_repair_log()
        if int(r["session_id"]) == int(session_id)
    ]
    ckpts = storage._write_conn.execute(
        "SELECT COUNT(*) FROM checkpoints WHERE session_id = ?",
        (int(session_id),),
    ).fetchone()[0]

    print()
    print("--- LAYER 1 ---")
    print(
        f"session_id={session_id} calls={len(calls)} checkpoints={ckpts} "
        f"failed={len(failed)} script_empty_replies={blanks_seen}"
    )
    print(f"blank_billed_failed={len(blank_failed)} ids={[c['id'] for c in blank_failed]}")
    print(
        f"blank_billed_still_success={len(blank_success)} "
        f"ids={[c['id'] for c in blank_success]}"
    )
    for c in calls:
        print(
            f"  call={c['id']} failed={bool(c.get('failed'))} "
            f"out={c.get('output_tokens')} rlen={len(c.get('response_text') or '')} "
            f"err={c.get('error_message')!r}"
        )

    print()
    print("--- LAYER 3 shadow ---")
    if not shadow:
        print("  empty")
    for row in shadow:
        print(
            f"  call={row.get('failed_call_id')} signal={row.get('dominant_signal')} "
            f"applied={row.get('applied')} resolved={row.get('resolved')} "
            f"dry_run={row.get('dry_run')}"
        )

    print()
    print("--- LAYER 4 export ---")
    bundle = export_attestation(int(session_id))
    bundle_path = PROOF_HOME / f"session_{session_id}_attestation.json"
    bundle_path.write_text(json.dumps(bundle, indent=2), encoding="utf-8")
    print(
        f"entries={len(bundle.get('entries') or [])} "
        f"repair_summary={bundle.get('repair_summary')}"
    )
    proc = subprocess.run(
        [
            sys.executable,
            str(VERIFY_SCRIPT),
            str(bundle_path),
            "--public-key",
            str(pub),
            "--verbose",
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    print(f"verify_exit={proc.returncode}")
    print(proc.stdout or proc.stderr)
    streamctx.stop()
    print("done.")


def main() -> int:
    print_scans()
    if not OPENAI_KEY and not OPENROUTER_KEY:
        print("No live API key; skipped live session.")
        return 0
    run_live()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
