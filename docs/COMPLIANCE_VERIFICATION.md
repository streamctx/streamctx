# StreamCtx Compliance Verification

This document is for an auditor, compliance reviewer, or customer engineer who has received a StreamCtx **attestation bundle** and needs to check that it has not been altered. You do not need access to StreamCtx servers, source code, or a StreamCtx account.

## What this proves

Each time StreamCtx attributes a failed AI-agent call (Layer 2) or records a verified-repair attempt (Layer 3), it writes a hash-chained, Ed25519-signed entry to an append-only evidence ledger.

An exported attestation bundle (schema **1.1**) lets you confirm, offline:

- Every included record still hashes to the value that was signed, including the first-class repair fields `applied`, `resolved`, `dry_run`, and `repair_disposition`.
- Each entry still links to the previous **same-session** entry (`session_prev_hash`). Other sessions may interleave on the operator's global chain; a session export is still verifiable without those rows.
- Each entry’s signature was produced by an issuer Ed25519 key. The embedded `public_key_pem` is enough to check **integrity**. **Authenticity** requires pinning the issuer key you already trust with `--public-key`.

In short: **the attribution and repair records in the bundle have not been tampered with after they were signed, and a `repair` row does not mean a live session was mutated.**

## Applied vs verified (read this first)

Layer 3 `verify_fix()` is **counterfactual**. It builds a candidate, optionally replays it in shadow, and **never writes checkpoints or call rows**. `applied` is always `false` unless a caller applied a candidate out of band.

| Signed field | Meaning |
|---|---|
| `repair_disposition=verified_not_applied` | Shadow verification succeeded (`resolved=true`) and the live session was **not** changed (`applied=false`). |
| `repair_disposition=unresolved_not_applied` | A repair attempt was recorded and not applied; shadow verification did not succeed. |
| `repair_disposition=applied` | A caller applied a candidate out of band. Layer 3 never sets this. |
| `record_type=repair` with `applied=false` | **Not** proof that a fix shipped. It is proof that a repair *record* was logged. |

`resolved=true` is **not** `applied=true`. The verifier reprints `repair_summary` from the signed entries and will FAIL if the bundle's summary does not match. Do not treat `record_type=repair` as “a repair happened to production.”

## What this does not prove

A PASS result does **not** mean:

- The AI agent’s original decision or reply was correct.
- The attributed root cause was the true cause of the failure.
- A repair actually fixed the underlying problem, or that it was applied to the live session.
- The payload behind a hash (the full attribution or repair record) is present in the bundle — only the hash plus the signed status fields are exported.
- StreamCtx itself was free of bugs at signing time.
- Every event that *should* have been logged was logged. A hash chain cannot detect a write that was never attempted. Pair `verify_chain()` with `reconcile_shadow_log()` when you have `sessions.db`.

This is tamper-evidence of the **record**, not an opinion about the quality of the AI system that produced the record.

## Prerequisites

- Python **3.9 or newer**
- The `cryptography` package (used only to verify Ed25519 signatures)

```bash
python -m pip install "cryptography>=41.0.0"
```

You do **not** install the `streamctx` package to verify a bundle.

Obtain the issuer’s Ed25519 **public** key by a channel you already trust (the operator’s published key, a previous pinned export, your own copy of `evidence_public.pem`). Compare its SHA-256 fingerprint to `public_key_fingerprint` in the bundle.

## How to verify a bundle

From a checkout of this repository, or with `verify_attestation.py` copied next to the bundle:

```bash
python verify_attestation.py bundle.json --public-key issuer.pem
python verify_attestation.py bundle.json --public-key issuer.pem --verbose
```

Integrity-only (no authenticity pin) is possible but **not** what an auditor should ship:

```bash
python verify_attestation.py bundle.json
```

That path prints `AUTHENTICITY: UNPINNED`. Anyone who can mint a new Ed25519 keypair can produce a self-consistent bundle. `--require-pin` refuses that path.

The script:

1. Loads and validates the JSON (schema version 1.0 or 1.1).
2. Rebuilds each `entry_hash` from the signed fields (v1 concatenated preimage, or v2 canonical JSON including `repair_disposition` / `session_prev_hash` / `key_id`).
3. Verifies each Ed25519 signature. With `--public-key`, signatures must match a pinned issuer key.
4. Checks the **session** hash chain (`session_prev_hash`) so a single-session export remains verifiable when other sessions interleaved on the operator ledger.
5. Checks that `chain_root_hash` / `chain_tip_hash` match the first and last bundled entries.
6. Recomputes `repair_summary` and rejects a bundle that claims a different applied/verified count than the signed entries.

It never contacts a network service.

## Reading the result

| Exit code | Verdict | Meaning |
|-----------|---------|---------|
| `0` | `VERDICT: PASS` + `AUTHENTICITY: PINNED` | Schema, hashes, session chain, signatures, and the pinned issuer key all matched. |
| `0` | `VERDICT: PASS` + `AUTHENTICITY: UNPINNED` | Integrity only. The bundle is self-consistent under the embedded key. |
| `1` | `VERDICT: FAIL` | At least one check failed. The bundle is missing fields, truncated, reordered, keyed by a foreign issuer, or altered. |

On failure the script prints `broken_at_entry_id: <id>` when it can identify the first bad entry. That id is the ledger `entry_id` inside the bundle, not a line number.

A FAIL means you must treat the corresponding records as **unverified**. Do not “fix” the JSON by hand and re-run the script — a repaired file is no longer the original attestation.

## If verification fails

1. Keep the original `bundle.json` unchanged (it is evidence of the failure).
2. Re-run with `--verbose` and save the full output.
3. Contact the StreamCtx operator who issued the bundle through **[your StreamCtx support contact]** — do not use an address from this document; none is published here.
4. Ask them to re-export the session from the original ledger and explain the `broken_at_entry_id`.

Do not share a private signing key. Verification uses only public keys.

## Bundle shape (schema_version 1.1)

The file is a single JSON object. It includes issuer public key(s), the signed hash chain, and signed repair disposition fields. It does **not** include the raw attribution or repair payloads — only `record_payload_hash` (SHA-256 of the canonical JSON of that record).

Signing is **Ed25519** (asymmetric). The issuer keeps the private key. Historic entries remain verifiable after key rotation because each row stores `key_id` and the ledger keeps every public key that has ever signed.

Schema 1.0 bundles (concatenated hashes, global `prev_hash` linkage, no disposition fields) are still accepted by this script. Prefer a 1.1 re-export: 1.0 session slices break if another session interleaved on the global chain, and 1.0 cannot show `applied=false` without the raw payload.
