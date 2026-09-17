---
knowledge_version: 2026-09-17.3
last_updated: 2026-09-17
pypi_version: 0.4.6
source_repo: streamctx/streamctx
source_commit: b4cad0d
canonical_branch: main
reviewed: true
review_note: Layer 1 section regenerated from the hardened working tree on main (parent b4cad0d). Canonical full-product copy for agents lives at streamctx-agents/knowledge/streamctx_project_knowledge.md. This file is the SDK-repo Layer 1 ground truth with path:line citations.
---

# StreamCtx Layer 1 — Core SDK (verified)

Citation format: `path:start-end` relative to this SDK checkout.

**Status:** SHIPPED in `src/streamctx/` of PyPI 0.4.6 / `main`. Hardened 2026-09-17
(senior-bar + structural fixes). Layers 2–4 are also shipped in this tree but
are **not** re-audited here.

Close-out: `knowledge/HARDENING.md` (Layer 1 — Core SDK hardening).

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
