# Import a ledger from CSV

`scripts/import_ledger.py` turns two explicitly formatted CSV files into a
`flow-case/v1` input. It invokes the actual Rust solver before publishing the
result: invalid timestamps, unfunded debits, unmatched closing balances or solver
limits cause failure with no output case.

This is a strict Tracebound interchange format. It does not recognize arbitrary
bank exports, infer missing accounts, convert currencies or guess column mappings.

## Use the included real command path

```sh
cargo build --release --locked
python3 scripts/import_ledger.py \
  --accounts examples/csv/accounts.csv \
  --transfers examples/csv/transfers.csv \
  --seed commingled-payments-02 \
  --targets commingled-payments-03,commingled-payments-08,commingled-payments-09 \
  --case-id commingled-payments \
  --attest-complete \
  --output imported-case.json

target/release/tracebound analyze \
  --input imported-case.json --output imported-report.json
```

The two CSV fixtures are derived directly from
[`examples/commingled-payments.json`](../examples/commingled-payments.json).
They contain original synthetic data. Tests compare the imported JSON and the
actual Rust target reports against that original case, including its witnesses
and certificates.

The importer prints counts and SHA-256 hashes for both source CSV files, the exact
validated JSON bytes and the solver report. Existing output files and dangling
symlinks are refused. A private temporary file is flushed and published with an
exclusive, atomic hard-link operation on the destination filesystem. If that
filesystem does not support hard links, import fails; there is no overwrite
fallback. On Unix, the resulting file has owner-only `0600` permissions.

## Accounts file

Required column names:

```csv
account,opening_minor,closing_minor
source,10000,0
recipient,0,10000
```

Include every account referenced by a transfer, with explicit opening and closing
balances, including zero balances. No missing counterparty is assigned an invented
balance. Account IDs are case-sensitive and contain 1–64 ASCII letters, digits,
underscores, hyphens, dots or colons.

## Transfers file

Required column names:

```csv
id,timestamp,sequence,from_account,to_account,amount_minor,currency
payment-1,2026-01-15T09:05:00Z,1,source,recipient,10000,EUR
```

- IDs must be unique and use the same identifier character set as accounts.
- `timestamp` must be an RFC3339 instant accepted by the Rust parser. The CSV
  importer preserves its text and precision; it does not pass through a floating
  point or Python date conversion.
- `sequence` is a positive, unique unsigned 64-bit posting sequence. It determines
  chronology even when timestamps tie. Timestamps must not move backwards in that
  sequence. A sequence supplied by the operator is not independently authenticated.
- `amount_minor` is a positive unsigned 64-bit integer. Opening/closing amounts
  may also be zero. All integer fields use ordinary decimal digits with no leading
  zeros except the value `0`. Signs, decimal points, scientific notation and
  thousands separators are rejected. For example, where the currency convention
  is cents, 123.45 major units must already be represented as `12345`.
- Every transfer must use the same three-uppercase-letter currency code. This
  checks syntax and separation, not membership of an ISO registry. The operator
  must define the correct minor-unit convention; no exchange rates are applied.

Header order may vary, but every required column must appear exactly once. Extra
columns, duplicate headers, blank rows, missing values, malformed quotes, repeated
accounts/transfers/sequences, negative amounts and mixed currencies are rejected.
Input must be UTF-8; an initial UTF-8 BOM and LF or CRLF line endings are supported.

Both CSV files together are capped at 2 MiB, with at most 128 accounts, 1,000
transfers and 16 selected targets. The normalized JSON also must fit the native
solver's 2 MiB input cap. A supplied `--binary` must be a trusted, locally built
Tracebound executable; the importer does not authenticate arbitrary executables.

## Completeness is an explicit assumption

`--attest-complete` is required. It means the submitter asserts that the chosen
account boundary, opening/closing cutoffs and posted movements are complete for
this model. Successful reconciliation cannot prove that assertion: omitted
offsetting movements can leave the same closing balances.

Do not silently drop fees, external movements, reversals, pending entries or
currency conversions to make a file pass. Normalize them according to the
documented model and an authorized source process first. Bounds remain conditional
allocations, not actual fund provenance, probabilities or proof of a crime.

## Reproduce the import tests

```sh
cargo build --release --locked
python3 -m unittest discover -s tests -p test_import_ledger.py -v
```

These invoke the importer and the compiled Rust binary. They cover exact fixture
round trips, source hashes, UTF-8 BOMs, reordered headers, malformed CSV, strict
integers, currencies, missing balances, failed reconciliation, resource limits,
private-value diagnostics and no-clobber output behavior. They require the native
binary and do not substitute a mocked successful response.
