# Architecture and mathematical contract

## Question and assumptions

Given a fully reconciled, ordered, single-currency ledger, mark the full amount of one analyst-selected transfer at the moment its recipient receives it. For each requested later transfer, compute the minimum and maximum amount of this label consistent with the observed capacities, chronology and account balances. The label is not a finding about source legality or real asset provenance.

Explicit sequence numbers must be unique and timestamps nondecreasing in sequence. Ordering equal timestamps is supplied by the operator, not inferred. Every account has opening and closing balances. All transfers must be posted, positive, between distinct known accounts, and never overdraw an account. Closing balances must reconcile exactly. A `completeness_attested: true` field records an operator assertion; omitted records that preserve reconciliation can remain undetected.

## Temporal network

1. Replay the ledger up to and including the seed transfer.
2. Construct account states after the seed. Inject exactly the seed amount into its recipient state.
3. For each later transfer, add an edge for its amount and carry edges for each affected account's remaining balance. New states preserve event order; time never runs backward.
4. Drain all final account states to a sink, bounded by actual closing balances.
5. Find a feasible max-flow equal to the seed. Integrality of the capacities ensures integer allocations; the implementation uses `u128` throughout.

A feasible flow is equivalent to an allocation that respects per-transfer capacity, account liquidity and conservation. Carry amounts are implied by the complete transfer allocation. The witness includes every transfer, including zeros before the seed, and the remaining label at every final account.

## Exact marginal extrema

Let target edge be `e = (u,v)` with capacity `c` and current feasible flow `f`.

To maximize its flow, exclude both residual directions of `e`, and augment residual flow from `v` to `u`, capped at `c-f`. Each augmentation plus the forward target edge forms a circulation, preserving conservation. To minimize it, augment from `u` to `v`, capped at `f`, and subtract the matching amount on the target. A residual circulation exists for any feasible alternative allocation; when no such path remains, further change is impossible. This is a standard flow optimization construction, not a claim of a new mathematical algorithm.

Each target is optimized independently from the feasible baseline. Its interval is exact under the model; a set of target-wise endpoints does not describe a joint allocation. Cycles between account names do not create cycles in time; later returns use different account-state nodes.

## Solver-free certificates

The report binds to the exact source bytes with SHA-256 and contains complete lower/upper witnesses. A verifier reconstructs the deterministic temporal network, replays each witness, and checks capacities, conservation, endpoint attainment and final balances.

Optimality is certified either by a trivial bound (zero, full transfer capacity, pre-seed zero or the seed definition), or by a residual cut. The cut contains the search source, excludes the destination and has no outgoing positive residual edge once target arcs are excluded. It proves there is no improving circulation. The verifier does not call max-flow. Structural statistics are checked; the reported optimizer work count is informational, not independently reproduced unless `--recompute` is used.

The native CLI first checks the report against an externally retained SHA-256 digest, then verifies certificates. A digest stored alongside mutable evidence is not an independent trust anchor. Neither hash nor certificate proves source authenticity or completeness.

## Components and decisions

| Component | Language | Reason |
| --- | --- | --- |
| Temporal network, bounded iterative Dinic, residual extrema, certificate verification | Rust | Exact integers, shared native/WASM implementation, bounded work, no recursive graph traversal |
| Exhaustive small-case oracle | Python | Independent enumeration rather than a second copy of max-flow |
| PostgreSQL snapshot lab | SQL + Python | Reproducible normalized storage and consistent export |
| Analyst UI and worker | JavaScript | Browser-native interaction; computation uses Rust/WASM |

No generic anomaly score was added: an unexplained number would weaken the investigative artifact. No LLM is in the arithmetic path. No database credentials or live banking connection are required for the demo. Native CLI paths are trusted operator inputs; this is not a multi-tenant file service.

## Evaluation strategy

Exhaustively enumerate all integer allocations for small controlled ledgers and compare both endpoints. Replay every Rust witness independently. Exercise ambiguous and forced values, temporal constraints, cycles, big integers, inconsistent ledgers, corrupted certificates and resource limits. Compare native and WASM reports for byte-equivalent input. Use real PostgreSQL only in a disposable local cluster. Benchmarks report a bounded synthetic workload and cannot establish production scale or detection accuracy.
