const $ = (id) => document.getElementById(id);
const SVG_NS = 'http://www.w3.org/2000/svg';
const MAX_BYTES = 2 * 1024 * 1024;
const CASES = [
  { id: 'split-payment-review', short: 'A split payment and a final payout', category: 'START HERE', title: 'Split payment review', primaryTarget: 'split-payment-review-06', description: 'Payments pass through two invoices, rejoin at settlement, and reach a final recipient. Opening balances add other funds along the route.', positions: [[90,240],[280,240],[450,100],[450,365],[650,240],[820,240]] },
  { id: 'seven-hop-bottleneck', short: 'Seven-hop bottleneck', category: '01 / MULTI-HOP', title: 'Seven-hop bottleneck', description: 'Follow one marked transfer through seven sequential legs, mixed balances and later returns. Inspect what must survive the bottleneck.', positions: [[105,115],[330,115],[555,115],[780,115],[780,345],[555,345],[330,345],[105,345]] },
  { id: 'commingled-payments', short: 'Commingled payments', category: '02 / AMBIGUITY', title: 'Commingled payments', description: 'Marked and unmarked balances meet at a payment desk, split into invoices and reunite at settlement. The interval preserves that ambiguity.', positions: [[100,95],[100,330],[325,210],[540,95],[540,330],[760,210],[760,380]] },
  { id: 'split-merge-cycle', short: 'Split, merge, return', category: '03 / CYCLES', title: 'Split, merge and return', description: 'Fourteen transfers create branching routes and a later cycle. Time ordering prevents a return from funding an earlier payment.', positions: [[85,225],[280,225],[460,85],[460,360],[640,225],[800,85],[800,360],[640,405]] },
  { id: 'unrelated-seed', short: 'Unrelated transfers', category: '04 / NEGATIVE CONTROL', title: 'Unrelated transfers', description: 'The marked transfer enters one branch while payroll and operating payments move elsewhere. Test whether the ledger actually connects them.', positions: [[95,100],[335,100],[575,100],[95,325],[335,270],[335,395],[575,395],[780,295]] },
  { id: 'treasury-sweep', short: 'Ordinary treasury', category: '05 / BUSINESS CONTEXT', title: 'Treasury sweep', description: 'An authored, legitimate treasury sweep mixes desk balances and returns liquidity to operations. A positive bound does not make the activity illicit.', positions: [[120,225],[340,85],[340,225],[340,365],[560,225],[775,100],[775,365]] },
];
const state = { config: CASES[0], caseData: null, report: null, targetId: null, witness: 'min', worker: null, timeout: null, generation: 0, busy: false, selectedTransfer: null, canonicalReport: null, inputBytes: null, reportSha256: null, exampleText: null, custom: false, playbackStep: 0, playTimer: null };

function setText(id, value) { $(id).textContent = value; }
function node(tag, text, className) { const el = document.createElement(tag); if (text !== undefined) el.textContent = text; if (className) el.className = className; return el; }
function svg(tag, attrs = {}, text) { const el = document.createElementNS(SVG_NS, tag); for (const [key, value] of Object.entries(attrs)) el.setAttribute(key, String(value)); if (text !== undefined) el.textContent = text; return el; }
function integer(value) { try { return BigInt(value ?? 0); } catch { return 0n; } }
function money(value, currency = state.caseData?.currency || 'EUR') {
  const amount = integer(value);
  // The curated cases use EUR cents. Unknown currencies stay in explicit minor units.
  const digitMap = { EUR: 2, USD: 2, GBP: 2, BRL: 2, CHF: 2, JPY: 0, KWD: 3 };
  const digits = Object.hasOwn(digitMap, currency) ? digitMap[currency] : undefined;
  if (digits === undefined) return `${amount.toLocaleString('en-US')} ${currency} minor`;
  const divisor = 10n ** BigInt(digits);
  const whole = (amount / divisor).toLocaleString('en-US');
  const fraction = digits ? '.' + (amount % divisor).toString().padStart(digits, '0') : '';
  const symbols = { EUR: '€', USD: '$', GBP: '£', BRL: 'R$', CHF: 'CHF ', JPY: '¥', KWD: 'KWD ' };
  const symbol = Object.hasOwn(symbols, currency) ? symbols[currency] : `${currency} `;
  return `${symbol}${whole}${fraction}`;
}
function percent(value, total) { const denominator = integer(total); if (!denominator) return '0.00%'; const basis = integer(value) * 10000n / denominator; return `${basis / 100n}.${(basis % 100n).toString().padStart(2, '0')}%`; }
function ratio(value, total) { const denominator = integer(total); return denominator > 0n ? Math.max(0, Math.min(100, Number(integer(value) * 100000n / denominator) / 1000)) : 0; }
function timeLabel(timestamp) { const d = new Date(timestamp); return Number.isNaN(d.getTime()) ? timestamp : d.toISOString().slice(11, 19); }
function targetResult() { return state.report?.targets?.find((t) => t.transfer_id === state.targetId) || null; }
function targetTransfer() { return state.caseData?.transfers.find((t) => t.id === state.targetId) || null; }
function witness() { const target = targetResult(); return target ? target[`${state.witness}_witness`] : null; }
function allocations() { return new Map((witness()?.transfer_allocations || []).map((a) => [a.transfer_id, a.seeded_minor])); }
function finals() { return new Map((witness()?.final_allocations || []).map((a) => [a.account, a.seeded_minor])); }
function status(text, kind = '') { setText('status-text', text); $('status-dot').className = `status-dot ${kind}`; }
function clearReport() {
  stopPlayback(); state.playbackStep = 0;
  for (const id of ['previous-step', 'next-step', 'play-steps', 'step-slider']) $(id).disabled = true;
  setText('bounds-heading', 'Waiting for the calculation.');
  setText('step-counter', '0 / 0'); setText('step-total', '—'); setText('step-marked', '—');
  setText('step-title', 'Run an analysis to follow the payments.'); setText('step-context', 'No modeled allocation is shown before successful analysis.');
  $('step-balances').replaceChildren();
  state.report = null; state.canonicalReport = null; state.inputBytes = null; state.reportSha256 = null;
  state.selectedTransfer = null;
  for (const id of ['download-report', 'download-input', 'download-anchor', 'lower-witness', 'upper-witness']) $(id).disabled = true;
  $('target-select').disabled = true;
  setText('minimum-value', '—'); setText('maximum-value', '—'); setText('minimum-percent', '—'); setText('maximum-percent', '—');
  setText('runtime', ''); setText('target-count', '—'); setText('anchor-text', 'A verified run will provide an exact report digest.');
  setText('target-description', 'Run the current ledger to inspect computed bounds.');
  setText('bound-explanation', 'No result is being shown for this ledger until the Rust analysis completes.');
  $('interval-fill').style.width = '0%';
  $('targets-body').replaceChildren(); const row = node('tr'); const cell = node('td', 'Run a case to inspect computed intervals.', 'empty-cell'); cell.colSpan = 5; row.append(cell); $('targets-body').append(row);
  $('ledger-body').replaceChildren(); $('final-allocations').replaceChildren();
}
function stopWorker() { if (state.worker) state.worker.terminate(); state.worker = null; clearTimeout(state.timeout); state.timeout = null; }
function error(message) { state.busy = false; stopWorker(); clearReport(); $('error-panel').hidden = false; $('error-panel').textContent = message; status('Analysis stopped. No result was published for this input.', 'error'); $('run-analysis').disabled = false; renderGraph(); }
function validateBrowserJSON(text) {
  if (new TextEncoder().encode(text).length > MAX_BYTES) throw new Error('The case exceeds the browser limit of 2 MiB.');
  const data = JSON.parse(text);
  const stack = [{ value: data, depth: 0 }]; let count = 0;
  while (stack.length) {
    const { value, depth } = stack.pop();
    if (++count > 200000 || depth > 128) throw new Error('The JSON structure exceeds the browser resource limit.');
    if (typeof value === 'number' && !Number.isSafeInteger(value)) throw new Error('All JSON numbers must be safe integers in the browser (at most 9,007,199,254,740,991). Use the native CLI for full u64 values.');
    if (value && typeof value === 'object') for (const child of Object.values(value)) stack.push({ value: child, depth: depth + 1 });
  }
  // This guard protects rendering only. Rust performs the authoritative strict schema and ledger checks.
  const validId = (value) => typeof value === 'string' && /^[A-Za-z0-9_.:-]{1,64}$/.test(value);
  if (!data || typeof data !== 'object' || Array.isArray(data) || data.schema !== 'flow-case/v1') throw new Error('Expected the flow-case/v1 schema.');
  if (!validId(data.case_id)) throw new Error('Case identifiers must contain 1–64 ASCII letters, digits, underscores, hyphens, dots or colons.');
  if (typeof data.currency !== 'string' || !/^[A-Z]{3}$/.test(data.currency)) throw new Error('Currency must be exactly three uppercase ASCII letters.');
  if (data.completeness_attested !== true) throw new Error('This model requires an explicit completeness_attested: true declaration for the supplied complete ledger.');
  if (!data || typeof data !== 'object' || !Array.isArray(data.transfers) || !Array.isArray(data.opening_balances) || !Array.isArray(data.closing_balances) || !Array.isArray(data.target_transfer_ids)) throw new Error('Expected a flow-case/v1 object with balances, transfers and target_transfer_ids arrays.');
  if (!data.transfers.length || data.transfers.length > 1000 || data.opening_balances.length > 128) throw new Error('This version supports 1–1,000 transfers and at most 128 accounts in both browser and native analysis.');
  if (!data.opening_balances.length || data.closing_balances.length > 128 || !data.target_transfer_ids.length || data.target_transfer_ids.length > 16) throw new Error('Supply 1–128 opening accounts, at most 128 closing accounts and 1–16 targets.');
  if (!validId(data.seed_transfer_id) || !data.target_transfer_ids.every(validId)) throw new Error('Seed and target transfer identifiers are invalid.');
  for (const t of data.transfers) {
    if (!t || !validId(t.id) || !validId(t.from_account) || !validId(t.to_account) || typeof t.timestamp !== 'string' || t.timestamp.length > 64) throw new Error('Each transfer needs valid 1–64 character identifiers and a bounded timestamp string.');
    if (!Number.isSafeInteger(t.amount_minor) || t.amount_minor <= 0 || !Number.isSafeInteger(t.sequence) || t.sequence <= 0) throw new Error('Transfer amounts and sequences must be positive safe integers.');
  }
  for (const b of [...data.opening_balances, ...data.closing_balances]) if (!b || !validId(b.account) || !Number.isSafeInteger(b.amount_minor) || b.amount_minor < 0) throw new Error('Each balance needs a valid account identifier and a nonnegative safe integer amount.');
  return data;
}
function invalidateInputView(message) {
  for (const id of ['story-seed-amount', 'story-payer-balance', 'story-target-amount']) setText(id, '—');
  state.caseData = null; state.custom = true;
  $('network').replaceChildren(); $('network-empty').hidden = false; $('network-empty').textContent = message;
  setText('case-title', 'Ledger awaiting validation.'); setText('case-description', message); setText('data-kind', 'CUSTOM INPUT');
  setText('stat-transfers', '—'); setText('stat-accounts', '—'); setText('stat-seed', '—');
  setDetail('No network or allocation is shown for an unvalidated edited input.');
}
function updateOverview() {
  const data = state.caseData;
  if (!data) return;
  const curated = !state.custom && data.case_id === state.config.id;
  setText('data-kind', curated ? 'SYNTHETIC' : 'CUSTOM INPUT');
  setText('case-title', curated ? state.config.title : 'Your complete-ledger case.');
  setText('case-description', curated ? state.config.description : 'Custom input. Closing balances and completeness remain the operator’s responsibility. Rust validates the declared ledger before computing bounds.');
  setText('stat-transfers', String(data.transfers.length)); setText('stat-accounts', String(data.opening_balances.length));
  const seed = data.transfers.find((t) => t.id === data.seed_transfer_id);
  setText('stat-seed', seed ? money(seed.amount_minor) : '—'); setText('currency-label', data.currency || 'minor units');
  updateStory();
  $('network-empty').hidden = true;
}
function analyzeText(text) {
  const generation = ++state.generation;
  stopWorker(); clearReport(); $('error-panel').hidden = true;
  let data;
  try { data = validateBrowserJSON(text); } catch (e) { invalidateInputView('Input rejected. Correct the JSON below and recompute.'); error(e.message); return; }
  state.custom = text !== state.exampleText;
  state.caseData = data; state.targetId = !state.custom && data.target_transfer_ids.includes(state.config.primaryTarget) ? state.config.primaryTarget : data.target_transfer_ids[0] || null;
  state.busy = true; $('run-analysis').disabled = true;
  updateOverview(); renderGraph(); status('Computing exact bounds and complete allocation witnesses locally…', 'working');
  let worker;
  try { worker = new Worker(new URL('./worker.js', import.meta.url), { type: 'module' }); } catch (e) { error(`Unable to start the local Rust worker: ${e.message}`); return; }
  state.worker = worker;
  state.timeout = setTimeout(() => { if (generation === state.generation) error('Analysis exceeded the 45-second browser time budget. The worker was stopped. Reduce the ledger or use the native CLI.'); }, 45000);
  worker.onmessage = ({ data: result }) => {
    if (generation !== state.generation) return;
    if (!result.ok) { error(result.error || 'The Rust analysis rejected this input.'); return; }
    if (!result.report || !Array.isArray(result.report.targets)) { error('The worker returned an unsupported report.'); return; }
    state.report = result.report; state.canonicalReport = result.canonicalReport || null; state.inputBytes = result.inputBytes || null; state.reportSha256 = result.reportSha256 || null;
    state.busy = false; clearTimeout(state.timeout); state.timeout = null; $('run-analysis').disabled = false;
    $('download-report').disabled = !state.canonicalReport; $('download-input').disabled = !state.inputBytes; $('download-anchor').disabled = !state.reportSha256;
    $('lower-witness').disabled = false; $('upper-witness').disabled = false;
    status('Ledger reconciled. Exact bounds and complete witnesses computed in your browser.', 'ready');
    const elapsed = typeof result.elapsedMs === 'number' ? result.elapsedMs : null;
    setText('runtime', elapsed === null ? 'Rust / WebAssembly' : `${elapsed < 10 ? elapsed.toFixed(2) : elapsed.toFixed(1)} ms · analysis + verification`);
    setText('anchor-text', state.reportSha256 ? `Report SHA-256: ${state.reportSha256}` : 'Canonical download artifacts are unavailable from this worker.');
    renderTargets(); renderSelection();
  };
  worker.onerror = (event) => { if (generation === state.generation) error(`The local analysis module could not run. ${event.message || 'Check that the WebAssembly files are deployed and this page is served over HTTP(S).'}`); };
  worker.postMessage({ caseData: data, inputText: text });
}
async function loadCase(config) {
  const generation = ++state.generation; stopWorker(); state.config = config; $('case-library').value = config.id;
  for (const button of $('case-buttons').children) { const active = button.dataset.caseId === config.id; button.classList.toggle('active', active); button.setAttribute('aria-pressed', String(active)); }
  clearReport(); $('error-panel').hidden = true; status('Loading an original synthetic ledger…', 'working');
  try {
    const response = await fetch(new URL(`./cases/${config.id}.json`, import.meta.url));
    if (!response.ok) throw new Error(`Unable to load example (${response.status}).`);
    const text = await response.text();
    if (generation !== state.generation) return;
    state.exampleText = text; $('case-editor').value = text; state.witness = 'min'; analyzeText(text);
  } catch (e) { if (generation === state.generation) error(e.message); }
}
function renderTargets() {
  const select = $('target-select'); select.replaceChildren();
  for (const target of state.report.targets) {
    const transfer = state.caseData.transfers.find((t) => t.id === target.transfer_id);
    const option = node('option', `#${transfer?.sequence ?? '?'} · ${prettyAccount(transfer?.from_account || '?')} → ${prettyAccount(transfer?.to_account || '?')} · ${transfer ? displayMoney(transfer.amount_minor) : ''}`);
    option.value = target.transfer_id; select.append(option);
  }
  if (!state.report.targets.some((t) => t.transfer_id === state.targetId)) state.targetId = state.report.targets[0]?.transfer_id || null;
  select.value = state.targetId || ''; select.disabled = !state.targetId;
  setText('target-count', `${state.report.targets.length}`);
  const body = $('targets-body'); body.replaceChildren();
  for (const target of state.report.targets) {
    const transfer = state.caseData.transfers.find((t) => t.id === target.transfer_id);
    if (!transfer) continue;
    const row = node('tr'); row.dataset.targetId = target.transfer_id; row.tabIndex = 0; row.setAttribute('aria-label', `Select ${target.transfer_id}`);
    const name = node('td'); name.append(node('strong', `#${transfer.sequence} → ${transfer.to_account}`), node('small', target.transfer_id));
    const total = node('td', money(transfer.amount_minor)); const minimum = node('td', money(target.minimum_minor)); const maximum = node('td', money(target.maximum_minor), 'maximum-cell');
    const rangeCell = node('td'); const range = node('div', undefined, 'mini-range'); const fill = node('i'); fill.style.left = `${ratio(target.minimum_minor, transfer.amount_minor)}%`; fill.style.width = `${ratio(integer(target.maximum_minor) - integer(target.minimum_minor), transfer.amount_minor)}%`; range.append(fill); range.setAttribute('aria-label', `${money(target.minimum_minor)} to ${money(target.maximum_minor)}`); rangeCell.append(range);
    row.append(name, total, minimum, maximum, rangeCell);
    const choose = () => { state.targetId = target.transfer_id; state.selectedTransfer = null; renderSelection(); };
    row.addEventListener('click', choose); row.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); choose(); } }); body.append(row);
  }
}
function renderSelection() {
  const target = targetResult(); const transfer = targetTransfer();
  if (!target || !transfer) return;
  $('target-select').value = target.transfer_id;
  for (const row of $('targets-body').children) row.classList.toggle('active', row.dataset.targetId === target.transfer_id);
  setText('target-description', `#${transfer.sequence} · ${transfer.from_account} → ${transfer.to_account} · ${timeLabel(transfer.timestamp)} UTC`);
  setText('minimum-value', money(target.minimum_minor)); setText('maximum-value', money(target.maximum_minor));
  setText('minimum-percent', `${percent(target.minimum_minor, transfer.amount_minor)} of this transfer`); setText('maximum-percent', `${percent(target.maximum_minor, transfer.amount_minor)} of this transfer`);
  setText('target-capacity', money(transfer.amount_minor));
  const minimum = integer(target.minimum_minor); const maximum = integer(target.maximum_minor);
  $('interval-fill').style.left = `${ratio(minimum, transfer.amount_minor)}%`;
  $('interval-fill').style.width = `${ratio(maximum - minimum, transfer.amount_minor)}%`;
  let explanation;
  if (maximum === 0n) explanation = 'Under this complete-ledger model, none of the marked amount can be allocated to this transfer. This is a constraint result, not a judgment about the account.';
  else if (minimum === maximum) explanation = `The ledger fixes this allocation at ${money(minimum)}. There is no allocation ambiguity for this target under the declared model.`;
  else if (minimum === 0n) explanation = `A valid allocation can send none of the marked amount here. Another can send up to ${money(maximum)}. The ledger alone cannot choose between them.`;
  else explanation = `Every valid allocation sends at least ${money(minimum)} here; one can send as much as ${money(maximum)}. The space between is unresolved by this ledger.`;
  setText('bound-explanation', explanation);
  for (const [kind, id] of [['min', 'lower-witness'], ['max', 'upper-witness']]) { $(id).classList.toggle('active', state.witness === kind); $(id).setAttribute('aria-pressed', String(state.witness === kind)); }
  setText('witness-note', `Showing one complete ${state.witness === 'min' ? 'lower' : 'upper'}-bound allocation for transfer #${transfer.sequence}. It is a feasible alternative, not a reconstruction of what actually happened.`);
  renderGraph(); renderLedger(); updateStory(); renderPlayback();
}
function getPositions() {
  const accounts = state.caseData.opening_balances.map((b) => b.account);
  const points = state.caseData.case_id === state.config.id && accounts.length === state.config.positions.length ? state.config.positions : accounts.map((_, i) => { const angle = -Math.PI / 2 + 2 * Math.PI * i / accounts.length; return [450 + Math.cos(angle) * 330, 230 + Math.sin(angle) * 160]; });
  return new Map(accounts.map((account, i) => [account, { x: points[i][0], y: points[i][1] }]));
}
function renderGraph() {
  const network = $('network'); network.replaceChildren();
  if (!state.caseData) return;
  const { transfers, seed_transfer_id } = state.caseData; const positions = getPositions(); const allocated = allocations(); const remaining = finals();
  const defs = svg('defs');
  for (const [id, color] of [['base', '#bec8ce'], ['flow', '#4776bd'], ['seed', '#b9792c'], ['target', '#183a71']]) { const marker = svg('marker', { id: `arrow-${id}`, viewBox: '0 0 10 10', refX: 8, refY: 5, markerWidth: 5, markerHeight: 5, orient: 'auto-start-reverse', markerUnits: 'strokeWidth' }); marker.append(svg('path', { d: 'M 0 0 L 10 5 L 0 10 z', fill: color })); defs.append(marker); }
  network.append(defs);
  const edges = svg('g'); const labels = svg('g'); const nodes = svg('g'); const edgeCounts = new Map();
  for (const t of transfers) { const key = [t.from_account, t.to_account].sort().join('\0'); edgeCounts.set(key, (edgeCounts.get(key) || 0) + 1); }
  const ordinals = new Map(); const maxAmount = state.report ? integer(state.report.seed?.amount_minor || 1) : 1n;
  for (const t of transfers) {
    const start = positions.get(t.from_account); const end = positions.get(t.to_account); if (!start || !end) continue;
    const key = [t.from_account, t.to_account].sort().join('\0'); const ordinal = ordinals.get(key) || 0; ordinals.set(key, ordinal + 1);
    const dx = end.x - start.x; const dy = end.y - start.y; const distance = Math.hypot(dx, dy) || 1;
    const separation = edgeCounts.get(key) > 1 ? 32 + ordinal * 34 : (distance > 330 ? 48 : 13);
    const bend = (ordinal % 2 ? -1 : 1) * separation;
    const control = { x: (start.x + end.x) / 2 - dy / distance * bend, y: (start.y + end.y) / 2 + dx / distance * bend };
    const startScale = 1 / Math.max(Math.abs(control.x - start.x) / 75, Math.abs(control.y - start.y) / 32, .001);
    const endScale = 1 / Math.max(Math.abs(end.x - control.x) / 75, Math.abs(end.y - control.y) / 32, .001);
    const sx = start.x + (control.x - start.x) * startScale; const sy = start.y + (control.y - start.y) * startScale;
    const ex = end.x - (end.x - control.x) * endScale; const ey = end.y - (end.y - control.y) * endScale;
    const path = `M${sx},${sy} Q${control.x},${control.y} ${ex},${ey}`;
    const amount = integer(allocated.get(t.id) || 0); const isSeed = t.id === seed_transfer_id; const isTarget = t.id === state.targetId && !!state.report; const selected = t.id === state.selectedTransfer;
    const kind = isSeed ? 'seed' : isTarget ? 'target' : amount > 0n ? 'flow' : 'base';
    const color = { base: '#bec8ce', seed: '#b9792c', target: '#183a71', flow: '#4776bd' }[kind];
    const width = amount > 0n ? 1.4 + ratio(amount, maxAmount) / 100 * 3.8 : 1.0;
    if (selected) edges.append(svg('path', { d: path, stroke: '#3f6cb5', 'stroke-width': width + 7, 'stroke-opacity': .12, fill: 'none' }));
    edges.append(svg('path', { d: path, stroke: color, 'stroke-width': isTarget ? Math.max(2.7, width) : width, 'stroke-opacity': amount || isSeed || isTarget ? .95 : .5, fill: 'none', 'marker-end': `url(#arrow-${kind})`, 'stroke-dasharray': !amount && state.report && !isSeed && !isTarget ? '4 5' : 'none' }));
    const hit = svg('path', { d: path, stroke: 'transparent', 'stroke-width': 18, fill: 'none', class: 'edge-hit', tabindex: 0, role: 'button', 'aria-label': `Transfer ${t.id}, ${t.from_account} to ${t.to_account}, ${money(t.amount_minor)}${state.report ? `, marked allocation ${money(amount)}` : ''}` });
    hit.append(svg('title', {}, `#${t.sequence} ${t.id}\n${t.from_account} → ${t.to_account}\n${money(t.amount_minor)} · ${timeLabel(t.timestamp)} UTC\n${state.report ? `Marked allocation: ${money(amount)}` : 'Analysis not yet available'}`));
    const inspect = () => { state.selectedTransfer = t.id; renderGraph(); showTransferDetail(t); };
    hit.addEventListener('click', inspect); hit.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); inspect(); } }); edges.append(hit);
    if (transfers.length <= 40) { const lx = .25 * sx + .5 * control.x + .25 * ex; const ly = .25 * sy + .5 * control.y + .25 * ey - 7; labels.append(svg('text', { x: lx, y: ly, 'text-anchor': 'middle', fill: color, class: 'edge-label' }, `#${t.sequence}`)); }
  }
  for (const balance of state.caseData.opening_balances) {
    const account = balance.account; const point = positions.get(account); const final = remaining.get(account); const isSeedRecipient = transfers.find((t) => t.id === seed_transfer_id)?.to_account === account;
    const group = svg('g', { class: 'node-group', tabindex: 0, role: 'button', 'aria-label': `Account ${account}${final !== undefined ? `, closing marked balance ${money(final)}` : ''}` });
    const positive = integer(final || 0) > 0n;
    group.append(svg('rect', { x: point.x - 71, y: point.y - 28, width: 142, height: 56, rx: 3, fill: '#ffffff', stroke: isSeedRecipient ? '#ba945d' : positive ? '#7a9bc9' : '#c8d1d6', 'stroke-width': 1, class: 'node-ring' }));
    group.append(svg('rect', { x: point.x - 71, y: point.y - 28, width: 3, height: 56, rx: 1, fill: isSeedRecipient ? '#b9792c' : positive ? '#4776bd' : '#c8d1d6' }));
    group.append(svg('text', { x: point.x + 1, y: point.y - 4, 'text-anchor': 'middle', class: 'node-label' }, account.length > 16 ? account.slice(0, 14) + '…' : account));
    group.append(svg('text', { x: point.x + 1, y: point.y + 16, 'text-anchor': 'middle', class: 'node-amount' }, final !== undefined ? `${money(final)} at close` : 'ALLOCATION PENDING'));
    const inspect = () => { const closing = state.caseData.closing_balances.find((b) => b.account === account); setDetail(`${account} · Opening ${money(balance.amount_minor)} · Closing ${money(closing?.amount_minor)}${final !== undefined ? ` · Marked balance in this witness ${money(final)}` : ' · Analysis pending'}`); };
    group.addEventListener('click', inspect); group.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); inspect(); } }); nodes.append(group);
  }
  network.append(edges, labels, nodes);
  if (state.selectedTransfer) { const t = transfers.find((x) => x.id === state.selectedTransfer); if (t) showTransferDetail(t); }
  else setDetail(state.report ? `Viewing the ${state.witness === 'min' ? 'lower' : 'upper'}-bound witness for transfer #${targetTransfer()?.sequence ?? '?'}. Select an edge for exact amounts and time. All zero allocations remain in the ledger table.` : 'Network topology only. No computed allocation is shown until Rust validates the ledger.');
}
function setDetail(text) { $('graph-detail').replaceChildren(node('span', '↗', 'detail-symbol'), node('p', text)); }
function showTransferDetail(t) { const amount = allocations().get(t.id); setDetail(`#${t.sequence} · ${t.id} · ${t.from_account} → ${t.to_account} · ${timeLabel(t.timestamp)} UTC · Total ${money(t.amount_minor)}${amount !== undefined ? ` · Marked allocation ${money(amount)} (${percent(amount, t.amount_minor)})` : ' · Analysis pending'}`); }
function renderLedger() {
  const body = $('ledger-body'); body.replaceChildren(); const allocated = allocations();
  for (const t of state.caseData.transfers) { const row = node('tr'); const id = node('td'); id.append(node('strong', `#${t.sequence}`), node('small', t.id)); if (t.id === state.caseData.seed_transfer_id) id.classList.add('row-seed'); if (t.id === state.targetId) id.classList.add('row-target'); row.append(id, node('td', timeLabel(t.timestamp)), node('td', `${t.from_account} → ${t.to_account}`), node('td', money(t.amount_minor)), node('td', allocated.has(t.id) ? money(allocated.get(t.id)) : 'Missing')); body.append(row); }
  const container = $('final-allocations'); container.replaceChildren();
  for (const [account, amount] of finals()) { const chip = node('span', undefined, 'balance-chip'); chip.append(document.createTextNode(account), node('strong', money(amount))); container.append(chip); }
}
function download(text, suffix, type = 'application/json') { if (typeof text !== 'string') return; const url = URL.createObjectURL(new Blob([text], { type })); const link = node('a'); link.href = url; const stem = String(state.caseData?.case_id || 'tracebound').replace(/[^a-zA-Z0-9_-]/g, '_').slice(0, 100); link.download = `${stem}.${suffix}`; document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000); }

function prettyAccount(account) { return String(account).split(/[-_]/).map((part) => part ? part[0].toUpperCase() + part.slice(1) : '').join(' '); }
function displayMoney(amount) { return money(amount).replace(/\.00$/, ''); }
function orderedTransfers(data) { return [...data.transfers].sort((a, b) => a.sequence - b.sequence); }
function balanceBefore(data, targetId) {
  const balances = new Map(data.opening_balances.map((b) => [b.account, BigInt(b.amount_minor)]));
  for (const transfer of orderedTransfers(data)) {
    if (transfer.id === targetId) return balances.get(transfer.from_account) ?? null;
    const amount = BigInt(transfer.amount_minor);
    if (!balances.has(transfer.from_account) || !balances.has(transfer.to_account) || balances.get(transfer.from_account) < amount) return null;
    balances.set(transfer.from_account, balances.get(transfer.from_account) - amount);
    balances.set(transfer.to_account, balances.get(transfer.to_account) + amount);
  }
  return null;
}
/** Replay a returned complete witness; the marked label is introduced at the seed transfer. */
function replayWitness(data, allocation, throughStep) {
  const transfers = orderedTransfers(data);
  if (!Number.isInteger(throughStep) || throughStep < 0 || throughStep > transfers.length) throw new Error('Playback step is outside the ledger.');
  const totals = new Map(data.opening_balances.map((b) => [b.account, BigInt(b.amount_minor)]));
  const marked = new Map(data.opening_balances.map((b) => [b.account, 0n]));
  const amounts = new Map(allocation.transfer_allocations.map((a) => [a.transfer_id, BigInt(a.seeded_minor)]));
  let transfer = null; let beforeTotals = new Map(totals); let beforeMarked = new Map(marked);
  for (let i = 0; i < throughStep; i++) {
    transfer = transfers[i]; beforeTotals = new Map(totals); beforeMarked = new Map(marked);
    if (!amounts.has(transfer.id)) throw new Error('The witness is missing a ledger transfer.');
    const total = BigInt(transfer.amount_minor); const share = amounts.get(transfer.id);
    if (share < 0n || share > total) throw new Error('The witness allocation exceeds a payment.');
    totals.set(transfer.from_account, totals.get(transfer.from_account) - total);
    totals.set(transfer.to_account, totals.get(transfer.to_account) + total);
    if (transfer.id !== data.seed_transfer_id) marked.set(transfer.from_account, marked.get(transfer.from_account) - share);
    marked.set(transfer.to_account, marked.get(transfer.to_account) + share);
    for (const [account, balance] of totals) if (balance < 0n || marked.get(account) < 0n || marked.get(account) > balance) throw new Error('Playback failed a balance or marked-allocation constraint.');
  }
  return { transfers, transfer, totals, marked, beforeTotals, beforeMarked, amounts };
}
function updateStory() {
  if (!state.caseData) return;
  const seed = state.caseData.transfers.find((t) => t.id === state.caseData.seed_transfer_id);
  const transfer = targetTransfer();
  if (!seed || !transfer) return;
  const isIntro = !state.custom && state.caseData.case_id === 'split-payment-review';
  setText('case-title', `A ${displayMoney(seed.amount_minor)} payment came in. How much could be part of this ${displayMoney(transfer.amount_minor)} ${isIntro && transfer.id === state.config.primaryTarget ? 'payout' : 'payment'}?`);
  setText('case-description', isIntro ? 'Payments split between Invoice A and Invoice B, then rejoin at Settlement. We use the complete ledger to test what can reach the recipient.' : `${prettyAccount(seed.from_account)} pays ${prettyAccount(seed.to_account)}. We then examine a later payment from ${prettyAccount(transfer.from_account)} to ${prettyAccount(transfer.to_account)}.`);
  setText('story-seed-amount', displayMoney(seed.amount_minor));
  setText('story-seed-route', `${prettyAccount(seed.from_account)} → ${prettyAccount(seed.to_account)}`);
  const before = balanceBefore(state.caseData, transfer.id);
  setText('story-payer-balance', before === null ? '—' : displayMoney(before));
  setText('story-payer-caption', `${prettyAccount(transfer.from_account)} holds this total before the selected payment.`);
  setText('story-mixing', 'This total includes opening balances and all earlier payments; it is not all necessarily marked.');
  setText('story-target-amount', displayMoney(transfer.amount_minor));
  setText('story-target-route', `${prettyAccount(transfer.from_account)} → ${prettyAccount(transfer.to_account)}`);
  const result = targetResult();
  if (result) {
    const minimum = BigInt(result.minimum_minor); const maximum = BigInt(result.maximum_minor);
    if (maximum === 0n) setText('bounds-heading', 'None of the marked payment can be allocated here.');
    else if (minimum === maximum) setText('bounds-heading', `The only possible marked share is ${displayMoney(minimum)}.`);
    else setText('bounds-heading', `The marked share can range from ${displayMoney(minimum)} to ${displayMoney(maximum)}.`);
    if (minimum !== maximum) setText('bound-explanation', `Balances mix along the way. The ledger allows more than one allocation, but no valid allocation falls outside this range. Compare the two checked alternatives below.`);
  }
}
function stopPlayback() { clearInterval(state.playTimer); state.playTimer = null; if (typeof document !== 'undefined' && $('play-steps')) setText('play-steps', 'Play payments'); }
function renderPlayback() {
  if (!state.report || !state.caseData || !witness()) return;
  const totalSteps = state.caseData.transfers.length;
  state.playbackStep = Math.max(0, Math.min(totalSteps, state.playbackStep));
  let snapshot;
  try { snapshot = replayWitness(state.caseData, witness(), state.playbackStep); } catch (failure) { stopPlayback(); setText('step-title', 'This allocation could not be replayed.'); setText('step-context', failure.message); return; }
  $('step-slider').max = String(totalSteps); $('step-slider').value = String(state.playbackStep); $('step-slider').disabled = false;
  $('previous-step').disabled = state.playbackStep === 0; $('next-step').disabled = state.playbackStep === totalSteps; $('play-steps').disabled = false;
  setText('step-counter', `${state.playbackStep} / ${totalSteps}`);
  const transfer = snapshot.transfer;
  const container = $('step-balances'); container.replaceChildren();
  if (!transfer) {
    setText('step-label', 'BEFORE THE FIRST PAYMENT'); setText('step-title', 'Start with the declared opening balances.');
    setText('step-context', 'Press Next or Play. The analysis label is introduced when the marked payment occurs; no opening balance is marked beforehand.');
    setText('step-total', '—'); setText('step-marked', '—');
    const funded = [...snapshot.totals].filter(([, amount]) => amount > 0n);
    for (const [account, amount] of funded) { const box = node('div', undefined, 'step-balance'); box.append(node('strong', prettyAccount(account)), document.createTextNode(` opens with ${displayMoney(amount)}. Marked: ${displayMoney(0)}.`)); container.append(box); }
    return;
  }
  const markedAmount = snapshot.amounts.get(transfer.id);
  setText('step-label', `PAYMENT ${state.playbackStep} OF ${totalSteps} · ${timeLabel(transfer.timestamp)} UTC`);
  setText('step-title', `${prettyAccount(transfer.from_account)} pays ${prettyAccount(transfer.to_account)}.`);
  setText('step-total', displayMoney(transfer.amount_minor)); setText('step-marked', displayMoney(markedAmount));
  let context = `${displayMoney(markedAmount)} of this ${displayMoney(transfer.amount_minor)} payment carries the analysis label in the ${state.witness === 'min' ? 'smallest' : 'largest'}-share alternative.`;
  if (transfer.id === state.caseData.seed_transfer_id) context = 'This is the selected marked payment. Its full amount receives the analysis label here.';
  else if (transfer.id === state.targetId) context += ' This is the payment being examined.';
  setText('step-context', context);
  for (const account of [transfer.from_account, transfer.to_account]) { const box = node('div', undefined, 'step-balance'); box.append(node('strong', `${prettyAccount(account)} after this payment`), node('br'), document.createTextNode(`Total balance: ${displayMoney(snapshot.totals.get(account))}. Marked share: ${displayMoney(snapshot.marked.get(account))}.`)); container.append(box); }
}
function bootWorkbench() {
  for (const config of CASES) { const option = node('option', config.short); option.value = config.id; $('case-library').append(option); }
  $('case-library').addEventListener('change', () => { const selected = CASES.find((c) => c.id === $('case-library').value); if (selected) loadCase(selected); });
  $('target-select').addEventListener('change', () => { stopPlayback(); state.targetId = $('target-select').value; state.selectedTransfer = null; state.playbackStep = 0; renderSelection(); });
  $('lower-witness').addEventListener('click', () => { stopPlayback(); state.witness = 'min'; renderSelection(); });
  $('upper-witness').addEventListener('click', () => { stopPlayback(); state.witness = 'max'; renderSelection(); });
  $('reset-graph').addEventListener('click', () => { state.selectedTransfer = null; renderGraph(); });
  $('run-analysis').addEventListener('click', () => analyzeText($('case-editor').value));
  $('restore-case').addEventListener('click', () => loadCase(state.config));
  $('download-report').addEventListener('click', () => download(state.canonicalReport, 'report.json'));
  $('download-input').addEventListener('click', () => download(state.inputBytes, 'input.json'));
  $('download-anchor').addEventListener('click', () => download(state.reportSha256 ? `${state.reportSha256}\n` : null, 'report.sha256', 'text/plain'));
  $('case-editor').addEventListener('input', () => { if (!state.report && !state.busy) return; ++state.generation; stopWorker(); state.busy = false; clearReport(); $('run-analysis').disabled = false; status('Ledger edited. Recompute before using or downloading a result.'); invalidateInputView('The input has changed. Recompute to validate this ledger and calculate new bounds.'); });
  $('open-editor').addEventListener('click', () => { $('editor-details').open = true; $('editor-details').scrollIntoView({ behavior: 'smooth', block: 'start' }); $('case-editor').focus({ preventScroll: true }); });
  $('upload-case').addEventListener('click', () => $('case-file').click());
  $('case-file').addEventListener('change', async () => {
    const file = $('case-file').files?.[0]; if (!file) return;
    if (file.size > MAX_BYTES) { error('The selected file exceeds 2 MiB.'); $('case-file').value = ''; return; }
    try { const text = await file.text(); $('case-editor').value = text; analyzeText(text); } catch (failure) { error(`Unable to read this local JSON file: ${failure.message}`); }
    $('case-file').value = '';
  });
  $('previous-step').addEventListener('click', () => { stopPlayback(); state.playbackStep--; renderPlayback(); });
  $('next-step').addEventListener('click', () => { stopPlayback(); state.playbackStep++; renderPlayback(); });
  $('step-slider').addEventListener('input', () => { stopPlayback(); state.playbackStep = Number($('step-slider').value); renderPlayback(); });
  $('play-steps').addEventListener('click', () => {
    if (state.playTimer) { stopPlayback(); return; }
    if (state.playbackStep >= state.caseData.transfers.length) state.playbackStep = 0;
    setText('play-steps', 'Pause');
    state.playTimer = setInterval(() => { state.playbackStep++; renderPlayback(); if (state.playbackStep >= state.caseData.transfers.length) stopPlayback(); }, 1800);
  });
  window.addEventListener('pagehide', () => { stopPlayback(); stopWorker(); });
  loadCase(CASES[0]);
}

if (typeof document !== 'undefined') bootWorkbench();
export { validateBrowserJSON, money, replayWitness, balanceBefore };
