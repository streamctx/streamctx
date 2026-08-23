"""Tests for streamctx.repair (VerifiedRepairEngine)."""

import json
import sqlite3

import pytest

from streamctx.repair import (
    INFRA_NOT_REPAIRABLE,
    UNFIXABLE_CONTENT,
    VerifiedRepairEngine,
    classify_failure,
    get_repair_engine,
    is_unfixable_content_failure,
)


class _FakeStorage:
    """Storage stand-in covering attribution + replay.

    Attribution reads ``get_calls_for_session()``.  Replay reads
    checkpoints via ``self.storage._connect()``.
    """

    def __init__(self, db_path):
        self._db_path = str(db_path)
        conn = sqlite3.connect(self._db_path)
        conn.execute(
            """
            CREATE TABLE calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER,
                timestamp TEXT,
                provider TEXT,
                model TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER,
                cost REAL,
                reused_tokens INTEGER,
                waste_category TEXT,
                messages_json TEXT,
                failed INTEGER,
                healed INTEGER,
                error_message TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE checkpoints (
                session_id INTEGER,
                step_number INTEGER,
                messages_json TEXT,
                timestamp TEXT
            )
            """
        )
        conn.commit()
        conn.close()

    def _connect(self):
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def seed_call(
        self,
        session_id,
        messages,
        *,
        input_tokens=100,
        reused_tokens=0,
        waste_category=None,
        failed=False,
        error_message=None,
        timestamp="2026-08-23T06:00:00",
    ):
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO calls (
                    session_id, timestamp, provider, model,
                    input_tokens, output_tokens, cost,
                    reused_tokens, waste_category, messages_json,
                    failed, healed, error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    timestamp,
                    "openrouter",
                    "test-model",
                    input_tokens,
                    50,
                    0.001,
                    reused_tokens,
                    waste_category,
                    json.dumps(messages),
                    int(failed),
                    0,
                    error_message,
                ),
            )
            return int(cur.lastrowid)

    def seed_checkpoint(self, session_id, step_number, messages):
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO checkpoints (session_id, step_number, messages_json, timestamp) "
                "VALUES (?, ?, ?, ?)",
                (session_id, step_number, json.dumps(messages), "2026-08-23T06:00:00"),
            )

    def get_calls_for_session(self, session_id):
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, session_id, timestamp, provider, model,
                       input_tokens, output_tokens, cost,
                       reused_tokens, waste_category, messages_json,
                       failed, healed, error_message
                FROM calls
                WHERE session_id = ?
                ORDER BY id ASC
                """,
                (session_id,),
            ).fetchall()
        return [dict(r) for r in rows]


@pytest.fixture
def storage(tmp_path):
    return _FakeStorage(tmp_path / "test_repair.db")


@pytest.fixture
def engine(storage):
    return VerifiedRepairEngine(storage=storage)


def _msgs(*pairs):
    return [{"role": r, "content": c} for r, c in pairs]


def _seed_session(storage, *, signal="recency", error_message="context overflow"):
    """Seed a 2-call session whose dominant signal we can steer.

    Attribution scores the failing call itself at offset 0 (recency=1.0).
    Ties go to the first max key in insertion order (drift, compression,
    recency), so:
      - high reuse + stable tokens  → compression
      - big token jump + waste flip → drift
      - otherwise                   → recency
    """
    session_id = 8
    first = _msgs(("user", "original task: summarize the report"))
    second = _msgs(
        ("user", "original task: summarize the report"),
        ("assistant", "ok"),
        ("user", "continue"),
    )

    if signal == "compression":
        storage.seed_call(
            session_id, first, input_tokens=200, reused_tokens=0, waste_category=None
        )
        failed_id = storage.seed_call(
            session_id,
            second,
            input_tokens=200,
            reused_tokens=200,
            waste_category=None,
            failed=True,
            error_message=error_message,
        )
    elif signal == "drift":
        storage.seed_call(
            session_id, first, input_tokens=50, reused_tokens=0, waste_category="ok"
        )
        failed_id = storage.seed_call(
            session_id,
            second,
            input_tokens=500,
            reused_tokens=0,
            waste_category="drift",
            failed=True,
            error_message=error_message,
        )
    else:
        storage.seed_call(
            session_id, first, input_tokens=100, reused_tokens=0, waste_category=None
        )
        failed_id = storage.seed_call(
            session_id,
            second,
            input_tokens=110,
            reused_tokens=0,
            waste_category=None,
            failed=True,
            error_message=error_message,
        )

    storage.seed_checkpoint(session_id, 1, first)
    storage.seed_checkpoint(session_id, 2, second)
    return session_id, failed_id


class _FakeResponse:
    def __init__(self, text):
        self.choices = [type("Choice", (), {"message": type("Msg", (), {"content": text})()})()]


# ---------------------------------------------------------------------
# classify_failure
# ---------------------------------------------------------------------

@pytest.mark.parametrize(
    "error_message, expected",
    [
        ("Error code: 401 - Unauthorized", "infra_error"),
        ("403 Forbidden: invalid api key", "infra_error"),
        ("Error code: 429 - rate limit exceeded", "infra_error"),
        ("Connection timed out talking to OpenRouter", "infra_error"),
        ("Invalid model: this-model-does-not-exist-12345", "infra_error"),
        ("Error code: 400 - malformed request", "infra_error"),
        ("Error code: 404 - This model is unavailable for free", "infra_error"),
        (None, "content_error"),
        ("", "content_error"),
        ("simulated failure", "content_error"),
        ("context overflow", "content_error"),
        ("incomplete/hallucinated response after compression", "content_error"),
    ],
)
def test_classify_failure(error_message, expected):
    assert classify_failure(error_message) == expected


def test_verify_fix_skips_infra_error(storage, engine):
    session_id, failed_id = _seed_session(
        storage,
        signal="recency",
        error_message="Error code: 401 - Unauthorized",
    )

    result = engine.verify_fix(session_id, failed_id, dry_run=False)

    assert result.resolved is False
    assert result.fix_candidate == {}
    assert result.root_cause_call_id is None
    assert result.dominant_signal is None
    assert result.reason == INFRA_NOT_REPAIRABLE
    assert result.proof["failure_class"] == "infra_error"
    assert result.proof["before_after_diff"] == {}
    assert "infrastructure/API error" in result.reason


# ---------------------------------------------------------------------
# factory
# ---------------------------------------------------------------------

def test_get_repair_engine_returns_instance():
    engine = get_repair_engine()
    assert isinstance(engine, VerifiedRepairEngine)


# ---------------------------------------------------------------------
# dry_run — no llm_fn required
# ---------------------------------------------------------------------

def test_verify_fix_dry_run_does_not_require_llm_fn(storage, engine):
    session_id, failed_id = _seed_session(storage, signal="recency")

    result = engine.verify_fix(session_id, failed_id)

    assert result.dry_run is True
    assert result.resolved is False
    assert result.confidence_delta == 0.0
    assert result.root_cause_call_id is not None
    assert result.proof["timestamp"]
    assert "before_after_diff" in result.proof
    assert result.proof["before_after_diff"]["added_messages"]


def test_verify_fix_live_without_llm_fn_raises(storage, engine):
    session_id, failed_id = _seed_session(storage)

    with pytest.raises(ValueError, match="llm_fn is required"):
        engine.verify_fix(session_id, failed_id, dry_run=False)


# ---------------------------------------------------------------------
# missing call
# ---------------------------------------------------------------------

def test_verify_fix_failed_call_not_found(engine):
    result = engine.verify_fix(session_id=8, failed_call_id=999)

    assert result.resolved is False
    assert result.root_cause_call_id is None
    assert result.proof["before_after_diff"] == {}
    assert "not found" in result.proof["attribution_reason"]


# ---------------------------------------------------------------------
# signal → fix_candidate
# ---------------------------------------------------------------------

def test_verify_fix_compression_generates_dedupe_note(storage, engine):
    session_id, failed_id = _seed_session(storage, signal="compression")

    result = engine.verify_fix(session_id, failed_id)

    assert result.dominant_signal == "compression"
    assert isinstance(result.fix_candidate, dict)
    assert result.fix_candidate["role"] == "system"
    assert "DEDUPE" in result.fix_candidate["content"]
    assert result.proof["fix_strategy"] == "dedupe"


def test_verify_fix_drift_reanchors_to_earliest_task(storage, engine):
    session_id, failed_id = _seed_session(storage, signal="drift")

    result = engine.verify_fix(session_id, failed_id)

    assert result.dominant_signal == "drift"
    assert result.proof["fix_strategy"] == "reanchor"
    blob = json.dumps(result.fix_candidate)
    assert "RE-ANCHOR" in blob
    assert "original task: summarize the report" in blob.lower()


def test_verify_fix_recency_resurfaces_earliest_task(storage, engine):
    session_id, failed_id = _seed_session(storage, signal="recency")

    result = engine.verify_fix(session_id, failed_id)

    assert result.dominant_signal == "recency"
    assert isinstance(result.fix_candidate, list)
    assert result.fix_candidate[0]["role"] == "system"
    assert "RESURFACE" in result.fix_candidate[0]["content"]
    assert result.proof["fix_strategy"] == "resurface"
    blob = json.dumps(result.fix_candidate).lower()
    assert "original task: summarize the report" in blob
    assert result.fix_candidate[-1]["content"] != "continue"


# ---------------------------------------------------------------------
# live replay verification
# ---------------------------------------------------------------------

def test_verify_fix_resolved_requires_correct_value_present(storage, engine):
    session_id, failed_id = _seed_session(
        storage, signal="recency", error_message="context overflow"
    )

    missing = engine.verify_fix(
        session_id,
        failed_id,
        llm_fn=lambda _msgs: _FakeResponse("here is a clean summary"),
        dry_run=False,
        correct_value="Lyon",
    )
    assert missing.resolved is False
    assert missing.correct_value == "Lyon"

    restored = engine.verify_fix(
        session_id,
        failed_id,
        llm_fn=lambda _msgs: _FakeResponse("Operating city is Lyon."),
        dry_run=False,
        correct_value="Lyon",
    )
    assert restored.dry_run is False
    assert restored.resolved is True
    assert restored.confidence_delta == round(
        1.0 - restored.proof["attribution_confidence"], 4
    )
    assert restored.proof["resolved"] is True
    assert restored.proof["correct_value"] == "Lyon"


def test_verify_fix_correct_value_matches_unicode_space(storage, engine):
    session_id, failed_id = _seed_session(storage, signal="compression")

    result = engine.verify_fix(
        session_id,
        failed_id,
        llm_fn=lambda _msgs: _FakeResponse("Revenue was $12.4\u202fmillion."),
        dry_run=False,
        correct_value="12.4 million",
    )
    assert result.resolved is True


def test_verify_fix_without_correct_value_is_not_resolved(storage, engine):
    session_id, failed_id = _seed_session(
        storage, signal="recency", error_message="context overflow"
    )

    result = engine.verify_fix(
        session_id,
        failed_id,
        llm_fn=lambda _msgs: _FakeResponse("here is a clean summary"),
        dry_run=False,
    )
    assert result.resolved is False
    assert result.correct_value is None


def test_verify_fix_live_unresolved_when_failure_persists(storage, engine):
    session_id, failed_id = _seed_session(
        storage, signal="recency", error_message="context overflow"
    )

    result = engine.verify_fix(
        session_id,
        failed_id,
        llm_fn=lambda _msgs: _FakeResponse("still seeing context overflow here"),
        dry_run=False,
        correct_value="Lyon",
    )

    assert result.resolved is False
    assert result.confidence_delta == round(-result.proof["attribution_confidence"], 4)


def test_verify_fix_unfixable_question_not_false_positive(storage, engine):
    """Ambiguous question + uncertain reply must not resolve as a false True.

    A paraphrased 'I don't know' (or even an invented confident answer)
    is not proof that context injection fixed anything — the referents
    were never in the session.
    """
    session_id = 8
    first = _msgs(
        ("system", "You are a careful assistant. Only answer when you have the facts."),
        ("user", "Please be careful and only answer when you have the facts."),
    )
    question = (
        "What did the unnamed stakeholder decide about the unspecified "
        "project last Tuesday, and which of the three options did they pick?"
    )
    original_reply = (
        "I don't have enough information to answer that. The question is "
        "ambiguous — it doesn't specify who the stakeholder is, which "
        "project you mean, or what the three options were."
    )
    failed_msgs = [
        {"role": "system", "content": first[0]["content"]},
        {"role": "user", "content": question},
        {"role": "assistant", "content": original_reply},
    ]
    assert is_unfixable_content_failure(failed_msgs) is True

    storage.seed_call(session_id, first, input_tokens=80, reused_tokens=0)
    failed_id = storage.seed_call(
        session_id,
        failed_msgs,
        input_tokens=90,
        reused_tokens=0,
        failed=True,
        error_message=None,
    )
    storage.seed_checkpoint(session_id, 1, first)
    storage.seed_checkpoint(session_id, 2, failed_msgs[:-1])

    paraphrased = engine.verify_fix(
        session_id,
        failed_id,
        llm_fn=lambda _msgs: _FakeResponse(
            "That's unclear — I still cannot tell which stakeholder "
            "or project you mean."
        ),
        dry_run=False,
    )
    assert paraphrased.resolved is False
    assert paraphrased.reason == UNFIXABLE_CONTENT
    assert paraphrased.proof["unfixable_content"] is True
    assert paraphrased.confidence_delta == round(
        -paraphrased.proof["attribution_confidence"], 4
    )

    invented = engine.verify_fix(
        session_id,
        failed_id,
        llm_fn=lambda _msgs: _FakeResponse("They picked option two on Tuesday."),
        dry_run=False,
    )
    assert invented.resolved is False
    assert invented.reason == UNFIXABLE_CONTENT


def test_verify_fix_live_unresolved_on_llm_error(storage, engine):
    session_id, failed_id = _seed_session(storage)

    def failing_llm(_msgs):
        raise RuntimeError("API down")

    result = engine.verify_fix(
        session_id, failed_id, llm_fn=failing_llm, dry_run=False
    )

    assert result.resolved is False


# ---------------------------------------------------------------------
# proof / attestation
# ---------------------------------------------------------------------

def test_verify_fix_proof_contains_timestamp_and_diff(storage, engine):
    session_id, failed_id = _seed_session(storage, signal="compression")

    result = engine.verify_fix(session_id, failed_id)

    proof = result.proof
    assert proof["attestation"] == "streamctx.repair.v1"
    assert "T" in proof["timestamp"]
    diff = proof["before_after_diff"]
    assert diff["from_step"] == proof["from_step"]
    assert len(diff["added_messages"]) >= 1
    assert "DEDUPE" in diff["added_messages"][0]["content"]
