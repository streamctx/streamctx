"""Session-grounded fact-contradiction review — senior-bar cases.

This is not a general hallucination detector. Findings must fire on
same-family ID / $ contradictions against stored session history, and
must not cry wolf on paraphrase, legitimate updates, or compression
drops (those last are missing_context, not contradiction).
"""
from __future__ import annotations

from types import SimpleNamespace

from streamctx.attribution import AttributionEngine
from streamctx.facts import (
    FAILURE_KIND,
    KIND_CONTRADICTION,
    KIND_MISSING_CONTEXT,
    compressed_view,
    find_reply_contradictions,
)
from streamctx.storage import SessionStorage
from streamctx.tracker import LLMTracker


def _user(text: str, call_id: int | None = None) -> dict:
    msg: dict = {"role": "user", "content": text}
    if call_id is not None:
        msg["call_id"] = call_id
    return msg


def _asst(text: str) -> dict:
    return {"role": "assistant", "content": text}


def _fake_response(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        usage=SimpleNamespace(prompt_tokens=8, completion_tokens=12),
    )


class _FakeCompletions:
    def __init__(self, on_create):
        self._on_create = on_create

    def create(self, **kwargs):
        return self._on_create(**kwargs)


class _FakeClient:
    def __init__(self, on_create):
        self.chat = SimpleNamespace(completions=_FakeCompletions(on_create))


def _tracker(tmp_path, agent_id: str) -> LLMTracker:
    tracker = LLMTracker(agent_id=agent_id)
    tracker.state.storage = SessionStorage(db_path=tmp_path / f"{agent_id}.db")
    return tracker


def _kinds(findings):
    return [(f.kind, f.fact_type, f.expected, f.observed) for f in findings]


# ---------------------------------------------------------------------------
# Unit: what is and is not a finding
# ---------------------------------------------------------------------------

def test_stable_id_contradiction_is_flagged():
    history = [_user("Work the support ticket ACME-9917 and nothing else.")]
    findings = find_reply_contradictions(history, "Closing ACME-1234 now.")
    assert _kinds(findings) == [
        (KIND_CONTRADICTION, "stable_id", "ACME-9917", "ACME-1234")
    ]


def test_early_id_in_long_session_is_still_caught():
    history = [_user("Ticket ACME-9917 is the only one in scope.", call_id=1)]
    history.extend(
        _user(f"Standup filler paragraph {i}. The coffee machine is still broken.")
        for i in range(24)
    )
    findings = find_reply_contradictions(
        history, "Sure — I will close ACME-1234 after lunch."
    )
    assert len(findings) == 1
    assert findings[0].expected == "ACME-9917"
    assert findings[0].observed == "ACME-1234"
    assert findings[0].kind == KIND_CONTRADICTION


def test_paraphrase_without_dollar_sign_is_not_flagged():
    history = [_user("Q3 budget is $12.4 million. Do not exceed it.")]
    findings = find_reply_contradictions(
        history, "Confirmed. The cap is 12,400,000 dollars."
    )
    assert findings == []


def test_dollar_scale_variant_is_not_flagged():
    history = [_user("Q3 budget is $12.4 million.")]
    findings = find_reply_contradictions(
        history, "Budget remains $12,400,000 for the quarter."
    )
    assert findings == []


def test_dollar_cents_padding_is_not_flagged():
    history = [_user("Budget is $12.4 million.")]
    findings = find_reply_contradictions(history, "Budget is $12.40 million.")
    assert findings == []


def test_different_dollar_amount_is_flagged():
    history = [_user("Q3 budget is $12.4 million.")]
    findings = find_reply_contradictions(
        history, "I will proceed with the $47.3 million envelope."
    )
    assert len(findings) == 1
    assert findings[0].fact_type == "dollar"
    assert findings[0].kind == KIND_CONTRADICTION
    assert findings[0].expected == "$12.4"
    assert findings[0].observed == "$47.3"


def test_recap_containing_ground_truth_is_not_flagged():
    history = [
        _user("Ticket ACME-9917.", call_id=1),
        _user("The ticket was reassigned to ACME-4401.", call_id=2),
    ]
    findings = find_reply_contradictions(
        history, "Was ACME-9917, now ACME-4401. I will use ACME-4401."
    )
    assert findings == []


def test_legitimate_update_is_not_flagged():
    history = [
        _user("Ticket ACME-9917 is assigned to Maya.", call_id=1),
        _user("The ticket was reassigned to ACME-4401.", call_id=2),
    ]
    findings = find_reply_contradictions(history, "Working ACME-4401 next.")
    assert findings == []


def test_stale_id_after_legitimate_update_is_flagged():
    history = [
        _user("Ticket ACME-9917 is assigned to Maya.", call_id=1),
        _user("The ticket was reassigned to ACME-4401.", call_id=2),
    ]
    findings = find_reply_contradictions(history, "Still on ACME-9917.")
    assert len(findings) == 1
    assert findings[0].expected == "ACME-4401"
    assert findings[0].observed == "ACME-9917"


def test_question_does_not_update_ground_truth():
    history = [
        _user("Ticket ACME-9917 is open.", call_id=1),
        _user("The ticket is ACME-1234, right?", call_id=2),
    ]
    findings = find_reply_contradictions(history, "Yes, ACME-1234.")
    assert len(findings) == 1
    assert findings[0].expected == "ACME-9917"
    assert findings[0].observed == "ACME-1234"


def test_assistant_text_never_becomes_ground_truth():
    history = [
        _user("Ticket ACME-9917.", call_id=1),
        _asst("Got it, ACME-1234."),
        _user("Continue.", call_id=2),
    ]
    findings = find_reply_contradictions(history, "Proceeding with ACME-1234.")
    assert len(findings) == 1
    assert findings[0].expected == "ACME-9917"


def test_new_id_family_is_not_flagged():
    history = [_user("Ticket ACME-9917.")]
    findings = find_reply_contradictions(
        history, "I also opened GH-4411 for the docs."
    )
    assert findings == []


def test_omitting_the_fact_is_not_a_finding():
    history = [_user("Ticket ACME-9917. Budget $12.4 million.")]
    findings = find_reply_contradictions(history, "Sounds good, I will start.")
    assert findings == []


def test_compression_drop_is_missing_context_not_contradiction():
    buried = (
        "Hello there. "
        + ("padding chatter about standup " * 80)
        + " The budget is $12.4 million."
    )
    history = [_user(buried, call_id=1)]
    history.extend(_user(f"more chatter {i} " + ("word " * 40)) for i in range(8))
    outbound = compressed_view(history, max_tokens=200, keep_last_n=4)
    blob = " ".join(str(m.get("content") or "") for m in outbound)
    assert "$12.4" not in blob
    findings = find_reply_contradictions(
        history,
        "I will spend $47.3 million this quarter.",
        compressed_outbound=outbound,
    )
    assert len(findings) == 1
    assert findings[0].kind == KIND_MISSING_CONTEXT
    assert findings[0].fact_type == "dollar"
    assert findings[0].expected_in_compressed is False


def test_wrong_id_while_still_in_compressed_view_is_contradiction():
    history = [_user("Must use ticket ACME-9917.", call_id=1)]
    history.extend(_user(f"chatter {i} " + ("word " * 80)) for i in range(10))
    outbound = compressed_view(history, max_tokens=2000, keep_last_n=4)
    blob = " ".join(str(m.get("content") or "") for m in outbound)
    assert "ACME-9917" in blob
    findings = find_reply_contradictions(
        history, "Closing ACME-1234.", compressed_outbound=outbound
    )
    assert len(findings) == 1
    assert findings[0].kind == KIND_CONTRADICTION
    assert findings[0].expected_in_compressed is True


# ---------------------------------------------------------------------------
# Intercept: review signal, not failed=True
# ---------------------------------------------------------------------------

def test_intercept_logs_review_without_failing_the_call(tmp_path):
    replies = iter(["Noted.", "Closing ACME-1234 after lunch."])
    tracker = _tracker(tmp_path, "fact-contradict")
    tracker.start()
    client = tracker.wrap(_FakeClient(lambda **kw: _fake_response(next(replies))))
    first = [{"role": "user", "content": "Support ticket ACME-9917 is open."}]
    client.chat.completions.create(model="x", messages=first)
    history = first + [
        {"role": "assistant", "content": "Noted."},
        {"role": "user", "content": "What is the ticket ID?"},
    ]
    client.chat.completions.create(model="x", messages=history)
    sid = tracker.get_session_id()
    storage = tracker.state.storage
    tracker.stop()

    rows = storage.get_calls_for_session(sid)
    assert all(r["failed"] in (0, False) for r in rows)
    log = [
        r
        for r in storage.get_shadow_attribution_log()
        if int(r["session_id"]) == int(sid)
    ]
    assert len(log) == 1
    assert log[0]["failure_kind"] == FAILURE_KIND
    assert log[0]["dominant_signal"] == KIND_CONTRADICTION
    assert "ACME-9917" in (log[0]["reason"] or "")
    assert "ACME-1234" in (log[0]["reason"] or "")
    assert rows[-1]["failed"] in (0, False)


def test_intercept_does_not_log_paraphrase(tmp_path):
    tracker = _tracker(tmp_path, "fact-paraphrase")
    tracker.start()
    client = tracker.wrap(
        _FakeClient(
            lambda **kw: _fake_response("The cap is 12,400,000 dollars.")
        )
    )
    client.chat.completions.create(
        model="x",
        messages=[{"role": "user", "content": "Q3 budget is $12.4 million."}],
    )
    sid = tracker.get_session_id()
    storage = tracker.state.storage
    tracker.stop()
    assert storage.get_calls_for_session(sid)[0]["failed"] in (0, False)
    assert storage.get_shadow_attribution_log() == []


def test_intercept_does_not_log_legitimate_update(tmp_path):
    replies = iter(["Noted ACME-9917.", "Working ACME-4401."])
    tracker = _tracker(tmp_path, "fact-update")
    tracker.start()
    client = tracker.wrap(_FakeClient(lambda **kw: _fake_response(next(replies))))
    client.chat.completions.create(
        model="x",
        messages=[{"role": "user", "content": "Ticket ACME-9917 is assigned to Maya."}],
    )
    client.chat.completions.create(
        model="x",
        messages=[
            {"role": "user", "content": "Ticket ACME-9917 is assigned to Maya."},
            {"role": "assistant", "content": "Noted ACME-9917."},
            {"role": "user", "content": "The ticket was reassigned to ACME-4401."},
        ],
    )
    sid = tracker.get_session_id()
    storage = tracker.state.storage
    tracker.stop()
    rows = storage.get_calls_for_session(sid)
    assert all(r["failed"] in (0, False) for r in rows)
    assert storage.get_shadow_attribution_log() == []


def test_attribution_engine_review_returns_none_when_clean(tmp_path):
    storage = SessionStorage(db_path=tmp_path / "clean.db")
    sid = storage.start_session()
    call_id = storage.persist_step(
        session_id=sid,
        provider="openai",
        model="x",
        input_tokens=4,
        output_tokens=4,
        cost=0.0,
        reused_tokens=0,
        waste_category=None,
        request_messages=[{"role": "user", "content": "Ticket ACME-9917."}],
        checkpoint_messages=[{"role": "user", "content": "Ticket ACME-9917."}],
        step_number=1,
        response_text="Working ACME-9917.",
    )
    result = AttributionEngine(storage=storage).review_success_reply(sid, call_id)
    assert result is None
    assert storage.get_shadow_attribution_log() == []


def test_fact_review_opt_out(tmp_path, monkeypatch):
    monkeypatch.setenv("STREAMCTX_FACT_REVIEW", "0")
    tracker = _tracker(tmp_path, "fact-opt-out")
    tracker.start()
    client = tracker.wrap(
        _FakeClient(lambda **kw: _fake_response("Closing ACME-1234."))
    )
    client.chat.completions.create(
        model="x",
        messages=[{"role": "user", "content": "Ticket ACME-9917 is open."}],
    )
    sid = tracker.get_session_id()
    storage = tracker.state.storage
    tracker.stop()
    assert storage.get_calls_for_session(sid)[0]["failed"] in (0, False)
    assert storage.get_shadow_attribution_log() == []
