/** Behavior checks for the browser's preflight boundary and exact monetary display.
 * No DOM mocks, evaluated source, network, browser or WASM solver is involved.
 * Rust remains authoritative for the complete schema, duplicate keys and ledger.
 */
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
import { validateBrowserJSON, money } from '../web/app.js';

const fixtureNames = ['commingled-payments', 'seven-hop-bottleneck', 'split-merge-cycle', 'unrelated-seed', 'treasury-sweep'];
const fixturePath = (name) => new URL(`../examples/${name}.json`, import.meta.url);
const base = JSON.parse(await readFile(fixturePath('seven-hop-bottleneck'), 'utf8'));

for (const name of fixtureNames) {
  test(`accepts curated complete-ledger input: ${name}`, async () => {
    const text = await readFile(fixturePath(name), 'utf8');
    assert.deepEqual(validateBrowserJSON(text), JSON.parse(text));
  });
}
const invalidInputs = [
  ['prototype currency property', (data) => { data.currency = 'constructor'; }],
  ['object as currency', (data) => { data.currency = {}; }],
  ['integer outside precise browser range', (data) => { data.transfers[0].amount_minor = Number.MAX_SAFE_INTEGER + 1; }],
  ['fractional monetary amount', (data) => { data.transfers[0].amount_minor = 1.5; }],
  ['negative opening balance', (data) => { data.opening_balances[0].amount_minor = -1; }],
  ['null transfer record', (data) => { data.transfers[0] = null; }],
  ['object as account identifier', (data) => { data.opening_balances[0].account = {}; }],
  ['HTML-shaped account identifier', (data) => { data.opening_balances[0].account = '<script>alert(1)</script>'; }],
  ['absent completeness attestation', (data) => { delete data.completeness_attested; }],
  ['transfer collection above the model limit', (data) => { data.transfers = Array(1001).fill(data.transfers[0]); }],
  ['target collection above the model limit', (data) => { data.target_transfer_ids = Array(17).fill(data.transfers[0].id); }],
];
for (const [name, mutate] of invalidInputs) {
  test(`rejects ${name} before rendering`, () => {
    const data = structuredClone(base);
    mutate(data);
    assert.throws(() => validateBrowserJSON(JSON.stringify(data)));
  });
}
test('rejects oversized text before JSON parsing', () => {
  assert.throws(() => validateBrowserJSON(' '.repeat(2 * 1024 * 1024 + 1)), /2 MiB/);
});
test('formats safe-boundary minor units without floating-point rounding', () => {
  assert.equal(money('9007199254740991', 'EUR'), '€90,071,992,547,409.91');
});
test('does not resolve inherited properties as currency display rules', () => {
  assert.equal(money('123456', 'constructor'), '123,456 constructor minor');
});
