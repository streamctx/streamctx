

# --- StreamCtx global state safety net -----------------------------------
# Some tests call streamctx.start() (which monkeypatches the real OpenAI /
# Anthropic SDK classes) without a matching streamctx.stop() in their own
# cleanup. If that patch is never undone, it leaks into every test that
# runs afterwards in the same process, causing wrap() to see an
# already-patched class and silently defer to stale state.
#
# This autouse fixture guarantees a clean slate after EVERY test, no
# matter how that test cleaned up (or didn't).
import pytest


@pytest.fixture(autouse=True)
def _streamctx_force_reset():
    yield
    try:
        from streamctx.tracker import _trackers, _wrapped_clients
    except ImportError:
        return

    for tracker in list(_trackers.values()):
        try:
            if tracker.state.active:
                tracker.stop()
        except Exception:
            pass
        tracker.state.active = False
        tracker.state.call_count = 0
        tracker.state.step_counter = 0
        tracker.state.auto_reported = False
        tracker.state.session_id = None
        tracker.state._originals.clear()

    _wrapped_clients.clear()
