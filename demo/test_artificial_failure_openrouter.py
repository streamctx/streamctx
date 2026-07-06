"""
Artificial-failure smoke test for the Causal Failure Attribution Engine,
using a REAL OpenRouter connection (free-tier key) via the official
`openai` SDK pointed at OpenRouter's base_url.

3 calls are made through the real tracker.wrap(client):
  call 1: small, valid prompt -> succeeds
  call 2: large/repeated context, valid prompt -> succeeds (drift/reuse precursor)
  call 3: invalid model name -> OpenRouter returns a real error -> tracker
          catches it -> failed=True gets persisted

Then the AttributionEngine is asked to explain call 3's failure.
"""

import os
from dotenv import load_dotenv
from openai import OpenAI

from streamctx.tracker import get_tracker
from streamctx.attribution import get_attribution_engine

load_dotenv()

API_KEY = os.environ.get("OPENROUTER_API_KEY")
if not API_KEY:
    raise SystemExit("OPENROUTER_API_KEY not found in .env — check your .env file location/name.")

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=API_KEY,
)

VALID_MODEL = "openrouter/free"  # free OpenRouter model
INVALID_MODEL = "this-model-does-not-exist-12345"


def main():
    tracker = get_tracker(agent_id="artificial-failure-test")
    tracker.start()

    wrapped_client = tracker.wrap(client)

    print("Call 1 (small, valid)...")
    wrapped_client.chat.completions.create(
        model=VALID_MODEL,
        messages=[
            {"role": "system", "content": "You are a concise assistant."},
            {"role": "user", "content": "Say hello in one short sentence."},
        ],
    )

    print("Call 2 (large repeated context, valid)...")
    big_system_prompt = "You are a concise assistant. " * 80  # force a big, repeated system prompt
    wrapped_client.chat.completions.create(
        model=VALID_MODEL,
        messages=[
            {"role": "system", "content": big_system_prompt},
            {"role": "user", "content": "Continue the previous task, one more short sentence."},
        ],
    )

    print("Call 3 (invalid model -> real OpenRouter error)...")
    try:
        wrapped_client.chat.completions.create(
            model=INVALID_MODEL,
            messages=[
                {"role": "system", "content": big_system_prompt},
                {"role": "user", "content": "One more step please."},
            ],
        )
    except Exception as e:
        print(f"  -> Caught expected error: {e}")

    session_id = tracker.get_session_id()
    tracker.stop()

    print(f"\nSession ID: {session_id}")
    print("Running Attribution Engine...\n")

    engine = get_attribution_engine()
    results = engine.attribute_session(session_id)

    if not results:
        print("No failed calls found in this session — check that the")
        print("invalid-model call actually raised inside tracker's except block.")
        return

    for r in results:
        print("-" * 70)
        print(f"failed_call_id      : {r.failed_call_id}")
        print(f"root_cause_call_id  : {r.root_cause_call_id}")
        print(f"step_offset         : {r.root_cause_step_offset}")
        print(f"confidence          : {r.confidence}")
        print(f"reason              : {r.reason}")
        print(f"signal_breakdown    : {r.signal_breakdown}")


if __name__ == "__main__":
    main()


