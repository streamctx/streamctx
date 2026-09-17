"""Offline verifier for a StreamCtx compliance attestation bundle.

This script has no StreamCtx dependency.  It only needs the Python
standard library plus the `cryptography` package.

Usage:
    python verify_attestation.py bundle.json
    python verify_attestation.py bundle.json --verbose
    python verify_attestation.py bundle.json --public-key issuer.pem

Exit codes:
    0  every check passed (integrity; authenticity too if a key was pinned)
    1  the bundle is missing fields, malformed, or tampered
"""

import argparse
import base64
import hashlib
import json
import sys

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key

REQUIRED_BUNDLE_FIELDS_1_0 = (
    "schema_version",
    "issuer",
    "session_id",
    "exported_at",
    "public_key_pem",
    "entries",
    "chain_root_hash",
    "chain_tip_hash",
)

REQUIRED_BUNDLE_FIELDS_1_1 = REQUIRED_BUNDLE_FIELDS_1_0 + (
    "keys",
    "repair_summary",
    "public_key_fingerprint",
)

REQUIRED_ENTRY_FIELDS_1_0 = (
    "entry_id",
    "record_type",
    "record_ref_id",
    "record_payload_hash",
    "prev_hash",
    "entry_hash",
    "timestamp",
    "signature",
)

REQUIRED_ENTRY_FIELDS_1_1 = REQUIRED_ENTRY_FIELDS_1_0 + (
    "session_prev_hash",
    "hash_version",
    "key_id",
    "repair_disposition",
    "applied",
    "resolved",
    "dry_run",
)

SIGNED_ENTRY_KEYS_V2 = (
    "applied",
    "dry_run",
    "entry_id",
    "hash_version",
    "key_id",
    "prev_hash",
    "record_payload_hash",
    "record_ref_id",
    "record_type",
    "repair_disposition",
    "resolved",
    "session_prev_hash",
    "timestamp",
)

SUPPORTED_SCHEMAS = ("1.0", "1.1")
EXPECTED_ISSUER = "streamctx"
ALLOWED_RECORD_TYPES = ("attribution", "repair")
ALLOWED_DISPOSITIONS = (
    None,
    "applied",
    "verified_not_applied",
    "unresolved_not_applied",
)
GENESIS_HASH = "0" * 64


def canonical_json(obj):
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def _json_bool(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    return bool(value)


def compute_entry_hash_v1(entry):
    """Must match StreamCtx ledger hashing schema 1.0 exactly."""
    preimage = "%s%s%s%s%s%s" % (
        entry["entry_id"],
        entry["record_type"],
        entry["record_ref_id"],
        entry["record_payload_hash"],
        entry["prev_hash"],
        entry["timestamp"],
    )
    return hashlib.sha256(preimage.encode("utf-8")).hexdigest()


def compute_entry_hash_v2(entry):
    """Must match StreamCtx ledger hashing schema 1.1 / hash_version 2."""
    signed = {}
    for key in SIGNED_ENTRY_KEYS_V2:
        signed[key] = entry.get(key)
    signed["hash_version"] = int(signed["hash_version"])
    signed["entry_id"] = int(signed["entry_id"])
    signed["record_ref_id"] = int(signed["record_ref_id"])
    signed["applied"] = _json_bool(signed["applied"])
    signed["resolved"] = _json_bool(signed["resolved"])
    signed["dry_run"] = _json_bool(signed["dry_run"])
    return hashlib.sha256(canonical_json(signed).encode("utf-8")).hexdigest()


def compute_entry_hash(entry):
    version = int(entry.get("hash_version") or 1)
    if version == 1:
        return compute_entry_hash_v1(entry)
    return compute_entry_hash_v2(entry)


def public_key_id_from_pem(pem_text):
    return hashlib.sha256(pem_text.encode("ascii")).hexdigest()


def load_bundle(path):
    try:
        handle = open(path, "r", encoding="utf-8")
    except OSError as exc:
        return None, "Cannot read %s: %s" % (path, exc)
    try:
        raw = handle.read()
    finally:
        handle.close()
    if not raw.strip():
        return None, "%s is empty. An attestation bundle must be a JSON object." % path
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, (
            "%s is not valid JSON (%s). "
            "Re-export the bundle or confirm the file was not truncated."
            % (path, exc)
        )
    if not isinstance(data, dict):
        return None, (
            "%s must contain a JSON object at the top level, not a %s."
            % (path, type(data).__name__)
        )
    return data, None


def load_embedded_public_key(pem_text):
    if not isinstance(pem_text, str) or "BEGIN PUBLIC KEY" not in pem_text:
        return None, (
            "public_key_pem is missing or not a PEM public key. "
            "The bundle must include the issuer's Ed25519 public key."
        )
    try:
        key = load_pem_public_key(pem_text.encode("ascii"))
    except Exception as exc:
        return None, "public_key_pem could not be parsed as a public key: %s" % exc
    if not isinstance(key, Ed25519PublicKey):
        return None, (
            "public_key_pem is not an Ed25519 public key. "
            "This verifier only accepts Ed25519."
        )
    return key, None


def load_pem_file(path):
    try:
        pem_text = open(path, "r", encoding="ascii").read()
    except OSError as exc:
        return None, None, "Cannot read public key %s: %s" % (path, exc)
    key, error = load_embedded_public_key(pem_text)
    if error:
        return None, None, error
    return key, pem_text, None


def verify_entry_signature(entry_hash, signature_b64, public_key):
    try:
        digest = bytes.fromhex(entry_hash)
    except (ValueError, TypeError):
        return False, "entry_hash is not valid hex"
    try:
        signature = base64.b64decode(signature_b64, validate=True)
    except (ValueError, TypeError):
        return False, "signature is not valid base64"
    try:
        public_key.verify(signature, digest)
        return True, None
    except InvalidSignature:
        return False, "Ed25519 signature does not match the public key"
    except Exception as exc:
        return False, "signature check failed: %s" % exc


def repair_summary_from_entries(entries):
    applied = 0
    verified_not = 0
    unresolved = 0
    for entry in entries:
        if entry.get("record_type") != "repair":
            continue
        disp = entry.get("repair_disposition")
        if disp == "applied":
            applied += 1
        elif disp == "verified_not_applied":
            verified_not += 1
        elif disp == "unresolved_not_applied":
            unresolved += 1
    return {
        "repair_entries": applied + verified_not + unresolved,
        "applied_count": applied,
        "verified_not_applied_count": verified_not,
        "unresolved_not_applied_count": unresolved,
    }


def _schema_fail(message):
    return False, None, ["[FAIL] schema — %s" % message], [], False


def check_bundle(bundle, pinned_pems=None, require_pin=False):
    """Return (ok, broken_at_entry_id, lines, verbose_lines, pinned)."""
    lines = []
    verbose_lines = []
    pinned_pems = pinned_pems or []

    schema = bundle.get("schema_version")
    if schema not in SUPPORTED_SCHEMAS:
        return _schema_fail(
            "unsupported schema_version %r (this script supports %s)"
            % (schema, ", ".join(SUPPORTED_SCHEMAS))
        )

    required = (
        REQUIRED_BUNDLE_FIELDS_1_1 if schema == "1.1" else REQUIRED_BUNDLE_FIELDS_1_0
    )
    missing = [field for field in required if field not in bundle]
    if missing:
        return _schema_fail("bundle is missing required field(s): %s" % ", ".join(missing))
    if bundle["issuer"] != EXPECTED_ISSUER:
        return _schema_fail(
            "unexpected issuer %r (expected %r)" % (bundle["issuer"], EXPECTED_ISSUER)
        )
    lines.append("[PASS] schema %s" % schema)

    public_key, key_error = load_embedded_public_key(bundle["public_key_pem"])
    if key_error:
        return False, None, lines + ["[FAIL] public key — %s" % key_error], [], False
    lines.append("[PASS] public key (embedded)")

    pinned_ok = False
    if pinned_pems:
        embedded_id = public_key_id_from_pem(bundle["public_key_pem"])
        pinned_ids = [public_key_id_from_pem(pem) for pem in pinned_pems]
        bundle_keys = bundle.get("keys") if isinstance(bundle.get("keys"), dict) else {}
        used_ids = set()
        if isinstance(bundle.get("entries"), list):
            for entry in bundle["entries"]:
                if isinstance(entry, dict) and entry.get("key_id"):
                    used_ids.add(entry["key_id"])
        if not used_ids:
            used_ids.add(embedded_id)
        allowed = set(pinned_ids)
        for key_id, pem in bundle_keys.items():
            if pem in pinned_pems or public_key_id_from_pem(pem) in allowed:
                allowed.add(key_id)
                allowed.add(public_key_id_from_pem(pem))
        if not used_ids.issubset(allowed) or embedded_id not in allowed:
            return (
                False,
                None,
                lines
                + [
                    "[FAIL] authenticity — embedded/used key does not match "
                    "--public-key pin"
                ],
                [],
                False,
            )
        pinned_ok = True
        lines.append("[PASS] authenticity (pinned public key)")
    elif require_pin:
        return (
            False,
            None,
            lines
            + [
                "[FAIL] authenticity — --require-pin was set but no --public-key "
                "was provided"
            ],
            [],
            False,
        )
    else:
        lines.append(
            "[WARN] authenticity — public key was not pinned; this bundle is "
            "self-consistent but a third party cannot tell who signed it. "
            "Re-run with --public-key <issuer.pem>."
        )

    entries = bundle["entries"]
    if not isinstance(entries, list):
        return (
            False,
            None,
            lines + ["[FAIL] schema — 'entries' must be a JSON array"],
            [],
            pinned_ok,
        )

    entry_fields = (
        REQUIRED_ENTRY_FIELDS_1_1 if schema == "1.1" else REQUIRED_ENTRY_FIELDS_1_0
    )

    if not entries:
        if bundle["chain_root_hash"] not in (None, "") or bundle["chain_tip_hash"] not in (
            None,
            "",
        ):
            return (
                False,
                None,
                lines
                + [
                    "[FAIL] chain markers — empty bundle must have null "
                    "chain_root_hash and chain_tip_hash"
                ],
                [],
                pinned_ok,
            )
        if schema == "1.1":
            expected_summary = repair_summary_from_entries([])
            if bundle.get("repair_summary") != expected_summary:
                return (
                    False,
                    None,
                    lines
                    + [
                        "[FAIL] repair_summary — does not match recomputed counts %s"
                        % expected_summary
                    ],
                    [],
                    pinned_ok,
                )
        lines.append("[PASS] entry hashes (0 entries)")
        lines.append("[PASS] chain linkage (0 entries)")
        lines.append("[PASS] signatures (0 entries)")
        lines.append("[PASS] chain_root_hash")
        lines.append("[PASS] chain_tip_hash")
        return True, None, lines, ["(bundle contains no entries)"], pinned_ok

    keyring = {}
    bundle_keys = bundle.get("keys") if isinstance(bundle.get("keys"), dict) else {}
    for key_id, pem in bundle_keys.items():
        key, error = load_embedded_public_key(pem)
        if error:
            return (
                False,
                None,
                lines + ["[FAIL] keys — key_id=%s: %s" % (key_id, error)],
                [],
                pinned_ok,
            )
        keyring[key_id] = key
    keyring.setdefault(public_key_id_from_pem(bundle["public_key_pem"]), public_key)

    if pinned_pems:
        pinned_keys = []
        for pem in pinned_pems:
            key, error = load_embedded_public_key(pem)
            if error:
                return False, None, lines + ["[FAIL] public key — %s" % error], [], False
            pinned_keys.append(key)
            keyring[public_key_id_from_pem(pem)] = key
    else:
        pinned_keys = None

    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            return (
                False,
                None,
                lines + ["[FAIL] schema — entries[%s] is not an object" % index],
                verbose_lines,
                pinned_ok,
            )
        missing_entry = [field for field in entry_fields if field not in entry]
        if missing_entry:
            return (
                False,
                entry.get("entry_id"),
                lines
                + [
                    "[FAIL] schema — entry %s is missing field(s): %s"
                    % (entry.get("entry_id", "entries[%s]" % index), ", ".join(missing_entry))
                ],
                verbose_lines,
                pinned_ok,
            )
        if entry["record_type"] not in ALLOWED_RECORD_TYPES:
            return (
                False,
                entry["entry_id"],
                lines
                + [
                    "[FAIL] schema — entry_id=%s has invalid record_type %r"
                    % (entry["entry_id"], entry["record_type"])
                ],
                verbose_lines,
                pinned_ok,
            )
        if schema == "1.1" and entry.get("repair_disposition") not in ALLOWED_DISPOSITIONS:
            return (
                False,
                entry["entry_id"],
                lines
                + [
                    "[FAIL] schema — entry_id=%s has invalid repair_disposition %r"
                    % (entry["entry_id"], entry.get("repair_disposition"))
                ],
                verbose_lines,
                pinned_ok,
            )
        if schema == "1.1" and entry["record_type"] == "repair":
            applied = _json_bool(entry.get("applied"))
            resolved = _json_bool(entry.get("resolved"))
            disp = entry.get("repair_disposition")
            if applied is True and disp != "applied":
                return (
                    False,
                    entry["entry_id"],
                    lines
                    + [
                        "[FAIL] disposition — entry_id=%s: applied=true must use "
                        "repair_disposition='applied'" % entry["entry_id"]
                    ],
                    verbose_lines,
                    pinned_ok,
                )
            if applied is not True and disp == "applied":
                return (
                    False,
                    entry["entry_id"],
                    lines
                    + [
                        "[FAIL] disposition — entry_id=%s: repair_disposition="
                        "'applied' requires applied=true" % entry["entry_id"]
                    ],
                    verbose_lines,
                    pinned_ok,
                )
            if (
                applied is not True
                and resolved is True
                and disp != "verified_not_applied"
            ):
                return (
                    False,
                    entry["entry_id"],
                    lines
                    + [
                        "[FAIL] disposition — entry_id=%s: resolved=true and "
                        "applied=false must be 'verified_not_applied' (do not "
                        "read resolved as applied)" % entry["entry_id"]
                    ],
                    verbose_lines,
                    pinned_ok,
                )

        recomputed = compute_entry_hash(entry)
        if recomputed != entry["entry_hash"]:
            return (
                False,
                entry["entry_id"],
                lines
                + [
                    "[FAIL] entry hashes — entry_id=%s: recomputed entry_hash "
                    "does not match the value stored in the bundle"
                    % entry["entry_id"]
                ],
                verbose_lines,
                pinned_ok,
            )
        verbose_lines.append(
            "  entry_id=%s type=%s ref=%s disposition=%s applied=%s resolved=%s hash=OK"
            % (
                entry["entry_id"],
                entry["record_type"],
                entry["record_ref_id"],
                entry.get("repair_disposition"),
                entry.get("applied"),
                entry.get("resolved"),
            )
        )

        if schema == "1.1":
            if index == 0:
                if entry["session_prev_hash"] != GENESIS_HASH:
                    return (
                        False,
                        entry["entry_id"],
                        lines
                        + [
                            "[FAIL] session chain — first bundled entry_id=%s "
                            "must have session_prev_hash equal to genesis"
                            % entry["entry_id"]
                        ],
                        verbose_lines,
                        pinned_ok,
                    )
            elif entry["session_prev_hash"] != entries[index - 1]["entry_hash"]:
                return (
                    False,
                    entry["entry_id"],
                    lines
                    + [
                        "[FAIL] session chain — entry_id=%s: session_prev_hash "
                        "does not equal the previous bundled entry's entry_hash"
                        % entry["entry_id"]
                    ],
                    verbose_lines,
                    pinned_ok,
                )
        elif index > 0 and entry["prev_hash"] != entries[index - 1]["entry_hash"]:
            return (
                False,
                entry["entry_id"],
                lines
                + [
                    "[FAIL] chain linkage — entry_id=%s: prev_hash does not "
                    "equal the previous entry's entry_hash (entries may have "
                    "been reordered or a link was altered)"
                    % entry["entry_id"]
                ],
                verbose_lines,
                pinned_ok,
            )

        verify_keys = []
        key_id = entry.get("key_id")
        if key_id and key_id in keyring:
            verify_keys.append(keyring[key_id])
        verify_keys.append(public_key)
        if pinned_keys:
            verify_keys = list(pinned_keys) + verify_keys
        ok_sig = False
        sig_error = "Ed25519 signature does not match any candidate public key"
        seen = set()
        for candidate in verify_keys:
            ident = id(candidate)
            if ident in seen:
                continue
            seen.add(ident)
            ok_sig, sig_error = verify_entry_signature(
                entry["entry_hash"], entry["signature"], candidate
            )
            if ok_sig:
                break
        if not ok_sig:
            return (
                False,
                entry["entry_id"],
                lines
                + [
                    "[FAIL] signatures — entry_id=%s: %s"
                    % (entry["entry_id"], sig_error)
                ],
                verbose_lines,
                pinned_ok,
            )
        verbose_lines[-1] += " sig=OK"

    lines.append("[PASS] entry hashes")
    if schema == "1.1":
        lines.append("[PASS] session chain linkage")
    else:
        lines.append("[PASS] chain linkage")
    lines.append("[PASS] signatures")

    first_hash = entries[0]["entry_hash"]
    last_hash = entries[-1]["entry_hash"]
    if bundle["chain_root_hash"] != first_hash:
        return (
            False,
            entries[0]["entry_id"],
            lines
            + [
                "[FAIL] chain_root_hash — does not match entry_hash of the "
                "first entry (entry_id=%s)" % entries[0]["entry_id"]
            ],
            verbose_lines,
            pinned_ok,
        )
    lines.append("[PASS] chain_root_hash")
    if bundle["chain_tip_hash"] != last_hash:
        return (
            False,
            entries[-1]["entry_id"],
            lines
            + [
                "[FAIL] chain_tip_hash — does not match entry_hash of the "
                "last entry (entry_id=%s)" % entries[-1]["entry_id"]
            ],
            verbose_lines,
            pinned_ok,
        )
    lines.append("[PASS] chain_tip_hash")

    if schema == "1.1":
        expected_summary = repair_summary_from_entries(entries)
        if bundle.get("repair_summary") != expected_summary:
            return (
                False,
                None,
                lines
                + [
                    "[FAIL] repair_summary — bundle claims %s but entries recompute %s"
                    % (bundle.get("repair_summary"), expected_summary)
                ],
                verbose_lines,
                pinned_ok,
            )
        lines.append(
            "[PASS] repair_summary applied=%s verified_not_applied=%s "
            "unresolved_not_applied=%s"
            % (
                expected_summary["applied_count"],
                expected_summary["verified_not_applied_count"],
                expected_summary["unresolved_not_applied_count"],
            )
        )
        if expected_summary["applied_count"] == 0:
            lines.append(
                "[NOTE] no bundled repair was applied to a live session "
                "(Layer 3 verify_fix is counterfactual; resolved≠applied)"
            )

    return True, None, lines, verbose_lines, pinned_ok


def format_report(
    path, bundle, ok, broken_at, lines, verbose_lines, verbose, pinned_ok
):
    out = []
    out.append("StreamCtx attestation verification")
    out.append("file: %s" % path)
    if isinstance(bundle, dict):
        out.append("schema_version: %s" % bundle.get("schema_version", "(missing)"))
        out.append("issuer: %s" % bundle.get("issuer", "(missing)"))
        out.append("session_id: %s" % bundle.get("session_id", "(missing)"))
        entries = bundle.get("entries")
        if isinstance(entries, list):
            out.append("entries: %s" % len(entries))
        summary = bundle.get("repair_summary")
        if isinstance(summary, dict):
            out.append(
                "repair_summary: applied=%s verified_not_applied=%s unresolved_not_applied=%s"
                % (
                    summary.get("applied_count"),
                    summary.get("verified_not_applied_count"),
                    summary.get("unresolved_not_applied_count"),
                )
            )
    out.append("")
    out.extend(lines)
    if verbose and verbose_lines:
        out.append("")
        out.append("entries checked:")
        out.extend(verbose_lines)
    out.append("")
    if ok:
        if pinned_ok:
            out.append("VERDICT: PASS")
            out.append("AUTHENTICITY: PINNED")
        else:
            out.append("VERDICT: PASS")
            out.append("AUTHENTICITY: UNPINNED")
        out.append(
            "The hash chain and Ed25519 signatures are intact. "
            "The records in this bundle have not been altered since they were signed."
        )
        out.append(
            "resolved=true is shadow verification only; applied=true is the only "
            "signal that a candidate was written into a live session."
        )
    else:
        out.append("VERDICT: FAIL")
        if broken_at is not None:
            out.append("broken_at_entry_id: %s" % broken_at)
        out.append(
            "Do not trust this bundle. Treat the corresponding attribution/"
            "repair records as unverified."
        )
    return "\n".join(out)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Verify a StreamCtx attestation bundle offline. "
            "Does not contact StreamCtx or require the streamctx package."
        )
    )
    parser.add_argument(
        "bundle",
        help="Path to the exported attestation JSON file",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Print every entry that was checked",
    )
    parser.add_argument(
        "--public-key",
        action="append",
        default=[],
        dest="public_keys",
        help=(
            "Issuer Ed25519 public key PEM (repeatable). Pins authenticity so a "
            "self-consistent bundle minted with a foreign keypair is rejected."
        ),
    )
    parser.add_argument(
        "--require-pin",
        action="store_true",
        help="Fail if --public-key is not supplied",
    )
    args = parser.parse_args(argv)

    bundle, error = load_bundle(args.bundle)
    if error:
        sys.stdout.write("StreamCtx attestation verification\n")
        sys.stdout.write("file: %s\n\n" % args.bundle)
        sys.stdout.write("[FAIL] load — %s\n\n" % error)
        sys.stdout.write("VERDICT: FAIL\n")
        return 1

    pinned_pems = []
    for path in args.public_keys:
        _key, pem, key_error = load_pem_file(path)
        if key_error:
            sys.stdout.write("StreamCtx attestation verification\n")
            sys.stdout.write("file: %s\n\n" % args.bundle)
            sys.stdout.write("[FAIL] public key — %s\n\n" % key_error)
            sys.stdout.write("VERDICT: FAIL\n")
            return 1
        pinned_pems.append(pem)

    ok, broken_at, lines, verbose_lines, pinned_ok = check_bundle(
        bundle, pinned_pems=pinned_pems, require_pin=args.require_pin
    )
    sys.stdout.write(
        format_report(
            args.bundle,
            bundle,
            ok,
            broken_at,
            lines,
            verbose_lines,
            args.verbose,
            pinned_ok,
        )
    )
    sys.stdout.write("\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
