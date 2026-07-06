"""
StreamCtx Demo Script — OpenRouter (FREE model) sathe REAL LLM calls
======================================================================
V2: Aa version ma har step ma EK J LAMBO REPEATED CONTEXT (system prompt +
conversation history) moklay che — taki StreamCtx no compression/caching
feature REAL numbers batavi shake (0% nahi).

Pehla version ma har prompt alag-alag hatu, etle reuse/compression
mate kai material j nahotu — etle "$0.00 saved" dekhatu hatu (sachu,
pan demo mate boring).

SETUP: Line 33-34 ma API key + model already set che (kale launch wali).

RECORDING FLOW:
1. python demo_agent_openrouter_v2.py  →  run karo
2. Joi shakay: token count step-by-step vadhe, pan StreamCtx
   "cached/reused context" % batavshe jem context repeat thay
3. Ctrl+C → pacho run → checkpoint resume batavo
"""

import time
from openai import OpenAI
import streamctx

# ---------------- CONFIG ----------------
OPENROUTER_API_KEY = "OPENROUTER_API_KEY"  # <-- apni real key
MODEL = "openrouter/free"                         # <-- working free router
SESSION_ID = "hn-launch-demo-v2"

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
)


# ---------------- LAMBO REPEATED CONTEXT ----------------
# Aa system prompt + "memory" block HAR call ma EXACT SAME jashe.
# Real agent ma aa rite j thay che — system prompt, tool definitions,
# conversation history badhu repeat thay che har turn ma.
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

# Multi-step agent jevu — pan HAR message ma j SYSTEM_CONTEXT punah moklay che
# (jevu real production agents kare che — statelessness ne lidhe)
steps = [
    "Step 1: Based on the context, list 3 reasons AI agents fail in production.",
    "Step 2: Based on the context, explain in one line what 'context drift' means here.",
    "Step 3: Based on the context, suggest one practical fix for agent #1's issue.",
    "Step 4: Based on the context, summarize the recommended fixes in 2 lines.",
]

print(f"\n🚀 StreamCtx session start: '{SESSION_ID}'")
print("   (Same long context resent every step — watch compression/caching kick in)\n")

streamctx.start()
session_id = streamctx.get_session_id()
print(f"   Session ID: {session_id}")

for i, step_prompt in enumerate(steps, start=1):
    print(f"\n--- Step {i}/{len(steps)} ---")
    print(f"Prompt: {step_prompt}")

    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_CONTEXT},  # <-- repeated har vakhte
            {"role": "user", "content": step_prompt},
        ],
    )

    reply = response.choices[0].message.content
    print(f"Response: {reply}")
    print(f"✅ Step {i} checkpointed")
    time.sleep(1.5)

print("\n🎉 Session complete!")
print("   Dashboard kholo: streamlit run dashboard.py  →  localhost:8501\n")
