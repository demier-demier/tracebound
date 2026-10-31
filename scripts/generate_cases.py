#!/usr/bin/env python3
"""Reproduce TRACEBOUND's original synthetic complete-ledger examples.

No external data is read. Closing balances follow the scripted ledger exactly;
this generator does not compute, predict or embed solver results.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def case(case_id, opening, rows, target_sequences, seed_sequence=1):
    balances = dict(opening)
    transfers = []
    start = datetime(2026, 1, 15, 9, 0, tzinfo=timezone.utc)
    for sequence, (source, destination, amount) in enumerate(rows, 1):
        assert source != destination
        assert source in balances and destination in balances
        assert isinstance(amount, int) and 0 < amount < 2**53
        assert balances[source] >= amount, (case_id, sequence, source)
        balances[source] -= amount
        balances[destination] += amount
        transfers.append({
            'id': f'{case_id}-{sequence:02}',
            'timestamp': (start + timedelta(minutes=sequence * 5)).isoformat().replace('+00:00', 'Z'),
            'sequence': sequence,
            'from_account': source,
            'to_account': destination,
            'amount_minor': amount,
        })
    assert sum(opening.values()) == sum(balances.values())
    return {
        'schema': 'flow-case/v1',
        'case_id': case_id,
        'currency': 'EUR',
        'completeness_attested': True,
        'opening_balances': [{'account': account, 'amount_minor': value} for account, value in opening.items()],
        'closing_balances': [{'account': account, 'amount_minor': value} for account, value in balances.items()],
        'transfers': transfers,
        'seed_transfer_id': transfers[seed_sequence - 1]['id'],
        'target_transfer_ids': [transfers[i - 1]['id'] for i in target_sequences],
    }


def examples():
    return [
        case('split-payment-review',
             {'origin': 900000, 'payment-desk': 100000, 'invoice-a': 50000,
              'invoice-b': 0, 'settlement': 100000, 'recipient': 0}, [
                 ('origin', 'payment-desk', 900000),
                 ('payment-desk', 'invoice-a', 600000),
                 ('payment-desk', 'invoice-b', 300000),
                 ('invoice-a', 'settlement', 550000),
                 ('invoice-b', 'settlement', 300000),
                 ('settlement', 'recipient', 700000),
             ], [6, 2, 4]),
        case('commingled-payments',
             {'reserve': 1000000, 'clean-income': 600000, 'payment-desk': 250000,
              'invoice-a': 0, 'invoice-b': 0, 'settlement': 0, 'treasury': 0}, [
                 ('clean-income', 'payment-desk', 250000),
                 ('reserve', 'payment-desk', 500000),
                 ('payment-desk', 'invoice-a', 400000),
                 ('payment-desk', 'invoice-b', 350000),
                 ('payment-desk', 'treasury', 200000),
                 ('invoice-a', 'settlement', 350000),
                 ('invoice-b', 'settlement', 300000),
                 ('settlement', 'treasury', 500000),
                 ('treasury', 'reserve', 100000),
                 ('clean-income', 'treasury', 100000),
                 ('treasury', 'invoice-b', 100000),
             ], [3, 8, 9], seed_sequence=2),
        case('seven-hop-bottleneck',
             {'source': 1200000, 'collector': 300000, 'relay-a': 0, 'relay-b': 0,
              'liquidity-pool': 200000, 'clearing': 0, 'receiver': 0, 'reserve': 0}, [
                 ('source', 'collector', 900000),
                 ('collector', 'relay-a', 1100000),
                 ('relay-a', 'relay-b', 1000000),
                 ('relay-b', 'liquidity-pool', 900000),
                 ('liquidity-pool', 'clearing', 1000000),
                 ('clearing', 'receiver', 950000),
                 ('receiver', 'reserve', 850000),
                 ('reserve', 'source', 100000),
                 ('reserve', 'liquidity-pool', 200000),
                 ('liquidity-pool', 'receiver', 100000),
             ], [7, 9, 10]),
        case('split-merge-cycle',
             {'source': 1000000, 'mixer': 300000, 'left-branch': 100000,
              'right-branch': 200000, 'merge': 0, 'vault': 0, 'merchant': 0, 'fees': 0}, [
                 ('source', 'mixer', 600000),
                 ('mixer', 'left-branch', 400000),
                 ('mixer', 'right-branch', 350000),
                 ('left-branch', 'merge', 450000),
                 ('right-branch', 'merge', 500000),
                 ('merge', 'vault', 800000),
                 ('vault', 'mixer', 200000),
                 ('mixer', 'merchant', 300000),
                 ('merge', 'fees', 100000),
                 ('vault', 'merchant', 500000),
                 ('right-branch', 'fees', 20000),
                 ('left-branch', 'fees', 30000),
                 ('mixer', 'left-branch', 50000),
                 ('left-branch', 'merchant', 60000),
             ], [6, 8, 10, 14]),
        case('unrelated-seed',
             {'source': 1000000, 'seed-vault': 0, 'seed-sink': 0,
              'operations': 1000000, 'payroll': 0, 'rent': 0, 'tax': 0, 'archive': 0}, [
                 ('source', 'seed-vault', 500000),
                 ('operations', 'payroll', 400000),
                 ('operations', 'rent', 200000),
                 ('operations', 'tax', 100000),
                 ('seed-vault', 'seed-sink', 250000),
                 ('payroll', 'archive', 100000),
                 ('rent', 'archive', 50000),
                 ('source', 'operations', 200000),
                 ('operations', 'payroll', 100000),
                 ('payroll', 'tax', 50000),
             ], [2, 6, 9]),
        case('treasury-sweep',
             {'treasury': 2000000, 'desk-a': 400000, 'desk-b': 300000,
              'desk-c': 300000, 'clearing': 0, 'reserve': 0, 'operations': 500000}, [
                 ('treasury', 'desk-a', 500000),
                 ('desk-b', 'clearing', 250000),
                 ('desk-c', 'clearing', 250000),
                 ('desk-a', 'clearing', 700000),
                 ('clearing', 'reserve', 1000000),
                 ('reserve', 'treasury', 400000),
                 ('treasury', 'operations', 300000),
                 ('operations', 'desk-a', 200000),
                 ('reserve', 'operations', 200000),
                 ('desk-a', 'clearing', 100000),
                 ('clearing', 'reserve', 200000),
                 ('reserve', 'treasury', 100000),
             ], [5, 7, 11]),
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Verify checked-in files without overwriting them')
    args = parser.parse_args()
    for data in examples():
        path = ROOT / 'examples' / f"{data['case_id']}.json"
        serialized = json.dumps(data, indent=2) + '\n'
        if args.check:
            if path.read_text() != serialized:
                raise SystemExit(f'Fixture differs from generator: {path.name}')
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(serialized)
        print(f"{'verified' if args.check else 'wrote'} {path.name}: {len(data['transfers'])} transfers")


if __name__ == '__main__':
    main()
