# StreamCtx Compliance Verification

This document is for an auditor, compliance reviewer, or customer engineer who has received a StreamCtx **attestation bundle** and needs to check that it has not been altered. You do not need access to StreamCtx servers, source code, or a StreamCtx account.

## What this proves

Each time StreamCtx attributes a failed AI-agent call (Layer 2) or records a verified-repair attempt (Layer 3), it writes a hash-chained, Ed25519-signed entry to an append-only evidence ledger.

An exported attestation bundle lets you confirm, offline:

- Every included record still hashes to the value that was signed.
- Each entry still links to the previous entry (`prev_hash` equals the prior `entry_hash`).
- Each entry’s signature was produced by the issuer’s Ed25519 private key, using the public key embedded in the bundle.

In short: **the attribution and repair records in the bundle have not been tampered with after they were signed.**

## What this does not prove

A PASS result does **not** mean:

- The AI agent’s original decision or reply was correct.
- The attributed root cause was the true cause of the failure.
- A repair actually fixed the underlying problem.
- The payload behind a hash (the full attribution or repair record) is present in the bundle — only the hash is exported.
- StreamCtx itself was free of bugs at signing time.

This is tamper-evidence of the **record**, not an opinion about the quality of the AI system that produced the record.

## Prerequisites

- Python **3.9 or newer**
- The `cryptography` package (used only to verify Ed25519 signatures)

```bash
python -m pip install "cryptography>=41.0.0"
```

You do **not** install the `streamctx` package to verify a bundle.

## How to verify a bundle

From a checkout of this repository, or with `verify_attestation.py` copied next to the bundle:

```bash
python verify_attestation.py bundle.json
```

Summary-only is the default. To print every entry that was checked:

```bash
python verify_attestation.py bundle.json --verbose
```

The script:

1. Loads and validates the JSON (schema version 1.0).
2. Rebuilds each `entry_hash` from the stored fields and checks the hash chain.
3. Verifies each Ed25519 signature against the embedded `public_key_pem`.
4. Checks that `chain_root_hash` / `chain_tip_hash` match the first and last entries.

It never contacts a network service.

## Reading the result

| Exit code | Verdict | Meaning |
|-----------|---------|---------|
| `0` | `VERDICT: PASS` | Schema, hashes, chain links, and signatures all matched. The bundle is intact. |
| `1` | `VERDICT: FAIL` | At least one check failed. The bundle is missing fields, truncated, reordered, or altered. |

On failure the script prints `broken_at_entry_id: <id>` when it can identify the first bad entry. That id is the ledger `entry_id` inside the bundle, not a line number.

A FAIL means you must treat the corresponding records as **unverified**. Do not “fix” the JSON by hand and re-run the script — a repaired file is no longer the original attestation.

## If verification fails

1. Keep the original `bundle.json` unchanged (it is evidence of the failure).
2. Re-run with `--verbose` and save the full output.
3. Contact the StreamCtx operator who issued the bundle through **[your StreamCtx support contact]** — do not use an address from this document; none is published here.
4. Ask them to re-export the session from the original ledger and explain the `broken_at_entry_id`.

Do not share a private signing key. Verification uses only the public key already inside the bundle.

## Bundle shape (schema_version 1.0)

The file is a single JSON object. It includes the issuer public key and the signed hash chain. It does **not** include the raw attribution or repair payloads — only `record_payload_hash` (SHA-256 of the canonical JSON of that record).

Signing is **Ed25519** (asymmetric). The issuer keeps the private key. Possession of this bundle and its public key is enough to verify, and is not enough to forge a new valid entry.
