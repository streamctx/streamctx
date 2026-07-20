from streamctx.tracker import get_tracker
from streamctx.attribution import get_attribution_engine

tracker = get_tracker(agent_id="dogfood_attribution_test")
tracker.start()
session_id = tracker.get_session_id()

for i in range(3):
    tracker.checkpoint()

tracker.stop()

engine = get_attribution_engine()

try:
    results = engine.attribute_session(session_id=session_id)
    print("Attribution results:", results)
except Exception as e:
    print("Attribution error:", type(e).__name__, "-", str(e))

