from streamctx.tracker import get_tracker

tracker = get_tracker(agent_id="dogfood_replay_test")
tracker.start()

session_id = tracker.get_session_id()
print("Session ID:", session_id)

tracker.checkpoint()
tracker.checkpoint()
tracker.checkpoint()

tracker.stop()

# Naya tracker banaine, purana session resume karva ni koshish
tracker2 = get_tracker(agent_id="dogfood_replay_test_2")
resumed_messages = tracker2.resume(session_id=session_id)
print("Resumed messages:", resumed_messages)
