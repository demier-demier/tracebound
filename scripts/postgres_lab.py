#!/usr/bin/env python3
"""Exercise normalized PostgreSQL tables and an isolated read-only ledger snapshot.

This is a synthetic local integration lab, not a production database connector.
It creates and removes its own socket-only cluster and never accepts a DSN.
"""

from __future__ import annotations

import argparse
import copy
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import subprocess
import sys
import tempfile
import time


PORT = "55439"
MAX_CASE_BYTES = 2 * 1024 * 1024
SQL_SCHEMA = """
CREATE ROLE trace_reader LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
ALTER ROLE trace_reader SET default_transaction_read_only = on;
CREATE SCHEMA tracebound_lab;
CREATE TABLE tracebound_lab.case_metadata (
    case_id text PRIMARY KEY,
    schema_version text NOT NULL CHECK (schema_version = 'flow-case/v1'),
    currency text NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    completeness_attested boolean NOT NULL CHECK (completeness_attested),
    seed_transfer_id text NOT NULL
);
CREATE TABLE tracebound_lab.account_openings (
    case_id text NOT NULL REFERENCES tracebound_lab.case_metadata(case_id),
    account text NOT NULL,
    amount_minor numeric NOT NULL CHECK (
        amount_minor = trunc(amount_minor) AND
        amount_minor BETWEEN 0 AND 18446744073709551615
    ),
    PRIMARY KEY (case_id, account)
);
CREATE TABLE tracebound_lab.account_closings (
    case_id text NOT NULL,
    account text NOT NULL,
    amount_minor numeric NOT NULL CHECK (
        amount_minor = trunc(amount_minor) AND
        amount_minor BETWEEN 0 AND 18446744073709551615
    ),
    PRIMARY KEY (case_id, account),
    FOREIGN KEY (case_id, account)
        REFERENCES tracebound_lab.account_openings(case_id, account)
);
CREATE TABLE tracebound_lab.transfers (
    case_id text NOT NULL REFERENCES tracebound_lab.case_metadata(case_id),
    transfer_id text NOT NULL,
    posted_at timestamptz NOT NULL,
    posting_sequence numeric NOT NULL CHECK (
        posting_sequence = trunc(posting_sequence) AND
        posting_sequence BETWEEN 1 AND 18446744073709551615
    ),
    from_account text NOT NULL,
    to_account text NOT NULL CHECK (to_account <> from_account),
    amount_minor numeric NOT NULL CHECK (
        amount_minor = trunc(amount_minor) AND
        amount_minor BETWEEN 1 AND 18446744073709551615
    ),
    PRIMARY KEY (case_id, transfer_id),
    UNIQUE (case_id, posting_sequence),
    FOREIGN KEY (case_id, from_account)
        REFERENCES tracebound_lab.account_openings(case_id, account),
    FOREIGN KEY (case_id, to_account)
        REFERENCES tracebound_lab.account_openings(case_id, account)
);
ALTER TABLE tracebound_lab.case_metadata ADD CONSTRAINT seed_exists
    FOREIGN KEY (case_id, seed_transfer_id)
    REFERENCES tracebound_lab.transfers(case_id, transfer_id)
    DEFERRABLE INITIALLY DEFERRED;
CREATE TABLE tracebound_lab.case_targets (
    case_id text NOT NULL,
    ordinal integer NOT NULL CHECK (ordinal > 0),
    transfer_id text NOT NULL,
    PRIMARY KEY (case_id, transfer_id),
    UNIQUE (case_id, ordinal),
    FOREIGN KEY (case_id, transfer_id)
        REFERENCES tracebound_lab.transfers(case_id, transfer_id)
);
CREATE TABLE tracebound_lab.snapshot_probe (
    singleton boolean PRIMARY KEY CHECK (singleton),
    generation integer NOT NULL
);
INSERT INTO tracebound_lab.snapshot_probe VALUES (true, 0);
GRANT USAGE ON SCHEMA tracebound_lab TO trace_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA tracebound_lab TO trace_reader;
"""

SQL_SNAPSHOT_STATE = """
SELECT json_build_object(
    'isolation', current_setting('transaction_isolation'),
    'read_only', current_setting('transaction_read_only'),
    'snapshot', pg_current_snapshot()::text,
    'probe_generation', (SELECT generation FROM tracebound_lab.snapshot_probe),
    'can_update_transfers', has_table_privilege(
        current_user, 'tracebound_lab.transfers', 'UPDATE'
    )
);
"""

SQL_EXPORT = """
SELECT json_build_object(
    'schema', metadata.schema_version,
    'case_id', metadata.case_id,
    'currency', metadata.currency,
    'completeness_attested', metadata.completeness_attested,
    'seed_transfer_id', metadata.seed_transfer_id,
    'opening_balances', (
        SELECT json_agg(json_build_object(
            'account', opening.account, 'amount_minor', opening.amount_minor
        ) ORDER BY opening.account)
        FROM tracebound_lab.account_openings AS opening
        WHERE opening.case_id = metadata.case_id
    ),
    'closing_balances', (
        SELECT json_agg(json_build_object(
            'account', closing.account, 'amount_minor', closing.amount_minor
        ) ORDER BY closing.account)
        FROM tracebound_lab.account_closings AS closing
        WHERE closing.case_id = metadata.case_id
    ),
    'transfers', (
        SELECT json_agg(json_build_object(
            'id', movement.transfer_id,
            'timestamp', to_char(movement.posted_at AT TIME ZONE 'UTC',
                'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
            'sequence', movement.posting_sequence,
            'from_account', movement.from_account,
            'to_account', movement.to_account,
            'amount_minor', movement.amount_minor
        ) ORDER BY movement.posting_sequence)
        FROM tracebound_lab.transfers AS movement
        WHERE movement.case_id = metadata.case_id
    ),
    'target_transfer_ids', (
        SELECT json_agg(target.transfer_id ORDER BY target.ordinal)
        FROM tracebound_lab.case_targets AS target
        WHERE target.case_id = metadata.case_id
    )
)
FROM tracebound_lab.case_metadata AS metadata;
"""


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def canonical_case(value):
    result = copy.deepcopy(value)
    result["opening_balances"].sort(key=lambda row: row["account"])
    result["closing_balances"].sort(key=lambda row: row["account"])
    result["transfers"].sort(key=lambda row: row["sequence"])
    result["target_transfer_ids"].sort()
    for row in result["transfers"]:
        text = row["timestamp"]
        match = re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:([0-5]\d)(?:\.(\d+))?(?:Z|[+-]\d{2}:\d{2})",
            text,
        )
        require(match is not None, "Lab timestamps must be standard RFC3339 without leap seconds")
        fraction = match[2] or ""
        require(not fraction[6:].strip("0"), "PostgreSQL lab refuses sub-microsecond precision loss")
        instant = datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
        row["timestamp"] = instant.isoformat(timespec="microseconds").replace("+00:00", "Z")
    return result


def copy_block(table, columns, rows):
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerows(rows)
    # Table/column identifiers are static call-site constants. Values use CSV,
    # never SQL interpolation. The Rust parser has validated all source fields.
    return f"COPY tracebound_lab.{table} ({columns}) FROM STDIN WITH (FORMAT csv);\n{buffer.getvalue()}\\.\n"


def load_sql(case):
    key = case["case_id"]
    blocks = [
        "BEGIN;\n",
        copy_block("case_metadata", "case_id,schema_version,currency,completeness_attested,seed_transfer_id", [[key, case["schema"], case["currency"], "true", case["seed_transfer_id"]]]),
        copy_block("account_openings", "case_id,account,amount_minor", [[key, row["account"], row["amount_minor"]] for row in case["opening_balances"]]),
        copy_block("account_closings", "case_id,account,amount_minor", [[key, row["account"], row["amount_minor"]] for row in case["closing_balances"]]),
        copy_block("transfers", "case_id,transfer_id,posted_at,posting_sequence,from_account,to_account,amount_minor", [[key, row["id"], row["timestamp"], row["sequence"], row["from_account"], row["to_account"], row["amount_minor"]] for row in case["transfers"]]),
        copy_block("case_targets", "case_id,ordinal,transfer_id", [[key, index + 1, target] for index, target in enumerate(case["target_transfer_ids"])]),
        "COMMIT;\n",
    ]
    return "".join(blocks)


def discover_bindir(requested, env):
    if requested:
        bindir = requested.resolve(strict=True)
    elif sys.platform == "darwin" and Path("/opt/homebrew/opt/postgresql@15/bin/pg_ctl").is_file():
        bindir = Path("/opt/homebrew/opt/postgresql@15/bin")
    else:
        result = subprocess.run(["pg_config", "--bindir"], env=env, capture_output=True, text=True, check=True, timeout=10)
        bindir = Path(result.stdout.strip()).resolve(strict=True)
    for tool in ["initdb", "pg_ctl", "psql", "postgres"]:
        require((bindir / tool).is_file(), "PostgreSQL installation lacks a required executable")
    return bindir


def snapshot(reader_command, writer_command, env):
    process = subprocess.Popen(reader_command, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    try:
        process.stdin.write("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;\nSET LOCAL timezone = 'UTC';\nSET LOCAL statement_timeout = '20s';\n" + SQL_SNAPSHOT_STATE)
        process.stdin.flush()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            require(selector.select(timeout=25), "Timed out establishing the PostgreSQL snapshot")
            line = process.stdout.readline()
        require(line, "Snapshot reader exited before returning transaction settings")
        before = json.loads(line)
        require(before["isolation"] == "repeatable read" and before["read_only"] == "on", "Unexpected snapshot transaction mode")
        require(before["can_update_transfers"] is False, "Snapshot role unexpectedly has UPDATE privilege")
        writer = subprocess.run(writer_command + ["-c", "UPDATE tracebound_lab.snapshot_probe SET generation = generation + 1"], env=env, capture_output=True, text=True, timeout=20)
        require(writer.returncode == 0, "Independent snapshot probe writer failed")
        process.stdin.write(SQL_EXPORT + SQL_SNAPSHOT_STATE + "COMMIT;\n\\q\n")
        process.stdin.flush()
        process.stdin.close()
        process.stdin = None
        stdout, _stderr = process.communicate(timeout=30)
        require(process.returncode == 0, "Repeatable-read ledger export failed")
        lines = [json.loads(line) for line in stdout.splitlines() if line.strip()]
        require(len(lines) == 2, "Expected exactly one exported case and snapshot observation")
        exported, after = lines
        require(before["snapshot"] == after["snapshot"], "Snapshot changed inside repeatable-read transaction")
        require(before["probe_generation"] == after["probe_generation"] == 0, "Concurrent update leaked into established snapshot")
        fresh = subprocess.run(reader_command + ["-c", "SELECT generation FROM tracebound_lab.snapshot_probe"], env=env, capture_output=True, text=True, timeout=20)
        require(fresh.returncode == 0 and fresh.stdout.strip() == "1", "Committed probe update not visible to fresh transaction")
        readonly = subprocess.run(writer_command + ["-v", "VERBOSITY=sqlstate", "-c", "BEGIN READ ONLY; UPDATE tracebound_lab.snapshot_probe SET generation = 99"], env=env, capture_output=True, text=True, timeout=20)
        require(readonly.returncode != 0 and "25006" in readonly.stderr, "Read-only transaction failed to reject a write")
        return exported, {
            "isolation": "repeatable read",
            "read_only": True,
            "reader_has_update_privilege": False,
            "snapshot_stable_across_queries": True,
            "concurrent_commit_hidden_from_snapshot": True,
            "concurrent_commit_visible_to_new_transaction": True,
            "read_only_write_rejected_sqlstate": "25006",
        }
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, default=Path("target/release/tracebound"))
    parser.add_argument("--case", type=Path, default=Path("examples/commingled-payments.json"))
    parser.add_argument("--bindir", type=Path)
    parser.add_argument("--output", type=Path, help="New measured summary JSON file; never overwritten")
    args = parser.parse_args()
    require(os.name == "posix", "The isolated PostgreSQL lab requires Unix sockets")
    if args.output:
        require(not args.output.exists(), "Refusing to overwrite integration evidence")
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("PG")}
    ignored_variables = len(os.environ) - len(env)
    env.update(LC_ALL="C", PGAPPNAME="tracebound-synthetic-lab")
    binary = args.binary.resolve(strict=True)
    binary_hash = sha256(binary.read_bytes())
    with args.case.open("rb") as source:
        original_bytes = source.read(MAX_CASE_BYTES + 1)
    require(len(original_bytes) <= MAX_CASE_BYTES, "Case exceeds 2 MiB input limit")
    bindir = discover_bindir(args.bindir, env)
    version = subprocess.run([str(bindir / "postgres"), "--version"], env=env, capture_output=True, text=True, check=True, timeout=10).stdout.strip()
    major = re.search(r"PostgreSQL\)\s+(\d+)", version)
    require(major is not None and int(major[1]) >= 15, "PostgreSQL 15 or newer is required")
    work = Path(tempfile.mkdtemp(prefix="tracebound-pg-", dir="/tmp"))
    os.chmod(work, 0o700)
    cluster, socket = work / "cluster", work / "socket"
    socket.mkdir(mode=0o700)
    require(len(os.fsencode(str(socket / f".s.PGSQL.{PORT}"))) <= 100, "Unix socket path is too long")
    attempted_start = False
    stopped = True
    result = None
    started = time.monotonic()

    def run(command, **kwargs):
        return subprocess.run(command, env=env, capture_output=True, text=True, timeout=60, **kwargs)

    try:
        original_path = work / "original.json"
        original_path.write_bytes(original_bytes)
        baseline = run([str(binary), "analyze", "--input", str(original_path)], check=True)
        baseline_report = json.loads(baseline.stdout)
        original_case = json.loads(original_bytes)
        normalized = canonical_case(original_case)
        run([str(bindir / "initdb"), "-D", str(cluster), "-U", "lab_admin", "-A", "trust"], check=True)
        with (cluster / "postgresql.conf").open("a") as config:
            config.write(
                "\nlisten_addresses = ''\n"
                f"port = {PORT}\n"
                "unix_socket_permissions = 0700\n"
                f"unix_socket_directories = '{str(socket).replace(chr(39), chr(39) * 2)}'\n"
                "timezone = 'UTC'\n"
                "log_timezone = 'UTC'\n"
            )
        attempted_start = True
        stopped = False
        run([str(bindir / "pg_ctl"), "-D", str(cluster), "-l", str(work / "server.log"), "-t", "30", "-w", "start"], check=True)
        base = [str(bindir / "psql"), "-X", "-w", "-q", "-A", "-t", "-P", "pager=off", "-v", "ON_ERROR_STOP=1", "-h", str(socket), "-p", PORT, "-U", "lab_admin", "-d", "postgres"]
        run(base + ["-c", "CREATE DATABASE tracebound_lab"], check=True)
        base[-1] = "tracebound_lab"
        run(base, input=SQL_SCHEMA, check=True)
        run(base, input=load_sql(normalized), check=True)
        observed_listener = run(base + ["-c", "SHOW listen_addresses"], check=True).stdout.strip()
        require(observed_listener == "", "Lab unexpectedly has configured TCP listeners")
        reader = list(base)
        reader[reader.index("lab_admin")] = "trace_reader"
        exported, isolation = snapshot(reader, base, env)
        require(canonical_case(exported) == normalized, "Normalized PostgreSQL round trip changed case values")
        exported_bytes = (json.dumps(exported, indent=2) + "\n").encode()
        exported_path = work / "snapshot.json"
        exported_path.write_bytes(exported_bytes)
        observed = run([str(binary), "analyze", "--input", str(exported_path)], check=True)
        observed_report = json.loads(observed.stdout)
        require(observed_report["targets"] == baseline_report["targets"], "Snapshot attribution targets or witnesses differ from original case")
        require(binary_hash == sha256(binary.read_bytes()), "Solver binary changed during validation; rerun")
        result = {
            "schema": "tracebound-postgresql-snapshot-lab/v1",
            "postgres_version": version,
            "binary_sha256": binary_hash,
            "source_case_sha256": sha256(original_bytes),
            "exported_case_sha256": sha256(exported_bytes),
            "snapshot_report_sha256": sha256(observed.stdout.encode()),
            "case_id": original_case["case_id"],
            "accounts": len(normalized["opening_balances"]),
            "transfers": len(normalized["transfers"]),
            "target_count": len(observed_report["targets"]),
            "normalized_values_preserved": True,
            "target_reports_including_witnesses_identical": True,
            "snapshot_observations": isolation,
            "tcp_listen_addresses_empty": True,
            "ambient_pg_variables_ignored": ignored_variables,
            "targets": [{key: target[key] for key in ["transfer_id", "minimum_minor", "maximum_minor"]} for target in observed_report["targets"]],
            "scope": "Real isolated local PostgreSQL with normalized tables and a repeatable-read read-only export of a supplied synthetic case. No existing database was contacted.",
            "limitations": [
                "This is a reproducible local integration lab, not a production connector, CDC pipeline or deployment validation.",
                "One MVCC snapshot prevents inter-query visibility drift; it does not prove ledger completeness, truthful source data or trusted posting sequence.",
                "PostgreSQL timestamps use microsecond precision. This lab rejects meaningful sub-microsecond values and leap-second timestamps instead of silently changing them.",
                "The model requires one currency, explicit funded postings and matching account openings/closings. Bank-specific normalization and authorization remain outside this fixture.",
            ],
        }
    finally:
        if attempted_start:
            status = run([str(bindir / "pg_ctl"), "-D", str(cluster), "status"])
            if status.returncode == 0:
                for mode, timeout in [("fast", "30"), ("immediate", "15")]:
                    try:
                        stop = run([str(bindir / "pg_ctl"), "-D", str(cluster), "-m", mode, "-t", timeout, "-w", "stop"])
                        if stop.returncode == 0:
                            break
                    except subprocess.TimeoutExpired:
                        continue
                stopped = run([str(bindir / "pg_ctl"), "-D", str(cluster), "status"]).returncode == 3
            else:
                stopped = status.returncode == 3
        if stopped:
            shutil.rmtree(work)
        else:
            raise RuntimeError(f"Cannot confirm the isolated cluster stopped; retained its directory at {work}")
    require(result is not None, "Integration produced no observed result")
    result["cluster_stopped"] = True
    result["temporary_cluster_removed"] = not work.exists()
    result["elapsed_seconds"] = round(time.monotonic() - started, 6)
    text = json.dumps(result, indent=2) + "\n"
    if args.output:
        descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
            destination.write(text)
    print(text, end="")


if __name__ == "__main__":
    main()
