# Provenance

All example accounts, transfers, balances and scenarios are original synthetic fixtures. Names denote fictional roles, not real individuals, banks, customers or investigations. No employer/client data or code was used. The exported case-review dossier comes from an actual local HTTP/SQLite/Rust execution with scripted synthetic actors; it is not evidence of a real human review or an institutional investigation. Scenario generators and the independent oracle are included for reproduction.

The source was developed with AI coding assistance and checked using tests, an independently implemented exhaustive oracle, native/WASM parity and a disposable PostgreSQL integration. These checks do not constitute a professional security audit or real-world financial-crime validation.

## Technical context

- [FCA review of detecting and preventing money mules](https://www.fca.org.uk/publications/multi-firm-reviews/proceeds-fraud-detecting-preventing-money-mules): context for examining transaction networks and the limits of static screening. No endorsement or certification is implied.
- [Topology-Agnostic Detection of Temporal Money Laundering Flows in Billion-Scale Transactions (FaSTMAN)](https://arxiv.org/abs/2309.13662): research context for time-aware financial networks. Tracebound does not reproduce this system, use its data, copy its implementation or claim its measured scale.
- [Making Bitcoin Legal](https://www.cl.cam.ac.uk/archive/rja14/Papers/making-bitcoin-legal.pdf): context for alternative tracing conventions. Tracebound computes conservation-constrained feasible intervals, not a jurisdiction's legal tracing rule.
- [wasm-bindgen documentation](https://wasm-bindgen.github.io/wasm-bindgen/examples/hello-world.html): browser bindings and shared Rust implementation.
- [PostgreSQL transaction isolation](https://www.postgresql.org/docs/current/transaction-iso.html): snapshot semantics used by the reproducible database lab.

Flow conservation, residual networks and cut certificates are established mathematical techniques. The project contribution is the bounded implementation, inspectable workflow and reproducible evidence, not a claim of academic novelty.

Rust dependencies are pinned in Cargo.lock and retain their upstream licenses. Python and browser application code use no third-party runtime packages beyond the generated Rust/WASM binding. No external imagery, fonts, customer datasets or pretrained model weights are bundled. The project source is MIT licensed.
