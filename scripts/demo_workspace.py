#!/usr/bin/env python3
"""Execute and export a real local HTTP case workflow with scripted synthetic actors."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from workspace.service import Workspace, create_config, load_config, make_server, verify_dossier  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True, help='New directory for synthetic public artifacts')
    parser.add_argument('--binary', type=Path, default=ROOT / 'target/release/tracebound')
    args = parser.parse_args()
    args.output_dir.mkdir(exist_ok=False, parents=True)
    observed = []
    with tempfile.TemporaryDirectory(prefix='tracebound-case-demo-') as temporary:
        root = Path(temporary)
        config, database = root / 'actors.json', root / 'cases.sqlite'
        create_config(config)
        actors = load_config(config)
        tokens = {actor.role: token.decode('ascii') for actor, token in actors}
        workspace = Workspace(database, args.binary.resolve())
        server = make_server(workspace, actors, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'

        def request(role, path, body=None, expected=200):
            raw = json.dumps(body).encode() if body is not None else None
            req = urllib.request.Request(base + '/api' + path, data=raw,
                                         headers={'Authorization': 'Bearer ' + tokens[role], 'Content-Type': 'application/json'})
            try:
                with urllib.request.urlopen(req, timeout=90) as response:
                    code, value = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                code, value = error.code, json.load(error)
            if code != expected:
                raise RuntimeError(f'Expected HTTP {expected}, received {code}: {value}')
            observed.append({'role': role, 'route': path, 'status': code})
            return value

        try:
            case = request('investigator', '/cases', {'id': 'TB-001', 'title': 'Following a split EUR 9,000 payment',
                           'description': 'Original synthetic case: determine what a complete ledger supports about a marked EUR 9,000 payment after it mixes with other balances. Review alternatives before making a finding.', 'expected_revision': 0}, expected=201)
            revision = case['case']['revision']
            case = request('investigator', '/cases/TB-001/analysis', {'input_text': (ROOT / 'examples/split-payment-review.json').read_text(), 'expected_revision': revision})
            revision = case['case']['revision']
            # A stale browser tab must not replace the current case revision.
            request('investigator', '/cases/TB-001/notes', {'text': 'Stale write control', 'expected_revision': revision - 1}, expected=409)
            case = request('investigator', '/cases/TB-001/notes', {'text': 'Question: how much of the selected EUR 9,000 payment can be assigned to the final EUR 7,000 payout? The ledger records movements, but does not identify which fungible units funded each payment. The model assigns between EUR 4,500 and EUR 7,000 to the final payout. Compare the complete endpoint allocations; do not sum marginal maxima.', 'expected_revision': revision})
            case = request('investigator', '/cases/TB-001/submit', {'expected_revision': case['case']['revision']})
            request('investigator', '/cases/TB-001/review', {'decision': 'approve', 'reason': 'Self-approval control', 'expected_revision': case['case']['revision']}, expected=403)
            case = request('reviewer', '/cases/TB-001/review', {'decision': 'return', 'reason': 'Add the legitimate settlement explanation and identify evidence absent from this closed ledger. Mathematical attribution alone cannot establish why the payments occurred.', 'expected_revision': case['case']['revision']})
            case = request('investigator', '/cases/TB-001/notes', {'text': 'Competing explanation: the same transfers are compatible with ordinary invoice payments and treasury settlement. Missing external evidence includes invoice authenticity, beneficial ownership and business purpose. The reviewed finding is limited to exact allocation bounds under the supplied ledger; no criminal or tax conclusion is supported.', 'expected_revision': case['case']['revision']})
            case = request('investigator', '/cases/TB-001/submit', {'expected_revision': case['case']['revision']})
            case = request('reviewer', '/cases/TB-001/review', {'decision': 'approve', 'reason': 'Approve this bounded analytical dossier: exact source retained, certificates verified, competing legitimate explanation recorded, and missing evidence explicit. This approves the documented analysis, not a finding of wrongdoing.', 'expected_revision': case['case']['revision']})
            request('auditor', '/cases/TB-001/notes', {'text': 'Read-only actor control', 'expected_revision': case['case']['revision']}, expected=403)
            request('investigator', '/cases/TB-001/notes', {'text': 'Frozen approval control', 'expected_revision': case['case']['revision']}, expected=409)
            dossier = request('auditor', '/cases/TB-001/dossier')
            anchor = dossier['anchor']['event_hash']
            request('auditor', '/cases/TB-001/verify?anchor=' + anchor)
            verify_dossier(dossier, anchor)
        finally:
            server.shutdown()
            thread.join(timeout=10)
            server.server_close()
        restarted = Workspace(database, args.binary.resolve())
        actor = next(actor for actor, _ in actors if actor.role == 'auditor')
        recovered = restarted.dossier(actor, 'TB-001', external_anchor=anchor)
        if recovered != dossier:
            raise RuntimeError('Persisted case differs after reopening the SQLite workspace')
        dossier_bytes = (json.dumps(dossier, indent=2, ensure_ascii=False) + '\n').encode()
        (args.output_dir / 'demo-dossier.json').write_bytes(dossier_bytes)
        (args.output_dir / 'demo-anchor.json').write_text(json.dumps(dossier['anchor'], indent=2) + '\n')
        summary = {'schema': 'tracebound-workspace-observed/v1', 'case_id': dossier['case']['id'],
                   'status': dossier['case']['status'], 'revision': dossier['case']['revision'],
                   'events': len(dossier['events']), 'artifacts': len(dossier['artifacts']),
                   'notes': len(dossier['notes']), 'anchor': dossier['anchor'],
                   'dossier_sha256': hashlib.sha256(dossier_bytes).hexdigest(),
                   'persistence_reopen_identical': True, 'http_observations': observed,
                   'scope': 'Actual loopback HTTP service, SQLite persistence, Rust analysis and certificate verification. Original synthetic ledger and scripted role-separated actors; no human case review or bank data is claimed.'}
        (args.output_dir / 'workspace-observed.json').write_text(json.dumps(summary, indent=2) + '\n')
        print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
