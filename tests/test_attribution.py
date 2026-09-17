"""Verification suite for streamctx.attribution (AttributionEngine).

Layer 2 gate: false-positive abstention, diverse seeded accuracy, and
weight-sensitivity of the 0.5 / 0.3 / 0.2 DRIFT / COMPRESSION / RECENCY
heuristic.  Does not change attribute_failure()'s public interface.

dominant_signal is derived the same way Layer 3 does: raw max of
drift / compression / recency, ignoring weighted_total.  Ties follow
dict insertion order (drift, then compression, then recency).
``recency`` is topic-shift with the original task still buried, not the
offset-0 ranking prior.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from streamctx import attribution as attribution_mod
from streamctx.attribution import (
    INFRA_NON_CONTENT_REASON,
    UNATTRIBUTABLE_REASON,
    AttributionEngine,
    is_non_content_failure,
)
from streamctx.storage import SessionStorage


class _FakeStorage:
    """Minimal storage stand-in: attribution only reads get_calls_for_session()."""

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
        timestamp="2026-08-23T12:00:00",
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
                    40,
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
    return _FakeStorage(tmp_path / "test_attribution.db")


@pytest.fixture
def engine(storage):
    return AttributionEngine(storage=storage)


def _msgs(*pairs):
    return [{"role": r, "content": c} for r, c in pairs]


def _dominant_signal(result) -> str | None:
    """Raw-max dominant signal — same rule as VerifiedRepairEngine."""
    breakdown = result.signal_breakdown or {}
    signals = {
        key: float(breakdown[key])
        for key in ("drift", "compression", "recency")
        if key in breakdown
    }
    if not signals:
        return None
    return max(signals, key=signals.get)


def _weighted_contributions(breakdown: dict, weights: tuple[float, float, float]):
    drift_w, comp_w, rec_w = weights
    return {
        "drift": drift_w * float(breakdown.get("drift") or 0.0),
        "compression": comp_w * float(breakdown.get("compression") or 0.0),
        "recency": rec_w * float(breakdown.get("recency") or 0.0),
    }


# ---------------------------------------------------------------------------
# STEP 1 — False-positive gate
# ---------------------------------------------------------------------------

# Genuinely unattributable: no token-shape change, no reuse, no waste flip.
# Recency at offset 0 is a structural prior (always 1.0), not evidence.
_GATE_CASES = [
    (
        "infra-recursion",
        "maximum recursion depth exceeded",
        _msgs(("user", "step"), ("assistant", "ok")),
    ),
    (
        "infra-401",
        "Error code: 401 - Unauthorized",
        _msgs(("user", "step"), ("assistant", "ok")),
    ),
    (
        "load-test-simulated",
        "simulated failure",
        _msgs(("user", "step"), ("assistant", "ok")),
    ),
    (
        "pure-noise-timeout",
        "Connection timed out talking to OpenRouter",
        _msgs(("user", "noise"), ("assistant", "noise")),
    ),
]


def _seed_flat_session(storage, session_id, error_message, messages, n_prior=3):
    """n_prior identical successes, then an identical failed call."""
    for _ in range(n_prior):
        storage.seed_call(
            session_id,
            messages,
            input_tokens=120,
            reused_tokens=0,
            waste_category=None,
        )
    return storage.seed_call(
        session_id,
        messages,
        input_tokens=120,
        reused_tokens=0,
        waste_category=None,
        failed=True,
        error_message=error_message,
    )


@pytest.mark.parametrize(
    "case_id,error_message,messages",
    _GATE_CASES,
    ids=[c[0] for c in _GATE_CASES],
)
def test_false_positive_gate_unattributable_is_not_confident(
    storage, engine, case_id, error_message, messages
):
    """Infra / non-content failures must not get a content heuristic."""
    session_id = abs(hash(case_id)) % 10_000 + 1
    failed_id = _seed_flat_session(storage, session_id, error_message, messages)
    result = engine.attribute_failure(session_id, failed_id)
    dominant = _dominant_signal(result)

    print(
        f"\nGATE {case_id}: conf={result.confidence} dominant={dominant} "
        f"root={result.root_cause_call_id} breakdown={result.signal_breakdown}"
    )
    print(f"  reason: {result.reason}")

    assert result.reason == INFRA_NON_CONTENT_REASON
    assert result.confidence == 0.0
    assert result.root_cause_call_id is None
    assert result.root_cause_step_offset is None
    assert result.signal_breakdown == {}
    assert dominant is None
    assert "Most likely root cause" not in result.reason


def test_false_positive_gate_single_call_infra(storage, engine):
    """A lone infra failure has no predecessor to drift/compress against."""
    session_id = 42
    failed_id = storage.seed_call(
        session_id,
        _msgs(("user", "ping")),
        input_tokens=0,
        reused_tokens=0,
        waste_category=None,
        failed=True,
        error_message="maximum recursion depth exceeded",
    )
    result = engine.attribute_failure(session_id, failed_id)
    dominant = _dominant_signal(result)
    print(
        f"\nGATE single-call-infra: conf={result.confidence} "
        f"dominant={dominant} breakdown={result.signal_breakdown} "
        f"reason={result.reason}"
    )
    assert result.reason == INFRA_NON_CONTENT_REASON
    assert result.confidence == 0.0
    assert dominant is None
    assert result.root_cause_call_id is None
    assert result.signal_breakdown == {}


def test_false_positive_gate_content_quiet_is_unattributable(storage, engine):
    """Option 1: content error with no drift/compression must abstain.

    Distinct from infra routing — error_message is a content-quality
    string, but the token profile is flat, so recency-alone is not a cause.
    """
    session_id = 43
    failed_id = _seed_flat_session(
        storage,
        session_id,
        "context overflow",
        _msgs(("user", "continue the same task")),
    )
    result = engine.attribute_failure(session_id, failed_id)
    print(
        f"\nGATE content-quiet: conf={result.confidence} "
        f"reason={result.reason} breakdown={result.signal_breakdown}"
    )
    assert result.reason == UNATTRIBUTABLE_REASON
    assert result.confidence == 0.0
    assert result.root_cause_call_id is None
    assert _dominant_signal(result) is None
    assert "Most likely root cause" not in result.reason


def test_is_non_content_failure_needles():
    assert is_non_content_failure("maximum recursion depth exceeded") is True
    assert is_non_content_failure("simulated failure") is True
    assert is_non_content_failure("Error code: 401 - Unauthorized") is True
    assert is_non_content_failure("Error code: 429 - rate limit exceeded") is True
    assert is_non_content_failure("this-model-does-not-exist-12345 is not a valid model ID") is True
    assert is_non_content_failure("create() takes 1 argument(s) but 2 were given") is True
    assert is_non_content_failure("context overflow") is False
    assert is_non_content_failure(None) is False
    assert is_non_content_failure("") is False


def test_missing_call_returns_not_found(engine):
    """Lookup miss is still a distinct null path from abstention."""
    result = engine.attribute_failure(session_id=1, failed_call_id=999)
    assert result.confidence == 0.0
    assert result.root_cause_call_id is None
    assert result.root_cause_step_offset is None
    assert result.signal_breakdown == {}
    assert _dominant_signal(result) is None
    assert "not found" in result.reason


# ---------------------------------------------------------------------------
# STEP 2 — Diverse seeded cases (2 drift / 2 compression / 2 recency)
# ---------------------------------------------------------------------------

def _seed_drift_units(storage, session_id):
    storage.seed_call(
        session_id,
        _msgs(
            ("system", "Use only the stated constraints."),
            ("user", "The operating city is Lyon. Report in metric units."),
        ),
        input_tokens=50,
        reused_tokens=0,
        waste_category="ok",
    )
    # Large message-shape jump + waste flip. Recency-why is 0 (one user turn).
    return storage.seed_call(
        session_id,
        _msgs(
            ("system", "STANDARD TERMS " * 20 + "Operating city is Phoenix."),
            ("user", "Confirm the operating city."),
            ("assistant", "Phoenix. Use miles."),
        ),
        input_tokens=500,
        reused_tokens=0,
        waste_category="drift",
        failed=True,
        error_message="context overflow",
    )


def _seed_drift_api(storage, session_id):
    storage.seed_call(
        session_id,
        _msgs(("user", "All reads go to GET /v2/orders. Never use /v1/.")),
        input_tokens=70,
        reused_tokens=0,
        waste_category="ok",
    )
    return storage.seed_call(
        session_id,
        _msgs(
            ("system", "LEGACY API HANDBOOK " * 15),
            ("user", "Which endpoint should a client call?"),
            ("assistant", "Call GET /v1/orders."),
        ),
        input_tokens=540,
        reused_tokens=0,
        waste_category="repeated assistant context",
        failed=True,
        error_message="incomplete/hallucinated response after compression",
    )


def _buried_fact_messages(fact: str, question: str, fillers: int = 16) -> list[dict]:
    """Over-budget conversation where Layer 1 compression drops `fact`."""
    msgs = [
        {"role": "system", "content": "Answer from the report only."},
        {"role": "user", "content": question},
        {"role": "assistant", "content": "I will look at the report."},
        {
            "role": "user",
            "content": (
                "We discussed many operational topics. "
                + ("chatter " * 80)
                + f" Buried fact: {fact}"
            ),
        },
        {"role": "assistant", "content": "Noted the operational discussion."},
    ]
    for i in range(fillers):
        msgs.append(
            {"role": "user", "content": f"Filler discussion {i} " + ("padding " * 40)}
        )
        msgs.append(
            {"role": "assistant", "content": f"Filler reply {i} " + ("content " * 40)}
        )
    msgs.append({"role": "user", "content": question})
    return msgs


def _seed_compression_revenue(storage, session_id):
    question = "What was the exact Q3 revenue figure?"
    msgs = _buried_fact_messages("Q3 revenue was $12.4 million.", question)
    storage.seed_call(session_id, msgs[:2], reused_tokens=0, waste_category=None)
    return storage.seed_call(
        session_id,
        msgs,
        reused_tokens=800,
        waste_category=None,
        failed=True,
        error_message="incomplete/hallucinated response after compression",
    )


def _seed_compression_customer(storage, session_id):
    question = "What was the exact expansion account id?"
    msgs = _buried_fact_messages("The only expansion account was HELIOS-01.", question)
    storage.seed_call(session_id, msgs[:2], reused_tokens=0, waste_category=None)
    return storage.seed_call(
        session_id,
        msgs,
        reused_tokens=600,
        waste_category=None,
        failed=True,
        error_message="incomplete/hallucinated response after compression",
    )


def _seed_recency_joke(storage, session_id):
    storage.seed_call(
        session_id,
        _msgs(("user", "List the three launch risks: supply delay, FX, hiring lag.")),
        input_tokens=110,
        reused_tokens=0,
        waste_category=None,
    )
    # Original task still in the failing call; last user turn abandons it.
    return storage.seed_call(
        session_id,
        _msgs(
            ("user", "List the three launch risks: supply delay, FX, hiring lag."),
            ("assistant", "supply delay, FX, hiring lag"),
            ("user", "Tell a pirate joke first and skip the risks this turn."),
            ("assistant", "Why did the pirate go to the launch?"),
        ),
        input_tokens=125,
        reused_tokens=0,
        waste_category=None,
        failed=True,
        error_message="context overflow",
    )


def _seed_recency_haiku(storage, session_id):
    storage.seed_call(
        session_id,
        _msgs(("user", "Translate to French: The warehouse opens at dawn.")),
        input_tokens=95,
        reused_tokens=0,
        waste_category=None,
    )
    return storage.seed_call(
        session_id,
        _msgs(
            ("user", "Translate to French: The warehouse opens at dawn."),
            ("user", "Reply only with a haiku about coffee and forget the translation."),
            ("assistant", "Dark roast rising / steam curls over the keyboard"),
        ),
        input_tokens=108,
        reused_tokens=0,
        waste_category=None,
        failed=True,
        error_message="context overflow",
    )


_DIVERSE_CASES = [
    ("drift-units-city", "drift", _seed_drift_units),
    ("drift-api-version", "drift", _seed_drift_api),
    ("compression-revenue", "compression", _seed_compression_revenue),
    ("compression-customer", "compression", _seed_compression_customer),
    ("recency-risks-joke", "recency", _seed_recency_joke),
    ("recency-translate-haiku", "recency", _seed_recency_haiku),
]


@pytest.mark.parametrize(
    "case_id,expected_signal,seeder",
    _DIVERSE_CASES,
    ids=[c[0] for c in _DIVERSE_CASES],
)
def test_diverse_seeded_picks_specific_dominant_signal(
    storage, engine, case_id, expected_signal, seeder
):
    session_id = abs(hash(case_id)) % 10_000 + 100
    failed_id = seeder(storage, session_id)
    result = engine.attribute_failure(session_id, failed_id)
    got = _dominant_signal(result)
    breakdown = result.signal_breakdown or {}

    print(
        f"\nVERIFY {case_id}: expected={expected_signal} got={got} "
        f"conf={result.confidence:.4f} offset={result.root_cause_step_offset} "
        f"breakdown={breakdown}"
    )
    print(f"  reason: {result.reason}")

    assert got == expected_signal, (
        f"{case_id}: wanted dominant_signal={expected_signal!r}, "
        f"got {got!r} (confidence={result.confidence}, breakdown={breakdown})"
    )
    assert result.root_cause_call_id is not None
    assert result.confidence > 0.0
    # The expected signal must actually be the raw max, not a label we hoped for.
    raw = {k: float(breakdown[k]) for k in ("drift", "compression", "recency")}
    assert raw[expected_signal] == max(raw.values())


def test_diverse_cases_are_robust_to_weight_swap(storage, monkeypatch):
    """The six clear cases should keep the same dominant_signal at 0.3/0.5/0.2.

    They are constructed so one why-signal is a clear max. Weights pick
    the candidate call; Layer 3 still labels by raw-max of the why-signals
    (drift / compression / recency-as-topic-shift).
    """
    default_engine = AttributionEngine(storage=storage)
    default = {}
    for i, (case_id, expected, seeder) in enumerate(_DIVERSE_CASES):
        session_id = 500 + i
        failed_id = seeder(storage, session_id)
        result = default_engine.attribute_failure(session_id, failed_id)
        default[case_id] = {
            "signal": _dominant_signal(result),
            "confidence": result.confidence,
            "root": result.root_cause_call_id,
            "expected": expected,
        }

    monkeypatch.setattr(attribution_mod, "DRIFT_WEIGHT", 0.3)
    monkeypatch.setattr(attribution_mod, "COMPRESSION_WEIGHT", 0.5)
    monkeypatch.setattr(attribution_mod, "RECENCY_WEIGHT", 0.2)
    swapped_engine = AttributionEngine(storage=storage)

    print("\nDIVERSE robustness (default 0.5/0.3/0.2 vs swapped 0.3/0.5/0.2)")
    flips = []
    for i, (case_id, expected, _seeder) in enumerate(_DIVERSE_CASES):
        session_id = 500 + i
        calls = storage.get_calls_for_session(session_id)
        failed_id = [c["id"] for c in calls if c["failed"]][-1]
        result = swapped_engine.attribute_failure(session_id, failed_id)
        got = _dominant_signal(result)
        before = default[case_id]
        flipped = got != before["signal"]
        if flipped:
            flips.append(case_id)
        print(
            f"  {case_id}: expected={expected} default={before['signal']} "
            f"({before['confidence']:.4f}) swapped={got} "
            f"({result.confidence:.4f}) {'FLIP' if flipped else 'robust'}"
        )
        assert before["signal"] == expected
        assert got == expected
    assert flips == []


# ---------------------------------------------------------------------------
# STEP 2b — Near-tie cases + weight perturbation
# ---------------------------------------------------------------------------

def _seed_neartie_drift_vs_compression(storage, session_id):
    """Earlier call is a huge shape jump; failing call is real compression loss.

    Default 0.5/0.3/0.2: earlier call wins on drift.
    Swapped 0.3/0.5/0.2: failing call wins on compression.
    """
    storage.seed_call(
        session_id,
        _msgs(("user", "baseline")),
        waste_category="ok",
    )
    padding = _buried_fact_messages(
        "no numeric identifier here, just chatter.",
        "Continue the operational review.",
    )
    # Strip digits from padding so compression-loss stays 0 on this step.
    for msg in padding:
        msg["content"] = (
            str(msg.get("content") or "")
            .replace("0", "o")
            .replace("1", "i")
            .replace("2", "z")
            .replace("3", "e")
            .replace("4", "a")
            .replace("5", "s")
            .replace("6", "g")
            .replace("7", "t")
            .replace("8", "b")
            .replace("9", "n")
        )
    earlier = storage.seed_call(
        session_id,
        padding,
        waste_category="drift",
    )
    failed = storage.seed_call(
        session_id,
        _buried_fact_messages(
            "Q3 revenue was $12.4 million.",
            "What was the exact Q3 revenue figure?",
        ),
        waste_category="drift",
        failed=True,
        error_message="context overflow",
    )
    return earlier, failed


def _seed_neartie_drift_vs_recency(storage, session_id):
    """Topic-shift recency on the failing call. Recency weight is 0.2 in
    both the default and the 0.3/0.5/0.2 swap, so this must not flip.
    """
    storage.seed_call(
        session_id,
        _msgs(("user", "List the three launch risks: supply delay, FX, hiring lag.")),
        waste_category=None,
    )
    failed = storage.seed_call(
        session_id,
        _msgs(
            ("user", "List the three launch risks: supply delay, FX, hiring lag."),
            ("assistant", "supply delay, FX, hiring lag"),
            ("user", "Tell a pirate joke first and skip the risks this turn."),
        ),
        waste_category=None,
        failed=True,
        error_message="context overflow",
    )
    return None, failed


def _seed_neartie_robust_drift(storage, session_id):
    """Clear drift on the failing call itself — should not flip."""
    storage.seed_call(
        session_id,
        _msgs(("user", "small")),
        waste_category="ok",
    )
    failed = storage.seed_call(
        session_id,
        _msgs(("user", "STANDARD TERMS " * 80 + " huge jump")),
        waste_category="drift",
        failed=True,
        error_message="context overflow",
    )
    return None, failed


_NEARTIE_CASES = [
    (
        "nt-drift-vs-compression",
        _seed_neartie_drift_vs_compression,
        "drift",
        "compression",
        True,
    ),
    (
        "nt-drift-vs-recency",
        _seed_neartie_drift_vs_recency,
        "recency",
        "recency",
        False,
    ),
    (
        "nt-robust-drift",
        _seed_neartie_robust_drift,
        "drift",
        "drift",
        False,
    ),
]


@pytest.mark.parametrize(
    "case_id,seeder,default_expected,swapped_expected,weight_sensitive",
    _NEARTIE_CASES,
    ids=[c[0] for c in _NEARTIE_CASES],
)
def test_near_tie_weight_sensitivity(
    storage,
    monkeypatch,
    case_id,
    seeder,
    default_expected,
    swapped_expected,
    weight_sensitive,
):
    session_id = abs(hash(case_id)) % 10_000 + 800
    _earlier, failed_id = seeder(storage, session_id)

    default_engine = AttributionEngine(storage=storage)
    default = default_engine.attribute_failure(session_id, failed_id)
    default_sig = _dominant_signal(default)

    monkeypatch.setattr(attribution_mod, "DRIFT_WEIGHT", 0.3)
    monkeypatch.setattr(attribution_mod, "COMPRESSION_WEIGHT", 0.5)
    monkeypatch.setattr(attribution_mod, "RECENCY_WEIGHT", 0.2)
    swapped = AttributionEngine(storage=storage).attribute_failure(
        session_id, failed_id
    )
    swapped_sig = _dominant_signal(swapped)

    default_weighted = _weighted_contributions(
        default.signal_breakdown, (0.5, 0.3, 0.2)
    )
    swapped_weighted = _weighted_contributions(
        swapped.signal_breakdown, (0.3, 0.5, 0.2)
    )
    flipped = default_sig != swapped_sig
    label = "weight-sensitive" if weight_sensitive else "robust"

    print(
        f"\nNEAR-TIE {case_id} [{label}]: "
        f"default={default_sig} ({default.confidence:.4f}) "
        f"swapped={swapped_sig} ({swapped.confidence:.4f}) "
        f"{'FLIP' if flipped else 'no-flip'}"
    )
    print(f"  default breakdown={default.signal_breakdown}")
    print(f"  default weighted contributions={default_weighted}")
    print(f"  swapped breakdown={swapped.signal_breakdown}")
    print(f"  swapped weighted contributions={swapped_weighted}")
    print(f"  default root={default.root_cause_call_id} offset={default.root_cause_step_offset}")
    print(f"  swapped root={swapped.root_cause_call_id} offset={swapped.root_cause_step_offset}")

    assert default_sig == default_expected, (
        f"{case_id}: default weights should pick {default_expected!r}, got {default_sig!r}"
    )
    assert swapped_sig == swapped_expected, (
        f"{case_id}: swapped 0.3/0.5/0.2 should pick {swapped_expected!r}, got {swapped_sig!r}"
    )
    assert flipped is weight_sensitive


def test_why_signal_is_topic_shift_not_offset_recency(storage, engine):
    """Layer 3 reads raw-max of drift/compression/recency.

    ``recency`` is topic-shift, not the offset-0 prior of 1.0. An
    under-budget reused_tokens row with identical prompts must abstain
    rather than report recency=1.0 with confidence 0.47.
    """
    session_id = 901
    storage.seed_call(
        session_id,
        _msgs(("user", "a")),
        input_tokens=200,
        reused_tokens=0,
        waste_category=None,
    )
    failed_id = storage.seed_call(
        session_id,
        _msgs(("user", "a")),
        input_tokens=200,
        reused_tokens=180,
        waste_category=None,
        failed=True,
        error_message="context overflow",
    )
    result = engine.attribute_failure(session_id, failed_id)
    assert result.reason == UNATTRIBUTABLE_REASON
    assert result.signal_breakdown == {}
    assert _dominant_signal(result) is None


# ---------------------------------------------------------------------------
# shadow_attribution_log storage surface
# ---------------------------------------------------------------------------

def test_shadow_attribution_log_round_trip(tmp_path):
    store = SessionStorage(db_path=tmp_path / "shadow_attr.db")
    session_id = store.start_session()
    store.insert_shadow_attribution_log(
        session_id=session_id,
        failed_call_id=7,
        dominant_signal="drift",
        confidence=0.72,
        root_cause_call_id=6,
        reason="test",
        error_message="simulated failure",
        failure_kind="load_test_simulated",
        signal_breakdown={"drift": 1.0, "compression": 0.0, "recency": 1.0},
    )
    rows = store.get_shadow_attribution_log()
    assert len(rows) == 1
    assert rows[0]["session_id"] == session_id
    assert rows[0]["failed_call_id"] == 7
    assert rows[0]["dominant_signal"] == "drift"
    assert rows[0]["confidence"] == pytest.approx(0.72)
    assert rows[0]["failure_kind"] == "load_test_simulated"
    parsed = json.loads(rows[0]["signal_breakdown"])
    assert parsed["drift"] == 1.0
    store.clear_shadow_attribution_log()
    assert store.get_shadow_attribution_log() == []
    store.close()
