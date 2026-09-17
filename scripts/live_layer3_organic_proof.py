"""Live Layer 3 organic proof — real API, real multi-turn session.

Starts a fresh streamctx session, wraps a live OpenAI-compatible client,
runs an 8–10 turn coding/data conversation with realistic chatter so
Layer 1 compression can fire, then asks an early buried-fact question.

Does NOT:
- seed synthetic failures into sessions.db
- call verify_fix / attribute_failure until AFTER the live conversation
- force failed=True on any call

Reports honestly: organic failures (if any), shadow_repair_log for this
session, whether compression fired, and a post-hoc dry-run attribution
on the last call only if nothing failed organically (diagnostic, labeled).

Usage::

    python scripts/live_layer3_organic_proof.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

# Prefer OpenAI, then OpenRouter (demo path), then Anthropic via OpenAI-compat is not used.
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY")

if not OPENAI_KEY and not OPENROUTER_KEY:
    raise SystemExit(
        "No OPENAI_API_KEY or OPENROUTER_API_KEY in environment / .env. "
        "Cannot run a live session proof."
    )

from openai import OpenAI

import streamctx
from streamctx.attribution import AttributionEngine
from streamctx.repair import VerifiedRepairEngine, classify_failure
from streamctx.storage import SessionStorage, get_storage

# Force compression earlier than default 2000 so a realistic 8–10 turn
# chat with chatter actually exercises Layer 1 compression on this run.
# Still the real compress_messages() path inside the tracker intercept.
os.environ.setdefault("STREAMCTX_SHADOW_REPAIR", "1")
os.environ.setdefault("STREAMCTX_SHADOW_REPAIR_SYNC", "1")

# Patch compressor default for this process only via tracker path:
# intercept always calls compress_messages(compress_source) with default
# max_tokens=2000. We monkeypatch the default by wrapping compress_messages
# so the live pressure case is honest about "compression fired" without
# hand-scripting a failure.
from streamctx import compressor as _comp

_orig_compress = _comp.compress_messages
LIVE_MAX_TOKENS = 400  # pressure: enough chatter to force extractive middle


def _compress_with_pressure(messages, max_tokens=2000, keep_system=True, keep_last_n=4):
    # Use LIVE_MAX_TOKENS when caller uses the default; honor explicit overrides.
    effective = LIVE_MAX_TOKENS if max_tokens == 2000 else max_tokens
    return _orig_compress(
        messages,
        max_tokens=effective,
        keep_system=keep_system,
        keep_last_n=keep_last_n,
    )


_comp.compress_messages = _compress_with_pressure

# Also patch the name imported inside tracker at call time (from .compressor import ...)
import streamctx.tracker as _tracker_mod

# tracker does `from .compressor import compress_messages` inside the method,
# so patching compressor.compress_messages is enough.


def _client() -> tuple[OpenAI, str, str]:
    if OPENAI_KEY:
        return (
            OpenAI(api_key=OPENAI_KEY),
            "gpt-4o-mini",
            "openai",
        )
    return (
        OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=OPENROUTER_KEY,
        ),
        "openrouter/free",
        "openrouter",
    )


# Realistic multi-turn: help debug a Python data pipeline. Early fact that
# must survive compression is the ticket ID + staging DB name.
EARLY_FACT_TICKET = "ACME-9917"
EARLY_FACT_DB = "staging-db-07"
EARLY_FACT_BUDGET = "$12.4 million"

SYSTEM = (
    "You are a senior data-engineering assistant helping a user debug a "
    "batch ETL job. Answer from the conversation. Be concise (2-4 sentences). "
    "If a figure or ID is not in context, say you do not know."
)

TURNS = [
    (
        "user",
        f"I'm on ticket {EARLY_FACT_TICKET}. Our Q3 analytics budget is "
        f"{EARLY_FACT_BUDGET}. The ETL must write only to {EARLY_FACT_DB}, "
        "never production. Help me figure out why last night's run dropped "
        "the revenue column.",
    ),
    (
        "user",
        "The job is Airflow DAG `northwind_q3_etl`, Python 3.11, pandas + "
        "psycopg2. It reads a CSV from S3 then upserts into Postgres.",
    ),
    (
        "user",
        "Here's some noise from the standup while I dig: the coffee machine "
        "broke, someone renamed the Slack channel, and we debated whether to "
        "migrate the wiki. Not related to the ETL — just context.",
    ),
    (
        "user",
        "Looking at the transform step, we do "
        "`df.rename(columns={'rev': 'revenue'})` then "
        "`df.drop(columns=['notes','chatter','padding'])`. "
        "Could the drop be wrong?",
    ),
    (
        "user",
        "Also: CI is green, unit tests mock Postgres, and the staging "
        "credentials rotate every 90 days. Still unrelated chatter — "
        "deploy window is Thursday, on-call is Maya, and the parking "
        "lot is full. Back to the pipeline.",
    ),
    (
        "user",
        "I added logging around the upsert. The log shows 14,203 rows "
        "read, 14,203 written, but the `revenue` column is NULL on every "
        "row in the destination table. Source CSV has values.",
    ),
    (
        "user",
        "Quick digression: remind me how pandas handles rename vs copy, "
        "and whether SettingWithCopyWarning could silently drop a column "
        "after a chained assignment. Keep it short.",
    ),
    (
        "user",
        "More filler while I paste logs: weather is rainy, the office "
        "thermostat is stuck at 68F, lunch was late, and the bike rack "
        "is blocked. Now — the schema dump shows column order "
        "`id, customer, amount, region` with no `revenue` field. "
        "Could the upsert target the wrong table?",
    ),
    (
        "user",
        "One more operational aside: the runbook says restart workers "
        "after a failed migrate, the pager went off twice for Redis, and "
        "someone filed a ticket about the docs site. Ignore those. "
        "Focus on the missing revenue column.",
    ),
    # Pressure question: depends on early buried facts after chatter.
    (
        "user",
        "Ignoring the digressions: what was the exact ticket ID, the only "
        "allowed database name, and the Q3 budget figure I gave you at the "
        "start? Answer with those three values only if they appeared earlier.",
    ),
]


def _extract(resp) -> str:
    try:
        return (resp.choices[0].message.content or "").strip()
    except Exception:
        return str(resp)


def main() -> int:
    raw_client, model, provider = _client()
    print(f"provider={provider} model={model}")
    print(f"compression pressure max_tokens={LIVE_MAX_TOKENS} (default path patched)")
    print(f"STREAMCTX_SHADOW_REPAIR={os.environ.get('STREAMCTX_SHADOW_REPAIR')}")
    print()

    streamctx.start()
    client = streamctx.wrap(raw_client)
    session_id = streamctx.get_session_id()
    print(f"session_id={session_id}")

    messages: list[dict] = [{"role": "system", "content": SYSTEM}]
    replies: list[str] = []
    call_errors: list[str] = []

    for i, (_role, content) in enumerate(TURNS, start=1):
        messages.append({"role": "user", "content": content})
        print(f"--- turn {i}/{len(TURNS)} ---")
        print(f"user: {content[:120]}{'...' if len(content) > 120 else ''}")
        t0 = time.time()
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=0.2,
                max_tokens=220,
            )
            text = _extract(resp)
            elapsed = time.time() - t0
            print(f"assistant ({elapsed:.1f}s): {text[:300]}{'...' if len(text) > 300 else ''}")
            messages.append({"role": "assistant", "content": text})
            replies.append(text)
        except Exception as exc:
            elapsed = time.time() - t0
            err = f"{type(exc).__name__}: {exc}"
            call_errors.append(err)
            print(f"ERROR ({elapsed:.1f}s): {err}")
            # Do not invent a failure row; tracker already persisted if intercept saw it.
            break
        time.sleep(0.3)

    streamctx.report()
    # Keep session open long enough for sync shadow (already SYNC=1).
    storage = get_storage()
    assert session_id is not None
    calls = storage.get_calls_for_session(int(session_id))
    failed = [c for c in calls if c.get("failed")]
    shadow = [
        r
        for r in storage.get_shadow_repair_log()
        if int(r["session_id"]) == int(session_id)
    ]

    print()
    print("=" * 72)
    print("LIVE SESSION SUMMARY")
    print("=" * 72)
    print(f"session_id={session_id}")
    print(f"calls={len(calls)} failed={len(failed)} shadow_rows={len(shadow)}")
    print(f"api_errors_in_script={len(call_errors)}")

    # Did early facts survive in the *model reply* (organic content quality)?
    last = replies[-1] if replies else ""
    facts = {
        "ticket": EARLY_FACT_TICKET in last,
        "db": EARLY_FACT_DB in last,
        "budget": "12.4" in last or EARLY_FACT_BUDGET in last,
    }
    print(f"last_reply_contains_early_facts={facts}")
    print(f"last_reply={last!r}")

    # Compression evidence from stored usage
    reused = [int(c.get("reused_tokens") or 0) for c in calls]
    print(f"reused_tokens_per_call={reused}")
    print(f"any_reused_tokens={any(r > 0 for r in reused)}")

    if failed:
        print()
        print("--- ORGANIC FAILURES ---")
        for row in failed:
            print(
                f"  call_id={row['id']} error_message={row.get('error_message')!r} "
                f"classify={classify_failure(row.get('error_message'))}"
            )
    else:
        print()
        print("--- ORGANIC FAILURES ---")
        print("  none (clean run — valid result)")

    if shadow:
        print()
        print("--- SHADOW REPAIR LOG (this session) ---")
        for row in shadow:
            print(
                f"  id={row.get('id')} call={row.get('failed_call_id')} "
                f"signal={row.get('dominant_signal')} applied={row.get('applied')} "
                f"resolved={row.get('resolved')} dry_run={row.get('dry_run')} "
                f"needs_human={row.get('needs_human_review')}"
            )
            print(f"  reason={row.get('attribution_reason')}")
            cand = row.get("fix_candidate") or ""
            print(f"  candidate={cand[:400]!r}")
            applied_ok = row.get("applied") in (False, 0, None)
            print(f"  applied_stayed_false={applied_ok}")
    else:
        print()
        print("--- SHADOW REPAIR LOG (this session) ---")
        print("  empty (expected if no empty-error_message content failure)")

    # Post-hoc diagnostic only: attribute the LAST successful call as if it
    # were a content failure — labeled clearly, does not mutate the session.
    # Only when no organic failure, to answer "would Layer 2/3 have seen
    # compression/drift if this last reply lost the early facts?"
    print()
    print("--- POST-HOC DIAGNOSTIC (not organic; dry-run only) ---")
    if not calls:
        print("  no calls to diagnose")
    else:
        last_call = calls[-1]
        engine_a = AttributionEngine(storage=storage)
        # Attribution only scores failed=True rows via attribute_session;
        # attribute_failure works on any call id.
        attr = engine_a.attribute_failure(int(session_id), int(last_call["id"]))
        print(
            f"  attribute_failure(last_call={last_call['id']}): "
            f"reason={attr.reason!r} conf={attr.confidence} "
            f"root={attr.root_cause_call_id} breakdown={attr.signal_breakdown}"
        )
        # Temporary: only run verify_fix dry_run if we treat last as content
        # by cloning? No — do not invent failed rows. Instead call verify_fix
        # on last call as-is: classify_failure(None on success)=content_error
        # but failed flag is False — verify_fix still runs attribution.
        repair = VerifiedRepairEngine(storage=storage)
        # verify_fix does not require failed=True; it loads the call and classifies.
        result = repair.verify_fix(
            int(session_id),
            int(last_call["id"]),
            dry_run=True,
        )
        print(
            f"  verify_fix(dry_run): resolved={result.resolved} "
            f"applied={result.applied} signal={result.dominant_signal} "
            f"needs_human={result.needs_human_review}"
        )
        print(f"  reason={result.reason!r}")
        cand = json.dumps(result.fix_candidate, ensure_ascii=False)
        print(f"  candidate={cand[:500]!r}")
        # Correctness of candidate vs real history
        hist = "\n".join(
            str(m.get("content") or "")
            for c in calls
            for m in (json.loads(c["messages_json"]) if c.get("messages_json") else [])
            if isinstance(m, dict)
        )
        cand_ok = {
            "has_ticket": EARLY_FACT_TICKET in cand,
            "has_db": EARLY_FACT_DB in cand,
            "has_budget": "12.4" in cand,
            "facts_in_history": all(
                x in hist for x in (EARLY_FACT_TICKET, EARLY_FACT_DB, "12.4")
            ),
        }
        print(f"  candidate_vs_history={cand_ok}")
        print(f"  applied_stayed_false={result.applied is False}")

        # Checkpoints unchanged after dry-run
        ckpt = storage.get_latest_valid_checkpoint(int(session_id))
        print(
            f"  latest_checkpoint_step={(ckpt or {}).get('step_number')} "
            f"calls_still={len(storage.get_calls_for_session(int(session_id)))}"
        )

    streamctx.stop()
    print()
    print("done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
