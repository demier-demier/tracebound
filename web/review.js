import init, { verify_json } from './pkg/tracebound.js';
const verifierReady = init();
const $ = (id) => document.getElementById(id);
const local = ['127.0.0.1', 'localhost'].includes(location.hostname);
const state = { token: '', actor: null, dossier: null, generation: 0 };
const text = (id, value) => { $(id).textContent = value; };
function el(tag, value, className) {
  const node = document.createElement(tag);
  if (value !== undefined) node.textContent = value;
  if (className) node.className = className;
  return node;
}
function message(value = '') { text('message', value); }
function date(value) { return new Date(value).toLocaleString('en-GB', { timeZone: 'UTC' }) + ' UTC'; }
async function digest(value) {
  const bytes = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value));
  return [...new Uint8Array(bytes)].map((b) => b.toString(16).padStart(2, '0')).join('');
}
function canonical(value) {
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  if (value && typeof value === 'object') return '{' + Object.keys(value).sort().map((key) => JSON.stringify(key) + ':' + canonical(value[key])).join(',') + '}';
  return JSON.stringify(value);
}
async function verifyDossier(dossier) {
  if (dossier.schema !== 'case-dossier/v1' || !Array.isArray(dossier.events) || !dossier.events.length) throw new Error('Unsupported or empty case history.');
  let previous = '0'.repeat(64); let sequence = 0;
  const artifacts = []; const analyses = []; const notes = [];
  for (const event of dossier.events) {
    const { hash, ...body } = event;
    if (event.case_id !== dossier.case.id || event.sequence !== ++sequence || event.previous_hash !== previous || await digest(canonical(body)) !== hash) throw new Error('The audit history does not verify.');
    previous = hash;
    artifacts.push(...event.payload.artifacts);
    if (event.payload.analysis) analyses.push(event.payload.analysis);
    if (event.payload.note) notes.push(event.payload.note);
  }
  if (dossier.anchor.case_id !== dossier.case.id || dossier.anchor.revision !== sequence || dossier.anchor.event_hash !== previous || canonical(dossier.events.at(-1).payload.case) !== canonical(dossier.case)) throw new Error('The case state is not bound to its audit root.');
  const actualMetadata = dossier.artifacts.map(({ content, ...metadata }) => metadata);
  if (canonical(artifacts) !== canonical(actualMetadata) || canonical(analyses) !== canonical(dossier.analyses) || canonical(notes) !== canonical(dossier.notes)) throw new Error('Dossier records differ from their audited exhibits.');
  await verifierReady;
  for (const analysis of dossier.analyses) {
    const input = dossier.artifacts.find((a) => a.id === analysis.input_artifact_id);
    const report = dossier.artifacts.find((a) => a.id === analysis.report_artifact_id);
    if (!input || !report) throw new Error('Analysis is missing a required exhibit.');
    verify_json(input.content, report.content);
  }
}
function download(content, filename, type = 'application/json') {
  const url = URL.createObjectURL(new Blob([content], { type }));
  const link = el('a'); link.href = url; link.download = filename;
  document.body.append(link); link.click(); link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
async function api(path, body) {
  if (!local || !state.token) throw new Error('Connect on your local workspace server first.');
  const response = await fetch(`/api${path}`, {
    method: body === undefined ? 'GET' : 'POST',
    headers: { Authorization: `Bearer ${state.token}`, ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `Request rejected (${response.status}).`);
  return result;
}
function formatMinor(value, currency) {
  const digits = Object.hasOwn({EUR:2,USD:2,GBP:2,BRL:2,JPY:0}, currency) ? {EUR:2,USD:2,GBP:2,BRL:2,JPY:0}[currency] : null;
  const n = BigInt(value);
  if (digits === null) return n.toLocaleString('en-US') + ' ' + currency + ' minor';
  const divisor = 10n ** BigInt(digits);
  return currency + ' ' + (n / divisor).toLocaleString('en-US') + (digits ? '.' + (n % divisor).toString().padStart(digits, '0') : '');
}
function artifactName(kind) { return /input|ledger/.test(kind) ? 'Original ledger snapshot' : /report|analysis/.test(kind) ? 'Certified attribution report' : kind; }
async function render(dossier, recorded = false) {
  const generation = ++state.generation;
  state.dossier = null;
  $('export-dossier').disabled = true; $('export-anchor').disabled = true;
  const current = dossier.case;
  text('case-id', current.id); text('case-title', current.title);
  text('case-description', current.description || 'Source-bound analysis with an independent review decision.');
  text('case-status', 'Unverified'); text('case-revision', 'Checking supplied revision');
  for (const id of ['artifacts', 'findings', 'notes', 'timeline']) $(id).replaceChildren();
  text('anchor-value', 'Not verified');
  text('mode', recorded ? 'Recorded synthetic case' : `${state.actor?.id} / ${state.actor?.role}`);
  $('recording-notice').hidden = !recorded;
  text('review-state', 'Checking exact artifact hashes…');
  const exhibits = dossier.artifacts || [];
  const checks = await Promise.all(exhibits.map(async (a) => typeof a.content === 'string' && await digest(a.content) === a.sha256));
  if (generation !== state.generation) return;
  if (!checks.every(Boolean)) throw new Error('An exhibit differs from its recorded hash. The dossier was not accepted.');
  await verifyDossier(dossier);
  if (generation !== state.generation) return;
  state.dossier = dossier;
  text('case-status', current.status.replaceAll('_', ' ')); text('case-revision', `Revision ${current.revision}`);
  text('review-state', `${exhibits.length} exhibits + audit chain + certificates checked · ${recorded ? 'Exported workflow record' : 'Live local workspace'}`);
  text('artifact-count', `${exhibits.length} exhibits`);
  $('artifacts').replaceChildren();
  exhibits.forEach((artifact, index) => {
    const row = el('article', undefined, 'artifact');
    const number = el('span', String(index + 1).padStart(2, '0'), 'artifact-number');
    const body = el('div'); body.append(el('h3', artifactName(artifact.kind)), el('p', `${artifact.byte_length.toLocaleString()} bytes · ${date(artifact.created_at)}`), el('code', artifact.sha256));
    const button = el('button', 'Export ↓'); button.addEventListener('click', () => download(artifact.content, `${artifact.id}.json`));
    row.append(number, body, button); $('artifacts').append(row);
  });
  $('findings').replaceChildren();
  const latest = (dossier.analyses || []).find((a) => a.id === current.current_analysis_id);
  const reportArtifact = exhibits.find((a) => a.id === latest?.report_artifact_id);
  if (reportArtifact) {
    const report = JSON.parse(reportArtifact.content);
    const source = JSON.parse(exhibits.find((a) => a.id === latest.input_artifact_id).content);
    for (const target of report.targets) {
      const movement = source.transfers.find((t) => t.id === target.transfer_id);
      const label = movement ? movement.from_account.replaceAll('-', ' ') + ' → ' + movement.to_account.replaceAll('-', ' ') : target.transfer_id;
      const row = el('tr'); row.append(el('td', label), el('td', formatMinor(target.minimum_minor, report.currency)), el('td', formatMinor(target.maximum_minor, report.currency)));
      $('findings').append(row);
    }
  } else { const row = el('tr'); const cell = el('td', 'No completed analysis.'); cell.colSpan = 3; row.append(cell); $('findings').append(row); }
  text('note-count', `${(dossier.notes || []).length} notes`); $('notes').replaceChildren();
  for (const note of dossier.notes || []) {
    const item = el('article', undefined, 'note');
    const meta = el('header'); meta.append(el('strong', note.actor), el('span', `Revision ${note.revision} · ${date(note.created_at)}`));
    item.append(meta, el('p', note.text)); $('notes').append(item);
  }
  $('timeline').replaceChildren(); text('event-count', `${dossier.events.length} events`);
  for (const event of dossier.events) {
    const item = el('li'); item.append(el('strong', event.action.replaceAll('_', ' ')), el('small', `${event.actor} / ${event.role} · ${date(event.at)}`));
    if (event.payload?.review?.reason) item.append(el('p', event.payload.review.reason));
    item.append(el('small', `#${event.sequence} · ${event.hash.slice(0, 18)}…`)); $('timeline').append(item);
  }
  text('anchor-value', dossier.anchor.event_hash);
  $('export-dossier').disabled = false; $('export-anchor').disabled = false;
  const draft = current.status === 'draft'; const investigator = state.actor?.role === 'investigator'; const reviewer = state.actor?.role === 'reviewer';
  $('analyze-form').hidden = !(draft && investigator); $('note-form').hidden = !(draft && (investigator || reviewer));
  $('submit-case').hidden = !(draft && investigator); $('review-form').hidden = !(current.status === 'in_review' && reviewer);
}
async function cases() {
  const result = await api('/cases');
  $('case-list').replaceChildren();
  for (const item of result.cases) {
    const button = el('button', item.title); button.append(el('small', `${item.id} · ${item.status}`));
    if (item.id === state.dossier?.case.id) button.classList.add('active');
    button.addEventListener('click', () => guarded(async () => { await open(item.id); await cases(); }));
    $('case-list').append(button);
  }
  return result.cases;
}
async function open(id) { await render(await api(`/cases/${encodeURIComponent(id)}/dossier`)); }
async function guarded(action) { message(); try { await action(); } catch (error) { message(error.message || 'Operation failed.'); } }
async function mutate(action, fields) {
  if (!state.dossier) throw new Error('Select a case first.');
  const current = state.dossier.case;
  await api(`/cases/${encodeURIComponent(current.id)}/${action}`, { ...fields, expected_revision: current.revision });
  await open(current.id); await cases();
}
$('export-dossier').addEventListener('click', () => { if (state.dossier) download(JSON.stringify(state.dossier, null, 2) + '\n', `${state.dossier.case.id}.dossier.json`); });
$('export-anchor').addEventListener('click', () => { if (state.dossier) download(JSON.stringify(state.dossier.anchor, null, 2) + '\n', `${state.dossier.case.id}.anchor.json`); });
$('local-panel').hidden = !local;
$('connect-form').addEventListener('submit', (event) => { event.preventDefault(); guarded(async () => {
  state.actor = null; $('workspace-controls').hidden = true;
  state.token = $('token').value; $('token').value = '';
  state.actor = (await api('/session')).actor;
  text('actor', `Connected: ${state.actor.id} (${state.actor.role})`); $('workspace-controls').hidden = false;
  $('new-case').hidden = state.actor.role !== 'investigator';
  const list = await cases(); if (list.length) { await open(list[0].id); await cases(); }
}); });
$('disconnect').addEventListener('click', () => { state.token = ''; state.actor = null; $('workspace-controls').hidden = true; text('actor', 'Disconnected. Token discarded.'); });
$('new-case').addEventListener('submit', (event) => { event.preventDefault(); guarded(async () => {
  const result = await api('/cases', { id: $('new-id').value, title: $('new-title').value, description: $('new-description').value, expected_revision: 0 });
  await open(result.case.id); await cases();
}); });
$('input-file').addEventListener('change', () => guarded(async () => {
  const file = $('input-file').files[0]; if (!file) return;
  if (file.size > 2 * 1024 * 1024) throw new Error('Ledger exceeds the 2 MiB input limit.');
  $('input-text').value = await file.text();
}));
$('analyze-form').addEventListener('submit', (event) => { event.preventDefault(); guarded(() => mutate('analysis', { input_text: $('input-text').value })); });
$('note-form').addEventListener('submit', (event) => { event.preventDefault(); guarded(async () => { await mutate('notes', { text: $('note-text').value }); $('note-text').value = ''; }); });
$('submit-case').addEventListener('click', () => guarded(() => mutate('submit', {})));
$('review-form').addEventListener('submit', (event) => { event.preventDefault(); guarded(() => mutate('review', { decision: $('review-decision').value, reason: $('review-reason').value })); });
await guarded(async () => {
  const response = await fetch('./evidence/demo-dossier.json');
  if (!response.ok) throw new Error('The recorded case dossier is not available.');
  const dossier = await response.json(); await render(dossier, true);
  const button = el('button', dossier.case.title, 'active'); button.append(el('small', `${dossier.case.id} · reviewed synthetic case`));
  $('case-list').replaceChildren(button);
});
