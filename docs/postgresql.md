# PostgreSQL snapshot integration

Tracebound includes a real, local PostgreSQL integration lab. It imports an
original synthetic case into relational tables, exports a consistent snapshot,
and runs the Rust solver on that export. The result must match the original
case's complete target reports, including bounds, witnesses and certificates.

This is a reproducible adapter example. It is not a production connector, an
installed bank integration, change-data capture, or a validation of any real
institution's records.

## Run it

Use an existing, trusted PostgreSQL installation and a built native binary:

```sh
cargo build --release --locked
python3 scripts/postgres_lab.py \
  --binary target/release/tracebound \
  --case examples/commingled-payments.json \
  --output postgres-observation.json
```

`--output` must name a new file. It contains the observed PostgreSQL version,
case/binary/export hashes, row counts, snapshot checks, target bounds and cleanup
status. Without it, the same JSON is written only to stdout. Runtime includes
database initialization, SQL, solver execution and shutdown; it is not a solver
throughput measurement.

On Apple Silicon, the default installation is
`/opt/homebrew/opt/postgresql@15/bin` when present. Otherwise the script uses
`pg_config --bindir`. Select another existing installation explicitly with
`--bindir /usr/lib/postgresql/16/bin`, for example. PostgreSQL 15 or newer and a
Unix environment with local sockets are required. No packages are installed.

## Relational data model

The complete DDL, loader and export query are versioned in
[`scripts/postgres_lab.py`](../scripts/postgres_lab.py), as `SQL_SCHEMA`,
`load_sql` and `SQL_EXPORT`.

| Table | Meaning | Enforced constraints |
| --- | --- | --- |
| `case_metadata` | Currency, schema version, submitter attestation and seed ID | Case primary key, currency syntax, deferred seed foreign key |
| `account_openings` | Explicit opening balance per case/account | Composite primary key, exact unsigned integer amount |
| `account_closings` | Explicit closing balance per case/account | Composite primary key and opening-account foreign key |
| `transfers` | Posted time, trusted posting sequence, sender, recipient and amount | Unique transfer ID and sequence; both accounts must exist; positive integer amounts; no self-transfer |
| `case_targets` | Analyst-selected target transfers | Target foreign key and unique ordinal |
| `snapshot_probe` | Synthetic concurrency control used only by this lab | Single control row |

Amounts and posting sequences use PostgreSQL `numeric` with explicit integral
and unsigned 64-bit range checks. They are not converted to floating point.
Values enter through CSV `COPY FROM STDIN`; source values are never interpolated
into SQL statements. The complete case is loaded in one transaction. The Rust
parser validates the original input before database initialization, then validates
and reconciles the exported case again.

`posted_at` is a `timestamptz`. Export renders UTC with six fractional digits.
The adapter rejects meaningful precision beyond microseconds and leap-second
timestamps, rather than silently changing their meaning. Arrays are ordered by
account, posting sequence or target ordinal. Posting sequence remains explicit;
database row order, timestamp ties and generated surrogate IDs do not establish
the posting order.

## Consistent, read-only extraction

The export connection uses a dedicated role granted only schema access and table
`SELECT`. Its transaction begins explicitly:

```sql
BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL timezone = 'UTC';
SET LOCAL statement_timeout = '20s';

SELECT current_setting('transaction_isolation'),
       current_setting('transaction_read_only'),
       pg_current_snapshot();

-- SQL_EXPORT builds one flow-case/v1 document from the normalized tables.
-- All opening/closing rows, transfers, metadata and targets share this snapshot.

COMMIT;
```

The lab verifies the transaction settings and demonstrates isolation with a
separate connection: after the reader establishes its snapshot, the writer
commits an update to the synthetic probe. The original transaction must continue
to see the old value and the same snapshot, while a fresh transaction must see the
new value. A separate attempted write in a read-only transaction must fail with
SQLSTATE `25006`. The probe does not modify the case or transfer tables.

PostgreSQL Repeatable Read supplies a stable transaction snapshot; it does not
prove the accuracy or completeness of the records being read. See the official
[transaction isolation documentation](https://www.postgresql.org/docs/15/transaction-iso.html)
and [transaction characteristics](https://www.postgresql.org/docs/15/sql-set-transaction.html).

## Isolation and cleanup of the lab

The script creates a new private directory under `/tmp`, initializes its own
database cluster, and sets `listen_addresses = ''`. It connects only through that
cluster's private Unix socket. Socket/directory permissions are `0700`; the local
trust authentication used for this disposable cluster relies on that boundary.

All inherited environment variables whose names start with `PG` are discarded.
Every connection supplies its socket, port, user and database explicitly. `psql
-X` ignores user startup files, and `-w` prevents password prompts. No DSN, server
hostname or existing cluster path is accepted. The script changes configuration
only in the cluster it just created.

Shutdown runs in `finally`, including after a failed export or comparison. It
requests a fast stop, falls back to immediate stop if necessary, and checks that
the dedicated cluster is stopped before deleting its temporary directory. If
shutdown cannot be confirmed, it retains the directory and raises an error for
inspection. It never stops a shared PostgreSQL service.

## What an actual source integration still requires

A stable snapshot and matching balances do not establish completeness. For
example, an omitted pair of offsetting transfers can leave closing balances
unchanged. The `completeness_attested` field is supplied by an authorized operator;
this lab does not convert it into an independently proven fact.

A real adapter must define the account boundary, opening/closing cutoffs, trusted
posting sequence, currency/minor-unit convention and treatment of fees, external
movements, reversals and pending entries. It also needs an authorized read-only
connection, access controls, retention policy and operational testing appropriate
to that source. Those tasks are outside this synthetic lab. Tracebound's output
remains conditional flow bounds rather than actual provenance or proof of crime.
