# Backlog

## Deferred / Future

### Dashboard automated testing (deferred)

`dashboard.py` currently has zero automated tests. The Compliance Evidence
export button (session detail page) was verified manually via HTTP-level
checks (localhost:8502 load) and Python-level checks (`export_attestation()`
output diffed against the button-triggered bundle) rather than true UI-level
automation, since browser automation tooling wasn't available during that
session.

Risk: future dashboard changes (new tabs, buttons, wiring) won't be caught
by automated regression tests — only manual testing.

Action when revisited: add a playwright-based e2e test layer for `dashboard.py`
— load the dashboard, click through key flows (export button, session
selection, etc.), assert on downloads/UI state. Prioritize this once the
dashboard grows past its current size/complexity (e.g. when it starts
resembling the multi-tab Agent Control Center in streamctx-agents).
