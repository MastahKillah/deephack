// Pretty-print a capture. usage: node show.js [file] [--from N] [--max N] [--full] [--no-dedupe]
const fs = require('fs');
const args = process.argv.slice(2);
const file = args.find((a) => !a.startsWith('--') && !/^\d+$/.test(a)) || require('path').join(__dirname, '..', 'captures', 'session.jsonl');
const opt = (n, d) => { const i = args.indexOf('--' + n); return i >= 0 ? (args[i + 1] && !args[i + 1].startsWith('--') ? args[i + 1] : true) : d; };
const from = Number(opt('from', 0)), max = Number(opt('max', 200)), full = opt('full', false), noDedupe = opt('no-dedupe', false);
const lines = fs.readFileSync(file, 'utf8').split('\n').filter(Boolean).map((l) => JSON.parse(l));
const t0 = lines[0].t;
const fmt = (h, n = full ? 1e9 : 64) => {
  const b = h.match(/../g) || [];
  return b.slice(0, n).join(' ') + (b.length > n ? ` …(+${b.length - n})` : '');
};
let shown = 0, last = '', rep = 0;
for (let i = from; i < lines.length && shown < max; i++) {
  const e = lines[i];
  const ts = ((e.t - t0) / 1000).toFixed(3).padStart(8);
  let s;
  if (e.k === 'out') s = `OUT ep${e.ep.toString(16).padStart(2, '0')} [${String(e.n).padStart(4)}] ${fmt(e.hex)}\n${' '.repeat(16)}↳ ${e.st}`;
  else if (e.k === 'in') s = `IN  ep${e.ep.toString(16)} [${String(e.n).padStart(4)}] ${e.err ? 'ERR ' + e.err : fmt(e.hex)}`;
  else if (e.k === 'ipc') s = `IPC  → ${e.channel} ${e.args}`;
  else if (e.k === 'ipc-ret') s = `IPC  ← ${e.channel} ${e.res}`;
  else if (e.k === 'ctrl' || e.k === 'ctrl-done') continue;
  else s = `${e.k} ${JSON.stringify({ ...e, t: undefined, tid: undefined, k: undefined })}`;
  const key = s.replace(/\n.*/s, '');
  if (!noDedupe && key === last) { rep++; continue; }
  if (rep) { console.log(`${' '.repeat(16)}(previous repeated ${rep}×)`); rep = 0; }
  last = key;
  console.log(`${String(i).padStart(5)} ${ts} ${s}`);
  shown++;
}
if (rep) console.log(`${' '.repeat(16)}(previous repeated ${rep}×)`);
console.log(`-- ${lines.length} events total`);
