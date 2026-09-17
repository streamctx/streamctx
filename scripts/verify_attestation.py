"""Offline verifier for a StreamCtx compliance attestation bundle.

This script has no StreamCtx dependency.  It only needs the Python
standard library plus the `cryptography` package.

Usage:
    python verify_attestation.py bundle.json
    python verify_attestation.py bundle.json --verbose

Exit codes:
    0  every check passed
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

REQUIRED_BUNDLE_FIELDS = (
    "schema_version",
    "issuer",
    "session_id",
    "exported_at",
    "public_key_pem",
    "entries",
    "chain_root_hash",
    "chain_tip_hash",
)

REQUIRED_ENTRY_FIELDS = (
    "entry_id",
    "record_type",
    "record_ref_id",
    "record_payload_hash",
    "prev_hash",
    "entry_hash",
    "timestamp",
    "signature",
)

SUPPORTED_SCHEMA = "1.0"
EXPECTED_ISSUER = "streamctx"
ALLOWED_RECORD_TYPES = ("attribution", "repair")


def compute_entry_hash(entry):
    """Must match StreamCtx ledger hashing exactly."""
    preimage = (
        "%s%s%s%s%s%s"
        % (
            entry["entry_id"],
            entry["record_type"],
            entry["record_ref_id"],
            entry["record_payload_hash"],
            entry["prev_hash"],
            entry["timestamp"],
        )
    )
    return hashlib.sha256(preimage.encode("utf-8")).hexdigest()


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
            "This verifier only accepts Ed25519 (schema_version 1.0)."
        )
    return key, None


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
        return False, "Ed25519 signature does not match public_key_pem"
    except Exception as exc:
        return False, "signature check failed: %s" % exc


def check_bundle(bundle):
    """Return (ok, broken_at_entry_id, lines, verbose_lines)."""
    lines = []
    verbose_lines = []
    missing = [field for field in REQUIRED_BUNDLE_FIELDS if field not in bundle]
    if missing:
        return (
            False,
            None,
            [
                "[FAIL] schema — bundle is missing required field(s): %s"
                % ", ".join(missing)
            ],
            [],
        )

    if bundle["schema_version"] != SUPPORTED_SCHEMA:
        return (
            False,
            None,
            [
                "[FAIL] schema — unsupported schema_version %r (this script supports %s)"
                % (bundle["schema_version"], SUPPORTED_SCHEMA)
            ],
            [],
        )
    if bundle["issuer"] != EXPECTED_ISSUER:
        return (
            False,
            None,
            [
                "[FAIL] schema — unexpected issuer %r (expected %r)"
                % (bundle["issuer"], EXPECTED_ISSUER)
            ],
            [],
        )
    lines.append("[PASS] schema")

    public_key, key_error = load_embedded_public_key(bundle["public_key_pem"])
    if key_error:
        return False, None, lines + ["[FAIL] public key — %s" % key_error], []
    lines.append("[PASS] public key")

    entries = bundle["entries"]
    if not isinstance(entries, list):
        return (
            False,
            None,
            lines + ["[FAIL] schema — 'entries' must be a JSON array"],
            [],
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
            )
        lines.append("[PASS] entry hashes (0 entries)")
        lines.append("[PASS] chain linkage (0 entries)")
        lines.append("[PASS] signatures (0 entries)")
        lines.append("[PASS] chain_root_hash")
        lines.append("[PASS] chain_tip_hash")
        return True, None, lines, ["(bundle contains no entries)"]

    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            return (
                False,
                None,
                lines
                + [
                    "[FAIL] schema — entries[%s] is not an object" % index
                ],
                verbose_lines,
            )
        missing_entry = [field for field in REQUIRED_ENTRY_FIELDS if field not in entry]
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
            )
        verbose_lines.append(
            "  entry_id=%s type=%s ref=%s hash=OK"
            % (entry["entry_id"], entry["record_type"], entry["record_ref_id"])
        )

        if index > 0 and entry["prev_hash"] != entries[index - 1]["entry_hash"]:
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
            )

        ok_sig, sig_error = verify_entry_signature(
            entry["entry_hash"], entry["signature"], public_key
        )
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
            )
        verbose_lines[-1] += " sig=OK"

    lines.append("[PASS] entry hashes")
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
        )
    lines.append("[PASS] chain_tip_hash")
    return True, None, lines, verbose_lines


def format_report(path, bundle, ok, broken_at, lines, verbose_lines, verbose):
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
    out.append("")
    out.extend(lines)
    if verbose and verbose_lines:
        out.append("")
        out.append("entries checked:")
        out.extend(verbose_lines)
    out.append("")
    if ok:
        out.append("VERDICT: PASS")
        out.append(
            "The hash chain and Ed25519 signatures are intact. "
            "The records in this bundle have not been altered since they were signed."
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
    args = parser.parse_args(argv)

    bundle, error = load_bundle(args.bundle)
    if error:
        sys.stdout.write("StreamCtx attestation verification\n")
        sys.stdout.write("file: %s\n\n" % args.bundle)
        sys.stdout.write("[FAIL] load — %s\n\n" % error)
        sys.stdout.write("VERDICT: FAIL\n")
        return 1

    ok, broken_at, lines, verbose_lines = check_bundle(bundle)
    sys.stdout.write(
        format_report(
            args.bundle, bundle, ok, broken_at, lines, verbose_lines, args.verbose
        )
    )
    sys.stdout.write("\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
