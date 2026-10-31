# Observed validation

These measurements describe the checked revision and original synthetic fixtures. They are not crime-detection accuracy, proof of source truth, or production-bank performance.

## Mathematical correctness

- 19 Rust unit tests cover forced/ambiguous bounds, splits/merges/cycles, chronology, tied timestamps with explicit order, full u64 inputs and larger intermediate balances, bounded decoding, work exhaustion, witness corruption and residual optimality certificates.
- 8 native CLI tests on this Mac cover exclusive evidence output, trusted-anchor mismatch, modified inputs, rehashed forged witnesses, strict report fields, oversized inputs and symlink rejection. The symlink test is Unix-only.
- The independently implemented Python oracle checked **355 valid cases, 1,710 exact target intervals and 3,420 complete witness replays**. It enumerates integer allocations directly rather than using the graph algorithm. All matched. Twelve invalid controls and two deliberately corrupted evidence controls were rejected.
- The corpus is deliberately small enough for exhaustive enumeration: the maximum observed reachable allocation-state count was nine. This finite validation is not a proof over all possible ledgers; the construction and residual proof are documented separately.
- Browser parity uses the same exact input bytes and compares complete native and WASM reports, including canonical exports. Forged reports and duplicate input fields are rejected by WASM as well.

Machine-readable observations: [oracle](../evidence/oracle.json), [native/WASM parity](../evidence/wasm-parity.json).

## Real integration and case workflow

The CSV importer passed 16 actual CLI tests, including exact fixture round-trip through Rust, BOM/reordered headers, malformed quoting, duplicate identifiers, unsafe monetary formats, missing accounts, reconciliation failure and no-clobber output. It accepts a documented normalized format, not arbitrary bank statements.

The workspace passed 14 actual HTTP/SQLite/native-Rust tests covering token/role checks, Origin/Host enforcement, read-only actors, revision conflicts, concurrent writers, stale analysis completion, separate review actors, approval freeze, restart persistence, immutable records, forged state, altered artifacts and malformed history.


The PostgreSQL 15.17 integration uses normalized tables in a new socket-only local cluster. In the recorded split/merge case, **8 accounts, 14 transfers and 4 target reports** round-tripped without changing the values, witnesses or certificates. A concurrent committed update remained hidden from the existing repeatable-read snapshot and became visible in a new transaction. Writes in the read-only transaction were rejected; cleanup stopped and removed the lab cluster. [Observed record](../evidence/postgresql.json).

The local workspace demonstration executed real HTTP requests, SQLite commits, native Rust analysis and certificate verification. Its eight-event case includes an initial submission, a review return, additional evidence notes and approval of a later revision. Stale writes, self-approval, auditor writes and modifications after approval were rejected. Reopening the database reproduced the exact dossier. Actors and decisions are scripted, not a claim of human review. [Observed HTTP workflow](../evidence/workspace.json) · [exported dossier](../web/evidence/demo-dossier.json).

## Native synthetic timing

Apple M4 Pro, macOS, arm64 release binary; seven consecutive fresh-process observations per generated ledger. Cache state was not controlled. The measurement includes process startup, file reading, graph construction, optimization, witness validation and full report serialization, but excludes database, browser, network and the separate certificate verifier. All individual observations, fixture seeds/hashes and binary digest are retained.

| Transfers | Accounts | Targets | Median wall time | Highest observed child RSS |
| --- | --- | --- | --- | --- |
| 100 | 16 | 16 | 4.69 ms | 3.91 MiB |
| 500 | 64 | 16 | 8.91 ms | 7.08 MiB |
| 1000 | 96 | 16 | 13.27 ms | 14.33 MiB |

[Raw benchmark](../evidence/benchmark.json). These random fixtures are not worst-case flow graphs. The largest case reaches the configured 1,000-transfer case limit, not an institution-scale dataset. Larger workloads require a separately validated case-selection/partitioning layer; no billion-transaction or competitor-superiority claim is made.

Reproduce with a new output path:

```sh
python3 scripts/benchmark.py --output /tmp/tracebound-new-benchmark.json
python3 scripts/verify_oracle.py --output /tmp/tracebound-new-oracle.json
```

## Still unvalidated

Real institution data and source completeness; investigator usefulness against an existing workflow; production identity/access controls; distributed/concurrent deployment; regulatory acceptance; incident response and recovery operations; bank-specific ingestion mappings; fraud/crime precision and recall. These require representative authorized data, evaluation criteria and operational review. The public demo calculates actual results but uses synthetic cases.
