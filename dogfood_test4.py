from openai import OpenAI
from streamctx.tracker import get_tracker

tracker = get_tracker(agent_id="dogfood_real_call_attempt")
tracker.start()

client = OpenAI(api_key="fake-key-123")
tracker.wrap(client)

try:
    client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[{"role": "user", "content": "hi"}]
    )
except Exception as e:
    print("Expected error (fake key):", type(e).__name__)

print("Stats after attempted call:", tracker.get_stats())
tracker.stop()