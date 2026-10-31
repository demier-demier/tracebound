"""Real CLI import tests using the compiled Rust solver, not a mock validator."""

import csv
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "import_ledger.py"
EXAMPLE = json.loads((ROOT / "examples" / "commingled-payments.json").read_text())
BINARY = Path(os.environ.get("TRACEBOUND_BINARY", ROOT / "target" / "release" / "tracebound")).resolve()


def csv_bytes(headers, rows):
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(headers)
    writer.writerows(rows)
    return buffer.getvalue().encode()


class ImportLedgerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not BINARY.is_file():
            raise RuntimeError("Build the real native solver with cargo build --release --locked first")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="tracebound-import-test-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.accounts = self.directory / "accounts.csv"
        self.transfers = self.directory / "transfers.csv"
        self.output = self.directory / "case.json"
        self.accounts.write_bytes((ROOT / "examples" / "csv" / "accounts.csv").read_bytes())
        self.transfers.write_bytes((ROOT / "examples" / "csv" / "transfers.csv").read_bytes())

    def run_import(self, attest=True):
        command = [
            sys.executable, str(SCRIPT), "--accounts", str(self.accounts),
            "--transfers", str(self.transfers), "--seed", EXAMPLE["seed_transfer_id"],
            "--targets", ",".join(EXAMPLE["target_transfer_ids"]),
            "--case-id", EXAMPLE["case_id"], "--output", str(self.output), "--binary", str(BINARY),
        ]
        if attest:
            command.append("--attest-complete")
        return subprocess.run(command, capture_output=True, timeout=65)

    def rejected(self, attest=True):
        result = self.run_import(attest=attest)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")
        self.assertFalse(self.output.exists())
        self.assertFalse(list(self.directory.glob(".tracebound-import-*")))
        return result

    def table(self, path):
        with path.open(newline="") as handle:
            values = list(csv.reader(handle))
        return values[0], values[1:]

    def analyze(self, path):
        result = subprocess.run([str(BINARY), "analyze", "--input", str(path)], capture_output=True, check=True, timeout=60)
        return json.loads(result.stdout)

    def test_exact_fixture_roundtrip_and_real_rust_targets(self):
        result = self.run_import()
        self.assertEqual(result.returncode, 0, result.stderr)
        data = self.output.read_bytes()
        self.assertEqual(json.loads(data), EXAMPLE)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["case_sha256"], hashlib.sha256(data).hexdigest())
        self.assertIs(summary["validated_by_rust"], True)
        self.assertEqual(self.analyze(self.output)["targets"], self.analyze(ROOT / "examples" / "commingled-payments.json")["targets"])
        if os.name == "posix":
            self.assertEqual(self.output.stat().st_mode & 0o777, 0o600)

    def test_utf8_bom_and_reordered_headers_preserve_values(self):
        headers, rows = self.table(self.accounts)
        self.accounts.write_bytes(b"\xef\xbb\xbf" + csv_bytes(headers[::-1], [row[::-1] for row in rows]))
        self.transfers.write_bytes(b"\xef\xbb\xbf" + self.transfers.read_bytes())
        result = self.run_import()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.output.read_bytes()), EXAMPLE)

    def test_missing_completeness_flag_does_not_write_case(self):
        self.rejected(attest=False)

    def test_missing_counterparty_is_not_given_an_invented_zero_balance(self):
        headers, rows = self.table(self.accounts)
        self.accounts.write_bytes(csv_bytes(headers, rows[1:]))
        self.rejected()

    def test_missing_balance_field_is_rejected(self):
        headers, rows = self.table(self.accounts)
        rows[0][2] = ""
        self.accounts.write_bytes(csv_bytes(headers, rows))
        self.rejected()

    def test_unreconciled_closing_is_rejected_by_rust_before_publication(self):
        headers, rows = self.table(self.accounts)
        rows[0][2] = str(int(rows[0][2]) + 1)
        self.accounts.write_bytes(csv_bytes(headers, rows))
        result = self.rejected()
        self.assertIn(b"Rust reconciliation or analysis failed", result.stderr)

    def test_integer_amounts_reject_floating_scientific_signed_and_negative_values(self):
        headers, original = self.table(self.transfers)
        for amount in ["1.0", "1e3", "+1", "-1", "-0", "01", "0", "18446744073709551616"]:
            with self.subTest(amount=amount):
                rows = [row[:] for row in original]
                rows[0][headers.index("amount_minor")] = amount
                self.transfers.write_bytes(csv_bytes(headers, rows))
                self.rejected()

    def test_negative_opening_is_rejected(self):
        headers, rows = self.table(self.accounts)
        rows[0][1] = "-1"
        self.accounts.write_bytes(csv_bytes(headers, rows))
        self.rejected()

    def test_malformed_currency_and_mixed_currency_are_rejected(self):
        headers, original = self.table(self.transfers)
        for value in ["USD", "eur", "EURO", "", "€"]:
            with self.subTest(currency=value):
                rows = [row[:] for row in original]
                rows[0][headers.index("currency")] = value
                self.transfers.write_bytes(csv_bytes(headers, rows))
                self.rejected()

    def test_duplicate_headers_and_unknown_columns_are_rejected(self):
        original = self.accounts.read_bytes()
        for header in [b"account,opening_minor,opening_minor", b"account,opening_minor,unknown"]:
            self.accounts.write_bytes(header + b"\n" + original.split(b"\n", 1)[1])
            self.rejected()

    def test_duplicate_rows_and_sequences_are_rejected(self):
        headers, original = self.table(self.transfers)
        for mode in ["duplicate-row", "duplicate-sequence"]:
            with self.subTest(mode=mode):
                rows = [row[:] for row in original]
                if mode == "duplicate-row":
                    rows.append(rows[0])
                else:
                    rows[1][headers.index("sequence")] = rows[0][headers.index("sequence")]
                self.transfers.write_bytes(csv_bytes(headers, rows))
                self.rejected()

    def test_malformed_quotes_columns_and_blank_rows_are_rejected(self):
        header = b"account,opening_minor,closing_minor\n"
        for body in [b'"unterminated,1,1\n', b'ab"cd,1,1\n', b'"abc"junk,1,1\n', b"abc,1\n", b"abc,1,1,extra\n", b"\n"]:
            with self.subTest(body=body):
                self.accounts.write_bytes(header + body)
                self.rejected()

    def test_parse_diagnostics_do_not_echo_private_values(self):
        sentinel = "private-secret-canary"
        headers, rows = self.table(self.accounts)
        rows[0][1] = sentinel
        self.accounts.write_bytes(csv_bytes(headers, rows))
        result = self.rejected()
        self.assertNotIn(sentinel.encode(), result.stderr)

    def test_input_size_and_row_count_limits(self):
        headers, original = self.table(self.accounts)
        for data in [
            b"x" * (2 * 1024 * 1024 + 1),
            csv_bytes(headers, [[f"account-{index}", "0", "0"] for index in range(129)]),
        ]:
            self.accounts.write_bytes(data)
            self.rejected()
        self.accounts.write_bytes(csv_bytes(headers, original))
        headers, rows = self.table(self.transfers)
        repeated = []
        for index in range(1001):
            row = rows[0][:]
            row[headers.index("id")] = f"transfer-{index}"
            row[headers.index("sequence")] = str(index + 1)
            repeated.append(row)
        self.transfers.write_bytes(csv_bytes(headers, repeated))
        self.rejected()

    def test_existing_output_remains_unchanged(self):
        self.output.write_bytes(b"existing evidence")
        result = self.run_import()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(self.output.read_bytes(), b"existing evidence")

    @unittest.skipUnless(os.name == "posix", "symlink behavior requires POSIX")
    def test_dangling_output_symlink_is_not_followed(self):
        target = self.directory / "must-not-create.json"
        self.output.symlink_to(target)
        result = self.run_import()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(target.exists())
        self.assertTrue(self.output.is_symlink())


if __name__ == "__main__":
    unittest.main()
