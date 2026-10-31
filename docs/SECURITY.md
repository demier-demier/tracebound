# Threat model

## Protected properties

Exact model arithmetic, coherent evidence, bounded resource consumption and confidentiality of case contents in diagnostics. The operator controls the ledger, selected seed, output paths and completeness assertion. They must have legitimate authority to process source data.

## Hostile inputs and controls

- Strict Serde schemas reject unknown and duplicate fields; identifier length and alphabet are bounded. Monetary inputs are unsigned integers; no floating-point funds arithmetic occurs.
- Repeated transfer IDs, account IDs, target IDs and sequence values fail. Missing balances, overdrafts, time reversals and nonreconciling closings fail.
- Input, report, account, transfer, target and graph work limits fail explicitly. The solver uses iterative traversal and does not silently truncate results.
- Verification checks external digest, exact input hash, model metadata, capacities, conservation, complete witness coverage and optimality certificates. A rehashed forged report must still satisfy the mathematical verifier. Optional replay detects changes in informational solver statistics as well.
- Native evidence output uses an exclusive persist operation and refuses overwrite. File reads reject symlinks and non-regular inputs. Paths are trusted operator choices; concurrent adversarial replacement of path ancestors is out of scope.
- The web app uses a local Web Worker and Rust/WASM. Rendered case identifiers use safe DOM/text construction. Amounts beyond `Number.MAX_SAFE_INTEGER` are rejected before worker handoff; report amounts remain strings. No user case is sent to a backend or third-party analytics endpoint.
- The PostgreSQL lab initializes a new private, socket-only cluster, clears inherited connection variables and cleans up its own process. It never connects to an existing database.

## Non-goals and limits

The mathematical model cannot distinguish legitimate and illegitimate payments with identical records. It does not establish real provenance, beneficial ownership, intent, crime, tax liability or admissibility of evidence. It does not automatically freeze accounts, contact authorities or make decisions about people.

Reconciliation and an attestation do not authenticate source records or establish completeness. Same-time ordering may change bounds. Cross-currency flows, overdrafts, unsettled/reversed payments and missing intermediaries are outside this version. Certificates are proofs of conditional optimization bounds, not signatures from a bank.

A malicious web host or browser extension can replace application code. For sensitive cases, build a reviewed revision locally and use native CLI verification. No audit of third-party dependencies or production hardening is claimed. SHA-256 anchors require an independent trusted retention mechanism; key management, signing and immutable archival infrastructure are not implemented.

## Reporting issues

For non-sensitive reproducible bugs, open a GitHub issue with an original synthetic fixture. Never post real customer information, secrets, financial records or access tokens. Include the revision, platform and expected mathematical behavior.
