
#   Changelog

   All notable changes to StreamCtx will be documented in this file.

   This project uses Semantic Versioning. While StreamCtx is on a 0.x version, minor version bumps         (0.4.  x → 0.5.0) may include breaking changes; patch bumps (0.4.2 → 0.4.3) are always safe,      backward-compatible updates.

## [Unreleased]
        •       (add new changes here as you make them)

## [0.4.3] - 2026-07

Added
        •       CI matrix now covers Ubuntu, macOS, and Windows across Python 3.9–3.12 (12/12 passing).
        •       bug_report.yml issue template with severity labels.
        •       greet-issue.yml workflow for automatic acknowledgment on new issues.

Fixed
        •       Expired PyPI publishing token replaced with a fresh scoped token.
        •       dist/ and *.egg-info/ added to .gitignore to prevent accidental artifact commits.

## [0.4.2] - 2026-07

Fixed
        •       SQLite concurrency hardening: resolved a 5-bug cascade (WAL mode singleton race,
                connection leak, read/write pool split, missing indexes). Verified with 50 concurrent
                workers completing in under 5 seconds with zero errors.

        •       README corrected to match the real API:

        •       get_tracker(agent_id) → .start() / .stop() / .checkpoint() / .resume(session_id) /
                .wrap(client) / .get_stats()

        •       Removed reference to non-existent attribute_failure(); correct method is
                get_attribution_engine().attribute_session(session_id).

        •       Clarified that wrap(client) mutates the client in-place and returns the same object.

Security

        •       Full security audit completed: no real secrets leaked, no vulnerable StreamCtx
                dependencies (pip-audit findings were all in the global environment), Bandit
                findings were low-severity and limited to test files.
Added

        •       Full test suite passing: 89 passed, 1 skipped.

## [0.3.2] - 2026-06/07

Added
        •       Attribution Engine (attribution.py): DRIFT / COMPRESSION / RECENCY weighted
                heuristic scoring, verified against real failures (0.82 confidence on a real case).

        •       Counterfactual Replay Engine (replay.py): supports dry_run=True/False, session
                rewind, and alternate context injection.

        •       MkDocs documentation site scaffolded.

        •       Streamlit dashboard reading live schema from ~/.streamctx/sessions.db.

## [0.3.1] - 2026-06

Added
        •       First working PyPI release (v0.3.0 was a broken empty namespace package and was pulled).

        •       Six core features, MIT-licensed and free forever: Checkpoint/Resume, Context
                Compression, Self-Healing, Poison Detector, Context Diff, Real-Time Streaming.

        •       Supabase storage backend as an alternative to the SQLite default.

        •       Test suite: 28 passed, covering all six core features.

## [0.3.0] - 2026-06-13

Added
        •       Initial public launch across LinkedIn, X, Dev.to, IndieHackers, and Product Hunt.
                Known Issues

        •       Shipped as a broken empty namespace package; superseded immediately by 0.3.1.




