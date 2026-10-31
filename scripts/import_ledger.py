#!/usr/bin/env python3
"""Strict integer-minor-unit CSV import, validated by the native Rust solver.

This accepts the documented Tracebound CSV schema, not arbitrary bank exports.
Output is published only after reconciliation and analysis succeed in Rust.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile


MAX_BYTES = 2 * 1024 * 1024
MAX_ACCOUNTS = 128
MAX_TRANSFERS = 1000
MAX_TARGETS = 16
MAX_U64 = 18446744073709551615
ACCOUNT_HEADERS = ("account", "opening_minor", "closing_minor")
TRANSFER_HEADERS = ("id", "timestamp", "sequence", "from_account", "to_account", "amount_minor", "currency")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def identifier(value, field):
    require(re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", value) is not None, f"Invalid {field} identifier")
    return value


def integer(value, field, positive=False):
    require(len(value) <= 20 and re.fullmatch(r"0|[1-9][0-9]*", value) is not None, f"{field} must be a canonical unsigned integer in minor units")
    result = int(value)
    require(result <= MAX_U64 and (result > 0 or not positive), f"{field} is outside its permitted unsigned integer range")
    return result


def read_file(path):
    require(stat.S_ISREG(path.lstat().st_mode), "CSV input must be a regular non-symlink file")
    with path.open("rb") as source:
        data = source.read(MAX_BYTES + 1)
    require(len(data) <= MAX_BYTES, "CSV input exceeds 2 MiB")
    return data


def validate_quoting(text):
    """Reject stray/unclosed quotes that csv.reader(strict=True) alone permits."""
    state = "start"
    for character in text:
        if state == "quoted":
            if character == '"':
                state = "after_quote"
        elif state == "after_quote":
            if character == '"':
                state = "quoted"
            elif character == "," or character in "\r\n":
                state = "start"
            else:
                raise ValueError("Malformed CSV quoting")
        elif character == '"':
            require(state == "start", "Malformed CSV quoting")
            state = "quoted"
        elif character == "," or character in "\r\n":
            state = "start"
        else:
            state = "unquoted"
    require(state != "quoted", "Unterminated CSV quoted field")


def rows(data, headers, limit):
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("CSV input must be UTF-8; an initial UTF-8 BOM is supported") from None
    validate_quoting(text)
    prior_limit = csv.field_size_limit()
    csv.field_size_limit(256)
    try:
        reader = csv.reader(io.StringIO(text, newline=""), strict=True)
        actual = next(reader, None)
        require(actual is not None, "CSV header is missing")
        require(len(actual) == len(set(actual)), "Duplicate CSV header")
        require(len(actual) == len(headers) and set(actual) == set(headers), "CSV columns do not match the required schema")
        result = []
        for number, fields in enumerate(reader, start=2):
            require(len(result) < limit, "CSV row count exceeds the model limit")
            require(len(fields) == len(actual), f"CSV column count mismatch at row {number}")
            require(all(field != "" for field in fields), f"Missing CSV value at row {number}")
            result.append(dict(zip(actual, fields)))
        require(result, "CSV must contain at least one data row")
        return result
    except csv.Error:
        raise ValueError("Malformed CSV syntax or oversized field") from None
    finally:
        csv.field_size_limit(prior_limit)


def build_case(account_bytes, transfer_bytes, seed, targets, case_id, attest_complete):
    require(attest_complete is True, "Explicit complete-ledger attestation is required")
    require(len(account_bytes) + len(transfer_bytes) <= MAX_BYTES, "Combined CSV input exceeds 2 MiB")
    identifier(case_id, "case")
    identifier(seed, "seed")
    require(0 < len(targets) <= MAX_TARGETS and len(set(targets)) == len(targets), "Targets must contain 1..16 unique transfer IDs")
    for target in targets:
        identifier(target, "target")
    opening, closing, accounts = [], [], set()
    for row in rows(account_bytes, ACCOUNT_HEADERS, MAX_ACCOUNTS):
        account = identifier(row["account"], "account")
        require(account not in accounts, "Duplicate account row")
        accounts.add(account)
        opening.append({"account": account, "amount_minor": integer(row["opening_minor"], "opening_minor")})
        closing.append({"account": account, "amount_minor": integer(row["closing_minor"], "closing_minor")})
    transfers, ids, sequences, currencies = [], set(), set(), set()
    for row in rows(transfer_bytes, TRANSFER_HEADERS, MAX_TRANSFERS):
        transfer_id = identifier(row["id"], "transfer")
        require(transfer_id not in ids, "Duplicate transfer ID")
        ids.add(transfer_id)
        sequence = integer(row["sequence"], "sequence", positive=True)
        require(sequence not in sequences, "Duplicate posting sequence")
        sequences.add(sequence)
        sender = identifier(row["from_account"], "sender")
        recipient = identifier(row["to_account"], "recipient")
        require(sender in accounts and recipient in accounts, "Every counterparty needs explicit opening and closing balances")
        require(sender != recipient, "Self-transfers are unsupported")
        currency = row["currency"]
        require(re.fullmatch(r"[A-Z]{3}", currency) is not None, "Currency must have three uppercase ASCII letters")
        currencies.add(currency)
        require(len(currencies) == 1, "Mixed currencies are not supported; do not combine currency ledgers")
        timestamp = row["timestamp"]
        require(len(timestamp) <= 64 and timestamp.isascii() and not any(ord(c) < 32 for c in timestamp), "Invalid timestamp field")
        transfers.append({
            "id": transfer_id, "timestamp": timestamp, "sequence": sequence,
            "from_account": sender, "to_account": recipient,
            "amount_minor": integer(row["amount_minor"], "amount_minor", positive=True),
        })
    require(seed in ids and set(targets).issubset(ids), "Seed and target IDs must exist in the transfer CSV")
    return {
        "schema": "flow-case/v1", "case_id": case_id, "currency": next(iter(currencies)),
        "completeness_attested": True, "opening_balances": opening, "closing_balances": closing,
        "transfers": transfers, "seed_transfer_id": seed, "target_transfer_ids": targets,
    }


def validate_rust(data, binary):
    with tempfile.TemporaryDirectory(prefix="tracebound-import-") as temporary:
        source = Path(temporary) / "case.json"
        report = Path(temporary) / "report.json"
        source.write_bytes(data)
        source.chmod(0o600)
        result = subprocess.run([str(binary), "analyze", "--input", str(source), "--output", str(report)], capture_output=True, timeout=60)
        require(result.returncode == 0, "Rust reconciliation or analysis failed; no output case was written")
        try:
            summary = json.loads(result.stdout)
            with report.open("rb") as handle:
                report_bytes = handle.read(16 * 1024 * 1024 + 1)
            require(len(report_bytes) <= 16 * 1024 * 1024, "Rust report exceeds its resource limit")
            require(summary["report_sha256"] == digest(report_bytes), "Rust report hash mismatch")
            require(json.loads(report_bytes)["input_sha256"] == digest(data), "Rust report is not bound to the exact imported bytes")
        except (KeyError, TypeError, json.JSONDecodeError):
            raise ValueError("Native solver returned an invalid validation response") from None
        return summary["report_sha256"]


def publish_new(path, data):
    require(not os.path.lexists(path), "Refusing to overwrite an existing output")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".tracebound-import-", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        # A same-filesystem hard link is an atomic create-if-absent operation.
        # A concurrent file or dangling symlink cannot be overwritten or followed.
        os.link(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--accounts", type=Path, required=True)
    parser.add_argument("--transfers", type=Path, required=True)
    parser.add_argument("--seed", required=True)
    parser.add_argument("--targets", required=True, help="Comma-separated existing transfer IDs")
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--attest-complete", action="store_true", required=True)
    parser.add_argument("--binary", type=Path, default=Path("target/release/tracebound"))
    args = parser.parse_args()
    try:
        require(not os.path.lexists(args.output), "Refusing to overwrite an existing output")
        account_bytes, transfer_bytes = read_file(args.accounts), read_file(args.transfers)
        case = build_case(account_bytes, transfer_bytes, args.seed, [target.strip() for target in args.targets.split(",")], args.case_id, args.attest_complete)
        data = (json.dumps(case, indent=2) + "\n").encode()
        require(len(data) <= MAX_BYTES, "Normalized JSON exceeds the model input limit")
        report_hash = validate_rust(data, args.binary.resolve(strict=True))
        publish_new(args.output, data)
        print(json.dumps({
            "schema": "tracebound-csv-import/v1", "accounts": len(case["opening_balances"]),
            "transfers": len(case["transfers"]), "currency": case["currency"],
            "accounts_csv_sha256": digest(account_bytes), "transfers_csv_sha256": digest(transfer_bytes),
            "case_sha256": digest(data), "solver_report_sha256": report_hash, "validated_by_rust": True,
            "note": "Completeness is the submitter's attestation. This conversion does not prove source completeness or criminal activity.",
        }))
    except ValueError as error:
        print(f"Import rejected: {error}", file=sys.stderr)
        raise SystemExit(2) from None
    except (OSError, subprocess.TimeoutExpired):
        print("Import failed due to filesystem access or native-solver execution; no success is reported.", file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
