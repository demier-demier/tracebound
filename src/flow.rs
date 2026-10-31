//! Exact conditional attribution bounds in a fully reconciled temporal ledger.
//!
//! Seeded flow is a mathematical label, not a claim of actual provenance or
//! criminal origin. Bounds range over all allocations consistent with the model.
use anyhow::{anyhow, bail, Context, Result};
use chrono::{DateTime, Utc};
use serde::{
    de::{SeqAccess, Visitor},
    Deserialize, Deserializer, Serialize,
};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet, VecDeque};

pub const MAX_INPUT_BYTES: usize = 2 * 1024 * 1024;
pub const MAX_TRANSFERS: usize = 1_000;
pub const MAX_ACCOUNTS: usize = 128;
pub const MAX_TARGETS: usize = 16;
pub const MAX_WORK_UNITS: u64 = 20_000_000;
pub const MAX_REPORT_BYTES: usize = 16 * 1024 * 1024;
const MAX_GRAPH_NODES: usize = MAX_ACCOUNTS + 2 + 2 * (MAX_TRANSFERS - 1);

/// Enforce structural limits during deserialization, before a forged bounded-
/// byte report can expand millions of tiny array values into large allocations.
fn bounded_vec<'de, D, T, const LIMIT: usize>(
    deserializer: D,
) -> std::result::Result<Vec<T>, D::Error>
where
    D: Deserializer<'de>,
    T: Deserialize<'de>,
{
    struct Bounded<T, const N: usize>(std::marker::PhantomData<T>);
    impl<'de, T: Deserialize<'de>, const N: usize> Visitor<'de> for Bounded<T, N> {
        type Value = Vec<T>;
        fn expecting(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
            write!(formatter, "an array with at most {N} entries")
        }
        fn visit_seq<A: SeqAccess<'de>>(
            self,
            mut sequence: A,
        ) -> std::result::Result<Self::Value, A::Error> {
            let mut values = Vec::with_capacity(sequence.size_hint().unwrap_or(0).min(N));
            while let Some(value) = sequence.next_element()? {
                if values.len() == N {
                    return Err(serde::de::Error::custom(
                        "array exceeds structural resource limit",
                    ));
                }
                values.push(value);
            }
            Ok(values)
        }
    }
    deserializer.deserialize_seq(Bounded::<T, LIMIT>(std::marker::PhantomData))
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct CaseInput {
    schema: String,
    case_id: String,
    currency: String,
    completeness_attested: bool,
    #[serde(deserialize_with = "bounded_vec::<_, _, MAX_ACCOUNTS>")]
    opening_balances: Vec<BalanceInput>,
    #[serde(deserialize_with = "bounded_vec::<_, _, MAX_ACCOUNTS>")]
    closing_balances: Vec<BalanceInput>,
    #[serde(deserialize_with = "bounded_vec::<_, _, MAX_TRANSFERS>")]
    transfers: Vec<TransferInput>,
    seed_transfer_id: String,
    #[serde(deserialize_with = "bounded_vec::<_, _, MAX_TARGETS>")]
    target_transfer_ids: Vec<String>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct BalanceInput {
    account: String,
    amount_minor: u64,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct TransferInput {
    id: String,
    timestamp: String,
    sequence: u64,
    from_account: String,
    to_account: String,
    amount_minor: u64,
}
struct Transfer {
    id: String,
    timestamp: DateTime<Utc>,
    sequence: u64,
    from: usize,
    to: usize,
    amount: u128,
}
struct Case {
    case_id: String,
    currency: String,
    accounts: Vec<String>,
    opening: Vec<u128>,
    closing: Vec<u128>,
    transfers: Vec<Transfer>,
    seed: usize,
    targets: Vec<usize>,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Report {
    pub schema: String,
    pub input_sha256: String,
    pub case_id: String,
    pub currency: String,
    pub policy: Policy,
    #[serde(deserialize_with = "bounded_vec::<_, _, 16>")]
    pub assumptions: Vec<String>,
    pub seed: Seed,
    pub ledger: Ledger,
    pub stats: Stats,
    #[serde(deserialize_with = "bounded_vec::<_, _, MAX_TARGETS>")]
    pub targets: Vec<TargetReport>,
}
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Policy {
    pub model: String,
    pub max_input_bytes: usize,
    pub max_transfers: usize,
    pub max_accounts: usize,
    pub max_targets: usize,
    pub max_work_units: u64,
    pub max_report_bytes: usize,
    pub work_unit_definition: String,
}
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Seed {
    pub transfer_id: String,
    pub sequence: u64,
    pub recipient: String,
    pub amount_minor: String,
}
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Ledger {
    pub completeness_attested: bool,
    pub closing_balances_reconciled: bool,
    pub transfer_count: usize,
    pub opening_total_minor: String,
    pub closing_total_minor: String,
    #[serde(deserialize_with = "bounded_vec::<_, _, MAX_ACCOUNTS>")]
    pub accounts: Vec<AccountBalance>,
}
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct AccountBalance {
    pub account: String,
    pub opening_minor: String,
    pub closing_minor: String,
}
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Stats {
    pub nodes: usize,
    pub capacity_edges: usize,
    pub suffix_transfers: usize,
    pub work_units: u64,
    pub witnesses_validated: usize,
}
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct TargetReport {
    pub transfer_id: String,
    pub sequence: u64,
    pub amount_minor: String,
    pub minimum_minor: String,
    pub maximum_minor: String,
    pub min_witness: Witness,
    pub max_witness: Witness,
    pub min_certificate: Certificate,
    pub max_certificate: Certificate,
}
/// A cut is expressed in deterministic temporal graph node IDs. The verifier
/// reconstructs the graph from the input and witness, without solving max-flow.
#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum Certificate {
    BeforeSeed {},
    SeedDefinition {},
    ZeroLowerBound {},
    TransferCapacityUpperBound {},
    ResidualCut { reachable_nodes: Vec<usize> },
}
impl<'de> Deserialize<'de> for Certificate {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> std::result::Result<Self, D::Error> {
        // An ordinary internally tagged enum deserializer can buffer its entire
        // map before dispatching to a bounded field. Use a typed wire struct so
        // the reachable-node list is bounded while bytes are being decoded.
        #[derive(Default)]
        enum Nodes {
            #[default]
            Missing,
            Present(Vec<usize>),
        }
        impl<'de> Deserialize<'de> for Nodes {
            fn deserialize<D: Deserializer<'de>>(d: D) -> std::result::Result<Self, D::Error> {
                bounded_vec::<D, usize, MAX_GRAPH_NODES>(d).map(Self::Present)
            }
        }
        #[derive(Deserialize)]
        #[serde(deny_unknown_fields)]
        struct Wire {
            kind: String,
            #[serde(default)]
            reachable_nodes: Nodes,
        }
        let wire = Wire::deserialize(deserializer)?;
        match (wire.kind.as_str(), wire.reachable_nodes) {
            ("before_seed", Nodes::Missing) => Ok(Self::BeforeSeed {}),
            ("seed_definition", Nodes::Missing) => Ok(Self::SeedDefinition {}),
            ("zero_lower_bound", Nodes::Missing) => Ok(Self::ZeroLowerBound {}),
            ("transfer_capacity_upper_bound", Nodes::Missing) => {
                Ok(Self::TransferCapacityUpperBound {})
            }
            ("residual_cut", Nodes::Present(reachable_nodes)) => {
                Ok(Self::ResidualCut { reachable_nodes })
            }
            _ => Err(serde::de::Error::custom(
                "invalid certificate kind or fields",
            )),
        }
    }
}
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Witness {
    #[serde(deserialize_with = "bounded_vec::<_, _, MAX_TRANSFERS>")]
    pub transfer_allocations: Vec<TransferAllocation>,
    #[serde(deserialize_with = "bounded_vec::<_, _, MAX_ACCOUNTS>")]
    pub final_allocations: Vec<FinalAllocation>,
}
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct TransferAllocation {
    pub transfer_id: String,
    pub seeded_minor: String,
}
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct FinalAllocation {
    pub account: String,
    pub seeded_minor: String,
}

fn identifier(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 64
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'-' | b'.' | b':'))
}
fn parse(raw: &[u8]) -> Result<Case> {
    if raw.len() > MAX_INPUT_BYTES {
        bail!("flow input exceeds 2 MiB limit");
    }
    // Avoid including source fields in errors: case inputs can contain private IDs.
    let input: CaseInput =
        serde_json::from_slice(raw).map_err(|_| anyhow!("invalid flow-case JSON schema"))?;
    if input.schema != "flow-case/v1" {
        bail!("unsupported flow-case schema");
    }
    if !input.completeness_attested {
        bail!("complete-ledger attestation is required");
    }
    if !identifier(&input.case_id) {
        bail!("invalid case identifier");
    }
    if input.currency.len() != 3 || !input.currency.bytes().all(|b| b.is_ascii_uppercase()) {
        bail!("currency must have three uppercase ASCII letters");
    }
    if input.opening_balances.is_empty() || input.opening_balances.len() > MAX_ACCOUNTS {
        bail!("opening accounts must number 1..128");
    }
    if input.closing_balances.len() != input.opening_balances.len() {
        bail!("closing account set must equal opening account set");
    }
    if input.transfers.is_empty() || input.transfers.len() > MAX_TRANSFERS {
        bail!("transfers must number 1..1000");
    }
    if input.target_transfer_ids.is_empty() || input.target_transfer_ids.len() > MAX_TARGETS {
        bail!("targets must number 1..16");
    }
    let mut balances = BTreeMap::new();
    for value in input.opening_balances {
        if !identifier(&value.account) {
            bail!("invalid opening account identifier");
        }
        if balances
            .insert(value.account, u128::from(value.amount_minor))
            .is_some()
        {
            bail!("duplicate opening account");
        }
    }
    let accounts: Vec<_> = balances.keys().cloned().collect();
    let account_index: BTreeMap<_, _> = accounts
        .iter()
        .enumerate()
        .map(|(i, name)| (name.as_str(), i))
        .collect();
    let opening: Vec<_> = balances.values().copied().collect();
    let mut closing = vec![0; accounts.len()];
    let mut seen_closing = BTreeSet::new();
    for value in input.closing_balances {
        let index = *account_index
            .get(value.account.as_str())
            .context("closing account missing from opening account set")?;
        if !seen_closing.insert(index) {
            bail!("duplicate closing account");
        }
        closing[index] = u128::from(value.amount_minor);
    }
    let mut ids = BTreeSet::new();
    let mut sequences = BTreeSet::new();
    let mut transfers = Vec::new();
    for value in input.transfers {
        if !identifier(&value.id) {
            bail!("invalid transfer identifier");
        }
        if !ids.insert(value.id.clone()) {
            bail!("duplicate transfer ID");
        }
        if value.sequence == 0 || !sequences.insert(value.sequence) {
            bail!("transfer sequences must be positive and unique");
        }
        if value.amount_minor == 0 {
            bail!("transfer amount must be positive");
        }
        let from = *account_index
            .get(value.from_account.as_str())
            .context("sender has no explicit opening balance")?;
        let to = *account_index
            .get(value.to_account.as_str())
            .context("recipient has no explicit opening balance")?;
        if from == to {
            bail!("self-transfers are unsupported by this flow model");
        }
        let timestamp = DateTime::parse_from_rfc3339(&value.timestamp)
            .map_err(|_| anyhow!("invalid RFC3339 transfer timestamp"))?
            .with_timezone(&Utc);
        transfers.push(Transfer {
            id: value.id,
            timestamp,
            sequence: value.sequence,
            from,
            to,
            amount: u128::from(value.amount_minor),
        });
    }
    transfers.sort_by_key(|t| t.sequence);
    if transfers
        .windows(2)
        .any(|w| w[0].timestamp > w[1].timestamp)
    {
        bail!("timestamps must be nondecreasing in explicit sequence order");
    }
    let seed = transfers
        .iter()
        .position(|t| t.id == input.seed_transfer_id)
        .context("seed transfer does not exist")?;
    let mut targets = BTreeSet::new();
    for id in input.target_transfer_ids {
        let position = transfers
            .iter()
            .position(|t| t.id == id)
            .context("target transfer does not exist")?;
        if !targets.insert(position) {
            bail!("duplicate target transfer");
        }
    }
    let case = Case {
        case_id: input.case_id,
        currency: input.currency,
        accounts,
        opening,
        closing,
        transfers,
        seed,
        targets: targets.into_iter().collect(),
    };
    // Full ledger replay occurs before constructing any attribution graph.
    let mut replay = case.opening.clone();
    for transfer in &case.transfers {
        apply_total(&mut replay, transfer)?;
    }
    if replay != case.closing {
        bail!("closing balances do not reconcile with complete ledger replay");
    }
    Ok(case)
}
fn apply_total(balances: &mut [u128], transfer: &Transfer) -> Result<()> {
    balances[transfer.from] = balances[transfer.from]
        .checked_sub(transfer.amount)
        .context("ledger overdraft: debit exceeds available balance")?;
    balances[transfer.to] = balances[transfer.to]
        .checked_add(transfer.amount)
        .context("ledger balance overflow")?;
    Ok(())
}

#[derive(Clone, Copy, Debug)]
struct Handle {
    from: usize,
    index: usize,
    capacity: u128,
}
#[derive(Clone, Debug)]
struct Edge {
    to: usize,
    reverse: usize,
    residual: u128,
}
#[derive(Clone, Debug)]
struct Graph {
    edges: Vec<Vec<Edge>>,
    originals: Vec<Handle>,
}
impl Graph {
    fn new(nodes: usize) -> Self {
        Self {
            edges: vec![Vec::new(); nodes],
            originals: Vec::new(),
        }
    }
    fn node(&mut self) -> usize {
        self.edges.push(Vec::new());
        self.edges.len() - 1
    }
    fn add(&mut self, from: usize, to: usize, capacity: u128) -> Handle {
        let forward = self.edges[from].len();
        let reverse = self.edges[to].len();
        self.edges[from].push(Edge {
            to,
            reverse,
            residual: capacity,
        });
        self.edges[to].push(Edge {
            to: from,
            reverse: forward,
            residual: 0,
        });
        let handle = Handle {
            from,
            index: forward,
            capacity,
        };
        self.originals.push(handle);
        handle
    }
    fn flow(&self, handle: Handle) -> Result<u128> {
        handle
            .capacity
            .checked_sub(self.edges[handle.from][handle.index].residual)
            .context("internal flow exceeds edge capacity")
    }
    fn forbidden(&self, handle: Handle) -> [(usize, usize); 2] {
        let edge = &self.edges[handle.from][handle.index];
        [(handle.from, handle.index), (edge.to, edge.reverse)]
    }
    fn augment(&mut self, from: usize, index: usize, amount: u128) -> Result<()> {
        let edge = &self.edges[from][index];
        let to = edge.to;
        let reverse = edge.reverse;
        self.edges[from][index].residual = self.edges[from][index]
            .residual
            .checked_sub(amount)
            .context("internal residual capacity exceeded")?;
        self.edges[to][reverse].residual = self.edges[to][reverse]
            .residual
            .checked_add(amount)
            .context("internal residual overflow")?;
        Ok(())
    }
    /// Iterative Dinic: no recursion even for a long temporal chain. Every
    /// residual edge inspection and augmentation is charged to a shared budget.
    fn max_flow(
        &mut self,
        source: usize,
        sink: usize,
        limit: u128,
        excluded: &[(usize, usize)],
        budget: &mut Budget,
    ) -> Result<u128> {
        if source == sink {
            bail!("internal flow endpoints must differ");
        }
        let mut total = 0_u128;
        while total < limit {
            let mut level = vec![-1_i32; self.edges.len()];
            level[source] = 0;
            let mut queue = VecDeque::from([source]);
            while let Some(node) = queue.pop_front() {
                for (index, edge) in self.edges[node].iter().enumerate() {
                    budget.tick()?;
                    if edge.residual > 0 && level[edge.to] < 0 && !excluded.contains(&(node, index))
                    {
                        level[edge.to] = level[node] + 1;
                        queue.push_back(edge.to);
                    }
                }
            }
            if level[sink] < 0 {
                break;
            }
            let mut next = vec![0; self.edges.len()];
            let mut path: Vec<(usize, usize)> = Vec::new();
            let mut node = source;
            loop {
                if node == sink {
                    let mut amount = limit - total;
                    for &(from, index) in &path {
                        budget.tick()?;
                        amount = amount.min(self.edges[from][index].residual);
                    }
                    if amount == 0 {
                        bail!("internal zero-capacity augmentation");
                    }
                    for &(from, index) in &path {
                        budget.tick()?;
                        self.augment(from, index, amount)?;
                    }
                    total = total.checked_add(amount).context("flow total overflow")?;
                    if total == limit {
                        return Ok(total);
                    }
                    path.clear();
                    node = source;
                    continue;
                }
                let mut advanced = false;
                while next[node] < self.edges[node].len() {
                    let index = next[node];
                    let edge = &self.edges[node][index];
                    budget.tick()?;
                    if edge.residual > 0
                        && level[edge.to] == level[node] + 1
                        && !excluded.contains(&(node, index))
                    {
                        path.push((node, index));
                        node = edge.to;
                        advanced = true;
                        break;
                    }
                    next[node] += 1;
                }
                if advanced {
                    continue;
                }
                level[node] = -1;
                if let Some((parent, index)) = path.pop() {
                    node = parent;
                    next[node] = index + 1;
                } else {
                    break;
                }
            }
        }
        Ok(total)
    }
    fn check_conservation(
        &self,
        source: usize,
        sink: usize,
        seed: u128,
        budget: &mut Budget,
    ) -> Result<()> {
        let mut incoming = vec![0_u128; self.edges.len()];
        let mut outgoing = incoming.clone();
        for &handle in &self.originals {
            budget.tick()?;
            let edge = &self.edges[handle.from][handle.index];
            let flow = self.flow(handle)?;
            if self.edges[edge.to][edge.reverse].residual != flow {
                bail!("internal reverse residual mismatch");
            }
            outgoing[handle.from] = outgoing[handle.from]
                .checked_add(flow)
                .context("flow conservation overflow")?;
            incoming[edge.to] = incoming[edge.to]
                .checked_add(flow)
                .context("flow conservation overflow")?;
        }
        for node in 0..self.edges.len() {
            let expected_in = incoming[node]
                .checked_add(if node == source { seed } else { 0 })
                .context("flow conservation overflow")?;
            let expected_out = outgoing[node]
                .checked_add(if node == sink { seed } else { 0 })
                .context("flow conservation overflow")?;
            if expected_in != expected_out {
                bail!("internal flow conservation failure");
            }
        }
        Ok(())
    }
}
struct Budget {
    used: u64,
    limit: u64,
}
impl Budget {
    fn tick(&mut self) -> Result<()> {
        self.used += 1;
        if self.used > self.limit {
            bail!("flow analysis work limit exceeded; no complete report produced");
        }
        Ok(())
    }
}
struct Network {
    graph: Graph,
    source: usize,
    sink: usize,
    transfer_edges: Vec<Option<Handle>>,
    terminal_edges: Vec<Handle>,
    source_edge: Handle,
    carry_edges: Vec<Option<(Handle, Handle)>>,
}
fn network(case: &Case) -> Result<Network> {
    let mut balance = case.opening.clone();
    for transfer in &case.transfers[..=case.seed] {
        apply_total(&mut balance, transfer)?;
    }
    let mut graph = Graph::new(case.accounts.len());
    let mut current: Vec<_> = (0..case.accounts.len()).collect();
    let source = graph.node();
    let sink = graph.node();
    let source_edge = graph.add(
        source,
        current[case.transfers[case.seed].to],
        case.transfers[case.seed].amount,
    );
    let mut transfer_edges = vec![None; case.transfers.len()];
    let mut carry_edges = vec![None; case.transfers.len()];
    for (i, transfer) in case.transfers.iter().enumerate().skip(case.seed + 1) {
        let from_next = graph.node();
        let to_next = graph.node();
        let sender_remaining = balance[transfer.from]
            .checked_sub(transfer.amount)
            .context("ledger overdraft in graph construction")?;
        let from_carry = graph.add(current[transfer.from], from_next, sender_remaining);
        let to_carry = graph.add(current[transfer.to], to_next, balance[transfer.to]);
        carry_edges[i] = Some((from_carry, to_carry));
        transfer_edges[i] = Some(graph.add(current[transfer.from], to_next, transfer.amount));
        current[transfer.from] = from_next;
        current[transfer.to] = to_next;
        apply_total(&mut balance, transfer)?;
    }
    let terminal_edges = current
        .iter()
        .enumerate()
        .map(|(i, &node)| graph.add(node, sink, balance[i]))
        .collect();
    Ok(Network {
        graph,
        source,
        sink,
        transfer_edges,
        terminal_edges,
        source_edge,
        carry_edges,
    })
}
fn extremize(base: &Graph, handle: Handle, maximize: bool, budget: &mut Budget) -> Result<Graph> {
    let mut graph = base.clone();
    let edge = &graph.edges[handle.from][handle.index];
    let to = edge.to;
    let reverse = edge.reverse;
    let excluded = graph.forbidden(handle);
    if maximize {
        let capacity = edge.residual;
        let amount = graph.max_flow(to, handle.from, capacity, &excluded, budget)?;
        graph.augment(handle.from, handle.index, amount)?;
    } else {
        let current = graph.flow(handle)?;
        let amount = graph.max_flow(handle.from, to, current, &excluded, budget)?;
        graph.augment(to, reverse, amount)?;
    }
    Ok(graph)
}
fn witness(case: &Case, network: &Network, graph: &Graph, budget: &mut Budget) -> Result<Witness> {
    let seed_amount = case.transfers[case.seed].amount;
    graph.check_conservation(network.source, network.sink, seed_amount, budget)?;
    let mut totals = case.opening.clone();
    let mut tracked = vec![0_u128; case.accounts.len()];
    let mut transfer_allocations = Vec::with_capacity(case.transfers.len());
    for (i, transfer) in case.transfers.iter().enumerate() {
        budget.tick()?;
        let amount = if i < case.seed {
            0
        } else if i == case.seed {
            seed_amount
        } else {
            graph.flow(network.transfer_edges[i].context("internal missing transfer edge")?)?
        };
        if i > case.seed {
            // Independently replay each witness against original ledger balances:
            // max(0, debit - unseeded_balance) <= allocated <= min(debit, seeded).
            let unseeded = totals[transfer.from]
                .checked_sub(tracked[transfer.from])
                .context("witness exceeds ledger balance")?;
            let minimum = transfer.amount.saturating_sub(unseeded);
            let maximum = transfer.amount.min(tracked[transfer.from]);
            if amount < minimum || amount > maximum {
                bail!("internal infeasible transfer witness allocation");
            }
            tracked[transfer.from] -= amount;
            tracked[transfer.to] = tracked[transfer.to]
                .checked_add(amount)
                .context("witness amount overflow")?;
        }
        apply_total(&mut totals, transfer)?;
        if i == case.seed {
            tracked[transfer.to] = seed_amount;
        }
        if tracked[transfer.from] > totals[transfer.from]
            || tracked[transfer.to] > totals[transfer.to]
        {
            bail!("internal witness account capacity failure");
        }
        transfer_allocations.push(TransferAllocation {
            transfer_id: transfer.id.clone(),
            seeded_minor: amount.to_string(),
        });
    }
    let total_tracked: u128 = tracked.iter().sum();
    if total_tracked != seed_amount {
        bail!("internal witness seed total mismatch");
    }
    let mut final_allocations = Vec::with_capacity(case.accounts.len());
    for (i, &handle) in network.terminal_edges.iter().enumerate() {
        budget.tick()?;
        if graph.flow(handle)? != tracked[i] {
            bail!("internal terminal witness mismatch");
        }
        final_allocations.push(FinalAllocation {
            account: case.accounts[i].clone(),
            seeded_minor: tracked[i].to_string(),
        });
    }
    Ok(Witness {
        transfer_allocations,
        final_allocations,
    })
}

fn certificate(
    graph: &Graph,
    handle: Handle,
    maximize: bool,
    budget: &mut Budget,
) -> Result<Certificate> {
    let amount = graph.flow(handle)?;
    if !maximize && amount == 0 {
        return Ok(Certificate::ZeroLowerBound {});
    }
    if maximize && amount == handle.capacity {
        return Ok(Certificate::TransferCapacityUpperBound {});
    }
    let head = graph.edges[handle.from][handle.index].to;
    let (source, sink) = if maximize {
        (head, handle.from)
    } else {
        (handle.from, head)
    };
    let excluded = graph.forbidden(handle);
    let mut reached = vec![false; graph.edges.len()];
    reached[source] = true;
    let mut queue = VecDeque::from([source]);
    while let Some(node) = queue.pop_front() {
        for (index, edge) in graph.edges[node].iter().enumerate() {
            budget.tick()?;
            if edge.residual > 0 && !reached[edge.to] && !excluded.contains(&(node, index)) {
                reached[edge.to] = true;
                queue.push_back(edge.to);
            }
        }
    }
    if reached[sink] {
        bail!("internal bound is not optimal: residual augmentation remains");
    }
    let result = Certificate::ResidualCut {
        reachable_nodes: reached
            .iter()
            .enumerate()
            .filter_map(|(i, reached)| reached.then_some(i))
            .collect(),
    };
    check_certificate(graph, handle, maximize, &result, budget)?;
    Ok(result)
}

fn check_certificate(
    graph: &Graph,
    handle: Handle,
    maximize: bool,
    certificate: &Certificate,
    budget: &mut Budget,
) -> Result<()> {
    let flow = graph.flow(handle)?;
    match certificate {
        Certificate::ZeroLowerBound {} if !maximize && flow == 0 => Ok(()),
        Certificate::TransferCapacityUpperBound {} if maximize && flow == handle.capacity => Ok(()),
        Certificate::ResidualCut { reachable_nodes } => {
            if reachable_nodes.is_empty()
                || reachable_nodes.len() > graph.edges.len()
                || reachable_nodes.windows(2).any(|w| w[0] >= w[1])
            {
                bail!("invalid residual cut node list");
            }
            let mut inside = vec![false; graph.edges.len()];
            for &node in reachable_nodes {
                if node >= inside.len() {
                    bail!("residual cut node is out of range");
                }
                inside[node] = true;
            }
            let head = graph.edges[handle.from][handle.index].to;
            let (source, sink) = if maximize {
                (head, handle.from)
            } else {
                (handle.from, head)
            };
            if !inside[source] || inside[sink] {
                bail!("residual cut does not separate the required target endpoints");
            }
            let excluded = graph.forbidden(handle);
            for &node in reachable_nodes {
                for (index, edge) in graph.edges[node].iter().enumerate() {
                    budget.tick()?;
                    if edge.residual > 0 && !inside[edge.to] && !excluded.contains(&(node, index)) {
                        bail!("residual cut has positive outgoing capacity");
                    }
                }
            }
            Ok(())
        }
        _ => bail!("certificate kind does not prove this bound"),
    }
}

fn minor(value: &str) -> Result<u128> {
    if value.is_empty() || value.len() > 39 || !value.bytes().all(|b| b.is_ascii_digit()) {
        bail!("report monetary value is not a canonical unsigned decimal integer");
    }
    let amount: u128 = value
        .parse()
        .map_err(|_| anyhow!("report monetary value exceeds integer range"))?;
    if amount.to_string() != value {
        bail!("report monetary value is not canonical");
    }
    Ok(amount)
}

fn assign_flow(graph: &mut Graph, handle: Handle, amount: u128) -> Result<()> {
    if amount > handle.capacity {
        bail!("witness flow exceeds graph capacity");
    }
    let edge = &graph.edges[handle.from][handle.index];
    let to = edge.to;
    let reverse = edge.reverse;
    graph.edges[handle.from][handle.index].residual = handle.capacity - amount;
    graph.edges[to][reverse].residual = amount;
    Ok(())
}

/// Reconstruct every flow edge from supplied allocations and original balances.
/// This path deliberately has no call to max_flow or extremize.
fn reconstruct_witness(
    case: &Case,
    network: &Network,
    supplied: &Witness,
    budget: &mut Budget,
) -> Result<Graph> {
    if supplied.transfer_allocations.len() != case.transfers.len()
        || supplied.final_allocations.len() != case.accounts.len()
    {
        bail!("witness must contain every transfer and final account exactly once");
    }
    let mut graph = network.graph.clone();
    let seed_amount = case.transfers[case.seed].amount;
    assign_flow(&mut graph, network.source_edge, seed_amount)?;
    let mut tracked = vec![0_u128; case.accounts.len()];
    for (i, (transfer, allocation)) in case
        .transfers
        .iter()
        .zip(&supplied.transfer_allocations)
        .enumerate()
    {
        budget.tick()?;
        if allocation.transfer_id != transfer.id {
            bail!("witness transfer IDs/order differ from the complete ledger");
        }
        let amount = minor(&allocation.seeded_minor)?;
        if i < case.seed {
            if amount != 0 {
                bail!("pre-seed witness allocation must be zero");
            }
        } else if i == case.seed {
            if amount != seed_amount {
                bail!("seed witness allocation must equal complete seed amount");
            }
            tracked[transfer.to] = seed_amount;
        } else {
            let (from_carry, to_carry) =
                network.carry_edges[i].context("internal missing carry edges")?;
            assign_flow(&mut graph, to_carry, tracked[transfer.to])?;
            tracked[transfer.from] = tracked[transfer.from]
                .checked_sub(amount)
                .context("witness transfers more seeded value than the sender holds")?;
            tracked[transfer.to] = tracked[transfer.to]
                .checked_add(amount)
                .context("witness allocation overflow")?;
            assign_flow(&mut graph, from_carry, tracked[transfer.from])?;
            assign_flow(
                &mut graph,
                network.transfer_edges[i].context("internal missing transfer edge")?,
                amount,
            )?;
        }
    }
    for (i, allocation) in supplied.final_allocations.iter().enumerate() {
        budget.tick()?;
        if allocation.account != case.accounts[i] {
            bail!("witness final account IDs/order differ from complete ledger");
        }
        if minor(&allocation.seeded_minor)? != tracked[i] {
            bail!("witness final balance differs from transfer allocation replay");
        }
        assign_flow(&mut graph, network.terminal_edges[i], tracked[i])?;
    }
    // A separate original-ledger replay checks the local lower as well as upper
    // allocation bounds and all graph conservation/capacity constraints.
    if witness(case, network, &graph, budget)? != *supplied {
        bail!("witness reconstruction mismatch");
    }
    Ok(graph)
}

/// Verify exact tightness certificates without invoking a max-flow solver.
/// Checks canonical input/metadata, full feasible witnesses and zero residual
/// cut capacity. Solver work_units is informational, bounded but not reproduced.
/// This establishes conditional model bounds, not authenticity/completeness of
/// the ledger or the identity of whoever generated the report.
pub fn verify_certificates(raw: &[u8], report: &Report) -> Result<()> {
    let case = parse(raw)?;
    let network = network(&case)?;
    if report.schema != "flow-attribution/v1"
        || report.input_sha256 != hex::encode(Sha256::digest(raw))
        || report.case_id != case.case_id
        || report.currency != case.currency
    {
        bail!("report is not bound to this input case");
    }
    if report.policy != model_policy(MAX_WORK_UNITS)
        || report.assumptions != model_assumptions()
        || report.seed != model_seed(&case)
        || report.ledger != model_ledger(&case)
    {
        bail!("report policy, assumptions, seed or reconciled ledger metadata mismatch");
    }
    if report.targets.len() != case.targets.len()
        || report.stats.nodes != network.graph.edges.len()
        || report.stats.capacity_edges != network.graph.originals.len()
        || report.stats.suffix_transfers != case.transfers.len() - case.seed - 1
        || report.stats.witnesses_validated != case.targets.len() * 2
        || report.stats.work_units > MAX_WORK_UNITS
    {
        bail!("report target set or structural statistics mismatch");
    }
    let mut budget = Budget {
        used: 0,
        limit: MAX_WORK_UNITS,
    };
    for (&position, target) in case.targets.iter().zip(&report.targets) {
        let transfer = &case.transfers[position];
        if target.transfer_id != transfer.id
            || target.sequence != transfer.sequence
            || target.amount_minor != transfer.amount.to_string()
        {
            bail!("report target identity/order/amount mismatch");
        }
        let minimum = minor(&target.minimum_minor)?;
        let maximum = minor(&target.maximum_minor)?;
        if minimum > maximum || maximum > transfer.amount {
            bail!("invalid report target bounds");
        }
        for (maximize, claimed, supplied, proof) in [
            (false, minimum, &target.min_witness, &target.min_certificate),
            (true, maximum, &target.max_witness, &target.max_certificate),
        ] {
            let graph = reconstruct_witness(&case, &network, supplied, &mut budget)?;
            if let Some(handle) = network.transfer_edges[position] {
                if graph.flow(handle)? != claimed {
                    bail!("witness does not attain the claimed target bound");
                }
                check_certificate(&graph, handle, maximize, proof, &mut budget)?;
            } else {
                let (expected, kind) = if position < case.seed {
                    (0, Certificate::BeforeSeed {})
                } else {
                    (
                        case.transfers[case.seed].amount,
                        Certificate::SeedDefinition {},
                    )
                };
                if claimed != expected || *proof != kind {
                    bail!("invalid fixed-by-definition target certificate");
                }
            }
        }
    }
    // All vector lengths, identifiers, monetary strings and cut node sets have
    // already been bounded and checked before allocating serialized output.
    if serde_json::to_vec(report)?.len() > MAX_REPORT_BYTES {
        bail!("certificate report exceeds size limit");
    }
    Ok(())
}

fn model_policy(work_limit: u64) -> Policy {
    Policy {
            model: "Temporal capacity flow with exact residual-circulation edge bounds".into(),
            max_input_bytes: MAX_INPUT_BYTES, max_transfers: MAX_TRANSFERS, max_accounts: MAX_ACCOUNTS,
            max_targets: MAX_TARGETS, max_work_units: work_limit, max_report_bytes: MAX_REPORT_BYTES,
            work_unit_definition: "One residual edge inspection/augmentation, cut edge check, original-edge conservation check or witness record/account replay; input and graph sizes are separately bounded.".into(),
        }
}
fn model_assumptions() -> Vec<String> {
    vec![
            "Bounds are conditional mathematical allocations, not evidence of actual fund provenance, criminal origin, ownership or legal liability.".into(),
            "All accounts have explicit opening and closing balances; every debit is funded and closing balances reconcile. Completeness is attested by the submitter and cannot be proven from this file alone.".into(),
            "The entire selected seed transfer is labelled at the recipient immediately after credit. Earlier transfers carry zero label, and the seed transfer itself carries its full amount by definition.".into(),
            "A single syntactically valid currency is used. Amounts are nonnegative integer minor units, with no FX, reversals, self-transfers, credit facilities, external deposits or omitted fees.".into(),
            "Positive unique sequence numbers establish event order, including tied timestamps. Timestamps must be nondecreasing in sequence order; arrays may be supplied in any order.".into(),
            "Each target minimum and maximum is tight independently and has a complete feasible witness. Bounds for different targets, or both endpoints of one target, need not be simultaneously attainable.".into(),
            "Any mixing allocation satisfying temporal capacity and conservation is allowed. FIFO, proportional mixing and account-wide reachability are not assumed to establish truth.".into(),
            "Certificate verification checks metadata, structural statistics, feasible witnesses and exact bounds without running the solver. The reported solver work count is informational and is not independently reproduced by certificate verification.".into(),
            "All monetary report fields are exact decimal strings. Exceeded work or size limits return an error rather than approximate or partial bounds.".into(),
        ]
}
fn model_seed(case: &Case) -> Seed {
    Seed {
        transfer_id: case.transfers[case.seed].id.clone(),
        sequence: case.transfers[case.seed].sequence,
        recipient: case.accounts[case.transfers[case.seed].to].clone(),
        amount_minor: case.transfers[case.seed].amount.to_string(),
    }
}
fn model_ledger(case: &Case) -> Ledger {
    Ledger {
        completeness_attested: true,
        closing_balances_reconciled: true,
        transfer_count: case.transfers.len(),
        opening_total_minor: case.opening.iter().sum::<u128>().to_string(),
        closing_total_minor: case.closing.iter().sum::<u128>().to_string(),
        accounts: case
            .accounts
            .iter()
            .enumerate()
            .map(|(i, account)| AccountBalance {
                account: account.clone(),
                opening_minor: case.opening[i].to_string(),
                closing_minor: case.closing[i].to_string(),
            })
            .collect(),
    }
}

/// Exact target-specific bounds, conditional on the attested reconciled ledger.
/// Errors never produce partial bounds. Each bound has its own feasible witness;
/// bounds for different targets need not hold simultaneously.
pub fn analyze(raw: &[u8]) -> Result<Report> {
    analyze_with_limit(raw, MAX_WORK_UNITS)
}
fn analyze_with_limit(raw: &[u8], work_limit: u64) -> Result<Report> {
    let case = parse(raw)?;
    let mut network = network(&case)?;
    let mut budget = Budget {
        used: 0,
        limit: work_limit,
    };
    let seed_amount = case.transfers[case.seed].amount;
    let initial =
        network
            .graph
            .max_flow(network.source, network.sink, seed_amount, &[], &mut budget)?;
    if initial != seed_amount {
        bail!("no feasible allocation for the complete seed amount");
    }
    let mut targets = Vec::new();
    for &position in &case.targets {
        let transfer = &case.transfers[position];
        let (minimum, maximum, min_witness, max_witness, min_certificate, max_certificate) =
            if let Some(handle) = network.transfer_edges[position] {
                let min_graph = extremize(&network.graph, handle, false, &mut budget)?;
                let minimum = min_graph.flow(handle)?;
                let min_witness = witness(&case, &network, &min_graph, &mut budget)?;
                let min_certificate = certificate(&min_graph, handle, false, &mut budget)?;
                let max_graph = extremize(&network.graph, handle, true, &mut budget)?;
                let maximum = max_graph.flow(handle)?;
                let max_witness = witness(&case, &network, &max_graph, &mut budget)?;
                let max_certificate = certificate(&max_graph, handle, true, &mut budget)?;
                if minimum > maximum {
                    bail!("internal target bound ordering failure");
                }
                (
                    minimum,
                    maximum,
                    min_witness,
                    max_witness,
                    min_certificate,
                    max_certificate,
                )
            } else {
                let amount = if position == case.seed {
                    seed_amount
                } else {
                    0
                };
                let min_witness = witness(&case, &network, &network.graph, &mut budget)?;
                // Recheck the second witness rather than count an unchecked artifact.
                let max_witness = witness(&case, &network, &network.graph, &mut budget)?;
                let fixed = if position == case.seed {
                    Certificate::SeedDefinition {}
                } else {
                    Certificate::BeforeSeed {}
                };
                (
                    amount,
                    amount,
                    min_witness,
                    max_witness,
                    fixed.clone(),
                    fixed,
                )
            };
        targets.push(TargetReport {
            transfer_id: transfer.id.clone(),
            sequence: transfer.sequence,
            amount_minor: transfer.amount.to_string(),
            minimum_minor: minimum.to_string(),
            maximum_minor: maximum.to_string(),
            min_witness,
            max_witness,
            min_certificate,
            max_certificate,
        });
    }
    let report = Report {
        schema: "flow-attribution/v1".into(),
        input_sha256: hex::encode(Sha256::digest(raw)),
        case_id: case.case_id.clone(),
        currency: case.currency.clone(),
        policy: model_policy(work_limit),
        assumptions: model_assumptions(),
        seed: model_seed(&case),
        ledger: model_ledger(&case),
        stats: Stats {
            nodes: network.graph.edges.len(),
            capacity_edges: network.graph.originals.len(),
            suffix_transfers: case.transfers.len() - case.seed - 1,
            work_units: budget.used,
            witnesses_validated: targets.len() * 2,
        },
        targets,
    };
    if serde_json::to_vec(&report)?.len() > MAX_REPORT_BYTES {
        bail!("flow report exceeds 16 MiB limit; no complete report produced");
    }
    Ok(report)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::{json, Value};
    fn transfer(id: &str, sequence: u64, from: &str, to: &str, amount: u64) -> Value {
        json!({"id":id,"timestamp":"2026-01-01T00:00:00Z","sequence":sequence,"from_account":from,"to_account":to,"amount_minor":amount})
    }
    fn case(opening: &[(&str, u64)], transfers: Vec<Value>, seed: &str, targets: &[&str]) -> Value {
        let mut ending: BTreeMap<String, u64> =
            opening.iter().map(|(a, n)| (a.to_string(), *n)).collect();
        for t in &transfers {
            let amount = t["amount_minor"].as_u64().unwrap();
            *ending.get_mut(t["from_account"].as_str().unwrap()).unwrap() -= amount;
            *ending.get_mut(t["to_account"].as_str().unwrap()).unwrap() += amount;
        }
        json!({"schema":"flow-case/v1","case_id":"lab","currency":"USD","completeness_attested":true,
            "opening_balances":opening.iter().map(|(a,n)| json!({"account":a,"amount_minor":n})).collect::<Vec<_>>(),
            "closing_balances":ending.iter().map(|(a,n)| json!({"account":a,"amount_minor":n})).collect::<Vec<_>>(),
            "transfers":transfers,"seed_transfer_id":seed,"target_transfer_ids":targets})
    }
    fn run(input: &Value) -> Report {
        let raw = serde_json::to_vec(input).unwrap();
        let report = analyze(&raw).unwrap();
        verify_certificates(&raw, &report).unwrap();
        report
    }
    fn bounds(report: &Report, id: &str) -> (u128, u128) {
        let t = report.targets.iter().find(|t| t.transfer_id == id).unwrap();
        (
            t.minimum_minor.parse().unwrap(),
            t.maximum_minor.parse().unwrap(),
        )
    }
    fn ambiguous() -> Value {
        case(
            &[("A", 10), ("B", 10), ("C", 0), ("D", 0)],
            vec![
                transfer("seed", 1, "A", "B", 10),
                transfer("target", 2, "B", "C", 10),
                transfer("later", 3, "B", "D", 10),
            ],
            "seed",
            &["target", "later"],
        )
    }
    #[test]
    fn exact_transfer_forced_by_empty_recipient_balance() {
        let input = case(
            &[("A", 10), ("B", 0), ("C", 0)],
            vec![
                transfer("seed", 1, "A", "B", 10),
                transfer("target", 2, "B", "C", 7),
            ],
            "seed",
            &["seed", "target"],
        );
        let report = run(&input);
        assert_eq!(bounds(&report, "seed"), (10, 10));
        assert_eq!(bounds(&report, "target"), (7, 7));
        assert_eq!(report.stats.witnesses_validated, 4);
    }
    #[test]
    fn ambiguous_bounds_have_distinct_feasible_complete_witnesses() {
        let report = run(&ambiguous());
        assert_eq!(bounds(&report, "target"), (0, 10));
        assert_eq!(bounds(&report, "later"), (0, 10));
        let target = &report.targets[0];
        assert_ne!(target.min_witness, target.max_witness);
        assert_eq!(target.min_witness.transfer_allocations[1].seeded_minor, "0");
        assert_eq!(
            target.max_witness.transfer_allocations[1].seeded_minor,
            "10"
        );
        assert_eq!(
            target
                .min_witness
                .final_allocations
                .iter()
                .map(|a| a.seeded_minor.parse::<u128>().unwrap())
                .sum::<u128>(),
            10
        );
    }
    #[test]
    fn partially_forced_transfer_matches_integer_capacity_bound() {
        let input = case(
            &[("A", 10), ("B", 3), ("C", 0)],
            vec![
                transfer("seed", 1, "A", "B", 10),
                transfer("target", 2, "B", "C", 8),
            ],
            "seed",
            &["target"],
        );
        assert_eq!(bounds(&run(&input), "target"), (5, 8));
    }
    #[test]
    fn split_merge_and_repeated_account_edges_preserve_bounds() {
        let input = case(
            &[("A", 6), ("B", 4), ("C", 0), ("D", 0), ("E", 0)],
            vec![
                transfer("seed", 1, "A", "B", 6),
                transfer("split1", 2, "B", "C", 5),
                transfer("split2", 3, "B", "D", 5),
                transfer("merge1", 4, "C", "E", 5),
                transfer("merge2", 5, "D", "E", 5),
                transfer("return", 6, "E", "B", 7),
            ],
            "seed",
            &["split1", "split2", "return"],
        );
        let report = run(&input);
        assert_eq!(bounds(&report, "split1"), (1, 5));
        assert_eq!(bounds(&report, "split2"), (1, 5));
        assert_eq!(bounds(&report, "return"), (3, 6));
    }
    #[test]
    fn causal_time_prevents_retroactive_attribution() {
        let input = case(
            &[("A", 10), ("B", 10), ("C", 0)],
            vec![
                transfer("earlier", 1, "B", "C", 10),
                transfer("seed", 2, "A", "B", 10),
            ],
            "seed",
            &["earlier", "seed"],
        );
        let report = run(&input);
        assert_eq!(bounds(&report, "earlier"), (0, 0));
        assert_eq!(bounds(&report, "seed"), (10, 10));
    }
    #[test]
    fn explicit_sequences_resolve_ties_and_input_array_order() {
        let first = run(&ambiguous());
        let mut input = ambiguous();
        input["transfers"].as_array_mut().unwrap().reverse();
        input["target_transfer_ids"]
            .as_array_mut()
            .unwrap()
            .reverse();
        let second = run(&input);
        assert_eq!(first.targets, second.targets);
        assert_eq!(first.stats, second.stats);
        assert_ne!(first.input_sha256, second.input_sha256);
        let raw = serde_json::to_vec(&input).unwrap();
        assert_eq!(
            serde_json::to_vec(&analyze(&raw).unwrap()).unwrap(),
            serde_json::to_vec(&second).unwrap()
        );
    }
    #[test]
    fn source_reversal_and_return_cycles_work() {
        let input = case(
            &[("A", 10), ("B", 0), ("C", 10)],
            vec![
                transfer("seed", 1, "A", "B", 10),
                transfer("return", 2, "B", "A", 6),
                transfer("mix", 3, "C", "A", 10),
                transfer("again", 4, "A", "B", 8),
            ],
            "seed",
            &["return", "again"],
        );
        let report = run(&input);
        assert_eq!(bounds(&report, "return"), (6, 6));
        assert_eq!(bounds(&report, "again"), (0, 6));
    }
    #[test]
    fn u64_max_values_remain_exact() {
        let input = case(
            &[("A", u64::MAX), ("B", 0), ("C", 0)],
            vec![
                transfer("seed", 1, "A", "B", u64::MAX),
                transfer("target", 2, "B", "C", u64::MAX),
            ],
            "seed",
            &["target"],
        );
        let report = run(&input);
        assert_eq!(
            bounds(&report, "target"),
            (u128::from(u64::MAX), u128::from(u64::MAX))
        );
    }
    #[test]
    fn strict_schema_and_ledger_rejection() {
        for mutate in 0..14 {
            let mut input = ambiguous();
            match mutate {
                0 => input["extra"] = json!("secret"),
                1 => input["completeness_attested"] = json!(false),
                2 => input["closing_balances"][0]["amount_minor"] = json!(999),
                3 => input["transfers"][1]["amount_minor"] = json!(100),
                4 => input["transfers"][1]["sequence"] = json!(1),
                5 => input["transfers"][1]["sequence"] = json!(0),
                6 => input["transfers"][1]["id"] = json!("seed"),
                7 => input["transfers"][1]["timestamp"] = json!("2025-01-01T00:00:00Z"),
                8 => input["transfers"][1]["from_account"] = json!("missing"),
                9 => input["transfers"][1]["to_account"] = json!("B"),
                10 => input["target_transfer_ids"] = json!(["target", "target"]),
                11 => input["seed_transfer_id"] = json!("missing"),
                12 => input["target_transfer_ids"] = json!(["missing"]),
                _ => input["transfers"][1]["amount_minor"] = json!(0),
            }
            assert!(
                analyze(&serde_json::to_vec(&input).unwrap()).is_err(),
                "mutation {mutate} accepted"
            );
        }
        let mut input = ambiguous();
        input.as_object_mut().unwrap().remove("closing_balances");
        assert!(analyze(&serde_json::to_vec(&input).unwrap()).is_err());
    }
    #[test]
    fn work_and_input_caps_fail_without_partial_report() {
        let raw = serde_json::to_vec(&ambiguous()).unwrap();
        assert!(analyze_with_limit(&raw, 1)
            .unwrap_err()
            .to_string()
            .contains("work limit"));
        assert!(analyze(&vec![b' '; MAX_INPUT_BYTES + 1])
            .unwrap_err()
            .to_string()
            .contains("2 MiB"));
        let mut input = ambiguous();
        input["target_transfer_ids"] = json!(vec!["target"; MAX_TARGETS + 1]);
        assert!(analyze(&serde_json::to_vec(&input).unwrap()).is_err());
    }
    #[test]
    fn longest_allowed_chain_uses_no_recursive_solver() {
        let transfers: Vec<_> = (0..MAX_TRANSFERS)
            .map(|i| {
                transfer(
                    &format!("t{i}"),
                    i as u64 + 1,
                    if i % 2 == 0 { "A" } else { "B" },
                    if i % 2 == 0 { "B" } else { "A" },
                    1,
                )
            })
            .collect();
        let input = case(&[("A", 1), ("B", 0)], transfers, "t0", &["t999"]);
        assert_eq!(bounds(&run(&input), "t999"), (1, 1));
    }
    #[test]
    fn certificate_verifier_rejects_metadata_and_witness_tampering() {
        let raw = serde_json::to_vec(&ambiguous()).unwrap();
        let original = analyze(&raw).unwrap();
        verify_certificates(&raw, &original).unwrap();
        for mutation in 0..12 {
            let mut report = original.clone();
            match mutation {
                0 => report.input_sha256 = "0".repeat(64),
                1 => report.policy.max_work_units += 1,
                2 => report.stats.nodes += 1,
                3 => report.seed.amount_minor = "11".into(),
                4 => report.targets[0].minimum_minor = "1".into(),
                5 => {
                    report.targets[0].min_witness.transfer_allocations[1].transfer_id =
                        "seed".into()
                }
                6 => report.targets[0].min_witness.final_allocations[1].account = "A".into(),
                7 => {
                    report.targets[0].max_witness.transfer_allocations[1].seeded_minor = "11".into()
                }
                8 => report.targets[0].maximum_minor = "010".into(),
                9 => report.targets[0].min_certificate = Certificate::TransferCapacityUpperBound {},
                10 => report.targets[0]
                    .min_witness
                    .final_allocations
                    .pop()
                    .map(|_| ())
                    .unwrap(),
                _ => report.targets.swap(0, 1),
            }
            assert!(
                verify_certificates(&raw, &report).is_err(),
                "accepted tampering mutation {mutation}"
            );
        }
    }
    #[test]
    fn cuts_reject_duplicates_invalid_nodes_and_missing_separation() {
        let input = case(
            &[("A", 10), ("B", 3), ("C", 0)],
            vec![
                transfer("seed", 1, "A", "B", 10),
                transfer("target", 2, "B", "C", 8),
            ],
            "seed",
            &["target"],
        );
        let raw = serde_json::to_vec(&input).unwrap();
        let original = analyze(&raw).unwrap();
        assert!(matches!(
            original.targets[0].min_certificate,
            Certificate::ResidualCut { .. }
        ));
        for nodes in [
            vec![],
            vec![usize::MAX],
            vec![0, 0],
            (0..original.stats.nodes).collect(),
        ] {
            let mut report = original.clone();
            report.targets[0].min_certificate = Certificate::ResidualCut {
                reachable_nodes: nodes,
            };
            assert!(verify_certificates(&raw, &report).is_err());
        }
    }
    #[test]
    fn feasible_but_nonoptimal_witness_fails_residual_cut_proof() {
        let input = case(
            &[("A", 10), ("B", 3), ("C", 0)],
            vec![
                transfer("seed", 1, "A", "B", 10),
                transfer("target", 2, "B", "C", 8),
            ],
            "seed",
            &["target"],
        );
        let raw = serde_json::to_vec(&input).unwrap();
        let mut report = analyze(&raw).unwrap();
        report.targets[0].minimum_minor = "6".into();
        report.targets[0].min_witness.transfer_allocations[1].seeded_minor = "6".into();
        for allocation in &mut report.targets[0].min_witness.final_allocations {
            if allocation.account == "B" {
                allocation.seeded_minor = "4".into();
            }
            if allocation.account == "C" {
                allocation.seeded_minor = "6".into();
            }
        }
        let error = verify_certificates(&raw, &report).unwrap_err().to_string();
        assert!(error.contains("positive outgoing capacity"), "{error}");
    }
    #[test]
    fn fixed_seed_certificates_are_required_and_checked() {
        let input = case(
            &[("A", 10), ("B", 10), ("C", 0)],
            vec![
                transfer("earlier", 1, "B", "C", 10),
                transfer("seed", 2, "A", "B", 10),
            ],
            "seed",
            &["earlier", "seed"],
        );
        let raw = serde_json::to_vec(&input).unwrap();
        let original = analyze(&raw).unwrap();
        assert_eq!(
            original.targets[0].min_certificate,
            Certificate::BeforeSeed {}
        );
        assert_eq!(
            original.targets[1].max_certificate,
            Certificate::SeedDefinition {}
        );
        let mut report = original.clone();
        report.targets[0].min_certificate = Certificate::ZeroLowerBound {};
        assert!(verify_certificates(&raw, &report).is_err());
        let mut report = original;
        report.targets[1].max_certificate = Certificate::TransferCapacityUpperBound {};
        assert!(verify_certificates(&raw, &report).is_err());
    }
    #[test]
    fn public_report_deserialization_is_strict_and_roundtrips() {
        let raw = serde_json::to_vec(&ambiguous()).unwrap();
        let original = analyze(&raw).unwrap();
        let encoded = serde_json::to_vec(&original).unwrap();
        let decoded: Report = serde_json::from_slice(&encoded).unwrap();
        verify_certificates(&raw, &decoded).unwrap();
        for mutate in 0..4 {
            let mut value = serde_json::to_value(&original).unwrap();
            match mutate {
                0 => value["unexpected"] = json!(true),
                1 => value["policy"]["unexpected"] = json!(true),
                2 => value["targets"][0]["min_witness"]["unexpected"] = json!(true),
                _ => value["targets"][0]["min_certificate"]["unexpected"] = json!(true),
            }
            assert!(serde_json::from_value::<Report>(value).is_err());
        }
    }
    #[test]
    fn bounded_deserialization_stops_before_oversized_array_tail() {
        // A tagged enum that buffers its map would reach the malformed tail.
        // The custom certificate decoder must stop at the structural limit.
        let attack = format!(
            "{{\"kind\":\"residual_cut\",\"reachable_nodes\":[{}malformed",
            "0,".repeat(MAX_GRAPH_NODES + 1)
        );
        let error = serde_json::from_str::<Certificate>(&attack)
            .unwrap_err()
            .to_string();
        assert!(
            error.contains("array exceeds structural resource limit"),
            "{error}"
        );
        let original = run(&ambiguous());
        let mut value = serde_json::to_value(&original).unwrap();
        value["assumptions"] = json!(vec!["x"; 17]);
        assert!(serde_json::from_value::<Report>(value).is_err());
        let mut value = serde_json::to_value(&original).unwrap();
        value["targets"][0]["min_witness"]["final_allocations"] = json!(vec![
            json!({"account":"A","seeded_minor":"0"});
            MAX_ACCOUNTS + 1
        ]);
        assert!(serde_json::from_value::<Report>(value).is_err());
        assert!(serde_json::from_str::<Certificate>(
            r#"{"kind":"zero_lower_bound","reachable_nodes":[]}"#
        )
        .is_err());
        assert!(serde_json::from_str::<Certificate>(
            r#"{"kind":"zero_lower_bound","reachable_nodes":null}"#
        )
        .is_err());
    }
    #[test]
    fn intermediate_balances_can_exceed_u64_without_precision_loss() {
        let input = json!({"schema":"flow-case/v1","case_id":"large-intermediate","currency":"USD","completeness_attested":true,
            "opening_balances":[{"account":"A","amount_minor":u64::MAX},{"account":"B","amount_minor":u64::MAX},{"account":"C","amount_minor":0}],
            "closing_balances":[{"account":"A","amount_minor":0},{"account":"B","amount_minor":u64::MAX},{"account":"C","amount_minor":u64::MAX}],
            "transfers":[transfer("seed",1,"A","B",u64::MAX),transfer("target",2,"B","C",u64::MAX)],
            "seed_transfer_id":"seed","target_transfer_ids":["target"]});
        let report = run(&input);
        assert_eq!(bounds(&report, "target"), (0, u128::from(u64::MAX)));
        assert_eq!(
            report.ledger.opening_total_minor,
            (u128::from(u64::MAX) * 2).to_string()
        );
    }
    #[test]
    fn forged_double_spending_and_oversized_monetary_strings_fail() {
        let raw = serde_json::to_vec(&ambiguous()).unwrap();
        let original = analyze(&raw).unwrap();
        let mut double_spend = original.clone();
        double_spend.targets[0].min_witness.transfer_allocations[1].seeded_minor = "10".into();
        double_spend.targets[0].min_witness.transfer_allocations[2].seeded_minor = "10".into();
        assert!(verify_certificates(&raw, &double_spend)
            .unwrap_err()
            .to_string()
            .contains("more seeded value"));
        let mut report = original;
        report.targets[0].minimum_minor = "9".repeat(1_000_000);
        assert!(verify_certificates(&raw, &report)
            .unwrap_err()
            .to_string()
            .contains("canonical"));
    }
}
