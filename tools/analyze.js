// Summarise a capture range, collapsing runs of identical (ep, cmd, stack) packets.
// usage: node analyze.js <from> [to] [--skip 01,10,0a]
const fs = require('fs');
const a = process.argv.slice(2);
const from = Number(a[0] || 0), to = a[1] && !a[1].startsWith('--') ? Number(a[1]) : Infinity;
const si = a.indexOf('--skip');
const skip = new Set(si >= 0 ? a[si + 1].split(',') : []);
const lines = fs.readFileSync(require('path').join(__dirname, '..', 'captures', 'session.jsonl'), 'utf8').split('\n').filter(Boolean).map((l) => JSON.parse(l));
const t0 = lines[0].t;
const sp = (h, n = 48) => { const b = h.match(/../g) || []; return b.slice(0, n).join(' ') + (b.length > n ? ` …(+${b.length - n})` : ''); };
let run = null;
const flush = () => {
  if (!run) return;
  const ts = ((run.t - t0) / 1000).toFixed(3).padStart(8);
  console.log(`${String(run.i).padStart(5)} ${ts} ${run.label} ×${run.count} bytes=${run.bytes}`);
  console.log(`        first: ${sp(run.first)}`);
  if (run.count > 1) console.log(`        last : ${sp(run.last)}`);
  if (run.st) console.log(`        ↳ ${run.st}`);
  run = null;
};
for (let i = from; i < Math.min(to, lines.length); i++) {
  const e = lines[i];
  if (e.k === 'out' || e.k === 'in') {
    const framed = e.hex.startsWith('aa') || e.hex.startsWith('55');
    const cmd = framed ? e.hex.slice(4, 6) : 'raw';
    if (framed && skip.has(cmd)) continue;
    const label = `${e.k === 'out' ? 'OUT' : 'IN '} ep${e.ep.toString(16).padStart(2, '0')} ${framed ? 'cmd ' + cmd : 'RAW'} len=${e.n}${e.err ? ' ERR ' + e.err : ''}`;
    const key = label + '|' + (e.st || '');
    if (run && run.key === key) { run.count++; run.bytes += e.n; run.last = e.hex; }
    else { flush(); run = { key, label, i, t: e.t, count: 1, bytes: e.n, first: e.hex, last: e.hex, st: e.k === 'out' ? e.st : '' }; }
  } else if (e.k === 'ipc' || e.k === 'ipc-ret') {
    flush();
    console.log(`${String(i).padStart(5)} ${((e.t - t0) / 1000).toFixed(3).padStart(8)} IPC ${e.k === 'ipc' ? '→' : '←'} ${e.channel} ${(e.args || e.res || '').slice(0, 300)}`);
  } else if (e.k === 'dev' || e.k === 'iface') {
    flush();
    console.log(`${String(i).padStart(5)} ${((e.t - t0) / 1000).toFixed(3).padStart(8)} ${e.k} ${e.op}`);
  }
}
flush();
