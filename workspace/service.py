"""Durable local case workflow. No external financial or law-enforcement actions.

SQLite ownership and the operator's private token configuration are trust
boundaries. A database administrator can rewrite history; retain audit anchors
outside this workspace to detect a rewrite or truncation of an anchored case.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import socket
import sqlite3
import stat
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

MAX_INPUT_BYTES = 2 * 1024 * 1024
MAX_REPORT_BYTES = 16 * 1024 * 1024
MAX_HTTP_BYTES = 4 * 1024 * 1024 + 65536
MAX_CASE_ARTIFACT_BYTES = 32 * 1024 * 1024
MAX_WORKSPACE_ARTIFACT_BYTES = 128 * 1024 * 1024
MAX_DOSSIER_BYTES = 64 * 1024 * 1024
MAX_CASES = 50
MAX_EVENTS = 200
MAX_ANALYSES = 20
ZERO_HASH = "0" * 64
ROLES = frozenset(("investigator", "reviewer", "auditor"))
IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,64}\Z")
DIGEST = re.compile(r"[a-f0-9]{64}\Z")


class WorkspaceError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class Actor:
    id: str
    role: str


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def strict_json(raw: bytes):
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicates,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Non-finite number")))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise WorkspaceError(400, "Invalid JSON; duplicate fields and non-finite values are rejected") from exc


def _fields(data, required, optional=()):
    if type(data) is not dict or not set(required) <= data.keys() or data.keys() - set(required) - set(optional):
        raise WorkspaceError(400, "Unexpected or missing request fields")


def _text(value, name, limit, allow_empty=False):
    if type(value) is not str or len(value) > limit or (not allow_empty and not value.strip()):
        raise WorkspaceError(400, f"Invalid {name}")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise WorkspaceError(400, f"Invalid Unicode in {name}") from exc
    if "\x00" in value:
        raise WorkspaceError(400, f"NUL is not permitted in {name}")
    return value


def _id(value):
    if type(value) is not str or not IDENTIFIER.fullmatch(value) or value in (".", ".."):
        raise WorkspaceError(400, "Invalid identifier")
    return value


def _revision(value):
    if type(value) is not int or value < 0 or value > MAX_EVENTS:
        raise WorkspaceError(400, "expected_revision must be an integer within the case revision limit")
    return value


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _require(actor, roles):
    if actor.role not in roles:
        raise WorkspaceError(403, "This actor role cannot perform this action")


def load_config(path: Path):
    """Read operator-controlled bearer credentials, never placed in a dossier."""
    path = Path(path)
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise WorkspaceError(400, "Authentication configuration must be a regular non-symlink file")
    if os.name == "posix" and (metadata.st_mode & 0o077 or metadata.st_uid != os.getuid()):
        raise WorkspaceError(400, "Authentication configuration must be owned by this user and mode 0600")
    if metadata.st_size > 65536:
        raise WorkspaceError(400, "Authentication configuration exceeds size limit")
    data = strict_json(path.read_bytes())
    _fields(data, ("actors",), ("schema",))
    if data.get("schema", "workspace-config/v1") != "workspace-config/v1":
        raise WorkspaceError(400, "Unsupported authentication configuration schema")
    if type(data["actors"]) is not list or not 1 <= len(data["actors"]) <= 20:
        raise WorkspaceError(400, "Configure between one and twenty actors")
    actors = []
    ids, tokens = set(), set()
    for entry in data["actors"]:
        _fields(entry, ("id", "role", "token"))
        actor_id = _id(entry["id"])
        role, token = entry["role"], entry["token"]
        if type(role) is not str or role not in ROLES or type(token) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token):
            raise WorkspaceError(400, "Invalid actor role or token; use a generated URL-safe secret of at least 32 characters")
        if actor_id in ids or token in tokens:
            raise WorkspaceError(400, "Actor IDs and tokens must be unique")
        ids.add(actor_id)
        tokens.add(token)
        actors.append((Actor(actor_id, role), token.encode("ascii")))
    return tuple(actors)


def create_config(path: Path):
    """Create private credentials once; the operator distributes them separately."""
    path = Path(path)
    config = {"schema": "workspace-config/v1", "actors": [
        {"id": role + "-1", "role": role, "token": secrets.token_urlsafe(32)}
        for role in ("investigator", "reviewer", "auditor")
    ]}
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(canonical(config) + b"\n")
        output.flush()
        os.fsync(output.fileno())


def _event_hash(event):
    return digest(canonical({key: value for key, value in event.items() if key != "hash"}))


def verify_dossier(dossier, trusted_anchor: str):
    """Verify an exported case against an independently retained latest anchor.

    This checks history/content consistency, not the truth of input evidence or
    the identity of a human behind a bearer-token actor. A self-supplied anchor
    is insufficient to establish authenticity after storage-admin compromise.
    """
    try:
        if type(trusted_anchor) is not str or not DIGEST.fullmatch(trusted_anchor):
            raise ValueError("Invalid external audit anchor")
        _fields(dossier, ("schema", "case", "artifacts", "analyses", "notes", "events", "anchor"))
        if dossier["schema"] != "case-dossier/v1":
            raise ValueError("Unsupported dossier schema")
        case = dossier["case"]
        events = dossier["events"]
        if type(events) is not list or not 1 <= len(events) <= MAX_EVENTS:
            raise ValueError("Missing or oversized event history")
        if type(case) is not dict or case.get("revision") != len(events):
            raise ValueError("Case revision does not match event history")
        previous = ZERO_HASH
        artifact_refs, analysis_refs, note_refs = [], [], []
        for sequence, event in enumerate(events, 1):
            _fields(event, ("case_id", "sequence", "actor", "role", "action", "at", "payload", "previous_hash", "hash"))
            if type(event["sequence"]) is not int or event["sequence"] != sequence or event["case_id"] != case["id"]:
                raise ValueError("Audit history order or case binding is invalid")
            if event["previous_hash"] != previous or event["hash"] != _event_hash(event):
                raise ValueError("Audit chain hash mismatch")
            payload = event["payload"]
            _fields(payload, ("case", "artifacts", "analysis", "note", "review"))
            if payload["case"]["revision"] != sequence or payload["case"]["id"] != case["id"]:
                raise ValueError("Audit case snapshot mismatch")
            if type(payload["artifacts"]) is not list:
                raise ValueError("Invalid artifact history")
            artifact_refs.extend(payload["artifacts"])
            if payload["analysis"] is not None:
                analysis_refs.append(payload["analysis"])
            if payload["note"] is not None:
                note_refs.append(payload["note"])
            previous = event["hash"]
        if previous != trusted_anchor:
            raise ValueError("Current history differs from the independently retained latest anchor")
        if events[-1]["payload"]["case"] != case:
            raise ValueError("Current case state differs from its audited snapshot")
        expected_anchor = {"case_id": case["id"], "revision": case["revision"], "event_hash": previous}
        if dossier["anchor"] != expected_anchor:
            raise ValueError("Dossier anchor metadata mismatch")
        artifacts = dossier["artifacts"]
        if type(artifacts) is not list or len(artifacts) > 2 * MAX_ANALYSES:
            raise ValueError("Invalid artifact list")
        metadata = []
        artifact_ids = set()
        total = 0
        for artifact in artifacts:
            _fields(artifact, ("id", "kind", "sha256", "byte_length", "created_at", "content"))
            if artifact["id"] in artifact_ids:
                raise ValueError("Duplicate artifact ID")
            artifact_ids.add(artifact["id"])
            if artifact["kind"] not in ("input", "report") or type(artifact["content"]) is not str:
                raise ValueError("Invalid artifact content")
            raw = artifact["content"].encode("utf-8")
            limit = MAX_INPUT_BYTES if artifact["kind"] == "input" else MAX_REPORT_BYTES
            if len(raw) > limit or len(raw) != artifact["byte_length"] or digest(raw) != artifact["sha256"]:
                raise ValueError("Artifact hash or size mismatch")
            total += len(raw)
            metadata.append({k: v for k, v in artifact.items() if k != "content"})
        if total > MAX_CASE_ARTIFACT_BYTES or metadata != artifact_refs:
            raise ValueError("Immutable artifact inventory differs from audited references")
        if dossier["analyses"] != analysis_refs or dossier["notes"] != note_refs:
            raise ValueError("Analysis or note inventory differs from audited references")
        if len({a["id"] for a in analysis_refs}) != len(analysis_refs) or len({n["id"] for n in note_refs}) != len(note_refs):
            raise ValueError("Duplicate immutable analysis or note ID")
        for analysis in analysis_refs:
            if analysis["input_artifact_id"] not in artifact_ids or analysis["report_artifact_id"] not in artifact_ids:
                raise ValueError("Analysis references a missing artifact")
        if case["current_analysis_id"] != (analysis_refs[-1]["id"] if analysis_refs else None):
            raise ValueError("Current analysis differs from audited analysis inventory")
        return True
    except (ValueError, TypeError, KeyError, UnicodeError, WorkspaceError, RecursionError) as exc:
        raise WorkspaceError(409, "Case integrity verification failed: " + str(exc)) from exc


class Workspace:
    def __init__(self, database: Path, rust_binary: Path, solver_timeout=60):
        self.database = Path(database).absolute()
        self.rust_binary = Path(rust_binary).resolve(strict=True)
        if not self.rust_binary.is_file() or not os.access(self.rust_binary, os.X_OK):
            raise WorkspaceError(400, "Configure an executable Rust CLI binary")
        self.solver_timeout = solver_timeout
        self.solver_slots = threading.BoundedSemaphore(2)
        self.database.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            fd = os.open(self.database, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
        except FileExistsError:
            metadata = self.database.lstat()
            if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                raise WorkspaceError(400, "Database must be a regular non-symlink file")
            if os.name == "posix" and metadata.st_mode & 0o077:
                raise WorkspaceError(400, "Database must have private 0600 permissions")
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
            CREATE TABLE IF NOT EXISTS cases (
              id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL,
              revision INTEGER NOT NULL, status TEXT NOT NULL CHECK(status IN ('draft','in_review','approved')),
              last_investigator TEXT NOT NULL, current_analysis_id TEXT,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS artifacts (
              id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id),
              kind TEXT NOT NULL CHECK(kind IN ('input','report')), sha256 TEXT NOT NULL,
              byte_length INTEGER NOT NULL, content BLOB NOT NULL, created_at TEXT NOT NULL,
              revision INTEGER NOT NULL, ordinal INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS analyses (
              id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id),
              input_artifact_id TEXT NOT NULL REFERENCES artifacts(id),
              report_artifact_id TEXT NOT NULL REFERENCES artifacts(id), actor TEXT NOT NULL,
              revision INTEGER NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS notes (
              id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id), actor TEXT NOT NULL,
              text TEXT NOT NULL, created_at TEXT NOT NULL, revision INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
              case_id TEXT NOT NULL REFERENCES cases(id), sequence INTEGER NOT NULL,
              actor TEXT NOT NULL, role TEXT NOT NULL, action TEXT NOT NULL, at TEXT NOT NULL,
              payload TEXT NOT NULL, previous_hash TEXT NOT NULL, hash TEXT NOT NULL,
              PRIMARY KEY(case_id,sequence));
            """)
            for table in ("artifacts", "analyses", "notes", "events"):
                for action in ("UPDATE", "DELETE"):
                    db.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{action.lower()} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'Immutable workspace evidence'); END")

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.database, isolation_level=None, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        try:
            yield connection
            if connection.in_transaction:
                connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def _case(self, db, case_id):
        size = db.execute("SELECT length(CAST(title AS BLOB))+length(CAST(description AS BLOB)) FROM cases WHERE id=?", (case_id,)).fetchone()
        if size is not None and size[0] > 16384:
            raise WorkspaceError(409, "Stored case metadata exceeds integrity resource limits")
        row = db.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
        if row is None:
            raise WorkspaceError(404, "Case not found")
        return dict(row)

    def _export(self, db, case_id):
        case = self._case(db, case_id)
        sizes = db.execute("SELECT COUNT(*), COALESCE(SUM(length(content)),0), COALESCE(MAX(length(content)),0) FROM artifacts WHERE case_id=?", (case_id,)).fetchone()
        if sizes[0] > MAX_ANALYSES * 2 or sizes[1] > MAX_CASE_ARTIFACT_BYTES or sizes[2] > MAX_REPORT_BYTES:
            raise WorkspaceError(409, "Stored artifacts exceed integrity resource limits")
        event_size = db.execute("SELECT COUNT(*),COALESCE(MAX(length(CAST(payload AS BLOB))),0),COALESCE(SUM(length(CAST(payload AS BLOB))),0) FROM events WHERE case_id=?", (case_id,)).fetchone()
        if event_size[0] > MAX_EVENTS or event_size[1] > 65536 or event_size[2] > 4*1024*1024:
            raise WorkspaceError(409, "Stored event history exceeds integrity resource limits")
        note_size = db.execute("SELECT COUNT(*),COALESCE(MAX(length(CAST(text AS BLOB))),0) FROM notes WHERE case_id=?", (case_id,)).fetchone()
        if note_size[0] > MAX_EVENTS or note_size[1] > 16000 or db.execute("SELECT COUNT(*) FROM analyses WHERE case_id=?", (case_id,)).fetchone()[0] > MAX_ANALYSES:
            raise WorkspaceError(409, "Stored notes or analyses exceed integrity resource limits")
        artifacts = []
        for row in db.execute("SELECT id,kind,sha256,byte_length,created_at,content FROM artifacts WHERE case_id=? ORDER BY revision,ordinal", (case_id,)):
            artifact = dict(row)
            try:
                artifact["content"] = bytes(artifact["content"]).decode("utf-8")
            except (TypeError, UnicodeError) as exc:
                raise WorkspaceError(409, "Stored artifact is not valid UTF-8 bytes") from exc
            artifacts.append(artifact)
        analyses = [dict(row) for row in db.execute("SELECT id,input_artifact_id,report_artifact_id,actor,revision,created_at FROM analyses WHERE case_id=? ORDER BY revision", (case_id,))]
        notes = [dict(row) for row in db.execute("SELECT id,actor,text,created_at,revision FROM notes WHERE case_id=? ORDER BY revision", (case_id,))]
        events = []
        for row in db.execute("SELECT * FROM events WHERE case_id=? ORDER BY sequence", (case_id,)):
            event = dict(row)
            try:
                event["payload"] = strict_json(event["payload"].encode("utf-8"))
            except (ValueError, RecursionError, WorkspaceError) as exc:
                raise WorkspaceError(409, "Stored audit payload is invalid") from exc
            events.append(event)
        anchor = {"case_id": case_id, "revision": case["revision"], "event_hash": events[-1]["hash"] if events else ZERO_HASH}
        return {"schema": "case-dossier/v1", "case": case, "artifacts": artifacts, "analyses": analyses, "notes": notes, "events": events, "anchor": anchor}

    def _checked(self, db, case_id):
        dossier = self._export(db, case_id)
        verify_dossier(dossier, dossier["anchor"]["event_hash"])
        return dossier

    @staticmethod
    def _detail(dossier):
        return {"case": dossier["case"], "artifacts": [{k: v for k, v in a.items() if k != "content"} for a in dossier["artifacts"]],
                "notes": dossier["notes"], "events": dossier["events"], "anchor": dossier["anchor"]}

    def list_cases(self, actor):
        _require(actor, ROLES)
        with self._connect() as db:
            db.execute("BEGIN")
            ids = [row[0] for row in db.execute("SELECT id FROM cases ORDER BY created_at,id LIMIT ?", (MAX_CASES+1,))]
            if len(ids) > MAX_CASES:
                raise WorkspaceError(409, "Stored case count exceeds integrity resource limits")
            return {"cases": [self._checked(db, case_id)["case"] for case_id in ids]}

    def detail(self, actor, case_id):
        _require(actor, ROLES)
        with self._connect() as db:
            db.execute("BEGIN")
            return self._detail(self._checked(db, _id(case_id)))

    def dossier(self, actor, case_id, external_anchor=None):
        _require(actor, ROLES)
        with self._connect() as db:
            db.execute("BEGIN")
            dossier = self._checked(db, _id(case_id))
            if external_anchor is not None:
                verify_dossier(dossier, external_anchor)
            return dossier

    def _append(self, db, case, actor, action, *, artifacts=None, analysis=None, note=None, review=None):
        previous = db.execute("SELECT hash FROM events WHERE case_id=? ORDER BY sequence DESC LIMIT 1", (case["id"],)).fetchone()
        event = {"case_id": case["id"], "sequence": case["revision"], "actor": actor.id, "role": actor.role,
                 "action": action, "at": case["updated_at"],
                 "payload": {"case": case, "artifacts": artifacts or [], "analysis": analysis, "note": note, "review": review},
                 "previous_hash": previous[0] if previous else ZERO_HASH}
        event["hash"] = _event_hash(event)
        db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)", (case["id"], event["sequence"], actor.id, actor.role, action,
                   event["at"], canonical(event["payload"]).decode("utf-8"), event["previous_hash"], event["hash"]))

    def _expect(self, db, case_id, expected, status="draft"):
        _revision(expected)
        dossier = self._checked(db, _id(case_id))
        case = dossier["case"]
        if case["revision"] != expected:
            raise WorkspaceError(409, "Stale case revision; reload before retrying")
        if case["revision"] >= MAX_EVENTS:
            raise WorkspaceError(409, "Case revision budget exhausted")
        if case["status"] != status:
            raise WorkspaceError(409, f"This action requires case status {status}")
        return case

    def _advance(self, db, case, **updates):
        case = dict(case)
        case.update(updates)
        case["revision"] += 1
        case["updated_at"] = _now()
        db.execute("UPDATE cases SET revision=?,status=?,last_investigator=?,current_analysis_id=?,updated_at=? WHERE id=?",
                   (case["revision"], case["status"], case["last_investigator"], case["current_analysis_id"], case["updated_at"], case["id"]))
        return case

    def create(self, actor, data):
        _require(actor, {"investigator"})
        _fields(data, ("id", "title", "expected_revision"), ("description",))
        case_id = _id(data["id"])
        title = _text(data["title"], "title", 160)
        description = _text(data.get("description", ""), "description", 2000, True)
        if _revision(data["expected_revision"]) != 0:
            raise WorkspaceError(409, "Creating a case requires expected_revision 0")
        now = _now()
        case = {"id": case_id, "title": title, "description": description, "revision": 1, "status": "draft",
                "last_investigator": actor.id, "current_analysis_id": None, "created_at": now, "updated_at": now}
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM cases WHERE id=?", (case_id,)).fetchone():
                raise WorkspaceError(409, "Case already exists")
            if db.execute("SELECT COUNT(*) FROM cases").fetchone()[0] >= MAX_CASES:
                raise WorkspaceError(409, "Workspace case budget exhausted")
            db.execute("INSERT INTO cases VALUES (?,?,?,?,?,?,?,?,?)", tuple(case[k] for k in ("id", "title", "description", "revision", "status", "last_investigator", "current_analysis_id", "created_at", "updated_at")))
            self._append(db, case, actor, "case_created")
            result = self._detail(self._checked(db, case_id))
            db.commit()
            return result

    def _run_rust(self, input_bytes):
        if not self.solver_slots.acquire(blocking=False):
            raise WorkspaceError(503, "Both local analysis slots are busy; retry later")
        try:
            with tempfile.TemporaryDirectory(prefix="tracebound-workspace-") as directory:
                root = Path(directory)
                source, output = root / "input.json", root / "report.json"
                descriptor = os.open(source, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(input_bytes)
                result = subprocess.run([str(self.rust_binary), "analyze", "--input", str(source), "--output", str(output)],
                                        capture_output=True, timeout=self.solver_timeout, check=False)
                if result.returncode != 0 or not output.is_file() or output.stat().st_size > MAX_REPORT_BYTES:
                    raise WorkspaceError(422, "Rust rejected the input ledger or could not produce a bounded report")
                report = output.read_bytes()
                verification = subprocess.run([str(self.rust_binary), "verify", "--input", str(source), "--report", str(output), "--anchor", digest(report)],
                                              capture_output=True, timeout=self.solver_timeout, check=False)
                if verification.returncode != 0:
                    raise WorkspaceError(422, "Rust certificate verification failed; no analysis was stored")
                result = strict_json(verification.stdout)
                if type(result) is not dict or result.get("verified") is not True or result.get("solver_recomputed") is not False:
                    raise WorkspaceError(422, "Rust CLI did not confirm solver-free certificate verification")
                report.decode("utf-8")
                return report
        except subprocess.TimeoutExpired as exc:
            raise WorkspaceError(503, "Local analysis exceeded the time budget; no analysis was stored") from exc
        except (OSError, UnicodeError) as exc:
            raise WorkspaceError(503, "Local Rust analysis is unavailable; no analysis was stored") from exc
        finally:
            self.solver_slots.release()

    def analyze(self, actor, case_id, data):
        _require(actor, {"investigator"})
        _fields(data, ("input_text", "expected_revision"))
        expected = _revision(data["expected_revision"])
        input_text = _text(data["input_text"], "input_text", MAX_INPUT_BYTES)
        input_bytes = input_text.encode("utf-8")
        if len(input_bytes) > MAX_INPUT_BYTES:
            raise WorkspaceError(413, "Ledger input exceeds 2 MiB")
        with self._connect() as db:
            db.execute("BEGIN")
            self._expect(db, case_id, expected)
        # Compute outside the transaction, then compare the exact starting
        # revision again inside BEGIN IMMEDIATE. Never commit stale results.
        report_bytes = self._run_rust(input_bytes)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            case = self._expect(db, case_id, expected)
            if db.execute("SELECT COUNT(*) FROM analyses WHERE case_id=?", (case_id,)).fetchone()[0] >= MAX_ANALYSES:
                raise WorkspaceError(409, "Case analysis budget exhausted")
            added_bytes = len(input_bytes) + len(report_bytes)
            current_case = db.execute("SELECT COALESCE(SUM(byte_length),0) FROM artifacts WHERE case_id=?", (case_id,)).fetchone()[0]
            current_all = db.execute("SELECT COALESCE(SUM(byte_length),0) FROM artifacts").fetchone()[0]
            if current_case + added_bytes > MAX_CASE_ARTIFACT_BYTES or current_all + added_bytes > MAX_WORKSPACE_ARTIFACT_BYTES:
                raise WorkspaceError(409, "Workspace evidence storage budget exhausted")
            analysis_id = secrets.token_hex(16)
            case = self._advance(db, case, current_analysis_id=analysis_id, last_investigator=actor.id)
            artifacts = []
            for ordinal, (kind, raw) in enumerate((("input", input_bytes), ("report", report_bytes))):
                artifact = {"id": secrets.token_hex(16), "kind": kind, "sha256": digest(raw), "byte_length": len(raw), "created_at": case["updated_at"]}
                db.execute("INSERT INTO artifacts VALUES (?,?,?,?,?,?,?,?,?)", (artifact["id"], case_id, kind, artifact["sha256"], len(raw), raw, case["updated_at"], case["revision"], ordinal))
                artifacts.append(artifact)
            analysis = {"id": analysis_id, "input_artifact_id": artifacts[0]["id"], "report_artifact_id": artifacts[1]["id"], "actor": actor.id, "revision": case["revision"], "created_at": case["updated_at"]}
            db.execute("INSERT INTO analyses VALUES (?,?,?,?,?,?,?)", (analysis_id, case_id, analysis["input_artifact_id"], analysis["report_artifact_id"], actor.id, case["revision"], case["updated_at"]))
            self._append(db, case, actor, "analysis_committed", artifacts=artifacts, analysis=analysis)
            result = self._detail(self._checked(db, case_id))
            db.commit()
            return result

    def add_note(self, actor, case_id, data):
        _require(actor, {"investigator", "reviewer"})
        _fields(data, ("text", "expected_revision"))
        text = _text(data["text"], "note", 4000)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            case = self._expect(db, case_id, data["expected_revision"])
            case = self._advance(db, case, **({"last_investigator": actor.id} if actor.role == "investigator" else {}))
            note = {"id": secrets.token_hex(16), "actor": actor.id, "text": text, "created_at": case["updated_at"], "revision": case["revision"]}
            db.execute("INSERT INTO notes VALUES (?,?,?,?,?,?)", (note["id"], case_id, actor.id, text, note["created_at"], case["revision"]))
            self._append(db, case, actor, "note_added", note=note)
            result = self._detail(self._checked(db, case_id))
            db.commit()
            return result

    def submit(self, actor, case_id, data):
        _require(actor, {"investigator"})
        _fields(data, ("expected_revision",))
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            case = self._expect(db, case_id, data["expected_revision"])
            if not case["current_analysis_id"]:
                raise WorkspaceError(409, "A verified analysis is required before review")
            case = self._advance(db, case, status="in_review", last_investigator=actor.id)
            self._append(db, case, actor, "review_submitted")
            result = self._detail(self._checked(db, case_id))
            db.commit()
            return result

    def review(self, actor, case_id, data):
        _require(actor, {"reviewer"})
        _fields(data, ("decision", "reason", "expected_revision"))
        if data["decision"] not in ("approve", "return"):
            raise WorkspaceError(400, "Review decision must be approve or return")
        reason = _text(data["reason"], "review reason", 2000)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            case = self._expect(db, case_id, data["expected_revision"], "in_review")
            analyst = db.execute("SELECT actor FROM analyses WHERE id=?", (case["current_analysis_id"],)).fetchone()
            if actor.id == case["last_investigator"] or analyst is None or actor.id == analyst[0]:
                raise WorkspaceError(403, "Review requires a different actor from the last substantive investigator")
            case = self._advance(db, case, status="approved" if data["decision"] == "approve" else "draft")
            self._append(db, case, actor, "review_approved" if data["decision"] == "approve" else "review_returned", review={"decision": data["decision"], "reason": reason})
            result = self._detail(self._checked(db, case_id))
            db.commit()
            return result


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler):
        if address[0] != "127.0.0.1":
            raise WorkspaceError(400, "Workspace server must bind only to 127.0.0.1")
        self.slots = threading.BoundedSemaphore(16)
        super().__init__(address, handler)

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(10)
        return request, address

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def make_server(workspace: Workspace, actors, port=0, static_root: Path | None = None):
    root = Path(static_root).resolve(strict=True) if static_root else None

    class Handler(BaseHTTPRequestHandler):
        server_version = "TraceboundLocal/1"
        protocol_version = "HTTP/1.0"

        def log_message(self, *_):
            pass  # Never log tokens, request bodies, case IDs or query strings.

        def _send(self, status, value, content_type="application/json; charset=utf-8"):
            raw = value if isinstance(value, bytes) else canonical(value)
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; connect-src 'self'; worker-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(raw)

        def _guard(self):
            host = f"127.0.0.1:{self.server.server_port}"
            if self.headers.get_all("Host", []) != [host]:
                raise WorkspaceError(403, "Host must match the loopback server address")
            origin = self.headers.get("Origin")
            if origin is not None and origin != "http://" + host:
                raise WorkspaceError(403, "Cross-origin workspace access is disabled")

        def _actor(self):
            values = self.headers.get_all("Authorization", [])
            if len(values) != 1 or not values[0].startswith("Bearer ") or len(values[0]) > 256:
                raise WorkspaceError(401, "A valid bearer token is required")
            provided = values[0][7:].encode("utf-8")
            actor = None
            for candidate, token in actors:
                if hmac.compare_digest(provided, token):
                    actor = candidate
            if actor is None:
                raise WorkspaceError(401, "A valid bearer token is required")
            return actor

        def _body(self):
            lengths = self.headers.get_all("Content-Length", [])
            if self.headers.get("Transfer-Encoding") or len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,10}", lengths[0]):
                raise WorkspaceError(400, "One bounded Content-Length is required; chunked bodies are unsupported")
            length = int(lengths[0])
            if length > MAX_HTTP_BYTES:
                raise WorkspaceError(413, "HTTP body exceeds resource limit")
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                raise WorkspaceError(415, "Use application/json")
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise WorkspaceError(400, "Incomplete request body")
            return strict_json(raw)

        def _static(self, path):
            if root is None:
                raise WorkspaceError(404, "Static interface is disabled")
            if path == "/":
                path = "/review.html" if (root / "review.html").is_file() else "/index.html"
            parts = path.removeprefix("/").split("/")
            top = {"index.html", "review.html", "review.js", "review.css", "app.js", "style.css", "worker.js", "favicon.ico"}
            allowed = (len(parts) == 1 and parts[0] in top
                       or len(parts) == 2 and parts[0] in ("pkg", "cases") and Path(parts[1]).suffix in (".js", ".wasm", ".json")
                       or parts == ["evidence", "demo-dossier.json"])
            if any(part in ("", ".", "..") or part.startswith(".") for part in parts) or not allowed:
                raise WorkspaceError(404, "Static file not found")
            try:
                target = root.joinpath(*parts).resolve(strict=True)
                target.relative_to(root)
                if not target.is_file() or target.stat().st_size > MAX_REPORT_BYTES:
                    raise ValueError("Not a bounded file")
            except (OSError, ValueError) as exc:
                raise WorkspaceError(404, "Static file not found") from exc
            self._send(200, target.read_bytes(), mimetypes.guess_type(target)[0] or "application/octet-stream")

        def _dispatch(self):
            self._guard()
            parsed = urlsplit(self.path)
            try:
                path = unquote(parsed.path, errors="strict")
            except UnicodeError as exc:
                raise WorkspaceError(400, "Invalid URL encoding") from exc
            if not path.startswith("/api/"):
                if self.command != "GET":
                    raise WorkspaceError(405, "Method not allowed")
                return self._static(path)
            actor = self._actor()
            if path == "/api/session" and self.command == "GET":
                return self._send(200, {"actor": {"id": actor.id, "role": actor.role}})
            if path == "/api/cases":
                if self.command == "GET":
                    return self._send(200, workspace.list_cases(actor))
                if self.command == "POST":
                    return self._send(201, workspace.create(actor, self._body()))
            parts = path.split("/")
            if len(parts) in (4, 5) and parts[1:3] == ["api", "cases"]:
                case_id = _id(parts[3])
                action = parts[4] if len(parts) == 5 else ""
                if self.command == "GET":
                    if action == "":
                        return self._send(200, workspace.detail(actor, case_id))
                    if action == "dossier":
                        dossier = workspace.dossier(actor, case_id)
                        raw = canonical(dossier)
                        if len(raw) > MAX_DOSSIER_BYTES:
                            raise WorkspaceError(413, "Dossier exceeds export size budget")
                        return self._send(200, raw)
                    if action == "verify":
                        try:
                            query = parse_qs(parsed.query, strict_parsing=True)
                        except ValueError as exc:
                            raise WorkspaceError(400, "Invalid audit anchor query") from exc
                        if set(query) != {"anchor"} or len(query["anchor"]) != 1:
                            raise WorkspaceError(400, "Provide the independently retained latest anchor")
                        dossier = workspace.dossier(actor, case_id, query["anchor"][0])
                        return self._send(200, {"verified": True, "anchor": dossier["anchor"]})
                if self.command == "POST":
                    operations = {"analysis": workspace.analyze, "notes": workspace.add_note, "submit": workspace.submit, "review": workspace.review}
                    if action in operations:
                        return self._send(200, operations[action](actor, case_id, self._body()))
            raise WorkspaceError(404, "API route not found")

        def _handle(self):
            try:
                self._dispatch()
            except WorkspaceError as exc:
                self._send(exc.status, {"error": exc.message})
            except (BrokenPipeError, ConnectionResetError):
                pass
            except (socket.timeout, TimeoutError):
                self._send(408, {"error": "Request timed out"})
            except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, RecursionError):
                self._send(500, {"error": "Workspace operation failed; no unverified result was returned"})

        do_GET = _handle
        do_POST = _handle
        do_OPTIONS = _handle
        do_PUT = _handle
        do_DELETE = _handle

    return LocalServer(("127.0.0.1", port), Handler)
