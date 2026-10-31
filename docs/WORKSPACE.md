# Persistent case workspace

The local workspace executes a complete investigation workflow over the Rust engine. It is a single-machine evaluation service, not a production banking deployment. Python's HTTP server binds only to `127.0.0.1`; SQLite WAL persists cases, evidence, notes and audit events.

## Run it

```sh
cargo build --release --locked
python3 scripts/build_web.py
mkdir -p ../tracebound-private
python3 scripts/serve_workspace.py --init-config ../tracebound-private/actors.json
python3 scripts/serve_workspace.py --config ../tracebound-private/actors.json --database ../tracebound-private/cases.sqlite --port 8787
```

Open `http://127.0.0.1:8787/review.html`. Read the generated private configuration locally and use the appropriate actor token in the connection form. Keep tokens, the database and its WAL/SHM files outside the public repository. Credentials are generated once, never published or embedded in the site. The public site presents an exported synthetic workflow; it does not host this private service or claim real users.

## End-to-end workflow

1. Connect as the investigator and create a case with a concrete question.
2. Import a complete `flow-case/v1` ledger. For normalized CSV exports, use the [CSV importer](IMPORT.md). Analyze it: the service runs the real Rust executable and its independent certificate verifier before committing evidence.
3. Record observations, competing explanations and missing source information. The original input and complete analysis are retained as immutable artifacts.
4. Submit the current revision. A different reviewer actor must approve it or return it with a reason. An approved case is frozen.
5. Connect as the auditor, inspect the history and export the dossier and its audit anchor. Retain the anchor in a separate trusted location. Restarting the service preserves the case.

The browser review page hashes each exhibit, verifies the event chain and current-state binding, and invokes the same Rust/WASM certificate verifier on each stored analysis. This validates internal consistency; a self-supplied anchor does not authenticate the underlying records or identity of the actors.

## Access and concurrency

| Actor | Allowed actions |
| --- | --- |
| Investigator | Create, analyze, add draft notes, submit for review; read cases |
| Reviewer | Add draft notes, approve or return submitted cases; read cases |
| Auditor | Read, export and verify; no writes |

Every mutation supplies `expected_revision`. A stale request is rejected with HTTP 409. Analysis happens outside the write transaction, then rechecks the starting revision inside `BEGIN IMMEDIATE`; a concurrent note or submission cannot be overwritten by a late calculation. A failed solver or verifier produces no partial evidence commit.

Actors share one trusted workspace; there is **no tenant isolation**. Distinct bearer credentials enforce role separation, but this local demonstration does not prove two distinct humans participated. It has no SSO, MFA, organizational identity lifecycle or delegated permissions.

## Evidence and history

Each event contains the case snapshot and new artifact/note/analysis metadata. Its hash is SHA-256 of UTF-8 JSON with recursively sorted keys, compact separators and unescaped Unicode, excluding the `hash` field. The first previous hash is 64 zeros. Sequence numbers equal revisions.

SQLite triggers reject UPDATE/DELETE of artifact, analysis, note and event rows through normal SQL. An administrator who controls the database can remove triggers and rewrite the database; this is tamper-evident evidence only when compared to a separately retained trusted anchor. Verification reconstructs immutable inventories and checks that the final audited snapshot matches the exported current case. There are no bank signatures, legal attestations or judicial guarantees.

## Reproduce the executed demonstration

```sh
python3 -m unittest discover -s tests -p 'test_workspace.py' -v
python3 scripts/demo_workspace.py --output-dir /tmp/tracebound-new-observed-run
```

The script starts a real authenticated HTTP service, creates a case, invokes Rust analysis, attempts rejected stale/self-approval/auditor writes, returns the case for further work, adds a competing legitimate explanation, approves a later revision, exports and verifies the dossier, then reopens the database and checks exact persistence. Its actors and decisions are scripted; the recorded artifact is clearly labeled.

## Limits before institutional deployment

There is no live bank connector, source attestation, case assignment, multi-tenant authorization, distributed queue, backup/restore service, encryption-at-rest management, production rate limiting, retention policy, SIEM export or independent security audit. Boundaries include 50 cases, 200 events per case, 20 analyses per case, 32 MiB artifact storage per case and 128 MiB per workspace. Requests and solver concurrency are bounded. The service should remain on loopback; do not place it directly on the Internet.
