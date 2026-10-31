import init, { analyze_json, verify_json } from './pkg/tracebound.js';
const ready = init();
self.onmessage = async ({data}) => {
  try {
    await ready;
    if (typeof data.inputText !== 'string') throw new Error('Exact input text is required');
    const input = data.inputText;
    if (new TextEncoder().encode(input).length > 2 * 1024 * 1024) throw new Error('Case exceeds 2 MiB');
    const start = performance.now();
    const raw = analyze_json(input);
    const canonicalReport = verify_json(input, raw);
    const report = JSON.parse(raw);
    const elapsedMs = performance.now()-start;
    const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(canonicalReport));
    const reportSha256 = [...new Uint8Array(digest)].map(b=>b.toString(16).padStart(2,'0')).join('');
    self.postMessage({ok:true,report,elapsedMs,canonicalReport,inputBytes:input,reportSha256});
  } catch (error) {
    self.postMessage({ok:false,error:String(error?.message || error)});
  }
};
