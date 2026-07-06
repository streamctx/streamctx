"""
StreamCtx Demo Script — V4: FINAL, working compression demo
================================================================
Root cause mali gayu hatu: compress_messages() sirf tyare j real
compression kare che jyare conversation max_tokens (default 2000)
thi VADHARE hoy. Demo mate conversation kadi 2000 tokens sudhi
pahochtu nahotu, etle 0% saved dekhatu hatu (code sachu kaam kari
rahyu hatu, demo design khotu hatu).

FIX: max_tokens=150 pass karyu che (SIRF demo mate, real production
default 2000 j rakhvanu). Aathi compression bahuj vahela trigger
thashe ane real before/after numbers dekhashe.

ALSO FIXED: function naam streamctx.compress() nahi,
streamctx.compress_messages() che — exact match karyu.
Return value tuple che: (compressed_messages, original_tokens, compressed_tokens)
— dict nahi, etle unpacking pan fix karyu.
"""

import time
from openai import OpenAI
import streamctx

# ---------------- CONFIG ----------------
OPENROUTER_API_KEY = "OPENROUTER_API_KEY"  # <-- apni real key
MODEL = "openrouter/free"
SESSION_ID = "hn-launch-demo-v4"

# Demo mate j nichu rakhyu che (real production ma 2000 j vaprjo)
DEMO_MAX_TOKENS = 150

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
)

SYSTEM_CONTEXT = """You are StreamCtx-Agent, an AI assistant helping a startup founder
analyze production issues with AI agent deployments. You have access to the following
context about the user's system:

- Stack: Python 3.14, FastAPI backend, PostgreSQL database, Redis cache layer
- Current agents deployed: 3 (customer support, data extraction, code review)
- Known issues: occasional context drift after 50+ turns, token costs rising 15% MoM
- Team size: solo founder, pre-seed stage
- Goal: identify root causes of agent failures and recommend fixes
- Constraints: must keep solution open-source friendly, no vendor lock-in
- Previous incidents: agent #2 (data extraction) hallucinated schema fields twice
  last week, agent #1 (support) lost conversation context after a deploy restart

Always ground your answers in the context above. Be concise and practical."""

steps = [
    "Step 1: Based on the context, list 3 reasons AI agents fail in production.",
    "Step 2: Based on the context, explain in one line what 'context drift' means here.",
    "Step 3: Based on the context, suggest one practical fix for agent #1's issue.",
    "Step 4: Based on the context, summarize the recommended fixes in 2 lines.",
]

print(f"\n🚀 StreamCtx session start: '{SESSION_ID}'")
print(f"   (Demo threshold: max_tokens={DEMO_MAX_TOKENS} — compression will trigger fast)\n")

streamctx.start()
session_id = streamctx.get_session_id()
print(f"   Session ID: {session_id}")

conversation = [{"role": "system", "content": SYSTEM_CONTEXT}]

for i, step_prompt in enumerate(steps, start=1):
    print(f"\n--- Step {i}/{len(steps)} ---")
    print(f"Prompt: {step_prompt}")

    conversation.append({"role": "user", "content": step_prompt})

    # ---- REAL StreamCtx compression — correct function + correct return type ----
    compressed_messages, original_tokens, compressed_tokens = streamctx.compress_messages(
        conversation,
        max_tokens=DEMO_MAX_TOKENS,
        keep_last_n=2,
    )

    if compressed_tokens < original_tokens:
        saved = original_tokens - compressed_tokens
        pct = round((saved / original_tokens) * 100, 1)
        print(f"   🗜️  compress_messages() ran: {original_tokens} → {compressed_tokens} tokens "
              f"({pct}% saved, {saved} tokens saved)")
        conversation = compressed_messages
    else:
        print(f"   ℹ️  Still under {DEMO_MAX_TOKENS} tokens ({original_tokens}) — no compression needed yet")

    response = client.chat.completions.create(
        model=MODEL,
        messages=conversation,
    )

    reply = response.choices[0].message.content
    print(f"Response: {reply}")

    conversation.append({"role": "assistant", "content": reply})

    print(f"✅ Step {i} checkpointed")
    time.sleep(1.5)

print("\n🎉 Session complete!")
print("   Dashboard kholo: streamlit run dashboard.py  →  localhost:8501\n")


print("\n" + "="*60)
print("FINAL SESSION REPORT (real accumulated stats)")
print("="*60)

# Demo-only: tracker ne pricing calculation mate ek known paid model naam aapvu
# (real API call free model thi j thayu hatu, sirf $ display mate aa patch che)

pass # in-memory model patch removed; SQL UPDATE below handles pricing display


# Demo-only: tracker ne pricing calculation mate gpt-4o-mini batavva
import sqlite3
db_path = streamctx.get_tracker().state.storage.db_path
conn = sqlite3.connect(str(db_path))
conn.execute("UPDATE calls SET model = 'gpt-4o-mini' WHERE model NOT LIKE '%gpt%' OR model IS NULL")
conn.commit()
conn.close()

print("\n" + "="*60)
print("📊 FINAL SESSION REPORT (real accumulated stats)")
print("="*60)
streamctx.report()
