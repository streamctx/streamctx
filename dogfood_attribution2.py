from openai import OpenAI
from streamctx.tracker import get_tracker
from streamctx.attribution import get_attribution_engine

tracker = get_tracker(agent_id="dogfood_attribution_real")
tracker.start()
session_id = tracker.get_session_id()

client = OpenAI(api_key="fake-key-123")
tracker.wrap(client)

# Multiple calls karo, jethi attribution ne pattern male
for i in range(3):
    try:
        client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": f"message {i}"}]
        )
    except Exception:
        pass

tracker.stop()

engine = get_attribution_engine()
results = engine.attribute_session(session_id=session_id)
print("Attribution results:", results)
