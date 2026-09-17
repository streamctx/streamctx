# Canonical branch and merge note (2026-09-17)

Date: 2026-09-17
SDK HEAD: `b25bab1` on `main`

## Canonical branch (policy going forward)

**`main` is the only canonical/production branch** for `github.com/streamctx/streamctx`.
`origin/HEAD` points at `origin/main`. CI in this repo already triggers on `main`.
Agent/tool SDK checkouts (`STREAMCTX_PRODUCT_ROOT`, GitHub Actions
`actions/checkout` of `streamctx/streamctx` with no `ref`) must follow that
default. Do **not** treat these as production:

- `release/v0.4.6` — feature branch; merged into `main` in `b25bab1`
- `deploy/streamlit-cloud` — deploy branch; HEAD `76ee241` is **not** on `main`
- `cursor/stage1-sandbox-runner` — that name is the *agents* repo working
  branch, not an SDK checkout ref. No SDK CI/config in this repo pointed at it.

No hardcoded `release/v0.4.6` or `cursor/stage1-sandbox-runner` SDK refs were
found in streamctx CI (`python-app.yml`, `multi-platform-tests.yml` both use
`branches: [main]`).

## What was merged

Real merge (not squash) of `release/v0.4.6` (`f5611ec`) into `main`
(`3175918`):

```
b25bab1 Merge branch 'release/v0.4.6' into main
parents: 3175918 (origin/main) + f5611ec (release/v0.4.6)
```

Ahead of `main` on the release side (Layers 2/3/4 and related):

| Commit | What |
| --- | --- |
| `5c982ec` | Version bump 0.4.6 (superseded on `main` by PR `#7` `3175918`) |
| `bf66d49` | `psutil`/`httpx` dev extras (same intent as `#7`) |
| `8de5998` | Layer 3: stacked SDK patches + silent shadow-repair |
| `44169fa` | Layer 2: stop attributing infra/zero-signal to recency |
| `10ba64f` | Layer 4: append-only evidence ledger |
| `c46e525` | Layer 4: Ed25519 + attestation schema 1.0 |
| `f5611ec` | Layer 4: dashboard session-scoped compliance export |

On `main` and **not** on `release/v0.4.6` before the merge: `3175918`
(PR `#7` — the GitHub squash of the 0.4.6 bump + dev extras). No unique
logic; release already contained the same version string and extras.

**Not in this merge:** `deploy/streamlit-cloud` commit `76ee241`
("Make the Streamlit demo Cloud-deployable…"). That remains only on the
deploy branch. The earlier knowledge audit scanned `76ee241` and therefore
missed Layers 2/3/4.

## Conflicts

Only `setup.py` conflicted. Both sides had already bumped to `0.4.6`.

| Side | `extras_require["dev"]` |
| --- | --- |
| `main` (`3175918`) | `["psutil", "httpx"]` |
| `release/v0.4.6` | `["psutil", "httpx", "cryptography>=41.0.0"]` |

Resolution (obvious, not a logic judgment): keep cryptography. Layer 4
already added `cryptography>=41.0.0` to `install_requires` from the release
side. No other file needed a human call. `__init__.py` auto-merged
(`verify_fix` from release + version `0.4.6` from both).

## Post-merge tests

Full suite, not a subset:

```
python -m pytest tests/ -v --tb=short
165 passed, 1 skipped in 22.03s
```

Skipped: `tests/test_openai_integration.py::TestOpenAILive::test_real_openai_call`
(needs a live key). Includes `tests/test_attribution.py`, `tests/test_repair.py`,
`tests/test_evidence.py`, `tests/test_shadow_repair.py`,
`tests/test_tracker_patch_stacking.py`, and everything that was already on `main`.

## Knowledge doc

Regenerated from this merged tree (not a three-line status flip):
`streamctx-agents/knowledge/streamctx_project_knowledge.md`
`knowledge_version: 2026-09-17.2`, `source_commit: b25bab1`.

---

# Layer 1 — Core SDK hardening

Date: 2026-09-17
SDK parent: `b4cad0d` on `main` (Layer 1 fixes are uncommitted on top of this)

## Senior-bar results against pre-fix `main`

Judged against live code, not docs. Same bar as the agent hardening chats
(do not round "weak" up to "works"):

| Differentiator | Pre-fix verdict | What actually happened |
| --- | --- | --- |
| Streaming / kill-9 mid-write | **WEAK** | `PRAGMA integrity_check=ok`, zero half-parsed JSON rows. Call+checkpoint were **two COMMITs**. Kill left `calls=21, checkpoints=20`. |
| Checkpoint / resume + side effect | **FAIL** | `resume()` returned request-only messages, `step_counter=0`, `session_id=None`. Re-`create()` re-ran the file write (2 writes). |
| Compression / buried constraint | **FAIL** | `_compress_middle` kept first **15 chars**. `ACME-9917` / `staging-db-07` dropped. Intercept counted `compression_savings` into `reused_tokens` but **did not mutate** outbound `kwargs["messages"]`. |
| Self-heal / two corrupt checkpoints | **FAIL** | Latest corrupt JSON → `JSONDecodeError` on resume. Healer is in-memory only (`can_heal=False` after "restart"). Intercept set recovery msgs then **re-raised**; `attempt_heal` never called. |

Monkey-patch layering (Issue #5/#6) and WAL + write-lock + read-pool **still hold**. Layer 2–4 merge added shadow tables and `record_call` → `maybe_schedule_shadow_repair`; it did not unwind `_sdk_originals` / `_sdk_started` or WAL. Tests: `tests/test_tracker_patch_stacking.py`. 50-worker write harness: `tests/concurrent_load_test.py --workers 50 --calls-per-worker 50` → PASS, 1.76s, 0 lock errors.

## Root-cause fixes (not one-test special cases)

1. **Atomic step persist.** `SessionStorage.persist_step()` writes the `calls` row and matching `checkpoints` row in one COMMIT (`storage.py:273-346`). Tracker success path uses it (`tracker.py:682-722`). Kill-9 during a step can no longer commit one side of the pair. Failures persist to `calls` only — they do **not** move the resume checkpoint (`tracker.py:653-680,563-573`).
2. **Exact-step resume.** Checkpoints store the post-response conversation (assistant reply appended, `tracker.py:585-611`). `resume()` restores `session_id`, `step_counter`, `_last_messages`, `active`, and healer context (`tracker.py:374-399`). Re-submitting that completed snapshot short-circuits `create()` without invoking the provider (`_lookup_completed_step`, `tracker.py:627-650`). A second **live** call with the same *request* (no assistant tail) is still a new step.
3. **Constraint-preserving compression.** Replaced 15-char squash with extractive middle summary + full keep of constraint/policy/stable-id messages (`compressor.py:40-86`). Intercept **applies** compressed messages to outbound `kwargs` when savings > 0; DB still stores the uncompressed request (`tracker.py:544-556`).
4. **Healing actually retries.** On provider exception: persist failure (no checkpoint), `ingest_valid_context` walks checkpoints newest-first skipping corrupt/invalid JSON (`healer.py:64-79`, `storage.py:352-377`), mutate `kwargs["messages"]` to recovery context, **retry `fn()`**. `healed=True` only if retry succeeds (`tracker.py:563-583`). Layer 3 `VerifiedRepairEngine` stays post-hoc (shadow repair on the persisted failure row). Complementary, not competing.

Corrupt-checkpoint walk: `valid=0`, invalid JSON, and non-list payloads are skipped so two consecutive bad rows fall through to a third.

## Proof vs still-assumed

| Path | Proven? | How |
| --- | --- | --- |
| Kill-9 leaves `integrity_check=ok`, parseable JSON, `len(calls)==len(checkpoints)` | **Yes** | `tests/test_layer1_hardening.py::test_kill9_mid_write_leaves_consistent_rows` |
| Resume does not re-run a `create()`-attached file write | **Yes** | `test_resume_does_not_rerun_file_write_side_effect` |
| Identical live prompts still count as two calls | **Yes** | `test_identical_live_prompts_are_two_calls_not_deduped` + `test_wrap_without_double_counting` |
| Buried `ACME-9917` / `staging-db-07` survive compression | **Yes** | `test_compression_preserves_buried_constraint` |
| Intercept sends compressed outbound messages | **Yes** | `test_intercept_sends_compressed_messages_and_keeps_constraint` |
| Compression % on >1 session shape | **Yes (measured)** | chatter 79%, tool-heavy 80%, buried-constraint 88% (`max_tokens=800`, `keep_last_n=4`, char/4 heuristic). Marketing "40-70%" is a lower band, not a ceiling. README still says 30-60%. |
| Two corrupt checkpoints fall through to a third | **Yes** | `test_healer_falls_through_two_corrupt_checkpoints` |
| Intercept retries after transient failure | **Yes** | `test_intercept_retries_with_previous_valid_context` |
| Failed call does not become the resume point | **Yes** | `test_failed_call_does_not_move_resume_checkpoint` |
| Persist error does not drop a successful provider response | **Yes** | `test_persist_failure_does_not_drop_provider_response` |
| 50-worker persist_step + existing write harness | **Yes** | `test_concurrent_persist_step_50_workers` (1000 atomic pairs); `concurrent_load_test.py --workers 50 --calls-per-worker 50` |
| Patch stacking after Layer 2-4 merge | **Yes** | `tests/test_tracker_patch_stacking.py` (50 start/stop workers then create) |
| Side effects in **caller** code after `create()` returns, before the next intercepted call | **Still assumed / out of SDK contract** | SDK cannot skip a file write that happens outside wrap/start. Callers must treat tools as idempotent or checkpoint after the tool result is in the message list. |
| Power-loss (not process kill) with `synchronous=NORMAL` | **Still assumed** | SQLite WAL+NORMAL survives `kill -9`; a hard power cut can lose the last COMMIT. Not tested. |
| Supabase `persist_step` / valid-checkpoint walk | **Not shipped** | SQLite-only. `SupabaseStorage.record_call` still ignores `failed`/`healed`. |

Full suite after the fix:

```
python -m pytest tests/ --tb=line -q
178 passed, 1 skipped in 22.13s
```

Skipped: live OpenAI key test. Includes the new `tests/test_layer1_hardening.py` (13 cases).

## Explicitly not done yet (as of Layer 1 close-out)

- Layers 2-4 re-hardening / cross-layer wiring prompt
- `deploy/streamlit-cloud` merge
- Changing README / `__init__.py` compression percentage copy (knowledge doc records the measured numbers; code wins on algorithm)
- Token-level SSE streaming (never the Layer 1 meaning of "real-time streaming")

---

# Layer 2 — Attribution Engine hardening

Date: 2026-09-17
SDK parent: `b3b17b7` on `cursor/layer1-core-sdk-hardening` (not merged to `main` in this pass). Layer 2 fixes are on `cursor/layer2-attribution-hardening`.

No part of Layer 2 is gated behind a paid tier. `attribution.py` has no license check, paid flag, or hosted-only branch. Core detection stays MIT.

## Senior-bar results against pre-fix code

Judged against live `src/streamctx/attribution.py` before the structural change. Probe: `scripts/layer2_senior_bar_pre.py`. Same bar as Layer 1 (do not round "weak" up to "works"):

| Case | Pre-fix verdict | What actually happened |
| --- | --- | --- |
| Determinism (same failure twice) | **PASS** | Identical confidence, breakdown, reason. Pure functions, `score > best_score` tie-break. |
| Infra timeout after real drift | **PASS** | `is_non_content_failure` already ran first → `infra/non-content`, conf 0. Not a described-but-unshipped fix. |
| Simultaneous compression+recency | **PASS** | 20 reruns, one outcome. Label was raw-max (compression 1.0 tied recency 1.0, insertion order). |
| Zero-signal abstention | **PASS** | `unattributable`, conf 0. Gate existed (`CONTENT_SIGNAL_EPS = 1e-6`) but only blocked *exact* zeros. |
| Adversarial confidence gaming | **FAIL** | Under-budget prompt, `reused_tokens=180/200`. Layer 1 compression did **not** fire (`orig=2`). Attributed `recency` conf 0.47 because offset-0 recency is always 1.0 and compression 0.9 lost the raw-max label. Layer 3 would have recency-repaired a fake signal. |
| Tracker `persist_failure` zero tokens | **PARTIAL** | Real intercept path: success `input_tokens=40`, failure `0/0/waste=None`. Drift scored 0.7 (40→0), recency stole the label, conf 0.55. Not crash-free-wrong: it attributed a same-size follow-up. |
| Compression did not fire | **FAIL** | 7-token prompt, `reused_tokens==input_tokens` (ContextDiffer overlap). Dominant `compression` conf 0.5. |
| `classify_failure` location | **PASS** | Lives in `repair.py`, not `attribution.py`. Abstention **does** exist in shipped `attribution.py` (`UNATTRIBUTABLE_REASON`, `INFRA_NON_CONTENT_REASON`). Prior audits conflicted because they scanned different commits. |
| 50-worker concurrent attribution | **PASS** | Shared `AttributionEngine`, 0 errors, 0 serial/concurrent mismatches. No mutable scoring state in `attribution.py`. |

`classify_failure()` in Layer 3 already treated timeouts/401/429 as `infra_error`. The remaining holes were (1) using `reused_tokens` as compression, (2) treating persist-zeroed usage as drift, (3) writing offset recency into the why-label Layer 3 reads, (4) an epsilon gate instead of a derived floor, (5) forcing a three-bucket label on prompt-injection rows that also happened to be over budget.

## Root-cause fixes (not one-test special cases)

1. **Token shape from messages.** `_shape_tokens` prefers `compress_messages`'s `len//4` estimate on stored messages. Layer 1 `_persist_failure` zeros (`tracker.py:653-680`) are missing usage, not 100% drift. Waste flip requires both sides labeled (`attribution.py:137-172`).
2. **Compression is Layer 1 replay.** `_compression_score` calls `compress_messages()` on the uncompressed request. Score 0 unless it would fire **and** numbers/stable IDs are actually dropped. `reused_tokens` is not a compression proxy (`attribution.py:195-220`).
3. **Recency-as-why ≠ offset prior.** Ranking still uses offset recency (0.2). `signal_breakdown["recency"]` is Jaccard topic-shift with the original task still buried and ≥2 user turns (`attribution.py:234-266,459-462`). Confidence uses why-signals only (`attribution.py:472-484`), so the 0.2 offset floor cannot mint medium confidence by itself.
4. **Derived abstention floor.** `CONTENT_SIGNAL_FLOOR = 0.15` (`attribution.py:50-62,285-289`). Replaces `1e-6`. Infra gate still runs first. Out-of-taxonomy needles (`prompt injection`, `jailbreak`, poisoned prompt) abstain as `unattributable` rather than force a bucket (`attribution.py:91-96,427-430`).

## Proof vs still-assumed

| Path | Proven? | How |
| --- | --- | --- |
| Infra-after-drift still infra | **Yes** | `tests/test_layer2_hardening.py::test_infra_timeout_after_real_drift_does_not_blame_drift` |
| Zero-signal abstains | **Yes** | `test_zero_signal_abstains` |
| Under-budget reuse is not COMPRESSION | **Yes** | `test_under_budget_reused_tokens_is_not_compression` |
| Over-budget `$12.4` drop is COMPRESSION | **Yes** | `test_over_budget_fact_drop_is_compression` |
| `ACME-9917` kept ⇒ not COMPRESSION | **Yes** | `test_constraint_preserving_id_is_not_compression` |
| Tracker zeroed tokens ⇒ not DRIFT | **Yes** | `test_tracker_zeroed_failure_tokens_are_not_drift` |
| Prompt injection abstains | **Yes** | `test_prompt_injection_does_not_force_a_heuristic_bucket` |
| 50-worker isolation | **Yes** | `test_concurrent_attribution_50_workers_no_contamination` |
| Shadow-run current code vs `sessions.db` | **Yes** | 1,340 real failed rows. **1,331** known non-content correctly `infra/non-content` (0 misattributed). 9 prompt-injection `unattributable`. Do not reuse 1,264. |
| Full suite | **Yes** | `python -m pytest tests/ --tb=line -q` → **190 passed, 1 skipped** |
| Same-length semantic drift without a token jump | **Still assumed / weak** | No embedding/diff of message text beyond Jaccard recency and token shape. |
| Layer 2 → Layer 3 import of `classify_failure` | **Debt** | Not moved; Layer 3 out of scope. |
| `deploy/streamlit-cloud` / Layer 1 merge to `main` | **Not this pass** | Explicitly out of scope. |

Layer 3 product code was not edited. `tests/test_repair.py` seeders were updated so they construct real drift/compression/recency mechanisms; the repair API is unchanged.
