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

---

# Layer 3 — Verified Auto-Repair hardening

Date: 2026-09-17
SDK parent: `ee9cc9d` on `cursor/layer2-attribution-hardening` (not merged to `main` in this pass). Layer 3 fixes are on `cursor/layer3-repair-hardening`.

No part of Layer 3 is gated behind a paid tier. `repair.py` / `shadow.py` have no license check, paid flag, or hosted-only branch. Core repair stays MIT.

**`classify_failure()` contract did not change.** Binary `infra_error` / `content_error` at `repair.py:130-151`. Layer 2's `is_non_content_failure()` wrapper needs no update. Simulated failure / recursion / SDK-signature strings remain `content_error` here and `infra/non-content` in Layer 2.

## Senior-bar results against pre-fix code

Judged against live `src/streamctx/repair.py` before the structural change. Probe: `scripts/layer3_senior_bar_pre.py`. Same bar as Layers 1–2 (do not round "weak" up to "works"):

| Case | Pre-fix verdict | What actually happened |
| --- | --- | --- |
| Circular verification (invented `correct_value`) | **FAIL** | Stub LLM echoed `ZEBRA-NOT-IN-SESSION-9917`. `resolved=True`. Gate was "substring in reply," same channel as the injection, no session-evidence check. |
| Compression re-injects dropped fact | **FAIL** | Dominant `compression`, but candidate was earliest-call framing (`original task: summarize…`). `$12.4` absent. |
| Stale earliest vs later Phoenix/$12.4 | **FAIL** | Compression repair injected **Lyon** from call 0. Phoenix / `$12.4` from the uncompressed later window were not restored. Compression had correctly dropped the superseded city; repair put it back. |
| Repair-loop backoff | **FAIL** | 12 content-quality persists → 12 shadow rows. No cap, no `needs_human_review` column. |
| Partial repair vs Layer 1 resume | **PASS** | `verify_fix` never wrote checkpoints/calls. Vacuous fail-safe: repairs are not applied. |
| Failure-without-checkpoint step mapping | **PASS** (end-failure layout) | Fallback used last checkpoint. Middle-failure ordinal zip was still wrong in code; fixed anyway (`repair.py:660-689`). |
| LLM timeout fail-safe | **FAIL** | Hung `llm_fn` ran to completion (6s in the probe). No timeout. Replay error path existed only after the call returned. |
| `classify_failure` vs Layer 2 | **PASS** | Binary contract held. `simulated failure` still `content_error` here, `is_non_content_failure=True` in Layer 2. No abstention token in `classify_failure` itself. |
| Never auto-applied | **PASS** | Shadow + `verify_fix` are counterfactual. Live session not mutated. |
| 50-worker shadow log (sequential SYNC) | **PASS** | 50 rows, 0 session/call mismatches. Concurrent 50-worker proof is in the post-fix test. |

The remaining holes were (1) verification that accepted an invented echo, (2) compression sourcing the earliest call, (3) no session attempt cap, (4) no LLM timeout, (5) `shadow_repair_log` not recording `resolved`/`applied`/`needs_human_review`.

## Root-cause fixes (not one-test special cases)

1. **Independent verification.** `_independent_verification` (`repair.py:812-840`) requires `correct_value` in the replay reply **and** in stored session messages **and** not an injection echo. Breaks circular "the model repeated the string we just asked it to repeat" when that string was never in the session.
2. **Compression restores dropped facts.** `_dropped_source_snippets` replays Layer 1 `compress_messages()` on the attributed uncompressed request and clips windows around dropped dollar/decimal/4+ digit/stable-ID facts (`repair.py:543-591,1004-1031`). Earliest-call fallback removed. Unknown signals no longer silently generate a DEDUPE note.
3. **Never mutate live state.** `applied=False` on every `RepairResult`. Proof stores a pre-repair checkpoint fingerprint. Timeout via a daemon thread (`REPAIR_LLM_TIMEOUT_S=30`, `repair.py:1041-1065`) returns `resolved=False`, `needs_human_review=True`, original rows untouched. Fail-safe is snapshot-and-don't-write, not rollback-after-write.
4. **Give-up cap = Layer 2 lookback.** `MAX_SHADOW_REPAIRS_PER_SESSION = DEFAULT_LOOKBACK` (5). One log row per failed call; slot reserved under the write lock (`storage.py:549-631`). More than one lookback window of auto-repairs is a loop.

## Proof vs still-assumed

| Path | Proven? | How |
| --- | --- | --- |
| Invented value does not resolve | **Yes** | `tests/test_layer3_hardening.py::test_invented_correct_value_is_not_verified` |
| Compression candidate has `$12.4`, not earliest task | **Yes** | `test_compression_reinjects_dropped_fact_not_earliest` |
| Lyon not re-injected when later window has Phoenix/$12.4 | **Yes** | `test_stale_earliest_city_is_not_re_injected` |
| Injection echo rejected | **Yes** | `test_injection_echo_is_not_verified` |
| Live restore + checkpoints unchanged | **Yes** | `test_live_restore_of_session_fact_is_resolved` |
| 12 failures cap at 5 | **Yes** | `test_repair_loop_gives_up_after_lookback_window` |
| LLM timeout ~1s, session untouched | **Yes** | `test_llm_timeout_fail_safe` |
| Middle failure `from_step` = last success | **Yes** | `test_middle_failure_replays_from_last_success_checkpoint` |
| `classify_failure` contract unchanged | **Yes** | `test_classify_failure_contract_unchanged_for_layer2` |
| Opt-out `STREAMCTX_SHADOW_REPAIR=0` | **Yes** | `test_shadow_opt_out_env`; `review_shadow_log.py` on real db is empty |
| Injected content-quality E2E | **Yes** | `test_injected_content_quality_e2e_pipeline` |
| 50-worker concurrent shadow fidelity | **Yes** | `test_concurrent_shadow_log_50_workers_no_contamination` |
| Shadow-run vs `~/.streamctx/sessions.db` | **Yes** | 1,340 real failed rows. `classify_failure`: 161 infra + 1,179 content. All 1,179 dry-run ok, conf 0, signal none (Layer 2 abstains). Zero organic content-quality repairs. |
| Full suite | **Yes** | `python -m pytest tests/ --tb=line -q` → **206 passed, 1 skipped** |
| Auto-apply into live session | **Not shipped** | By design. |
| Tracker success-path hallucination → shadow | **Review, not failure** | Non-empty wrong text is still `failed=False` (no Layer 3 shadow repair). Session-grounded ID/$ contradiction now writes `shadow_attribution_log` with `failure_kind=stable_fact_review`. |
| Layer 4 / `deploy/streamlit-cloud` / merge to `main` | **Not this pass** | Explicitly out of scope. |

`classify_failure()` was not moved out of Layer 3. Layer 2 still imports it. That layering inversion remains debt; this pass refused to change the function's meaning to "fix" it.

---

# Layer 4 — Compliance Evidence hardening

Date: 2026-09-17
SDK parent: `e432cb7` on `cursor/layer3-repair-hardening` (not merged to `main` in this pass). Layer 4 fixes are on `cursor/layer4-evidence-hardening`.

No part of Layer 4 is gated behind a paid tier. `evidence.py` / `scripts/verify_attestation.py` have no license check, paid flag, or hosted-only branch. Core evidence stays MIT.

**The applied/verified distinction was not already correct in the exported attestation.** Layer 3 recorded `applied=False` in the in-process `RepairResult` and in `evidence_payloads.payload_json`, but schema 1.0 `export_attestation()` stripped payloads. An outside reader of the JSON bundle saw `record_type: "repair"` and could not tell verified-in-shadow from applied. `evidence_payloads` also had no append-only trigger, so rewriting `applied` to `true` left `verify_chain()` valid. That is the fix this pass exists for.

Layer 3's counterfactual contract was not changed. `applied` is still always `False` from `verify_fix()` unless a caller applies a candidate out of band.

## Senior-bar results against pre-fix code

Judged against live `src/streamctx/evidence.py` / `scripts/verify_attestation.py` before the structural change. Probe: `scripts/layer4_senior_bar_pre.py`. Same bar as Layers 1–3 (do not round "weak" up to "works"):

| Case | Pre-fix verdict | What actually happened |
| --- | --- | --- |
| Signing coverage | **PARTIAL** | Every row signed. `entry_hash` covered `entry_id+type+ref+payload_hash+prev_hash+timestamp` (concat, no delimiters). `applied` / `resolved` / `key_id` / `session_prev_hash` were not signed fields. |
| Tamper field-modify | **PASS** | `broken_at_entry_id=3`, `total_checked=2`. |
| Tamper delete | **PARTIAL** | Delete of 3 reported `broken_at_entry_id=4` (successor prev mismatch). No `entry_id` gap reason. |
| Tamper reorder | **PASS** | `broken_at_entry_id=2`. |
| Tamper foreign-splice | **PASS** | `broken_at_entry_id=99`. |
| Payload rewrite `applied→true` | **FAIL** | `verify_chain()` stayed `valid=True`. No append-only trigger on `evidence_payloads`. |
| Interleaved session export | **FAIL** | Honest session-A bundle (entries 1 and 3) failed `verify_attestation.py` on global `prev_hash` vs previous bundled row. |
| Applied vs verified in the bundle | **FAIL** | Real `verify_fix(dry_run=False)`: `resolved=True`, `applied=False` in the payload. Export had no `applied` / `repair_disposition`. `record_type=repair` was the only signal. |
| Unpinned embedded public key | **FAIL** | Attacker-generated keypair bundle: `verify_attestation.py` exit 0. No `--public-key`. |
| Key rotation | **PARTIAL** | `verify_chain` broke at entry 1 after a new key. No per-entry `key_id`. Historic rows unverifiable. |
| 50-worker concurrent append (separate ledger objects) | **FAIL** | 38 `UNIQUE constraint failed: evidence_ledger.entry_id`, 12 of 50 rows. Per-instance `threading.Lock` does not serialize cross-connection `SELECT max(id)+1`. Chain of the 12 looked valid. |
| Silent omitted event | **WEAK** | Never-written entry: `verify_chain` valid. No reconcile helper. Inherent to hash chains for events never presented to the logger. |
| kill-9 / torn write | **WEAK** | Two INSERTs, one COMMIT (atomic pair) but no `BEGIN IMMEDIATE`, no intent log. Uncommitted kill looks like a valid shorter chain. |
| MIT / no paid gate | **PASS** | No license/paid needles. |

The remaining holes were (1) exporting opaque hashes so applied/verified could not be read, (2) unsigned mutable payloads, (3) session exports that used the global chain, (4) unpinned authenticity, (5) no key_id so rotation broke history, (6) racy `entry_id` assignment across connections, (7) imprecise delete detection, (8) no detectable incomplete write.

## Root-cause fixes (not one-test special cases)

1. **Signed repair disposition.** Schema 1.1 / `hash_version` 2 hashes canonical JSON including `repair_disposition`, `applied`, `resolved`, `dry_run`, `session_prev_hash`, `key_id`. `disposition_from_payload()` maps Layer 3 payloads and never treats `resolved` as `applied`. Shadow success is `verified_not_applied`. `export_attestation()` emits those fields; the offline verifier recomputes `repair_summary` and FAILs a lying summary (`evidence.py:336-359,732-748,1087-1115`, `verify_attestation.py:224-243,639-661`).
2. **Append-only payloads + payload-hash walk.** UPDATE/DELETE triggers on `evidence_payloads`. `verify_chain()` joins payloads, requires a row, and checks `payload_hash(payload_json) == record_payload_hash` (`evidence.py:103-106,163-173,904-915`).
3. **Session chain + precise gaps.** Signed `session_prev_hash` so a single-session export verifies when other sessions interleaved. `verify_chain()` walks the full global chain (filter does not skip predecessors), reports `entry_id_gap` at the missing id, `found_entry_id` for a splice (`evidence.py:715-729,846-853,894-901`).
4. **Atomic append + detectable incomplete write.** `BEGIN IMMEDIATE`, retries on busy/unique, `synchronous=FULL`, intent file fsync'd before COMMIT (`evidence.py:430-441,583-591,695-826`). Cross-connection 50-worker appends serialize on SQLite's write lock, not only a process `threading.Lock`.
5. **Key pinning + rotation.** `signing_keys` is append-only. Each entry stores `key_id` (SHA-256 of the PEM). Verifier `--public-key` / `--require-pin`. Embedded key remains an integrity convenience and is labeled `AUTHENTICITY: UNPINNED` without a pin (`verify_attestation.py:250-327,744-756`).

## Proof vs still-assumed

| Path | Proven? | How |
| --- | --- | --- |
| Four tamper classes, precise `broken_at_entry_id` | **Yes** | `tests/test_layer4_hardening.py::test_tamper_*` |
| Payload rewrite detected | **Yes** | `test_payload_rewrite_is_detected` |
| Interleaved session export + pinned offline verify | **Yes** | `test_interleaved_session_export_verifies` |
| Layer 3 shadow verify is `verified_not_applied` in the bundle | **Yes** | `test_shadow_verified_not_applied_is_unambiguous` |
| Foreign keypair rejected when pinned | **Yes** | `test_foreign_keypair_bundle_rejected_when_pinned` |
| Key rotation keeps historic rows verifiable | **Yes** | `test_key_rotation_keeps_historic_entries_verifiable` |
| 50-worker concurrent append, separate ledger objects | **Yes** | `test_concurrent_append_50_separate_ledger_objects` |
| Kill-9 fail-safe (integrity ok, ledger==payload, intent or valid) | **Yes** | `test_kill9_mid_write_fail_safe` |
| Shadow log vs ledger reconcile | **Yes** | `test_silent_omission_is_detectable_against_shadow_log` |
| Full suite | **Yes** | `python -m pytest tests/ --tb=line -q` → **221 passed, 1 skipped** |
| Completeness of never-attempted Layer 2 attributions | **Still assumed / weak** | No independent attribution table. `safe_append_evidence` swallows errors so Layer 2/3 never break. |
| Power-loss (not process kill) | **Still assumed** | Same SQLite limit as Layer 1. |
| `deploy/streamlit-cloud` | **Not this pass** | Deploy branch HEAD `76ee241` is still not on `main`. |
| Merge of Layers 1–4 into `main` | **Done later** | Integration close-out 2026-09-18, merge `98c7f03`. |

Schema 1.0 bundles remain verifiable. Prefer a 1.1 re-export: 1.0 session slices break under interleaving, and 1.0 cannot show `applied=false` without the raw payload.

---

# Integration close-out — all four layers on `main`

Date: 2026-09-18
SDK HEAD: `98c7f03` on `main`

## Part A — merges

Real `--no-ff` merge commits, in chain order, same discipline as `b25bab1` (`release/v0.4.6` into `main`). No squash. **Conflicts: none** (linear chain, each branch 1 commit ahead of the previous tip).

| Order | Branch | Content commit | Merge commit | Parents | Post-merge suite |
| --- | --- | --- | --- | --- | --- |
| 1 | `cursor/layer1-core-sdk-hardening` | `b3b17b7` | `83b474f` | `b4cad0d` + `b3b17b7` | **178 passed, 1 skipped** |
| 2 | `cursor/layer2-attribution-hardening` | `ee9cc9d` | `8c4d2ce` | `83b474f` + `ee9cc9d` | **190 passed, 1 skipped** |
| 3 | `cursor/layer3-repair-hardening` | `e432cb7` | `dc9bf02` | `8c4d2ce` + `e432cb7` | **206 passed, 1 skipped** |
| 4 | `cursor/layer4-evidence-hardening` | `7443e5f` | `98c7f03` | `dc9bf02` + `7443e5f` | **221 passed, 1 skipped** |

Final suite on merged `main` (same tree as the Layer 4 post-merge run):

```
python -m pytest tests/ -v --tb=short
221 passed, 1 skipped in 36.43s
```

Skipped: `tests/test_openai_integration.py::TestOpenAILive::test_real_openai_call` (needs `OPENAI_API_KEY`; this machine had OpenRouter only).

Knowledge doc regenerated against this tree: `knowledge/streamctx_project_knowledge.md`, `knowledge_version: 2026-09-18.1`, `source_commit: 98c7f03`.

## Part B — live pipeline proof

Script: `scripts/live_full_pipeline_proof.py`. Isolated `STREAMCTX_HOME=artifacts/integration-proof`, Ed25519 keypair generated there, `STREAMCTX_EVIDENCE_PRIVATE_KEY` set (without it Layer 4 `safe_append_evidence` is a silent no-op). Compression default patched to `max_tokens=400` so Layer 1 compression/reuse could fire. Provider: OpenRouter `openrouter/free`. 15 turns.

### Organic session (`session_id=1`)

Did **not** force a failure.

| Layer | What actually happened |
| --- | --- |
| 1 | 15 calls, 15 checkpoints, 0 failed, 0 healed. `reused_tokens` 49 → 827. Report: 10,554 tokens, 45% cached/reused, biggest waste = repeated system prompt. |
| 2 | Never invoked by intercept. `failed=0`. |
| 3 | `shadow_repair_log` empty. |
| 4 | `export_attestation(1)` → schema 1.1, **0 entries**, `repair_summary` all zeros. Fresh-process `verify_attestation.py --public-key issuer.pem --verbose` **exit 0**, AUTHENTICITY PINNED. |

Honest content outcome: 7 of 15 turns stored `response_text=''` with `failed=0` and `output_tokens=220` (calls 4, 5, 6, 9, 11, 14, 15). Last-turn fact check was False because the last reply was empty, not because Layer 1 dropped `ACME-9917` / `staging-db-07` / `$12.4`. Turn 12 stored `"From Turn 1: ticket ACME"` (truncated). Turn 13 stored a chain-of-thought dump, not the three values. `_response_text` only reads `choices[0].message.content` (`tracker.py:187-201`). Empty `content` with billed output tokens is still a success row.

The empty attestation is an **accurate picture of the ledger** (no attribution/repair events). An outside auditor would not learn that seven replies were blank. That is the success-path gap, now seen live.

### Injected session (`session_id=2`, labeled injected)

One content-quality row: `persist_step` success + `record_call(failed=True, error_message=None)` with a buried `$12.4 million` and a `$47.3 million` hallucination. Shadow sync on.

| Layer | What actually happened |
| --- | --- |
| 1 | 2 calls. Success checkpointed. Failure: `failed=1`, `input_tokens=0`, `reused_tokens=0`, `error_message=None`. Shadow scheduled from `record_call`. |
| 2 | `attribute_failure`: dominant compression (raw 0.884), drift 0.698, recency-why 0.0, confidence 0.6144, root=`call_id=17` (the failing row itself). Did **not** abstain. |
| 3 | Shadow: `signal=compression`, `dry_run=True`, `resolved=False`, `applied=False`, candidate contains `12.4`. Live `verify_fix(dry_run=False)` with a stub LLM that restored `$12.4 million`: `resolved=True`, `applied=False`. Checkpoints of the live session were not the apply target. |
| 4 | Bundle: 5 entries (3 attribution + 2 repair — extra attributions are from the proof script calling `attribute_failure` / `verify_fix` again, each `_finish` appends). `repair_summary`: `applied=0`, `verified_not_applied=1`, `unresolved_not_applied=1`. Offline verify **exit 0**, PINNED, note "no bundled repair was applied". `reconcile_shadow_log` `complete=True`. |

An outside auditor reading only the injected bundle would correctly conclude: one shadow dry-run that did not resolve, one later shadow-verify that resolved and was **not applied**, and that nothing was written into a live session. That matches what happened.

## Part C — verdict

The merged pipeline **works end-to-end on the paths it claims**: Layer 1 persists and compresses; when a content-quality row is actually marked `failed=True` with empty `error_message`, Layer 2 attributes, Layer 3 shadow-verifies without applying, Layer 4 exports a pinned bundle an independent process accepts, and `applied=false` is unambiguous.

It does **not** round up to "the live conversation was fully observed." Organic usage on this provider produced blank replies that Layer 1 stored as successes, so Layers 2–4 correctly had nothing to say. That is not a merge-conflict bug; it is a cross-layer contract the four individual passes left as "dead in intercept," now confirmed on a real 15-turn session.

### New finding (not covered as its own case in Layers 1–4)

**Blank `message.content` with `output_tokens > 0` is a success.** Live OpenRouter turns billed 220 output tokens, stored `response_text=''`, `failed=False`, and moved the resume checkpoint. Layer 3's "success-path hallucination" note assumed *wrong text*; this is *no text*. **Fixed in the Layer 1 blank-reply pass below.** Not fixed in the integration pass itself.

### Not new

- Organic sessions rarely produce `failed=True` content rows. Reconfirmed.
- `STREAMCTX_EVIDENCE_PRIVATE_KEY` unset → Layer 4 logs nothing. By contract (`evidence.py:1201-1212`).
- `deploy/streamlit-cloud` (`76ee241`) is still not on `main`.
- Ledger appends once per `attribute_failure`/`verify_fix` call, not once per failed_call_id. Attempt log, not unique-event log.

Do not ship a claim that "the attestation is a complete record of the conversation." It is a complete record of **attribution and repair attempts that were presented to the logger**.

---

# Layer 1 — Blank-reply blind spot

Date: 2026-09-18
SDK parent: merged `main` after `98c7f03`. Fix is uncommitted on top of that tree unless committed separately.

## What was wrong

`failed` was set only when `fn()` raised (`tracker.py` intercept). A 200 with empty `choices[0].message.content` always took `_persist_success`, which **hardcodes** `failed=False`. `_response_text` did not read `refusal`, Anthropic text blocks, or tool payloads. Billed empty replies moved the checkpoint and were invisible to Layers 2–4.

Same decision path for OpenAI-style (`wrap` / SDK patch → `provider="openai"`) and Anthropic-style (`provider="anthropic"`). Anthropic had a slightly better content extractor but still never set `failed` from content.

## What counts as this failure

| Shape | `failed` | Why |
| --- | --- | --- |
| Empty/`None`/whitespace content, **provider-reported** output tokens > 0, no tool call | **True** | Observed live defect. `error_message=None` so shadow fires. Checkpoint not moved. Usage kept. |
| Empty content, reported output tokens = 0, or missing usage | **False** | Not billed; stubs and no-ops. Still open as a different mode. |
| `tool_calls` / legacy `function_call` / Anthropic `tool_use`, even with empty text | **False** | Valid response shape. |
| Non-empty `refusal` | **False** | Visible text; stored as `response_text`. |
| Reasoning/thinking only, billed, no visible text | **True** | Not user-visible. Same as the live miss. |
| Non-empty wrong text (hallucination) | **False** | Not an automatic failure. Session-grounded ID/$ contradiction is a Layer 2 review signal (see close-out below). |

Billed means `usage.completion_tokens` / `usage.output_tokens` from the provider, **not** the `len//4` estimate. Tests without usage must not trip this gate.

## Fix

`_is_blank_billed_reply` in the intercept after a non-exception response (`tracker.py:261-365,749-775`). Hits `_persist_failure` with empty `error_message` and real token counts. `_persist_success` is not used (it still hardcodes `failed=False`). Response is returned to the caller. No auto-retry (unlike exceptions).

Layers 2–4 unchanged. Empty-error `content_error` already shadows.

## Proof

| Path | Proven? | How |
| --- | --- | --- |
| Blank + billed → failed, no checkpoint | **Yes** | `test_blank_billed_reply_is_failed_and_does_not_move_checkpoint` |
| `content=None` billed | **Yes** | `test_blank_none_content_billed_is_failed` |
| Whitespace billed | **Yes** | `test_whitespace_only_billed_is_failed` |
| Empty + zero reported tokens | **Yes** (not failed) | `test_empty_zero_output_tokens_is_not_this_failure` |
| Missing usage | **Yes** (not failed) | `test_empty_missing_usage_is_not_this_failure` |
| Tool-call-only billed | **Yes** (success) | `test_tool_call_only_billed_is_success` |
| Legacy function_call | **Yes** (success) | `test_legacy_function_call_only_is_success` |
| Refusal | **Yes** (success, checkpointed) | `test_refusal_is_success_and_checkpointed` |
| Reasoning-only billed | **Yes** (failed) | `test_reasoning_only_billed_is_still_failed` |
| Anthropic tool_use | **Yes** (success) | `test_anthropic_tool_use_only_is_success` |
| Anthropic thinking-only billed | **Yes** (failed) | `test_anthropic_blank_billed_is_failed` |
| Shadow on blank billed | **Yes** | `test_blank_billed_triggers_shadow_repair` |
| Live OpenRouter, `max_tokens=80` | **Yes** | `scripts/live_blank_reply_proof.py`: 10 calls, **3** organic blanks (`ids 3,7,9`), all `failed=True`, **0** left as success, checkpoints=7, shadow recency dry-run `applied=False` ×3, attestation 6 entries `unresolved_not_applied=3` `applied=0`, `verify_attestation.py` exit 0 pinned |
| Full suite | **Yes** | `python -m pytest tests/ -v --tb=short` → **234 passed, 1 skipped** |
| Historic `~/.streamctx/sessions.db` | **Counted, not rewritten** | Exact empty-string + billed + success: **4** rows (session 1848, 2026-09-17, `openrouter/free`, 220 tokens). Do **not** use the 20,564 NULL `response_text` successes — that column was added later and not backfilled. Integration-proof DB: **7** empty-string billed successes from the pre-fix live run. |
| Wrong-text vs session history | **Review signal, not failed** | See Layer 1/2 session-grounded fact review below. Still not `failed=True`. |
| Empty + zero billed tokens | **Still open / not this case** | Left as success so mocks and no-ops do not false-fire. |
| Audio-only / image-only assistants | **Still assumed** | Not extracted; would look blank if billed. |
| OpenAI Responses API / non-`chat.completions` | **Not this intercept** | Only `Completions.create` and Anthropic `messages.create`. |

**Verdict:** the observed blank-but-billed blind spot is **closed** on the chat-completions intercept for OpenAI-style and Anthropic-style clients. Tool-call-only and refusals are not misclassified. Non-empty wrong text is still `failed=False`; the follow-up pass adds a Layer 2 review signal for a narrow class of session-grounded contradictions.

---

# Layer 1/2 — Session-grounded fact review (not a hallucination detector)

Date: 2026-09-18
SDK parent: uncommitted on merged `main` after `98c7f03` (blank-reply intercept already in the tree).

## Step 0 — What is actually detectable

A fluent, well-formed reply that is *factually wrong* is a different problem from a blank reply. Blank is structural. Wrongness needs ground truth. The SDK only has the session.

| Class | In scope? | Why |
| --- | --- | --- |
| Reply contradicts a stable ID already stored in this session (`ACME-9917` vs `ACME-1234`) | **Yes** | Ground truth is in `messages_json`. Reuses Layer 1 `_STABLE_ID_RE` (`compressor.py:37`). |
| Reply contradicts a `$` amount already stored in this session (`$12.4` vs `$47.3`) | **Yes, with paraphrase guards** | Dollar arm of Layer 3 `_REPAIR_FACT_RE` (`repair.py:204-206`), plus commas. `$12.4` vs `$12,400,000` is treated as the same amount (×1e3/1e6/1e9). |
| Reply cites a stable-ID family that never appeared | **No** | Presence/absence of a *new* family is not contradiction; assistants mint IDs. |
| Reply is wrong about the world (legal advice, science, news) | **No** | Requires external ground truth the SDK does not have and must not pretend to have. |
| Bare decimals, years, unprefixed numbers | **No** | Too noisy; would cry wolf. |

This is **not** a general hallucination detector. Shipping one would be dishonest.

## What already existed (audit)

- Layer 1 compressor pins whole messages that match `_STABLE_ID_RE` / constraint language (`compressor.py:28-51`). Dollars are **not** pinned unless they sit in a high-value message.
- Layer 2 compression scoring uses `_FACT_RE` (digits + stable IDs) on uncompressed vs compressed blobs (`attribution.py:73,183-192`).
- Layer 3 `verify_fix` extracts `$` / decimals / 4+ digit runs / stable IDs (`repair.py:204-206,540-541`) and only runs on `failed=True` with empty `error_message`.
- Until this pass, none of that ran on a **successful** reply. `_persist_success` still hardcodes `failed=False` (`tracker.py:905-938`).

## Senior-bar results (must not cry wolf)

Judged against `find_reply_contradictions` (`facts.py:175-248`) and the intercept hook (`tracker.py:799-801,884-903`). False positives on paraphrase/updates are as bad as misses.

| Case | Verdict | What happened |
| --- | --- | --- |
| Early `ACME-9917` in a 24-filler-turn history, reply `ACME-1234` | **PASS** | Caught. GT is stored uncompressed history, not the compressed outbound window. |
| `$12.4 million` restated as `12,400,000 dollars` (no `$`) | **PASS** | No finding. Unprefixed numbers are out of scope. |
| `$12.4` vs `$12,400,000` | **PASS** | Scale-variant; no finding. |
| `$12.4` vs `$12.40` | **PASS** | Equal after parse; no finding. |
| `$12.4` vs `$47.3` | **PASS** | Contradiction. |
| Recap "was ACME-9917, now ACME-4401" after a real reassignment | **PASS** | Contains GT; no finding. |
| User "reassigned to ACME-4401", reply uses `ACME-4401` | **PASS** | Last non-question user/system assertion wins. |
| Reply still uses `ACME-9917` after that update | **PASS** | Stale; flagged. |
| User question "the ticket is ACME-1234, right?" | **PASS** | `?` does not update GT. Agreeing with the trap is a contradiction. |
| Prior assistant hallucination does not become GT | **PASS** | Assistant role is ignored for ground truth. |
| New family `GH-4411` while session is `ACME-*` | **PASS** | Not flagged. |
| Omitting the fact entirely | **PASS** | Not completeness. |
| `$` amount dropped by compression (first-sentence extract / budget trim), reply uses a different `$` | **PASS** | `missing_context`, not `contradiction`. The SDK dropped the fact on purpose. |
| Wrong ID while compression still pinned the original | **PASS** | `contradiction` (`expected_in_compressed=True`). |

False-positive rate on the paraphrase/update cases above is **zero** in this suite. The check is narrow enough to ship. If it were not, this pass would have stopped here.

## What shipped

A review signal, **not** `failed=True`:

1. `src/streamctx/facts.py` — session-grounded extractor. Same-family IDs + `$` amounts. Latest non-question USER/SYSTEM value wins.
2. After `_persist_success`, `LLMTracker._review_success_facts` calls `AttributionEngine.review_success_reply` (`tracker.py:799-801,884-903`, `attribution.py:508-598`).
3. On a finding: row in `shadow_attribution_log` with `failure_kind=stable_fact_review`, `dominant_signal` `contradiction` or `missing_context`. Layer 4 `safe_append_evidence("attribution", …)` if a key is configured. Call row stays `failed=False`. Checkpoint still moves.
4. Opt-out: `STREAMCTX_FACT_REVIEW=0`.
5. Does **not** enter Layer 2's 0.5/0.3/0.2 failure ranking and does **not** start Layer 3 shadow repair (those still require `failed=True`).

Lower confidence than a blank reply is the point. Blank is structural. This is a flagged-for-review signal.

## Proof

| Path | Proven? | How |
| --- | --- | --- |
| ID contradiction, long session, paraphrase, scale variant, update, question trap, compression drop | **Yes** | `tests/test_fact_contradiction.py` (20 cases) |
| Intercept logs review and leaves `failed=False` | **Yes** | `test_intercept_logs_review_without_failing_the_call` |
| Paraphrase / legitimate update do not log | **Yes** | `test_intercept_does_not_log_paraphrase`, `test_intercept_does_not_log_legitimate_update` |
| Opt-out | **Yes** | `test_fact_review_opt_out` |
| Live OpenRouter paraphrase `$12.4 million` → `$12,400,000` | **Yes** | `scripts/live_fact_contradiction_proof.py` session B: review_count=0 |
| Live legitimate reassignment `ACME-9917` → `ACME-4401` | **Yes** | Session C: reply `ACME-4401`, review_count=0 |
| Live sycophancy trap "it's ACME-1234, right?" | **Organic miss, honest** | Session A: model answered `ACME-9917` (correct). No review row. Turn 2 was blank-billed (`failed=True`) — blank-reply fix still holds. |
| Labeled injected wrap, wrong ID, still `failed=False` | **Yes** | Same script: `contradiction stable_id: expected ACME-9917, reply used ACME-1234`, `failed=false` |
| Full suite | **This pass** | `tests/test_fact_contradiction.py` 20 passed. Full `tests/`: **253 passed, 1 skipped**, plus 1 pre-existing flake `test_kill9_mid_write_fail_safe` (`unreadable_intent` vs `uncommitted_intent`) unrelated to this change. |

## Explicit capability boundary

Caught:

- Same-family stable-ID swap against the latest non-question user/system assertion in stored (uncompressed) session history.
- `$` amount swap that is not a 1e3/1e6/1e9 scale paraphrase of that assertion.
- Same mismatch when compression dropped the fact from outbound context, labeled `missing_context` rather than `contradiction`.

Deliberately not attempted:

- General factual correctness against the world. The SDK has no external ground truth.
- Completeness (omitting a known fact).
- New ID families, bare numbers, years, cities, names, or unconstrained natural-language claims.
- Automatic `failed=True` / Layer 3 repair on these rows. Confidence is lower than a blank reply; crying wolf on a billed success would be worse than missing some wrong text.

A system that says it catches X and Y and does not attempt Z because Z needs ground truth it does not have is more trustworthy than one that implies a hallucination detector.

**Verdict:** the narrow session-grounded check survived the senior-bar cases and is shipped as a Layer 2 review signal. The "said something wrong about the world" case remains out of scope on purpose.


