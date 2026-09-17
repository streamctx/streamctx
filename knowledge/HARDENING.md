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

## Explicitly not done yet

- Layers 2-4 re-hardening / cross-layer wiring prompt
- `deploy/streamlit-cloud` merge
- Changing README / `__init__.py` compression percentage copy (knowledge doc records the measured numbers; code wins on algorithm)
- Token-level SSE streaming (never the Layer 1 meaning of "real-time streaming")
