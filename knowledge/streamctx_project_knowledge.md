---
knowledge_version: 2026-09-18.4
last_updated: 2026-09-18
pypi_version: 0.4.6
source_repo: streamctx/streamctx
source_commit: 4047f58
canonical_branch: main
reviewed: true
review_note: Regenerated from fully-merged main after Layers 1–4 merges, blank-reply + fact-review follow-ups, and the 2026-09-18 final polish (atomic intent, classify_failure move, README compression range, attribution reconcile, streamlit-cloud merge). Citations are against this tree. Canonical full-product copy for agents lives at streamctx-agents/knowledge/streamctx_project_knowledge.md.
---

# StreamCtx Layer 1 — Core SDK (verified)

Citation format: `path:start-end` relative to this SDK checkout.

**Status:** SHIPPED in `src/streamctx/` of PyPI 0.4.6 / `main` (HEAD `98c7f03`,
2026-09-18). Layers 1–4 hardening is merged. Sections below cite this tree.

Close-out: `knowledge/HARDENING.md` (Layer 1 — Core SDK hardening; Layer 2 —
Attribution Engine hardening; Layer 3 — Verified Auto-Repair hardening; Layer 4 —
Compliance Evidence hardening).

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
| Session-grounded facts | `src/streamctx/facts.py` | `find_reply_contradictions()` (review signal, not `failed`) |
| Token $ estimates | `src/streamctx/pricing.py` | `resolve_pricing()`, `estimate_cost()` |
| Terminal report | `src/streamctx/reporter.py` | `print_report()`, `print_auto_summary()` |
| Supabase backend | `src/streamctx/supabase_storage.py` | `SupabaseStorage` |

---

<!-- section: layer-1-streaming -->
## 1. Real-time step streaming to DB — SHIPPED

Means persist-each-call, **not** OpenAI `stream=True` / SSE.

- Success path: `_intercept_call` → `_persist_success` → `SessionStorage.persist_step`
  (call row + checkpoint, **one COMMIT**). Sources: `src/streamctx/tracker.py:683-815,882-922`,
  `src/streamctx/storage.py:288-361`.
- Per step: provider, model, tokens, cost, reused_tokens, waste_category,
  uncompressed `messages_json`, `message_fingerprint`, `response_text`,
  `failed`/`healed`/`error_message`. Source: `src/streamctx/storage.py:288-348`.
- Sync (not async). Write lock + WAL + `synchronous=NORMAL` + busy_timeout 30000
  + read pool 8. Source: `src/streamctx/storage.py:49-73`.
- Mid-write `kill -9`: no half-parsed JSON; atomic pair cannot commit one side.
  Proven: `tests/test_layer1_hardening.py::test_kill9_mid_write_leaves_consistent_rows`.
- Provider succeeded / DB failed: exception swallowed, response still returned.
  Source: `src/streamctx/tracker.py:919-922`. Proven: `test_persist_failure_does_not_drop_provider_response`.
- Blank-but-billed reply: provider HTTP 200, **reported** `output_tokens > 0`, no
  user-visible text, no tool/function call → `failed=True`, `error_message=None`,
  checkpoint **not** moved. `_response_text` reads `message.content`, then
  `refusal`, then Anthropic `type=text` blocks. It does **not** treat
  reasoning/thinking as visible text. Tool-call-only is success.
  Sources: `src/streamctx/tracker.py:261-365,749-775,841-880`.
  Proven: `tests/test_layer1_hardening.py` blank-billed / tool-call / refusal /
  Anthropic cases; live `scripts/live_blank_reply_proof.py` (3/3 organic blanks
  marked failed, 0 left as success).

After a non-blank success persist, Layer 2 may write a **review** row if the
reply contradicts a stable ID or `$` amount already in stored session history.
That does **not** set `failed=True` and does **not** start shadow repair.
Sources: `src/streamctx/facts.py:1-29,175-248`, `src/streamctx/tracker.py:799-801,884-903`,
`src/streamctx/attribution.py:508-598`. Proven: `tests/test_fact_contradiction.py`;
live paraphrase and legitimate-update sessions did not fire; a labeled injected
wrong ID did. This is not a general hallucination detector.

Monkey-patch layering (`_sdk_originals` / `_sdk_started`) and WAL still hold after
the Layer 2–4 merge. Sources: `src/streamctx/tracker.py:21-31,125-156`.
Tests: `tests/test_tracker_patch_stacking.py`. 50-worker write harness:
`tests/concurrent_load_test.py --workers 50 --calls-per-worker 50` PASS.

---

<!-- section: layer-1-checkpoint -->
## 2. Auto-checkpoint per step with exact-step resume — SHIPPED

- After each **successful** intercepted call, assistant reply is appended and
  the conversation is checkpointed atomically with the call row.
  Source: `src/streamctx/tracker.py:777-815,882-922`.
- Failures persist to `calls` only; they do **not** become the resume point.
  Source: `src/streamctx/tracker.py:725-735,754-775,841-880`.
- `resume(session_id)` restores `session_id`, `step_counter`, `_last_messages`,
  `active`, and healer context from the latest **valid** checkpoint.
  Source: `src/streamctx/tracker.py:538-564`.
- Corrupt walk: skip `valid=0`, invalid JSON, non-list payloads, newest-first.
  Source: `src/streamctx/storage.py:367-397`.
- Re-submitting the completed snapshot (resume output, assistant tail included)
  does not re-invoke the provider. Source: `src/streamctx/tracker.py:815-838`.
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
  request. Source: `src/streamctx/tracker.py:715-720`.
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
  only on retry success. Source: `src/streamctx/tracker.py:725-747`.
- Layer 3 `VerifiedRepairEngine` is post-hoc (shadow on the persisted failure
  row). Complementary: Layer 1 heals live; Layer 3 attributes/repairs later.
  Intercept `record_failure()` passes no ids; `record_call` / `persist_step`
  schedule shadow. Sources: `src/streamctx/healer.py:34-62`,
  `src/streamctx/storage.py:255-266,349-360`.

---

<!-- section: layer-2-attribution -->
# StreamCtx Layer 2 — Attribution Engine (verified)

Citation format: `path:start-end` relative to this SDK checkout.
Audited against live `src/streamctx/attribution.py` on merged `main`
(`98c7f03`). Layer 2 landed via merge `8c4d2ce` (`ee9cc9d`). Prior
knowledge-doc Layer 2 claims were not trusted; numbers and mechanisms
below were re-derived.

**Status:** SHIPPED, MIT, free forever. No paid flag, license check, or
hosted-only gate in `attribution.py`. Close-out: `knowledge/HARDENING.md`.

Module: `src/streamctx/attribution.py`. Public surface: `AttributionEngine`,
`AttributionResult`, `get_attribution_engine()`, `is_non_content_failure()`,
weights `DRIFT_WEIGHT=0.5` / `COMPRESSION_WEIGHT=0.3` / `RECENCY_WEIGHT=0.2`
(`src/streamctx/attribution.py:41-43`).

`classify_failure()` lives in `src/streamctx/failure.py` (below Layer 2 and
Layer 3). Layer 2 wraps it via `is_non_content_failure()`
(`attribution.py`) plus extra SDK / load-test needles. `repair.py`
re-exports the same function. Binary contract unchanged. Not a paywall.

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
(`repair.py:649-658`) — that contract is unchanged.

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
  on the **exception** path (`tracker.py:733-735,841-880` defaults). Blank-billed
  content failures keep the reported usage. Pre-fix, `40 → 0` looked like drift 0.7 and the
  recency floor stole the label. Now `_shape_tokens` reads `messages_json`.
  Proven: `test_tracker_zeroed_failure_tokens_are_not_drift`.
- `reused_tokens` is ContextDiffer prefix-reuse **plus** compression savings
  (`tracker.py:712-720,791`). It is **not** the compression signal.
  Under-budget prompts score compression 0. Proven:
  `test_under_budget_reused_tokens_is_not_compression`.
- Compression only fires when estimated tokens `> max_tokens` (default 2000)
  (`compressor.py:54-66`). Layer 2 replays that. Constraint-preserving pin of
  `ACME-9917` ⇒ no factual loss ⇒ not COMPRESSION. Proven:
  `test_constraint_preserving_id_is_not_compression` and
  `test_over_budget_fact_drop_is_compression`.

## Session-grounded fact review (success path)

`AttributionEngine.review_success_reply` (`attribution.py:508-598`) runs after
every non-empty successful persist. It is **not** a fourth failure-ranking
weight. Findings go to `shadow_attribution_log` with
`failure_kind=stable_fact_review` and `dominant_signal` `contradiction` or
`missing_context`. Ground truth is the latest non-question USER/SYSTEM
assertion per stable-ID family and `$` slot (`facts.py:126-158`). Assistant
text never becomes truth. A later user update wins. Questions (`?`) do not
update GT.

In scope: same-family ID swap; `$` swap that is not a ×1e3/1e6/1e9 paraphrase.
Out of scope on purpose: world-factual correctness, new ID families, bare
numbers, completeness. Compression-dropped facts are `missing_context`, not
`contradiction`. Opt-out `STREAMCTX_FACT_REVIEW=0`. Proven:
`tests/test_fact_contradiction.py`. Live: paraphrase `$12,400,000` and
reassignment `ACME-4401` did not fire; injected `ACME-1234` vs `ACME-9917` did,
`failed=False`.

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
| Full suite after this pass | **Yes** | Layer 2 merge: `190 passed, 1 skipped`. Merged `main` (`98c7f03`): **221 passed, 1 skipped**. |
| Semantic drift with similar token counts and no waste flip | **Still assumed / weak** | Drift is still shape + waste, not embedding similarity. Same-length Lyon→Phoenix without a token jump can undershoot the floor. |
| Success-path session-grounded ID/$ contradiction | **Yes (review, not failed)** | `tests/test_fact_contradiction.py`; live paraphrase/update clean; injected wrap fires. Not a world-knowledge hallucination detector. |
| Error messages that do not match infra / extra / taxonomy needles | **Still assumed** | Unlabeled content failures still go through the three-weight heuristic. |
| Layer 2 importing Layer 3 `classify_failure` | **Closed 2026-09-18** | Function lives in `src/streamctx/failure.py`. Attribution does not import repair. `repair.py` re-exports. Proven: `test_attribution_module_does_not_import_repair`, `test_classify_failure_shared_object`. |
| Supabase `get_calls_for_session` attribution | **Not re-proven** | Tests use SQLite / fake storage. |

---

<!-- section: layer-3-repair -->
# StreamCtx Layer 3 — Verified Auto-Repair (verified)

Citation format: `path:start-end` relative to this SDK checkout.
Audited against live `src/streamctx/repair.py`, `shadow.py`, and
`storage.py` on merged `main` (`98c7f03`). Layer 3 landed via merge
`dc9bf02` (`e432cb7`). Prior knowledge-doc Layer 3 claims were not
trusted; mechanisms below were re-derived from the code.

**Status:** SHIPPED, MIT, free forever. No paid flag, license check, or
hosted-only gate in `repair.py` / `shadow.py`. Close-out: `knowledge/HARDENING.md`.

**`classify_failure()` contract:** unchanged. Still a binary
`infra_error` / `content_error` function at `repair.py:130-151`. No third
abstention token. Layer 2's `is_non_content_failure()` wrapper
(`attribution.py:269-283`) is therefore still valid. Do not "fix" simulated
failure / recursion / SDK-signature strings into `infra_error` without a
coordinated Layer 2 change.

Module: `src/streamctx/repair.py`. Public surface: `VerifiedRepairEngine`,
`RepairResult`, `classify_failure()`, `is_unfixable_content_failure()`,
`get_repair_engine()`, `verify_fix()` on the package. Shadow path:
`src/streamctx/shadow.py`. Log table: `storage.py:128-142,506-632`.

---

## What `verify_fix()` actually does

End-to-end (`repair.py:308-488`):

1. Load the failed call. `classify_failure(error_message)` — if `infra_error`,
   return unresolved, empty candidate, `applied=False` (`repair.py:347-355`).
2. `AttributionEngine.attribute_failure()` (Layer 2). No root cause → unresolved,
   `needs_human_review=True`. Layer 2 abstention (`infra/non-content`,
   `unattributable`) stops the repair here.
3. Generate a signal-based candidate (`repair.py:494-520`):
   - **compression** → facts Layer 1 `compress_messages()` would drop from the
     *attributed* uncompressed request, clipped around those facts
     (`repair.py:543-591,1004-1031`). Not the earliest call.
   - **drift** → re-anchor to earliest task framing.
   - **recency** → re-surface earliest assigned task.
   - unknown signal → empty candidate (no silent compression fallback).
4. Counterfactual replay via `CounterfactualReplayer` (does **not** write
   checkpoints or call rows).
5. Live verification (`dry_run=False`) is **independent of the attribution
   signal** (`repair.py:812-840`): `correct_value` must appear in the assistant
   reply, must already exist in stored session messages, and the reply must
   not be a copy of the `[STREAMCTX REPAIR …]` injection. Invented strings
   that the LLM echoes do **not** resolve.
6. Dry-run (default, and the only shadow path) never sets `resolved=True`.

`applied` is always `False` from `verify_fix()`. There is no auto-apply API.
A failed / timed-out replay leaves the original session untouched
(`repair.py:1041-1065`, daemon-thread timeout `REPAIR_LLM_TIMEOUT_S=30`).
Proof stores a pre-repair checkpoint fingerprint (`repair.py:842-850`).

---

## False-positive gate vs circular verification

Pre-fix, `resolved=True` meant only "the caller-supplied `correct_value`
appeared in the replay." An invented code (`ZEBRA-NOT-IN-SESSION-9917`) that
the stub LLM echoed was accepted. That is circular: the check did not use
session evidence.

Post-fix independent checks, in order (`repair.py:812-840`):

1. `correct_value` present in replay **reply** (not the injected system note).
2. Every required value already present in stored `messages_json` for the session.
3. Reply does not contain `[streamctx repair` and is not an 85%+ word overlap
   with the injection (length ≥ 80).

Absence of the old `error_message` is still **not** treated as success.

---

## Shadow log and opt-out

`maybe_schedule_shadow_repair` (`shadow.py:53-83`) runs only when:

- `STREAMCTX_SHADOW_REPAIR` is not `0`/`false`/`no`/`off` (default on)
- `should_shadow_repair`: `classify_failure == content_error` **and**
  `error_message` empty/None (`shadow.py:44-50`)

So tracker exception failures (`_persist_failure` writes the exception text)
do **not** shadow. Organic content-quality rows with empty `error_message`
do. `STREAMCTX_SHADOW_REPAIR_SYNC=1` runs in-process (tests).

Cap (`shadow.py:31`, `storage.py:549-631`): one row per `(session_id,
failed_call_id)`; at most `DEFAULT_LOOKBACK` (5) verify_fix runs per session;
further failures skip. Justified: more than one Layer-2 lookback window of
unresolved auto-repairs is the same cause looping.

Log columns after migration (`storage.py:186-191,128-142`): `resolved`,
`dry_run`, `applied`, `needs_human_review`, `attempt_count`. Shadow is always
`dry_run=True`, `applied=False`. `scripts/review_shadow_log.py` reads
`$STREAMCTX_HOME/sessions.db` (default `~/.streamctx/sessions.db`). Proven
2026-09-17: table exists, **0 rows** — consistent with zero organic
content-quality failures in that DB.

---

## Layer 1 interaction

- Failures persist to `calls` only (`tracker.py:841-880`); they do not move
  the resume checkpoint. `_step_for_call` (`repair.py:660-689`) pairs
  successes to checkpoints in order; a failure replays from the last success.
  1:1 zip only when `len(checkpoints)==len(calls)` (seeded content-quality
  rows that were checkpointed).
- Compression repair replays Layer 1 `compress_messages()` on the attributed
  uncompressed request, then injects windows around dropped dollar/decimal/
  4+ digit / stable-ID facts. Earliest-call Lyon is not re-injected when the
  later window has Phoenix / `$12.4`.
- `persist_step` is not used to apply a repair. Resume after `verify_fix` is
  the last valid success checkpoint, unchanged. Proven:
  `test_live_restore_of_session_fact_is_resolved`,
  `test_middle_failure_replays_from_last_success_checkpoint`.

---

## Paywall / license

None in Layer 3. No `license_key`, `requires_pro`, `STREAMCTX_PAID`, or
`if paid` in `src/streamctx/repair.py`. Proven:
`tests/test_layer3_hardening.py::test_no_paid_gate_in_repair_source`.

---

## Proof vs still-assumed

| Path | Proven? | How |
| --- | --- | --- |
| Invented `correct_value` does not resolve | **Yes** | `test_invented_correct_value_is_not_verified` |
| Compression candidate contains dropped `$12.4`, not earliest framing | **Yes** | `test_compression_reinjects_dropped_fact_not_earliest` |
| Stale Lyon is not re-injected when later window has Phoenix/$12.4 | **Yes** | `test_stale_earliest_city_is_not_re_injected` |
| Injection echo is not verified | **Yes** | `test_injection_echo_is_not_verified` |
| Session-grounded restore resolves, `applied=False`, checkpoints unchanged | **Yes** | `test_live_restore_of_session_fact_is_resolved` |
| 12 content failures do not retry indefinitely (cap=5) | **Yes** | `test_repair_loop_gives_up_after_lookback_window` |
| LLM hang returns in ~1s, session untouched | **Yes** | `test_llm_timeout_fail_safe` |
| Failure between successes replays from last success step | **Yes** | `test_middle_failure_replays_from_last_success_checkpoint` |
| `classify_failure` binary contract unchanged vs Layer 2 | **Yes** | `test_classify_failure_contract_unchanged_for_layer2` |
| `STREAMCTX_SHADOW_REPAIR=0` writes nothing | **Yes** | `test_shadow_opt_out_env` + `tests/test_shadow_repair.py::test_shadow_disabled_env` |
| Injected content-quality E2E (attr → shadow log → live verify) | **Yes** | `test_injected_content_quality_e2e_pipeline` |
| 50-worker shadow log, no cross-session contamination | **Yes** | `test_concurrent_shadow_log_50_workers_no_contamination` |
| `review_shadow_log.py` against `~/.streamctx/sessions.db` | **Yes** | 2026-09-17: db exists, `shadow_repair_log` empty |
| Shadow-run `verify_fix(dry_run=True)` on real `sessions.db` | **Yes** | 1,360 failed; 20 seeded excluded; **1,340** real. `classify_failure`: **161** `infra_error`, **1,179** `content_error`. All 1,179 dry-run ok, 0 errors, conf 0.0, signal none. Attribution then abstains: recursion 642 + simulated 484 + sdk_signature 44 = 1,170 `infra/non-content`; 9 prompt-injection `unattributable`. **Zero organic content-quality repairs.** Do not reuse 1,264. |
| Full suite after this pass | **Yes** | Layer 3 merge: **206 passed, 1 skipped**. Merged `main` (`98c7f03`): **221 passed, 1 skipped**. |
| Auto-apply of a verified candidate into the live session | **Not shipped** | Explicitly `applied=False`. Safer than applying; Layer 4 will audit whatever was *not* applied. |
| Tracker intercept flagging a wrong-text hallucination as `failed=True` | **Not shipped, on purpose** | Blank-but-billed is `failed=True`. A non-empty reply that contradicts a stored stable ID or `$` amount is a Layer 2 **review** (`failure_kind=stable_fact_review`), still `failed=False`. World-factual correctness is out of scope. |
| Blank-but-billed (`content` empty, reported output tokens > 0, no tool call) | **Yes** | Intercept `_is_blank_billed_reply`. Live proof: 3 organic blanks → failed, shadow, ledger. |
| Semantic "the reply used the restored fact correctly" beyond substring match | **Still assumed / weak** | Independent gate is evidence + echo, not an NLI check. |
| Supabase shadow_repair_log | **Not shipped** | SQLite-only. |
| `deploy/streamlit-cloud` | **Merged 2026-09-18** | `4047f58`. Cloud config only. |
| Merge of Layers 1–4 into `main` | **Done 2026-09-18** | Real `--no-ff` merges `83b474f` / `8c4d2ce` / `dc9bf02` / `98c7f03`. |

---

<!-- section: layer-4-compliance-evidence -->
# Layer 4 — Compliance Evidence (SHIPPED, free forever)

Merged to `main` 2026-09-18 (`98c7f03`, parent hardening commit `7443e5f`).

MIT-licensed. `evidence.py` / `scripts/verify_attestation.py` have no license
check, paid flag, or hosted-only branch. Source: `src/streamctx/evidence.py:23-24`.
Proven: `tests/test_layer4_hardening.py::test_no_paid_gate_in_evidence_source`.

Separate SQLite file `evidence_ledger.db`, never `sessions.db`.
Source: `src/streamctx/evidence.py:1-5,189-192`.

Layer 2/3 write through `safe_append_evidence` and never fail the caller.
Sources: `src/streamctx/attribution.py:353-363`, `src/streamctx/repair.py:292-301`,
`src/streamctx/evidence.py:1201-1216`.

## Signing coverage (schema 1.1 / hash_version 2)

Every ledger row is Ed25519-signed. The signature is over
`bytes.fromhex(entry_hash)`. v2 `entry_hash` is SHA-256 of canonical JSON of
`SIGNED_ENTRY_KEYS`: `entry_id`, `record_type`, `record_ref_id`,
`record_payload_hash`, `prev_hash` (global), `session_prev_hash`, `timestamp`,
`hash_version`, `key_id`, `repair_disposition`, `applied`, `resolved`, `dry_run`.
Sources: `src/streamctx/evidence.py:61-99,226-241,298-301,732-748`.
Schema 1.0 concatenated hashes remain verifiable as `hash_version=1`.
Source: `src/streamctx/evidence.py:210-223`.

`prev_hash` **is** in the signed preimage. Reordering or dropping a committed
row breaks the successor. A write that was never attempted is **not** visible
to the hash chain; use `reconcile_shadow_log()` against `shadow_repair_log`
for Layer 3 omissions and `reconcile_attribution_log()` against
`shadow_attribution_log` for computed Layer 2 rows. An attribution that
was never computed is still invisible.
Source: `src/streamctx/evidence.py` (`reconcile_shadow_log`, `reconcile_attribution_log`).

## Applied vs verified (was a FAIL; now first-class signed fields)

Layer 3 `verify_fix()` is counterfactual: `applied` is always `False` unless a
caller applies a candidate out of band. Source: `src/streamctx/repair.py:245-247,264`.

Pre-fix schema 1.0 exported only hashes. An auditor reading `record_type=repair`
could not see `applied=false`. The payload inside `evidence_payloads` did
contain `applied`, but (1) it was not exported, and (2) `evidence_payloads` had
no append-only trigger, so flipping `applied` to `true` left `verify_chain()`
valid.

Fix: `disposition_from_payload()` maps a repair payload onto signed columns and
**never treats `resolved` as `applied`**. Shadow success is
`repair_disposition=verified_not_applied`. Sources:
`src/streamctx/evidence.py:17-21,69-80,336-359`.
`export_attestation()` emits those fields plus a verifier-recomputed
`repair_summary` and a `layer3_contract` note. Sources:
`src/streamctx/evidence.py:1016-1115`.
Payloads are append-only. Sources: `src/streamctx/evidence.py:103-106,163-173`.
`verify_chain()` hashes `payload_json` against `record_payload_hash`.
Source: `src/streamctx/evidence.py:904-915`.

Proven: `test_shadow_verified_not_applied_is_unambiguous` (real
`VerifiedRepairEngine.verify_fix(dry_run=False)` → bundle
`repair_disposition=verified_not_applied`, `applied=false`,
`repair_summary.applied_count=0`; offline script prints
"no bundled repair was applied"). `test_payload_rewrite_is_detected`.

## `verify_chain()` precision

Walks the **full** global chain. `record_ref_id` only fills `matched_ref`; it
does not skip predecessors. Source: `src/streamctx/evidence.py:846-853`.

| Tamper | `broken_at_entry_id` | `reason` |
| --- | --- | --- |
| Modify `record_payload_hash` of entry 3 | 3 | `payload_hash_mismatch` or `hash_mismatch` |
| Delete entry 3 | 3 | `entry_id_gap` (`found_entry_id=4`, `gap_after_entry_id=2`) |
| Swap fields of entries 2 and 3 | 2 or 3 | hash / prev mismatch |
| Splice foreign `entry_id=99` | 4 | `entry_id_gap` (`found_entry_id=99`) |

Proven: `tests/test_layer4_hardening.py` tamper cases.

Uncommitted kill-9: `*.intent` fsync-before-COMMIT. On open, a leftover intent
for a missing `entry_id` → `valid=False`, `reason=uncommitted_intent`.
Sources: `src/streamctx/evidence.py:583-591,600-627,750-756,857-863`.
Committed pair is one SQLite transaction (`BEGIN IMMEDIATE`, WAL,
`synchronous=FULL`). Sources: `src/streamctx/evidence.py:430-441,700-701`.

## Session export vs global chain

Pre-fix 1.0 offline verifier linked `prev_hash` to the previous **bundled**
row. Interleaved sessions (A, B, A) made an honest session-A export FAIL.

Fix: signed `session_prev_hash` plus 1.1 verifier session-chain check.
Sources: `src/streamctx/evidence.py:715-729`,
`scripts/verify_attestation.py:524-554,604`.
Proven: `test_interleaved_session_export_verifies`.

## Third-party verify

`scripts/verify_attestation.py` imports stdlib + `cryptography` only (no
`streamctx`). Proven: `tests/test_evidence.py::test_verify_script_has_zero_streamctx_imports`.

Embedded `public_key_pem` is enough for **integrity**, not authenticity.
Anyone can mint a keypair and a self-consistent bundle. `--public-key` pins
the issuer; `--require-pin` refuses the unpinned path. Sources:
`scripts/verify_attestation.py:250-327,703-706,744-756`.
`signing_keys` is append-only; each row stores `key_id` so rotation keeps
historic signatures verifiable. Sources: `src/streamctx/evidence.py:145-149,174-185,629-633`.
Proven: `test_foreign_keypair_bundle_rejected_when_pinned`,
`test_key_rotation_keeps_historic_entries_verifiable`.

Auditor doc: `docs/COMPLIANCE_VERIFICATION.md`.

## Concurrency

Pre-fix: 50 separate `EvidenceLedger` objects on one DB → 38
`UNIQUE constraint failed: evidence_ledger.entry_id`, 12 of 50 rows.
Fix: `BEGIN IMMEDIATE` under the write lock, retry on busy/unique.
Source: `src/streamctx/evidence.py:695-826`.
Proven: `test_concurrent_append_50_separate_ledger_objects` (50 unique ids,
chain valid, ledger rows == payload rows).

## Proof vs still-assumed

| Path | Proven? | How |
| --- | --- | --- |
| Four tamper classes, precise `broken_at_entry_id` | **Yes** | `test_tamper_*` |
| Payload rewrite `applied=true` is detected | **Yes** | `test_payload_rewrite_is_detected` |
| Interleaved session export verifies offline | **Yes** | `test_interleaved_session_export_verifies` |
| Layer 3 shadow verify → `verified_not_applied` in the bundle | **Yes** | `test_shadow_verified_not_applied_is_unambiguous` |
| Foreign keypair rejected when issuer key is pinned | **Yes** | `test_foreign_keypair_bundle_rejected_when_pinned` |
| Key rotation, historic rows still verify | **Yes** | `test_key_rotation_keeps_historic_entries_verifiable` |
| 50-worker concurrent append, separate ledger objects | **Yes** | `test_concurrent_append_50_separate_ledger_objects` |
| Kill-9: `integrity_check=ok`, equal ledger/payload counts, intent or valid chain | **Yes** | `test_kill9_mid_write_fail_safe` |
| Shadow log without evidence row is reconcilable | **Yes** | `test_silent_omission_is_detectable_against_shadow_log` |
| Full suite after this pass | **Yes** | Merged `main` (`98c7f03`): `python -m pytest tests/ -v --tb=short` → **221 passed, 1 skipped** |
| Never-computed Layer 2 attribution | **Still assumed / weak** | Independent log now exists for attributions that *were* computed (`reconcile_attribution_log`). Events never presented to the engine remain invisible. `safe_append_evidence` still no-ops when `STREAMCTX_EVIDENCE_PRIVATE_KEY` is unset. |
| Power-loss (not process kill) with `synchronous=FULL` | **Still assumed** | SQLite FULL+WAL survives `kill -9`; a hard power cut can still lose the last COMMIT. Intent file is best-effort. |
| `deploy/streamlit-cloud` | **Merged 2026-09-18** | `4047f58`. Cloud config only. |
| Merge of Layers 1–4 into `main` | **Done 2026-09-18** | Real `--no-ff` merges ending at `98c7f03`. |
| Live 15-turn organic session + injected four-layer event | **Yes** | `scripts/live_full_pipeline_proof.py` on `98c7f03`. Organic: 15/15 success, empty attestation verifies. Injected: compression → shadow dry-run + live `verified_not_applied`, `applied_count=0`, pinned verify exit 0. |
| Blank `message.content` with `output_tokens>0` | **Closed 2026-09-18** | Intercept marks `failed=True` with empty `error_message`. Tool-call-only and refusals stay success. Empty + *zero reported* tokens is **not** this failure. Live: 3/3 organic blanks failed, 0 left as success. |
| Session-grounded fact review on successful replies | **Shipped 2026-09-18** | Same-family stable IDs and `$` amounts vs latest non-question user/system assertion. Review log, not `failed=True`. Live paraphrase `$12,400,000` and update `ACME-4401` did not fire. Injected `ACME-1234` vs `ACME-9917` did. Explicitly **not** a general hallucination detector. |
