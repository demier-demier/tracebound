#!/usr/bin/env python3
"""Independently check flow bounds by enumerating integer color allocations.

This oracle does not construct a graph, call a flow library, or reuse solver code.
For an outgoing transfer of m units, sender balance B and tracked balance x permit
exactly k in [max(0, m - (B - x)), min(m, x)] tracked units. It enumerates every
reachable tracked-account vector at every event. Every such state can extend
through a valid closed ledger, so observed per-event extrema are global extrema.
It also replays each solver witness directly against the original ledger.

The 100 systematic cases exhaust two-event, two-account ledgers with total opening
balance 1..3, all positive transfer amounts and both seed positions. Random cases
add three/four accounts, longer histories, and mixing. This is exact checking of
tiny integer cases, not a proof of arbitrary-size implementation correctness or
evidence that supplied real-world ledgers are complete.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import random
import re
import subprocess
import tempfile
import time


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def ledger(case_id, opening, movements, seed_index):
    balances = dict(opening)
    transfers = []
    for index, (sender, recipient, amount) in enumerate(movements):
        require(sender != recipient and 0 < amount <= balances[sender], "bad generated movement")
        balances[sender] -= amount
        balances[recipient] += amount
        transfers.append(
            {
                "id": f"t{index:03}",
                "timestamp": f"2026-01-01T00:{index:02}:00Z",
                "sequence": index + 1,
                "from_account": sender,
                "to_account": recipient,
                "amount_minor": amount,
            }
        )
    return {
        "schema": "flow-case/v1",
        "case_id": case_id,
        "currency": "USD",
        "completeness_attested": True,
        "opening_balances": [
            {"account": account, "amount_minor": value}
            for account, value in sorted(opening.items())
        ],
        "closing_balances": [
            {"account": account, "amount_minor": value}
            for account, value in sorted(balances.items())
        ],
        "transfers": transfers,
        "seed_transfer_id": transfers[seed_index]["id"],
        "target_transfer_ids": [row["id"] for row in transfers],
    }


def possible_moves(balances):
    for sender in sorted(balances):
        for recipient in sorted(balances):
            if sender != recipient:
                for amount in range(1, balances[sender] + 1):
                    yield sender, recipient, amount


def systematic_cases():
    cases = []
    for total in range(1, 4):
        for a in range(total + 1):
            opening = {"A": a, "B": total - a}
            for first in possible_moves(opening):
                after = dict(opening)
                after[first[0]] -= first[2]
                after[first[1]] += first[2]
                for second in possible_moves(after):
                    for seed_index in range(2):
                        cases.append(
                            ledger(f"systematic-{len(cases):03}", opening, [first, second], seed_index)
                        )
    require(len(cases) == 100, "systematic corpus unexpectedly changed")
    return cases


def random_cases(count, seed):
    rng = random.Random(seed)
    cases = []
    for index in range(count):
        accounts = list("ABCD")[: rng.randint(3, 4)]
        opening = {account: rng.randint(0, 4) for account in accounts}
        if not any(opening.values()):
            opening[accounts[0]] = 1
        balances = dict(opening)
        movements = []
        for _ in range(rng.randint(3, 9)):
            sender = rng.choice([account for account in accounts if balances[account]])
            recipient = rng.choice([account for account in accounts if account != sender])
            amount = rng.randint(1, min(4, balances[sender]))
            balances[sender] -= amount
            balances[recipient] += amount
            movements.append((sender, recipient, amount))
        case = ledger(f"random-{index:03}", opening, movements, rng.randrange(len(movements)))
        # Array order carries no chronology; the explicit trusted sequence does.
        rng.shuffle(case["transfers"])
        rng.shuffle(case["target_transfer_ids"])
        rng.shuffle(case["opening_balances"])
        rng.shuffle(case["closing_balances"])
        cases.append(case)
    return cases


def named_cases():
    return [
        ledger("named-forced-flow", {"A": 4, "B": 0, "C": 0}, [("A", "B", 4), ("B", "C", 3)], 0),
        ledger("named-ambiguous-branch", {"A": 4, "B": 4, "C": 0, "D": 0}, [("A", "B", 4), ("B", "C", 4), ("B", "D", 4)], 0),
        ledger("named-partially-forced", {"A": 4, "B": 1, "C": 0}, [("A", "B", 4), ("B", "C", 3)], 0),
        ledger("named-returning-units", {"A": 3, "B": 2, "C": 0}, [("A", "B", 3), ("B", "C", 4), ("C", "A", 2), ("A", "B", 1)], 0),
        ledger("named-prefix-untracked", {"A": 3, "B": 1, "C": 0}, [("A", "B", 2), ("B", "C", 3), ("C", "A", 2)], 1),
    ]


def enumerate_oracle(case):
    opening = {row["account"]: row["amount_minor"] for row in case["opening_balances"]}
    accounts = sorted(opening)
    account_index = {account: index for index, account in enumerate(accounts)}
    balances = dict(opening)
    states = {tuple(0 for _ in accounts)}
    bounds = {}
    active = False
    seed_amount = None
    max_states = 1
    for row in sorted(case["transfers"], key=lambda item: item["sequence"]):
        sender, recipient, amount = row["from_account"], row["to_account"], row["amount_minor"]
        require(amount <= balances[sender], "oracle received an overdrawn ledger")
        source, destination = account_index[sender], account_index[recipient]
        if row["id"] == case["seed_transfer_id"]:
            require(not active, "duplicate seed")
            active = True
            seed_amount = amount
            state = [0 for _ in accounts]
            state[destination] = amount
            states = {tuple(state)}
            bounds[row["id"]] = (amount, amount)
        elif active:
            next_states = set()
            minimum, maximum = amount, 0
            for state in states:
                tracked = state[source]
                lower = max(0, amount - (balances[sender] - tracked))
                upper = min(amount, tracked)
                require(lower <= upper, "no valid allocation from reachable state")
                for allocation in range(lower, upper + 1):
                    updated = list(state)
                    updated[source] -= allocation
                    updated[destination] += allocation
                    next_states.add(tuple(updated))
                    minimum = min(minimum, allocation)
                    maximum = max(maximum, allocation)
            states = next_states
            bounds[row["id"]] = (minimum, maximum)
        else:
            bounds[row["id"]] = (0, 0)
        balances[sender] -= amount
        balances[recipient] += amount
        max_states = max(max_states, len(states))
        for state in states:
            require(all(0 <= state[i] <= balances[a] for i, a in enumerate(accounts)), "oracle capacity violation")
            require(sum(state) == (seed_amount if active else 0), "oracle lost tracked units")
    require(active, "missing seed")
    closing = {row["account"]: row["amount_minor"] for row in case["closing_balances"]}
    require(balances == closing, "oracle closing mismatch")
    return bounds, max_states


def decimal(value):
    require(isinstance(value, str) and re.fullmatch(r"0|[1-9][0-9]*", value) is not None, "noncanonical integer evidence")
    return int(value)


def allocation_map(rows, id_field):
    result = {}
    for row in rows:
        key = row[id_field]
        require(key not in result, "duplicate witness allocation")
        result[key] = decimal(row["seeded_minor"])
    return result


def replay_witness(case, witness, target_id, extremum):
    transfers = sorted(case["transfers"], key=lambda item: item["sequence"])
    allocations = allocation_map(witness["transfer_allocations"], "transfer_id")
    require(set(allocations) == {row["id"] for row in transfers}, "witness omits or adds transfers")
    require(allocations[target_id] == extremum, "witness does not attain advertised bound")
    balances = {row["account"]: row["amount_minor"] for row in case["opening_balances"]}
    tracked = dict.fromkeys(balances, 0)
    active = False
    seed_amount = 0
    for row in transfers:
        sender, recipient, amount = row["from_account"], row["to_account"], row["amount_minor"]
        allocation = allocations[row["id"]]
        require(0 < amount <= balances[sender], "witness ledger overdraw")
        if row["id"] == case["seed_transfer_id"]:
            require(not active and allocation == amount, "seed must be fully tracked exactly once")
            active = True
            seed_amount = amount
            tracked[recipient] += amount
        elif active:
            lower = max(0, amount - (balances[sender] - tracked[sender]))
            upper = min(amount, tracked[sender])
            require(lower <= allocation <= upper, "witness assigns impossible tracked amount")
            tracked[sender] -= allocation
            tracked[recipient] += allocation
        else:
            require(allocation == 0, "witness tracks units before seed receipt")
        balances[sender] -= amount
        balances[recipient] += amount
        require(sum(tracked.values()) == seed_amount, "witness violates conservation")
        require(all(0 <= tracked[a] <= balances[a] for a in balances), "witness exceeds an account balance")
    closing = {row["account"]: row["amount_minor"] for row in case["closing_balances"]}
    require(balances == closing, "witness closing balance mismatch")
    require(allocation_map(witness["final_allocations"], "account") == tracked, "witness terminal allocation mismatch")


def validate_report(case, report, expected):
    targets = report["targets"]
    ids = [target["transfer_id"] for target in targets]
    require(len(ids) == len(set(ids)), "duplicate report targets")
    require(set(ids) == set(case["target_transfer_ids"]), "report target set mismatch")
    for target in targets:
        target_id = target["transfer_id"]
        minimum, maximum = decimal(target["minimum_minor"]), decimal(target["maximum_minor"])
        require((minimum, maximum) == expected[target_id], f"non-tight interval at {target_id}: got {(minimum, maximum)}, expected {expected[target_id]}")
        replay_witness(case, target["min_witness"], target_id, minimum)
        replay_witness(case, target["max_witness"], target_id, maximum)
    return len(targets)


def reject_corrupted_report_controls(case, report, expected):
    """Show that interval comparison and direct witness replay actually reject bad evidence."""
    bad_bound = copy.deepcopy(report)
    target = bad_bound["targets"][0]
    target["minimum_minor"] = str(decimal(target["minimum_minor"]) + 1)
    bad_terminal = copy.deepcopy(report)
    allocation = bad_terminal["targets"][0]["min_witness"]["final_allocations"][0]
    allocation["seeded_minor"] = str(decimal(allocation["seeded_minor"]) + 1)
    for broken in [bad_bound, bad_terminal]:
        try:
            validate_report(case, broken, expected)
        except AssertionError:
            continue
        raise AssertionError("independent oracle accepted deliberately corrupted evidence")


def require_marginal_bounds_are_separate(case, report):
    """The two branch maxima deliberately cannot occur in one seed-conserving history."""
    if case["case_id"] != "named-ambiguous-branch":
        return
    targets = {target["transfer_id"]: target for target in report["targets"]}
    first, second = targets["t001"], targets["t002"]
    seed = next(row["amount_minor"] for row in case["transfers"] if row["id"] == case["seed_transfer_id"])
    require(decimal(first["maximum_minor"]) + decimal(second["maximum_minor"]) > seed, "named scenario lost marginal ambiguity")
    require(first["min_witness"] != first["max_witness"], "ambiguous extrema need different witnesses")
    require(first["max_witness"] != second["max_witness"], "incompatible target maxima cannot share a witness")


def negative_cases():
    base = named_cases()[0]
    cases = []

    def changed(name, edit):
        case = copy.deepcopy(base)
        case["case_id"] = name
        edit(case)
        cases.append(case)

    changed("reject-no-attestation", lambda c: c.update(completeness_attested=False))
    changed("reject-missing-opening", lambda c: c["opening_balances"].pop())
    changed("reject-closing-mismatch", lambda c: c["closing_balances"][0].update(amount_minor=1))
    changed("reject-overdraft", lambda c: c["transfers"][0].update(amount_minor=5))
    changed("reject-duplicate-sequence", lambda c: c["transfers"][1].update(sequence=1))
    changed("reject-time-reversal", lambda c: c["transfers"][1].update(timestamp="2025-12-31T23:59:00Z"))
    changed("reject-duplicate-id", lambda c: c["transfers"][1].update(id="t000"))
    changed("reject-missing-seed", lambda c: c.update(seed_transfer_id="absent"))
    changed("reject-missing-target", lambda c: c.update(target_transfer_ids=["absent"]))
    changed("reject-self-transfer", lambda c: c["transfers"][0].update(to_account="A"))
    changed("reject-zero-amount", lambda c: c["transfers"][0].update(amount_minor=0))
    changed("reject-fractional-amount", lambda c: c["transfers"][0].update(amount_minor=1.5))
    return cases


def encoded(case):
    return (json.dumps(case, sort_keys=True, separators=(",", ":")) + "\n").encode()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, default=Path("target/release/tracebound"))
    parser.add_argument("--random-cases", type=int, default=250)
    parser.add_argument("--seed", type=int, default=20261031)
    parser.add_argument("--output", type=Path, help="New summary JSON file; existing files are refused")
    args = parser.parse_args()
    require(0 <= args.random_cases <= 350, "random case count must be 0..350")
    if args.output:
        require(not args.output.exists(), "refusing to overwrite oracle summary")
    binary = args.binary.resolve(strict=True)
    systematic = systematic_cases()
    random = random_cases(args.random_cases, args.seed)
    named = named_cases()
    positive = systematic + random + named
    negatives = negative_cases()
    corpus_hash = hashlib.sha256()
    for case in positive + negatives:
        corpus_hash.update(encoded(case))
    checked_targets = 0
    maximum_states = 0
    corrupted_controls = 0
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="tracebound-oracle-") as temporary:
        input_path = Path(temporary) / "case.json"
        for case in positive:
            expected, states = enumerate_oracle(case)
            maximum_states = max(maximum_states, states)
            input_path.write_bytes(encoded(case))
            process = subprocess.run([str(binary), "analyze", "--input", str(input_path)], capture_output=True, timeout=15)
            require(process.returncode == 0, f"solver rejected valid synthetic case {case['case_id']}")
            report = json.loads(process.stdout)
            try:
                checked_targets += validate_report(case, report, expected)
                require_marginal_bounds_are_separate(case, report)
                if not corrupted_controls:
                    reject_corrupted_report_controls(case, report, expected)
                    corrupted_controls = 2
            except (AssertionError, KeyError, TypeError) as error:
                raise AssertionError(f"{case['case_id']}: {error}") from error
        for case in negatives:
            input_path.write_bytes(encoded(case))
            process = subprocess.run([str(binary), "analyze", "--input", str(input_path)], capture_output=True, timeout=15)
            require(process.returncode != 0, f"solver accepted {case['case_id']}")
            require(not process.stdout.strip(), f"rejected case emitted partial report: {case['case_id']}")
    result = {
        "schema": "tracebound-exhaustive-oracle/v1",
        "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "case_corpus_sha256": corpus_hash.hexdigest(),
        "random_seed": args.seed,
        "systematic_cases_passed": len(systematic),
        "random_cases_passed": len(random),
        "named_cases_passed": len(named),
        "negative_controls_rejected": len(negatives),
        "corrupted_evidence_controls_rejected_by_oracle": corrupted_controls,
        "incompatible_marginal_witness_control_passed": True,
        "target_intervals_checked": checked_targets,
        "extremal_witnesses_replayed": checked_targets * 2,
        "maximum_reachable_allocation_states": maximum_states,
        "elapsed_seconds": round(time.monotonic() - started, 6),
        "scope": "Exact enumeration of reachable integer account-color states on tiny synthetic ledgers; independent direct balance replay of every extremal witness. No graph or solver implementation reused.",
        "limitations": [
            "This is a reproducible finite validation corpus, not a proof for every ledger size or every input.",
            "Completeness and trusted posting order are model assumptions, not established by this oracle.",
            "Intervals are marginal feasible allocations, not probabilities, actual money provenance or evidence of a crime.",
            "Runtime includes process startup and JSON I/O; it is not a solver throughput benchmark.",
        ],
    }
    text = json.dumps(result, indent=2) + "\n"
    if args.output:
        with args.output.open("x", encoding="utf-8") as output:
            output.write(text)
    print(text, end="")


if __name__ == "__main__":
    main()
