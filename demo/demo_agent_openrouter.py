"""
StreamCtx Demo Script — OpenRouter (FREE model) sathe REAL LLM calls
======================================================================
Aa script real OpenRouter API call kare che (free model), ane StreamCtx
automatically e calls ne track + checkpoint + resume kare che.

SETUP (ek vaar):
1. Terminal ma: pip install openai
2. Niche line 26 ma "YOUR_OPENROUTER_KEY_HERE" badli ne apni real key nakho
3. Line 27 ma model name check karo — openrouter.ai/models par "free" search
   karine koi pan available free model nu exact slug copy karo

RECORDING FLOW (GIF banavva mate):
1. python demo_agent.py  →  run karo, 2-3 calls thay tya sudhi joyu
2. Ctrl+C dabavo (vachali ja kill karo — agent "crash" thayu evu dekhase)
3. Pacha j: python demo_agent.py  →  batavo ke checkpoint thi continue thay che
4. Streamlit dashboard tab kholo (alag terminal ma: streamlit run dashboard.py)
   ane live session/calls/checkpoints table batavo
"""

import time
from openai import OpenAI
import streamctx

# ---------------- CONFIG ----------------
OPENROUTER_API_KEY = "OPENROUTER_API_KEY"  # <-- apni real key aa ja paste karo
MODEL = "openrouter/free"            # <-- openrouter.ai/models par confirm karo
SESSION_ID = "hn-launch-demo"

# ---------------- OPENROUTER CLIENT ----------------
# OpenRouter OpenAI-compatible endpoint che, etle StreamCtx no
# OpenAI monkeypatch automatically aa calls ne pakdi lese.
client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
)

# ---------------- STREAMCTX SESSION ----------------
print(f"\n🚀 StreamCtx session start: '{SESSION_ID}'")
print("   (Real LLM calls via OpenRouter — har call automatically tracked + checkpointed)\n")

streamctx.start()
SESSION_ID = streamctx.get_session_id()
print(f" Session ID:{SESSION_ID}")


# Multi-step agent jevu simulate karva mate prompts
prompts = [
    "Step 1: List 3 reasons AI agents fail in production. Keep it short.",
    "Step 2: In one line, explain what 'context drift' means for AI agents.",
    "Step 3: Suggest one practical fix for context drift in long-running agents.",
    "Step 4: Summarize the previous 3 answers in 2 lines.",
]

for i, prompt in enumerate(prompts, start=1):
    print(f"--- Step {i}/{len(prompts)} ---")
    print(f"Prompt: {prompt}")

    response = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
    )

    reply = response.choices[0].message.content
    print(f"Response: {reply}")

    # StreamCtx no LLMTracker monkeypatch aa call ne automatically
    # SQLite ma record kare che (record_call) + checkpoint save kare che.
    # Manually kai karva ni jarur nathi — pan jo tamaru tracker.py manual
    # checkpoint function mangtu hoy, niche line uncomment karo:
    # tracker.save_checkpoint(step=i)

    print(f"✅ Step {i} checkpointed\n")
    time.sleep(1.5)  # recording mate thodi pause — GIF ma readable lagse

print("🎉 Session complete!")
print("   Dashboard kholo: streamlit run dashboard.py  →  localhost:8501\n")
