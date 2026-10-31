"""Bounded synthetic throughput observations, not deployment or detection claims."""
import argparse
import hashlib
import json
import platform
import random
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def fixture(count, accounts, seed):
    rng = random.Random(seed)
    names = [f"account-{i:03d}" for i in range(accounts)]
    initial = {name: 1_000_000 for name in names}
    balances = dict(initial)
    transfers = []
    for index in range(count):
        sender, recipient = rng.sample(names, 2)
        amount = rng.randint(1, min(balances[sender], 50_000))
        balances[sender] -= amount
        balances[recipient] += amount
        transfers.append({"id": f"t{index:04d}", "sequence": index + 1,
                          "timestamp": "2026-01-01T00:00:00Z", "from_account": sender,
                          "to_account": recipient, "amount_minor": amount})
    targets = [transfers[i]["id"] for i in range(count - 16, count)]
    return {"schema": "flow-case/v1", "case_id": f"random-{count}-{accounts}-{seed}",
            "currency": "EUR", "completeness_attested": True,
            "opening_balances": [{"account": a, "amount_minor": v} for a, v in initial.items()],
            "closing_balances": [{"account": a, "amount_minor": v} for a, v in balances.items()],
            "transfers": transfers, "seed_transfer_id": transfers[0]["id"],
            "target_transfer_ids": targets}


def sample(binary, source):
    import resource
    started = time.perf_counter()
    child = subprocess.run([binary, "analyze", "--input", source], capture_output=True, check=True, timeout=60)
    elapsed = time.perf_counter() - started
    report = json.loads(child.stdout)
    rss = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    return {"wall_seconds": elapsed, "max_rss_bytes": rss * (1 if sys.platform == "darwin" else 1024),
            "report_bytes": len(child.stdout), "work_units": report["stats"]["work_units"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", default="target/release/tracebound")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--sample", type=Path)
    args = parser.parse_args()
    binary = str(Path(args.binary).resolve())
    if args.sample:
        print(json.dumps(sample(binary, str(args.sample))))
        return
    cases = []
    with tempfile.TemporaryDirectory(prefix="tracebound-bench-") as temporary:
        for count, accounts, seed in [(100, 16, 17), (500, 64, 29), (1000, 96, 47)]:
            case = fixture(count, accounts, seed)
            data = json.dumps(case, separators=(",", ":")).encode()
            path = Path(temporary) / "case.json"
            path.write_bytes(data)
            runs = []
            for _ in range(7):
                observed = subprocess.run([sys.executable, __file__, "--sample", str(path), "--binary", binary],
                                          check=True, capture_output=True, timeout=90)
                runs.append(json.loads(observed.stdout))
            cases.append({"transfers": count, "accounts": accounts, "targets": 16, "generator_seed": seed,
                          "input_sha256": hashlib.sha256(data).hexdigest(), "input_bytes": len(data),
                          "median_wall_seconds": statistics.median(r["wall_seconds"] for r in runs),
                          "max_observed_rss_bytes": max(r["max_rss_bytes"] for r in runs), "runs": runs})
    result = {"schema": "tracebound-synthetic-benchmark/v1", "system": platform.system(),
              "machine": platform.machine(), "os_release": platform.release(),
              "binary_sha256": hashlib.sha256(Path(binary).read_bytes()).hexdigest(),
              "scope": "Seven consecutive subprocess observations (including the first run; cache state not controlled) per deterministic random fixture. Includes process start, input parse, graph optimization, witness validation and complete report serialization. Does not include certificate verification, database, browser or network. Not a worst-case or production benchmark.",
              "cases": cases}
    if args.output:
        with args.output.open("x", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2)
            handle.write("\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
