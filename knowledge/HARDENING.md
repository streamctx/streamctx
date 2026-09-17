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
