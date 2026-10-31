"""Real HTTP + SQLite + native Rust tests for the local case workspace."""
import copy
import http.client
import json
import os
from pathlib import Path
import secrets
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from workspace.service import (MAX_HTTP_BYTES, Workspace, WorkspaceError,
                               canonical, load_config, make_server, verify_dossier)

ROOT = Path(__file__).resolve().parents[1]
BINARY = Path(os.environ.get("TRACEBOUND_BIN", ROOT / "target/release/tracebound"))


def ledger():
    return {"schema": "flow-case/v1", "case_id": "synthetic-ledger", "currency": "USD", "completeness_attested": True,
            "opening_balances": [{"account": "A", "amount_minor": 10}, {"account": "B", "amount_minor": 10}, {"account": "C", "amount_minor": 0}],
            "closing_balances": [{"account": "A", "amount_minor": 0}, {"account": "B", "amount_minor": 10}, {"account": "C", "amount_minor": 10}],
            "transfers": [{"id": "seed", "sequence": 1, "timestamp": "2026-01-01T00:00:00Z", "from_account": "A", "to_account": "B", "amount_minor": 10},
                          {"id": "target", "sequence": 2, "timestamp": "2026-01-01T00:01:00Z", "from_account": "B", "to_account": "C", "amount_minor": 10}],
            "seed_transfer_id": "seed", "target_transfer_ids": ["target"]}


class WorkspaceHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not BINARY.is_file():
            raise RuntimeError("Build the actual Rust CLI with cargo build --release before workspace tests")

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="tracebound-workspace-test-")
        self.root = Path(self.directory.name)
        self.config = self.root / "private-config.json"
        self.tokens = {role: secrets.token_urlsafe(32) for role in ("investigator", "reviewer", "auditor")}
        self.config_data = {"actors": [{"id": role + "-1", "role": role, "token": token} for role, token in self.tokens.items()]}
        self._write_config()
        self.static = self.root / "web"
        self.static.mkdir()
        (self.static / "index.html").write_text("<h1>Local test</h1>")
        (self.static / "app.js").write_text("/* safe public file */")
        self.workspace = Workspace(self.root / "private.sqlite", BINARY)
        self._start()

    def _write_config(self):
        self.config.write_bytes(canonical(self.config_data))
        self.config.chmod(0o600)

    def _start(self):
        self.server = make_server(self.workspace, load_config(self.config), static_root=self.static)
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.02), daemon=True)
        self.thread.start()

    def _stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def tearDown(self):
        self._stop()
        self.directory.cleanup()

    def request(self, method, path, data=None, role="investigator", raw=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        outgoing = {"Content-Type": "application/json"}
        if role is not None:
            outgoing["Authorization"] = "Bearer " + self.tokens[role]
        if headers:
            outgoing.update(headers)
        body = raw if raw is not None else canonical(data) if data is not None else None
        connection.request(method, path, body=body, headers=outgoing)
        response = connection.getresponse()
        status, response_headers, content = response.status, dict(response.getheaders()), response.read()
        connection.close()
        try:
            value = json.loads(content)
        except ValueError:
            value = content
        return status, value, response_headers

    def create(self, case_id="case-1"):
        status, detail, _ = self.request("POST", "/api/cases", {"id": case_id, "title": "Synthetic review — ação", "description": "Original lab fixture", "expected_revision": 0})
        self.assertEqual(status, 201, detail)
        return detail

    def analyze(self, revision=1):
        status, detail, _ = self.request("POST", "/api/cases/case-1/analysis", {"input_text": canonical(ledger()).decode(), "expected_revision": revision})
        self.assertEqual(status, 200, detail)
        return detail

    def submit(self, revision=2):
        status, detail, _ = self.request("POST", "/api/cases/case-1/submit", {"expected_revision": revision})
        self.assertEqual(status, 200, detail)
        return detail

    def test_auth_roles_origins_and_host_are_enforced(self):
        self.assertEqual(self.request("GET", "/api/cases", role=None)[0], 401)
        self.assertEqual(self.request("GET", "/api/cases", headers={"Authorization": "Bearer incorrect"})[0], 401)
        for role in ("reviewer", "auditor"):
            self.assertEqual(self.request("POST", "/api/cases", {"id": "x", "title": "x", "expected_revision": 0}, role=role)[0], 403)
        self.assertEqual(self.request("GET", "/api/cases", headers={"Origin": "https://other.example"})[0], 403)
        self.assertEqual(self.request("GET", "/api/cases", headers={"Host": "attacker.example"})[0], 403)
        status, actor, headers = self.request("GET", "/api/session", role="auditor")
        self.assertEqual(status, 200)
        self.assertEqual(actor["actor"]["role"], "auditor")
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_actual_rust_workflow_approval_and_external_dossier_anchor(self):
        created = self.create()
        self.assertEqual(created["case"]["revision"], 1)
        analyzed = self.analyze()
        self.assertEqual(len(analyzed["artifacts"]), 2)
        self.assertNotIn("content", analyzed["artifacts"][0])
        self.submit()
        status, approved, _ = self.request("POST", "/api/cases/case-1/review", {"decision": "approve", "reason": "Verified conditional bounds and complete source ledger assumptions.", "expected_revision": 3}, role="reviewer")
        self.assertEqual(status, 200, approved)
        self.assertEqual(approved["case"]["status"], "approved")
        status, dossier, _ = self.request("GET", "/api/cases/case-1/dossier", role="auditor")
        self.assertEqual(status, 200)
        anchor = dossier["anchor"]["event_hash"]
        self.assertTrue(verify_dossier(dossier, anchor))
        report = json.loads(next(a["content"] for a in dossier["artifacts"] if a["kind"] == "report"))
        self.assertEqual((report["targets"][0]["minimum_minor"], report["targets"][0]["maximum_minor"]), ("0", "10"))
        status, verification, _ = self.request("GET", "/api/cases/case-1/verify?anchor=" + anchor, role="auditor")
        self.assertEqual(status, 200)
        self.assertTrue(verification["verified"])
        for token in self.tokens.values():
            self.assertNotIn(token, canonical(dossier).decode())
        self.assertEqual(self.request("GET", "/api/cases/case-1/verify?anchor=" + "0" * 64)[0], 409)

    def test_approved_and_submitted_cases_are_frozen(self):
        self.create()
        self.analyze()
        self.submit()
        self.assertEqual(self.request("POST", "/api/cases/case-1/notes", {"text": "Late mutation", "expected_revision": 3})[0], 409)
        self.assertEqual(self.request("POST", "/api/cases/case-1/analysis", {"input_text": canonical(ledger()).decode(), "expected_revision": 3})[0], 409)
        self.assertEqual(self.request("POST", "/api/cases/case-1/review", {"decision": "approve", "reason": "", "expected_revision": 3}, role="reviewer")[0], 400)
        self.assertEqual(self.request("POST", "/api/cases/case-1/review", {"decision": "approve", "reason": "Reviewed.", "expected_revision": 3}, role="reviewer")[0], 200)
        self.assertEqual(self.request("POST", "/api/cases/case-1/notes", {"text": "Changed after approval", "expected_revision": 4}, role="reviewer")[0], 409)

    def test_return_for_revision_allows_new_immutable_analysis(self):
        self.create()
        self.analyze()
        self.submit()
        status, returned, _ = self.request("POST", "/api/cases/case-1/review", {"decision": "return", "reason": "Document completeness limitations.", "expected_revision": 3}, role="reviewer")
        self.assertEqual(status, 200)
        self.assertEqual(returned["case"]["status"], "draft")
        updated = self.analyze(4)
        self.assertEqual(len(updated["artifacts"]), 4)
        self.assertEqual(updated["case"]["revision"], 5)

    def test_four_eyes_survives_actor_role_change(self):
        self.create()
        self.analyze()
        self.submit()
        self._stop()
        self.config_data["actors"][0]["role"] = "reviewer"
        self._write_config()
        self._start()
        status, response, _ = self.request("POST", "/api/cases/case-1/review", {"decision": "approve", "reason": "Own work.", "expected_revision": 3})
        self.assertEqual(status, 403, response)
        self.assertIn("different actor", response["error"])

    def test_input_failure_and_stale_revision_store_nothing(self):
        original = self.create()
        for text in ("{}", "not JSON", canonical({**ledger(), "completeness_attested": False}).decode()):
            status, _, _ = self.request("POST", "/api/cases/case-1/analysis", {"input_text": text, "expected_revision": 1})
            self.assertEqual(status, 422)
        current = self.request("GET", "/api/cases/case-1")[1]
        self.assertEqual(current, original)
        self.assertEqual(self.request("POST", "/api/cases/case-1/notes", {"text": "New context", "expected_revision": 0})[0], 409)
        for invalid in (True, 1.0, "1"):
            self.assertEqual(self.request("POST", "/api/cases/case-1/notes", {"text": "Bad revision", "expected_revision": invalid})[0], 400)

    def test_concurrent_mutations_have_one_winner(self):
        self.create()
        barrier = threading.Barrier(3)
        statuses = []
        def write(number):
            barrier.wait()
            statuses.append(self.request("POST", "/api/cases/case-1/notes", {"text": f"Context {number}", "expected_revision": 1})[0])
        threads = [threading.Thread(target=write, args=(i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(sorted(statuses), [200, 409])
        self.assertEqual(self.request("GET", "/api/cases/case-1")[1]["case"]["revision"], 2)

    def test_analysis_computes_outside_transaction_and_rejects_stale_commit(self):
        self.create()
        solved, release = threading.Event(), threading.Event()
        original = self.workspace._run_rust
        outcome = []
        def delayed(raw):
            report = original(raw)
            solved.set()
            if not release.wait(timeout=5):
                raise RuntimeError("Test synchronization failed")
            return report
        with patch.object(self.workspace, "_run_rust", side_effect=delayed):
            thread = threading.Thread(target=lambda: outcome.append(self.request("POST", "/api/cases/case-1/analysis", {"input_text": canonical(ledger()).decode(), "expected_revision": 1})[0]))
            thread.start()
            self.assertTrue(solved.wait(timeout=5))
            status = self.request("POST", "/api/cases/case-1/notes", {"text": "New evidence while solver runs", "expected_revision": 1})[0]
            release.set()
            thread.join(timeout=10)
        self.assertEqual(status, 200)
        self.assertEqual(outcome, [409])
        detail = self.request("GET", "/api/cases/case-1")[1]
        self.assertEqual(detail["artifacts"], [])
        self.assertEqual(detail["case"]["revision"], 2)

    def test_restart_preserves_artifacts_chain_and_review_state(self):
        self.create()
        self.analyze()
        before = self.request("GET", "/api/cases/case-1/dossier")[1]
        self._stop()
        self.workspace = Workspace(self.root / "private.sqlite", BINARY)
        self._start()
        after = self.request("GET", "/api/cases/case-1/dossier", role="auditor")[1]
        self.assertEqual(before, after)
        with sqlite3.connect(self.workspace.database) as db:
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_sqlite_immutability_and_artifact_tampering_fail_closed(self):
        self.create()
        self.analyze()
        with sqlite3.connect(self.workspace.database) as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE artifacts SET content=?", (b"tampered",))
            db.execute("DROP TRIGGER immutable_artifacts_update")
            db.execute("UPDATE artifacts SET content=? WHERE kind='input'", (b"tampered",))
        self.assertEqual(self.request("GET", "/api/cases/case-1/dossier")[0], 409)
        self.assertEqual(self.request("POST", "/api/cases/case-1/notes", {"text": "Cannot append to broken evidence", "expected_revision": 2})[0], 409)

    def test_status_tampering_outside_audit_is_rejected(self):
        self.create()
        with sqlite3.connect(self.workspace.database) as db:
            db.execute("UPDATE cases SET status='approved' WHERE id='case-1'")
        self.assertEqual(self.request("GET", "/api/cases/case-1")[0], 409)
        self.assertEqual(self.request("GET", "/api/cases")[0], 409)

    def test_empty_reordered_or_forged_history_is_rejected(self):
        self.create()
        self.analyze()
        self.submit()
        dossier = self.request("GET", "/api/cases/case-1/dossier")[1]
        anchor = dossier["anchor"]["event_hash"]
        for mutation in range(4):
            changed = copy.deepcopy(dossier)
            if mutation == 0:
                changed["events"] = []
            elif mutation == 1:
                changed["events"].reverse()
            elif mutation == 2:
                changed["events"][0]["payload"]["case"]["title"] = "Forged title"
            else:
                changed["case"]["status"] = "approved"
            with self.assertRaises(WorkspaceError):
                verify_dossier(changed, anchor)
        with sqlite3.connect(self.workspace.database) as db:
            db.execute("DROP TRIGGER immutable_events_update")
            db.execute("UPDATE events SET hash=? WHERE sequence=2", ("0"*64,))
        self.assertEqual(self.request("GET", "/api/cases/case-1/dossier")[0], 409)

    def test_http_limits_duplicates_and_static_traversal(self):
        body = b'{"id":"first","id":"second","title":"Bad","expected_revision":0}'
        self.assertEqual(self.request("POST", "/api/cases", raw=body)[0], 400)
        self.assertEqual(self.request("POST", "/api/cases", raw=b"{}", headers={"Content-Length": str(MAX_HTTP_BYTES+1)})[0], 413)
        self.assertEqual(self.request("POST", "/api/cases", raw=b"{}", headers={"Content-Type": "text/plain"})[0], 415)
        for path in ("/../private-config.json", "/%2e%2e/private-config.json", "/pkg/%2e%2e/private-config.json", "/private-config.json"):
            self.assertEqual(self.request("GET", path)[0], 404)
        (self.static / "app.js").unlink()
        (self.static / "app.js").symlink_to(self.config)
        self.assertEqual(self.request("GET", "/app.js")[0], 404)
        self.assertEqual(self.request("GET", "/index.html")[0], 200)

    def test_auditor_read_only_and_private_configuration(self):
        self.create()
        for route, body in (("notes", {"text": "No", "expected_revision": 1}), ("analysis", {"input_text": "{}", "expected_revision": 1}), ("submit", {"expected_revision": 1}), ("review", {"decision": "approve", "reason": "No", "expected_revision": 1})):
            self.assertEqual(self.request("POST", "/api/cases/case-1/" + route, body, role="auditor")[0], 403)
        self.assertEqual(self.request("GET", "/api/cases", role="auditor")[0], 200)
        if os.name == "posix":
            self.config.chmod(0o644)
            with self.assertRaises(WorkspaceError):
                load_config(self.config)
            self.config.chmod(0o600)
        for secret in self.tokens.values():
            self.assertNotIn(secret.encode(), self.workspace.database.read_bytes())


if __name__ == "__main__":
    unittest.main(verbosity=2)
