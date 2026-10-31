# Tracebound

**Follow the possibilities. Prove the bounds.**

An analyst workbench for a difficult question: after funds mix, split and move between accounts, how much of an analyst-selected transfer **must**, or **could**, have passed through a later payment?

[Open the interactive workbench](https://bernardo-demier-ai-lab.fancy-fly-9200.chatgpt.site/tracebound/) · [Architecture and proof](docs/ARCHITECTURE.md) · [Reproducible results](docs/RESULTS.md) · [PostgreSQL integration](docs/postgresql.md) · [Persistent case workflow](docs/WORKSPACE.md)

Tracebound computes exact conditional intervals over a reconciled temporal ledger. Every endpoint comes with a complete feasible allocation and an optimality certificate. A separate verifier checks both without running the optimizer. The browser runs the **same Rust solver compiled to WebAssembly**, not prerecorded answers.

## What makes this useful

- **Ambiguity is visible.** Two different, valid allocations can explain the same recorded transactions. Inspect the witness behind each bound on the network and in the ledger.
- **Time and balances constrain the answer.** A graph path alone is insufficient: funds cannot arrive after the payment, exceed an account balance or be allocated twice within one witness.
- **Evidence can be checked independently.** Export exact input, canonical report and SHA-256 anchor; verify capacity, conservation and optimality offline.
- **Cases survive a restart.** A local authenticated workspace preserves source artifacts, revisions, review decisions and a verifiable audit history. Investigator, reviewer and auditor roles enforce a bounded workflow.
- **Integrates with market software.** The PostgreSQL lab stores normalized accounts and transfers, exports a consistent snapshot, and runs the same analysis.
- **Polyglot by purpose.** Rust handles bounded exact-integer optimization and verification; Python exhaustively checks small cases; JavaScript presents the evidence and hosts a Web Worker.

This is deterministic financial-investigation software. There is no LLM, learned crime score, automatic account action or inference of criminal intent. A selected transfer is a mathematical label; the model does not establish actual provenance. Ledger completeness and posting order are explicit, unverified assumptions.

## Try it

Use the public workbench without installation. Six original synthetic cases cover a simple split-payment investigation, commingling, a seven-hop bottleneck, a split/merge cycle, disconnected payments and an ordinary treasury workflow. Select a target payment, switch between its lower and upper allocation, and inspect the evidence. Cases are processed in your browser; no case is uploaded by this application.

For a persistent investigation with review and evidence export, follow the [local workspace guide](docs/WORKSPACE.md). The public [case review](https://bernardo-demier-ai-lab.fancy-fly-9200.chatgpt.site/tracebound/review.html) shows a recorded synthetic workflow and verifies its evidence in the browser.

For native use, install Rust and run:

```sh
cargo build --release --locked
cargo run --release -- analyze --input examples/commingled-payments.json --output report.json
# Retain the printed report_sha256 separately, then:
cargo run --release -- verify --input examples/commingled-payments.json --report report.json --anchor <report_sha256>
# Optional stronger version-specific reproducibility check:
cargo run --release -- verify --input examples/commingled-payments.json --report report.json --anchor <report_sha256> --recompute
```

The first command refuses to overwrite an existing report. The anchor detects a changed report only if retained in a separate trusted location. Certificate verification checks the mathematics, not the authenticity of source records. `--recompute` additionally requires byte-identical solver output from this version.

## Validate and build

```sh
cargo test --locked
cargo clippy --all-targets --all-features --locked -- -D warnings
cargo build --release --locked
python3 scripts/verify_oracle.py --binary target/release/tracebound
python3 scripts/generate_cases.py --check
python3 -m unittest discover -s tests -p 'test_*.py' -v
# Real PostgreSQL instance in a disposable socket-only cluster:
python3 scripts/postgres_lab.py --binary target/release/tracebound --case examples/split-merge-cycle.json
# Browser build; install the CLI version matching Cargo.lock:
rustup target add wasm32-unknown-unknown
cargo install wasm-bindgen-cli --version 0.2.130 --locked
python3 scripts/build_web.py
node scripts/check_wasm.mjs
python3 -m http.server 8786 --bind 127.0.0.1 --directory web
```

Open `http://127.0.0.1:8786`. Native Windows builds use `target/release/tracebound.exe`. PostgreSQL lab requires Unix socket support; CI runs it on Linux. Python scripts use the standard library only. No API key, cloud account or paid service is needed.

## Boundaries

- A complete, single-currency ledger with explicit opening and closing balances; integer minor units; no overdrafts, reversals or FX. Reconciliation is necessary, **not proof of completeness**.
- Up to 1,000 transfers, 128 accounts and 16 target payments per case; 2 MiB input, 16 MiB report and a 20-million graph-edge work budget. Exhaustion fails explicitly.
- Browser inputs are limited to JavaScript safe integers; native inputs accept `u64` values and use `u128` internally. Report amounts are decimal strings.
- Bounds for different targets are **marginal**. Separate maxima need not be simultaneously attainable; never add them as total recovered funds.
- No identity resolution, sanctions screening, legal tracing standard, bank connectivity or production deployment is claimed. Synthetic validation does not estimate real-world crime-detection accuracy.

Read the [threat model](docs/SECURITY.md), [model decisions](docs/ARCHITECTURE.md) and [provenance](PROVENANCE.md). MIT licensed.
