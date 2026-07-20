from openai import OpenAI
from streamctx.tracker import get_tracker

# Session 1: messages generate karo aur checkpoint lo
tracker = get_tracker(agent_id="dogfood_replay_v2")
tracker.start()
session_id = tracker.get_session_id()
print("Session ID:", session_id)

client = OpenAI(api_key="fake-key-123")
tracker.wrap(client)

try:
    client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[{"role": "user", "content": "hello"}]
    )
except Exception as e:
    print("Call attempted (expected fail):", type(e).__name__)

tracker.checkpoint()
print("Stats before stop:", tracker.get_stats())
tracker.stop()

# Session 2: naya tracker banaine same session_id resume karo
tracker2 = get_tracker(agent_id="dogfood_replay_v2_reader")
resumed = tracker2.resume(session_id=session_id)
print("Resumed messages:", resumed)
