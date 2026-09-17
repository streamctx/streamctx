---
knowledge_version: 2026-09-17.4
last_updated: 2026-09-17
pypi_version: 0.4.6
source_repo: streamctx/streamctx
source_commit: working-tree on cursor/layer2-attribution-hardening (parent b3b17b7)
canonical_branch: main
reviewed: true
review_note: Layer 1 section is the prior hardened tree. Layer 2 section regenerated from live attribution.py on cursor/layer2-attribution-hardening. Layer 1 PR is not merged to main as part of this pass. Canonical full-product copy for agents lives at streamctx-agents/knowledge/streamctx_project_knowledge.md.
---

# StreamCtx Layer 1 — Core SDK (verified)

Citation format: `path:start-end` relative to this SDK checkout.

**Status:** SHIPPED in `src/streamctx/` of PyPI 0.4.6 / `main`. Hardened 2026-09-17
(senior-bar + structural fixes). Layer 2 is re-audited in this file (section
below). Layers 3–4 are shipped but **not** re-audited here.

Close-out: `knowledge/HARDENING.md` (Layer 1 — Core SDK hardening; Layer 2 —
Attribution Engine hardening).

---

<!-- section: layer-1-core -->
## Layer 1 — Core SDK (SHIPPED, free forever)

CHANGELOG 0.3.1 names **six** core features, MIT-licensed and free forever:
Checkpoint/Resume, Context Compression, Self-Healing, Poison Detector,
Context Diff, Real-Time Streaming. Source: `CHANGELOG.md:79-82`.

Layer 1 modules actually on disk:

| Concern | Module | Key exports |
| --- | --- | --- |
| Tracking / wrap | `src/streamctx/tracker.py` | `LLMTracker`, `get_tracker()`, `list_active_agents()`, `CallRecord` |
| Storage | `src/streamctx/storage.py` | `SessionStorage`, `get_storage()` |
| Healing | `src/streamctx/healer.py` | `SelfHealingEngine` |
| Poison | `src/streamctx/poison_detector.py` | `PoisonDetector` |
| Diff | `src/streamctx/differ.py` | `ContextDiffer` |
| Compression | `src/streamctx/compressor.py` | `compress_messages()`, `get_compression_stats()` |
| Token $ estimates | `src/streamctx/pricing.py` | `resolve_pricing()`, `estimate_cost()` |
| Terminal report | `src/streamctx/reporter.py` | `print_report()`, `print_auto_summary()` |
| Supabase backend | `src/streamctx/supabase_storage.py` | `SupabaseStorage` |

---

<!-- section: layer-1-streaming -->
## 1. Real-time step streaming to DB — SHIPPED

Means persist-each-call, **not** OpenAI `stream=True` / SSE.

- Success path: `_intercept_call` → `_persist_success` → `SessionStorage.persist_step`
  (call row + checkpoint, **one COMMIT**). Sources: `src/streamctx/tracker.py:519-625,682-722`,
  `src/streamctx/storage.py:273-346`.
- Per step: provider, model, tokens, cost, reused_tokens, waste_category,
  uncompressed `messages_json`, `message_fingerprint`, `response_text`,
  `failed`/`healed`/`error_message`. Source: `src/streamctx/storage.py:273-310`.
- Sync (not async). Write lock + WAL + `synchronous=NORMAL` + busy_timeout 30000
  + read pool 8. Source: `src/streamctx/storage.py:49-73`.
- Mid-write `kill -9`: no half-parsed JSON; atomic pair cannot commit one side.
  Proven: `tests/test_layer1_hardening.py::test_kill9_mid_write_leaves_consistent_rows`.
- Provider succeeded / DB failed: exception swallowed, response still returned.
  Source: `src/streamctx/tracker.py:719-722`. Proven: `test_persist_failure_does_not_drop_provider_response`.

Monkey-patch layering (`_sdk_originals` / `_sdk_started`) and WAL still hold after
the Layer 2–4 merge. Sources: `src/streamctx/tracker.py:21-31,125-156`.
Tests: `tests/test_tracker_patch_stacking.py`. 50-worker write harness:
`tests/concurrent_load_test.py --workers 50 --calls-per-worker 50` PASS.

---

<!-- section: layer-1-checkpoint -->
## 2. Auto-checkpoint per step with exact-step resume — SHIPPED

- After each **successful** intercepted call, assistant reply is appended and
  the conversation is checkpointed atomically with the call row.
  Source: `src/streamctx/tracker.py:585-611,682-722`.
- Failures persist to `calls` only; they do **not** become the resume point.
  Source: `src/streamctx/tracker.py:563-573,653-680`.
- `resume(session_id)` restores `session_id`, `step_counter`, `_last_messages`,
  `active`, and healer context from the latest **valid** checkpoint.
  Source: `src/streamctx/tracker.py:374-399`.
- Corrupt walk: skip `valid=0`, invalid JSON, non-list payloads, newest-first.
  Source: `src/streamctx/storage.py:348-377`.
- Re-submitting the completed snapshot (resume output, assistant tail included)
  does not re-invoke the provider. Source: `src/streamctx/tracker.py:627-650`.
  A second live call with the same *request* is a new step.
- Still assumed: tool side effects in **caller** code after `create()` returns
  (outside wrap) are not skippable by the SDK.

---

<!-- section: layer-1-compression -->
## 3. Real-time context compression — SHIPPED

- Algorithm: keep system + last `keep_last_n` (default 4); pin full high-value
  middle messages (constraint/policy language, stable IDs, tool_calls); extractive
  first-sentence summary of remaining chatter. **Not** a 15-char prefix.
  Sources: `src/streamctx/compressor.py:40-86,89-129`.
- Intercept **mutates** outbound `kwargs["messages"]` when savings > 0 and
  estimated tokens exceed `max_tokens` (default 2000). DB stores uncompressed
  request. Source: `src/streamctx/tracker.py:544-556`.
- Token estimate: `len(text) // 4`. Source: `src/streamctx/compressor.py:9-12`.
- Measured (`max_tokens=800`, `keep_last_n=4`, 2026-09-17): chatter **79%**,
  tool-heavy **80%**, buried-constraint **88%** with `ACME-9917` preserved.
- **DOC DISCREPANCY:** `__init__.py` 40–70%; README 30–60%; `docs/concepts/compression.md`
  still says "exact duplicates only". Code + measurements win.

Wrong-compression failure mode: a constraint that matches none of the pin
heuristics and sits outside last-N can still be summarized to a first sentence.
The senior-bar case (CRITICAL CONSTRAINT + stable id) is pinned in full.

---

<!-- section: layer-1-healing -->
## 4. Self-healing via previous valid context — SHIPPED

- Previous valid = in-memory last success, else newest valid checkpoint
  (`ingest_valid_context`). Source: `src/streamctx/healer.py:64-79`.
- Two consecutive corrupt checkpoints fall through to a third.
  Proven: `test_healer_falls_through_two_corrupt_checkpoints`.
- Intercept retries `fn()` after injecting recovery messages. `healed=True`
  only on retry success. Source: `src/streamctx/tracker.py:563-583`.
- Layer 3 `VerifiedRepairEngine` is post-hoc (shadow on the persisted failure
  row). Complementary: Layer 1 heals live; Layer 3 attributes/repairs later.
  Intercept `record_failure()` passes no ids; `record_call` schedules shadow.
  Sources: `src/streamctx/healer.py:34-62`, `src/streamctx/storage.py:240-250`.

---

<!-- section: layer-2-attribution -->
# StreamCtx Layer 2 — Attribution Engine (verified)

Citation format: `path:start-end` relative to this SDK checkout.
Audited 2026-09-17 against live `src/streamctx/attribution.py` on
`cursor/layer2-attribution-hardening` (Layer 1 parent `b3b17b7`, **not**
merged to `main` in this pass). Prior knowledge-doc Layer 2 claims were
not trusted; numbers and mechanisms below were re-derived.

**Status:** SHIPPED, MIT, free forever. No paid flag, license check, or
hosted-only gate in `attribution.py`. Close-out: `knowledge/HARDENING.md`.

Module: `src/streamctx/attribution.py`. Public surface: `AttributionEngine`,
`AttributionResult`, `get_attribution_engine()`, `is_non_content_failure()`,
weights `DRIFT_WEIGHT=0.5` / `COMPRESSION_WEIGHT=0.3` / `RECENCY_WEIGHT=0.2`
(`src/streamctx/attribution.py:41-43`).

`classify_failure()` does **not** live in `attribution.py`. It lives in
Layer 3 `src/streamctx/repair.py:117-137`. Layer 2 wraps it via
`is_non_content_failure()` (`attribution.py:269-283`) plus extra SDK /
load-test needles (`attribution.py:79-86`). That import is a layering
inversion (Layer 2 → Layer 3). Left in place this pass because Layer 3
was out of scope; not a paywall.

---

## Weights — what they actually compute

For each candidate call in `[fail_idx, fail_idx - lookback]` (default
lookback 5, `attribution.py:48,439-464`):

| Signal | Weight | What is measured | Written to `signal_breakdown`? |
| --- | --- | --- | --- |
| Drift | 0.5 | Relative token-shape change from **stored messages** (`_shape_tokens`, `attribution.py:137-152`), denominator floored at 50 tokens, plus waste flip only when **both** sides have a non-null `waste_category` (`attribution.py:155-172`) | yes, `drift` |
| Compression | 0.3 | Replay Layer 1 `compress_messages()` on the uncompressed request. 0 if it would not fire or drops no numbers/IDs (`attribution.py:195-220`) | yes, `compression` |
| Offset recency | 0.2 | `1 - offset/(lookback+1)` — **ranking prior only** (`attribution.py:259-266`) | **no** |
| Recency-as-why | 0.2 (confidence only) | Jaccard topic-shift, original task still buried, ≥2 user turns (`attribution.py:234-256`) | yes, `recency` |

Candidate **rank** = `0.5*drift + 0.3*compression + 0.2*offset_recency`
(`attribution.py:449-453`). Ties keep the closer candidate (`score > best_score`).

Reported **confidence** uses the why-signals, not the ranking prior:
`0.5*drift + 0.3*compression + 0.2*recency_why` (`attribution.py:472-484`).
Layer 3 still labels by raw-max of `drift` / `compression` / `recency`
(`repair.py:528-537`) — that contract is unchanged.

Deterministic given the same rows: pure functions, no RNG, strict `>`
tie-break. Proven: `tests/test_layer2_hardening.py::test_determinism_same_input_twice`
and 20-run simultaneous-cause test.

---

## Gates, in order (before any heuristic)

1. Missing `failed_call_id` → reason `"failed_call_id not found in session"`, confidence 0 (`attribution.py:408-419`).
2. Infra / SDK / load-test (`is_non_content_failure`) → `"infra/non-content"` (`attribution.py:423-426`). Runs **before** content scoring, so a timeout after a drifted prompt is infra, not DRIFT. Proven: `test_infra_timeout_after_real_drift_does_not_blame_drift`.
3. Out-of-taxonomy content (`prompt injection` / `jailbreak` / poisoned prompt) → `"unattributable"` (`attribution.py:91-96,427-430`). Does not force a three-bucket label. Proven: `test_prompt_injection_does_not_force_a_heuristic_bucket`.
4. Winning candidate's max(drift, compression, recency_why) `< CONTENT_SIGNAL_FLOOR` (0.15) → `"unattributable"` (`attribution.py:59-60,285-289,466-468`).

`CONTENT_SIGNAL_FLOOR = 0.15` is derived, not an epsilon: token estimator is
`len//4`; 21% shape change against a 50-token floor is `0.7 * 0.21 ≈ 0.15`.
Sub-20% jitter is tokenizer noise. Waste flip (0.3) clears the floor when
both sides are labeled. Compression requires actual numeric/ID loss, not
chatter summary. Recency-why uses the same floor so `"continue"`-level
overlap does not become a cause by itself. `CONTENT_SIGNAL_EPS` is an alias
of the floor (`attribution.py:61-62`).

Zero-signal proven: `test_zero_signal_abstains` and
`test_false_positive_gate_content_quiet_is_unattributable`.

---

## Layer 1 interaction

- Failures persist with `input_tokens=0`, `reused_tokens=0`, `waste_category=None`
  (`tracker.py:653-680`). Pre-fix, `40 → 0` looked like drift 0.7 and the
  recency floor stole the label. Now `_shape_tokens` reads `messages_json`.
  Proven: `test_tracker_zeroed_failure_tokens_are_not_drift`.
- `reused_tokens` is ContextDiffer prefix-reuse **plus** compression savings
  (`tracker.py:542-550,602`). It is **not** the compression signal.
  Under-budget prompts score compression 0. Proven:
  `test_under_budget_reused_tokens_is_not_compression`.
- Compression only fires when estimated tokens `> max_tokens` (default 2000)
  (`compressor.py:54-66`). Layer 2 replays that. Constraint-preserving pin of
  `ACME-9917` ⇒ no factual loss ⇒ not COMPRESSION. Proven:
  `test_constraint_preserving_id_is_not_compression` and
  `test_over_budget_fact_drop_is_compression`.

---

## Paywall / license

None in Layer 2. No `paid`, license check, or feature flag in
`src/streamctx/attribution.py`. Core detection stays MIT.

**DOC DISCREPANCY:** `docs/concepts/attribution.md` still says four signals
and shows `tracker.healing_stats()` as the example output. Code wins.

---

## Proof vs still-assumed

| Path | Proven? | How |
| --- | --- | --- |
| Same input twice → identical result | **Yes** | `test_determinism_same_input_twice` |
| Timeout after drifted context is infra, not DRIFT | **Yes** | `test_infra_timeout_after_real_drift_does_not_blame_drift` |
| Zero-signal abstains (`unattributable`, conf 0) | **Yes** | `test_zero_signal_abstains` |
| Under-budget `reused_tokens` is not COMPRESSION | **Yes** | `test_under_budget_reused_tokens_is_not_compression` |
| Over-budget fact drop (`$12.4`) is COMPRESSION | **Yes** | `test_over_budget_fact_drop_is_compression` |
| Constraint-preserving ID keep is not COMPRESSION | **Yes** | `test_constraint_preserving_id_is_not_compression` |
| Tracker `input_tokens=0` failure is not DRIFT | **Yes** | `test_tracker_zeroed_failure_tokens_are_not_drift` |
| Recency-why is topic shift, not offset=1.0 | **Yes** | `test_recency_why_is_topic_shift_not_offset_floor` |
| Prompt injection does not force a bucket | **Yes** | `test_prompt_injection_does_not_force_a_heuristic_bucket` |
| 50-worker concurrent attribution, no cross-session contamination | **Yes** | `test_concurrent_attribution_50_workers_no_contamination` |
| Shadow-run on `~/.streamctx/sessions.db` (2026-09-17) | **Yes** | 1,360 failed rows; 20 seeded excluded; **1,340** real. Known non-content **1,331** (642 recursion + 484 simulated + 161 infra_api + 44 sdk_signature) all `infra/non-content`, **0** misattributed. 9 prompt-injection rows `unattributable`. Max confidence 0.0. Do not reuse the old 1,264 figure. |
| Full suite after this pass | **Yes** | `190 passed, 1 skipped` (live OpenAI key) |
| Semantic drift with similar token counts and no waste flip | **Still assumed / weak** | Drift is still shape + waste, not embedding similarity. Same-length Lyon→Phoenix without a token jump can undershoot the floor. |
| Error messages that do not match infra / extra / taxonomy needles | **Still assumed** | Unlabeled content failures still go through the three-weight heuristic. |
| Layer 2 importing Layer 3 `classify_failure` | **Debt** | Layering inversion. Not moved this pass (Layer 3 out of scope). |
| Supabase `get_calls_for_session` attribution | **Not re-proven** | Tests use SQLite / fake storage. |
