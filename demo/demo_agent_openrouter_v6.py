"""
StreamCtx Demo Script - V5: HEAVY CONTEXT + HONEST PRICING
===========================================================
Built directly on top of V4's structure (same imports, same function
calls: streamctx.start(), streamctx.get_session_id(),
streamctx.compress_messages(), streamctx.report()).

ONLY 2 changes from V4:
1. REMOVED the "demo-only" SQLite patch that rewrote the model name to
   'gpt-4o-mini' for fake pricing display. Real model + real cost
   (which will genuinely show $0.00 if you're on openrouter/free) is
   shown instead - no fake numbers.
2. BIGGER context: longer SYSTEM_CONTEXT (~2x) + 10 steps instead of 4,
   so compress_messages() has realistic room to work as the
   conversation grows past the demo threshold multiple times.

API key is loaded from .env via OPENROUTER_API_KEY - never hardcoded.
"""

import time
from openai import OpenAI
import streamctx
import os
from dotenv import load_dotenv

load_dotenv()

# -------------------- CONFIG ------------------
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")  # <-- loaded from .env
MODEL = "openrouter/free"
SESSION_ID = "heavy-demo-v5"

# Demo mate j nichu rakhyu che (real production ma 2000 j vaprjo)
DEMO_MAX_TOKENS = 150

if not OPENROUTER_API_KEY:
    raise RuntimeError(
        "OPENROUTER_API_KEY not found. Add it to your .env file in the project root."
    )

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
)

# Attribution Engine mate client wrap kariye
client = streamctx.wrap(client)

SYSTEM_CONTEXT = """You are StreamCtx-Agent, an AI assistant helping a startup founder
analyze production issues with AI agent deployments. You have access to the following
context about the user's system:

- Stack: Python 3.14, FastAPI backend, PostgreSQL database, Redis cache layer
- Current agents deployed: 5 (customer support, data extraction, code review,
  billing reconciliation, fraud detection)
- Known issues: occasional context drift after 50+ turns, token costs rising 15% MoM
- Team size: solo founder, pre-seed stage
- Goal: identify root causes of agent failures and recommend fixes
- Constraints: must keep solution open-source friendly, no vendor lock-in
- Previous incidents: agent #2 (data extraction) hallucinated schema fields twice
  last week, agent #1 (support) lost conversation context after a deploy restart,
  agent #4 (billing) double-charged 3 customers due to a race condition,
  agent #5 (fraud) flagged 12% false positives last sprint
- Budget constraints: monthly LLM spend must stay under $500 across all 5 agents

Always ground your answers in the context above. Be concise and practical."""

steps = [
    "Step 1: Based on the context, list 3 reasons AI agents fail in production.",
    "Step 2: Based on the context, explain in one line what 'context drift' means here.",
    "Step 3: Based on the context, suggest one practical fix for agent #1's issue.",
    "Step 4: Based on the context, suggest one practical fix for agent #2's hallucination issue.",
    "Step 5: Based on the context, explain the likely cause of agent #4's race condition.",
    "Step 6: Based on the context, suggest a fix for agent #4's double-charging issue.",
    "Step 7: Based on the context, why might agent #5 have a 12% false positive rate?",
    "Step 8: Based on the context, suggest one or more ways to reduce agent #5's false positives.",
    "Step 9: Based on the context, propose a way to keep monthly LLM spend under $500.",
    "Step 10: Based on the context, summarize the recommended fixes in 4 lines.",
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

    # REAL StreamCtx compression
    compressed_messages, original_tokens, compressed_tokens = streamctx.compress_messages(
        conversation,
        max_tokens=DEMO_MAX_TOKENS,
        keep_last_n=2,
    )

    if compressed_tokens < original_tokens:
        saved = original_tokens - compressed_tokens
        pct = round((saved / original_tokens) * 100, 1)
        print(f"   🗜  compress_messages() ran: {original_tokens} → {compressed_tokens} tokens "
              f"({pct}% saved, {saved} tokens saved)")
        conversation = compressed_messages
    else:
        print(f"   📋 Still under {DEMO_MAX_TOKENS} tokens ({original_tokens}) — no compression needed yet")

    response = client.chat.completions.create(
        model=MODEL,
        messages=conversation,
    )

    reply = response.choices[0].message.content
    print(f"Response: {reply}")

    conversation.append({"role": "assistant", "content": reply})

    print(f"   ✅ Step {i} checkpointed")
    time.sleep(1.5)

print("\n✅ Session complete!")
print("   Dashboard kholo: streamlit run dashboard.py → localhost:8501\n")

print(f"\n🔵 Real model used: {MODEL}")
print(f"🔵 Real cost: $0.00 (free-tier model — no pricing patch applied)\n")

print("\n" + "=" * 60)
print("🏁 FINAL SESSION REPORT (real accumulated stats)")
print("=" * 60)

# Session ID capture karo BEFORE report/stop
sid = streamctx.get_session_id()
streamctx.report()

