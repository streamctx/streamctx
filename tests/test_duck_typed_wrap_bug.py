from streamctx import get_tracker


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]
        self.usage = type("U", (), {"prompt_tokens": 10, "completion_tokens": 5})()


class _FakeCompletions:
    def create(self, *args, **kwargs):
        return _FakeResponse("ok")


class _FakeChat:
    def __init__(self):
        self.completions = _FakeCompletions()


class _FakeClient:
    def __init__(self):
        self.chat = _FakeChat()


def test_wrap_tracks_duck_typed_client():
    tracker = get_tracker(agent_id="test-duck-typed")
    tracker.start()
    client = tracker.wrap(_FakeClient())
    client.chat.completions.create(model="fake", messages=[{"role": "user", "content": "hi"}])
    tracker.stop()
    stats = tracker.get_stats()
    assert stats["call_count"] == 1, "wrap() should track calls even on duck-typed clients"


