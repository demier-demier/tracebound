import fs from 'node:fs';
import {spawnSync} from 'node:child_process';
import assert from 'node:assert/strict';
import {replayWitness} from '../web/app.js';
import init,{analyze_json,verify_json} from '../web/pkg/tracebound.js';
await init({module_or_path:fs.readFileSync(new URL('../web/pkg/tracebound_bg.wasm',import.meta.url))});
const results=[];
for (const file of fs.readdirSync('examples').filter(x=>x.endsWith('.json'))) {
  const raw=fs.readFileSync(`examples/${file}`,'utf8');
  const native=spawnSync('target/release/tracebound',['analyze','--input',`examples/${file}`],{encoding:'utf8'});
  assert.equal(native.status,0,native.stderr);
  const browser=JSON.parse(analyze_json(raw));
  assert.deepEqual(browser,JSON.parse(native.stdout));
  assert.equal(verify_json(raw,JSON.stringify(browser)),native.stdout);
  const forged=structuredClone(browser);
  forged.targets[0].minimum_minor='999999999999';
  assert.throws(()=>verify_json(raw,JSON.stringify(forged)));
  assert.throws(()=>analyze_json(raw.replace('"schema":', '"schema":"flow-case/v1","schema":')));
  let playbackSnapshots = 0;
  const ledger=JSON.parse(raw);
  for (const target of browser.targets) for (const witness of [target.min_witness,target.max_witness]) {
    for (let step=0;step<=ledger.transfers.length;step++) {
      const snapshot=replayWitness(ledger,witness,step);
      for (const [account,amount] of snapshot.marked) {
        assert.ok(amount>=0n && amount<=snapshot.totals.get(account));
      }
      if(step===ledger.transfers.length) for(const final of witness.final_allocations) {
        assert.equal(snapshot.marked.get(final.account),BigInt(final.seeded_minor));
      }
      playbackSnapshots++;
    }
  }
  results.push({case:file,playback_snapshots_checked:playbackSnapshots,targets:browser.targets.length,exact_native_wasm_parity:true,canonical_export_parity:true,forged_report_rejected:true,duplicate_input_key_rejected:true});
}
assert.ok(results.length>0,'No cases tested');
console.log(JSON.stringify({schema:'tracebound-wasm-parity/v1',cases:results},null,2));
