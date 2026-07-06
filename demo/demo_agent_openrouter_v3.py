"""
StreamCtx Demo Script — V3: REAL compression stats via compress() + get_compression_stats()
================================================================================================
Pehla versions ma sirf start()/checkpoint thatu hatu — compression engine
manually trigger nahotu thatu, etle "0% saved" dekhatu hatu.

Aa V3 ma:
1. Conversation history build thay che (system + previous turns) har step pachi
2. Jyare history lambi thay, streamctx.compress() REAL rite call thay che
3. streamctx.get_compression_stats() REAL before/after token numbers aape che
4. Aa numbers GIF ma spasht "X% compressed, Y tokens saved" batavshe

SETUP: Line niche API key + model already set che.
"""

import time
from openai import OpenAI
import streamctx

# ---------------- CONFIG ----------------
OPENROUTER_API_KEY = "OPENROUTER_API_KEY"  # <-- apni real key
MODEL = "openrouter/free"
SESSION_ID = "hn-launch-demo-v3"

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


def rough_token_count(text: str) -> int:
    """Quick approx: ~4 chars per token (good enough for a live demo counter)."""
    return max(1, len(text) // 4)


print(f"\n🚀 StreamCtx session start: '{SESSION_ID}'")
print("   (Conversation history grows each step — watch compress() kick in)\n")

streamctx.start()
session_id = streamctx.get_session_id()
print(f"   Session ID: {session_id}")

# Conversation history humans build manually karta jaiye (agent jevu)
conversation = [{"role": "system", "content": SYSTEM_CONTEXT}]

for i, step_prompt in enumerate(steps, start=1):
    print(f"\n--- Step {i}/{len(steps)} ---")
    print(f"Prompt: {step_prompt}")

    conversation.append({"role": "user", "content": step_prompt})

    # ---- REAL StreamCtx compression check ----
    original_tokens = sum(rough_token_count(m["content"]) for m in conversation)
    print(f"   📏 Context before compress: ~{original_tokens} tokens, {len(conversation)} messages")

    if original_tokens > 150 and len(conversation) > 3:
        # Real compress() call — keeps system + last 4 turns, summarizes rest
        compressed = streamctx.compress(conversation, max_tokens=2000, keep_last_n=4)
        compressed_messages = compressed.get("messages", conversation)
        compressed_tokens = sum(rough_token_count(m["content"]) for m in compressed_messages)

        stats = streamctx.get_compression_stats(
            original_tokens=original_tokens,
            compressed_tokens=compressed_tokens,
        )
        pct_saved = round((1 - compressed_tokens / original_tokens) * 100, 1) if original_tokens else 0
        print(f"   🗜️  streamctx.compress() ran: {original_tokens} → {compressed_tokens} tokens "
              f"({pct_saved}% saved)")
        print(f"   📊 get_compression_stats(): {stats}")

        conversation = compressed_messages
    else:
        print("   ℹ️  Context still small — compress() skipped (not enough to compress yet)")

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
